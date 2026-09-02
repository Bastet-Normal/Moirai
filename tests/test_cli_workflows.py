"""CLI commands exercise real persistence with deterministic model-stage doubles."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import main
import services.generation_jobs as jobs
import web_ui
from agents.concept_planner import InitialNovelPackage
from config.settings import get_settings
from core.llm import LLMClient
from core.schemas import (
    ChapterContent,
    ChapterOutline,
    CharacterCard,
    CharacterState,
    GlobalOutline,
    NovelConcept,
    StateUpdateDelta,
    VolumeOutline,
    WorldSetting,
)
from core.state_manager import StateManager
from core.task_status import GenerationTaskStatus
from pipeline.novel_pipeline import NovelPipeline


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("OPENAI_API_KEY", "offline-cli-fixture-not-real")
    get_settings.cache_clear()
    transport = Mock(side_effect=AssertionError("No real model transport in CLI tests"))
    monkeypatch.setattr(LLMClient, "_chat", transport)
    status = GenerationTaskStatus()
    monkeypatch.setattr(web_ui, "generation_status", status)
    monkeypatch.setattr(jobs, "generation_status", status)
    control = SimpleNamespace(
        fail_chapter=None, fail_outline=False, fail_initial_stage=None, pipelines=[], status=status,
    )

    def outline(**kwargs):
        if control.fail_outline:
            raise RuntimeError("offline outline unavailable")
        total, per_volume = kwargs["total_volumes"], kwargs["chapters_per_volume"]
        return GlobalOutline(
            novel_title="离线策划", main_theme="归乡", total_volumes=total, final_climax="抵岸",
            volumes=[VolumeOutline(
                volume_number=volume, volume_title="航路", volume_goal="航行", main_antagonist="风暴",
                chapter_range={"start": (volume - 1) * per_volume + 1, "end": volume * per_volume},
                volume_climax="渡海", chapters=[ChapterOutline(
                    chapter_number=(volume - 1) * per_volume + number, volume_number=volume,
                    title="航行", summary="离港", main_conflict="风暴", expectation_hook="暗礁",
                ) for number in range(1, per_volume + 1)],
            ) for volume in range(1, total + 1)],
        )

    def pipeline_factory(*args, **kwargs):
        pipeline = NovelPipeline(*args, **kwargs)
        control.pipelines.append(pipeline)
        pipeline.concept_planner = Mock()
        package = InitialNovelPackage(
            concept=NovelConcept(title="离线策划", genre="奇幻", logline="渡海", golden_finger="罗盘", core_conflict="归乡"),
            world_setting=WorldSetting(world_name="雾海", geography_overview="岛屿"),
            initial_characters=[CharacterCard(
                id="hero", name="林岚", role="主角", background="旅人",
                initial_state=CharacterState(level="学徒", equipment_and_items=["罗盘"]),
            )],
        )

        def plan_novel(**kwargs):
            callback = kwargs["stage_callback"]
            values = {
                "concept": package.concept,
                "world": package.world_setting,
                "cast": package.initial_characters,
            }
            for stage in ("concept", "world", "cast"):
                existing = {
                    "concept": kwargs.get("concept"),
                    "world": kwargs.get("world_setting"),
                    "cast": kwargs.get("initial_characters"),
                }[stage]
                if existing is not None:
                    continue
                if control.fail_initial_stage == stage:
                    raise RuntimeError(f"offline {stage} unavailable with private diagnostic")
                callback(stage, values[stage])
            return package

        pipeline.concept_planner.plan_novel.side_effect = plan_novel
        pipeline.outline_architect = Mock()
        pipeline.outline_architect.generate_global_outline.side_effect = outline
        pipeline.multimodal_generator = Mock()
        pipeline.multimodal_generator.generate_audio_drama_package.side_effect = RuntimeError("offline optional stage")
        pipeline.generate_commercial_package = Mock(return_value=None)

        def generate(chapter_number):
            if chapter_number == control.fail_chapter:
                raise RuntimeError("provider error containing a private diagnostic")
            chapter = ChapterContent(
                chapter_number=chapter_number, volume_number=1, title=f"航行{chapter_number}",
                full_text=f"第{chapter_number}章离线正文。", word_count=8,
            )
            pipeline.state_manager.save_chapter(chapter, StateUpdateDelta(summary_of_events="航行"))
            return chapter

        pipeline.generate_single_chapter = Mock(side_effect=generate)
        return pipeline

    pipeline_factory.plan_chapter_range = NovelPipeline.plan_chapter_range
    monkeypatch.setattr(main, "NovelPipeline", pipeline_factory)
    monkeypatch.setattr(jobs, "NovelPipeline", pipeline_factory)
    control.run = lambda args: CliRunner().invoke(main.app, args, terminal_width=140)
    control.root = tmp_path / "projects"
    try:
        yield control
    finally:
        get_settings.cache_clear()
        transport.assert_not_called()


def tree(path):
    return {p.relative_to(path): p.read_bytes() for p in path.rglob("*") if p.is_file()}


def test_auto_creates_complete_plan_and_chapters(cli):
    result = cli.run(["auto", "--name", "book", "--idea", "海上冒险", "--chapters", "2", "--volumes", "1"])
    assert result.exit_code == 0, result.output
    manager = StateManager("book")
    assert sorted(manager.state.chapters) == [1, 2]
    assert manager.state.pending_outline_plan is None
    assert manager.state.characters["林岚"].current_state.level == "学徒"
    assert "全自动流程执行完毕" in result.output


@pytest.mark.parametrize("command", ["auto", "plan"])
def test_new_commands_never_silently_continue_an_existing_name(cli, command):
    manager = StateManager("book")
    manager.save_state()  # Even an existing empty/incomplete project is not new.
    before = tree(manager.base_dir)
    result = cli.run([command, "--name", "book", "--idea", "另一部作品"])
    assert result.exit_code == 1
    assert "已存在同名作品" in result.output and "continue-gen" in result.output
    assert cli.pipelines == []
    assert tree(manager.base_dir) == before


@pytest.mark.parametrize("command,flag,value", [
    ("auto", "--chapters", "0"), ("auto", "--chapters", "51"),
    ("auto", "--volumes", "21"), ("plan", "--chap-per-vol", "101"),
    ("continue-gen", "--count", "0"), ("continue-gen", "--count", "1.5"),
    ("chapter", "--num", "1000001"), ("chapter", "--num", "0"),
])
def test_invalid_numeric_cli_arguments_fail_before_loading_or_calling_models(cli, command, flag, value):
    args = [command, "--name", "book", flag, value]
    if command in {"auto", "plan"}:
        args += ["--idea", "创意"]
    result = cli.run(args)
    assert result.exit_code == 2
    assert cli.pipelines == []
    assert not (cli.root / "book").exists()


@pytest.mark.parametrize("idea", ["  ", "x" * 20_001, "a\x00b", "bad\ud800"])
def test_invalid_idea_never_reaches_model_or_creates_project(cli, idea):
    result = cli.run(["auto", "--name", "book", "--idea", idea])
    assert result.exit_code == 2
    assert cli.pipelines == []
    assert not (cli.root / "book").exists()


def test_failed_batch_has_nonzero_exit_preserves_prefix_and_next_continue_starts_after_it(cli):
    cli.fail_chapter = 2
    failed = cli.run(["auto", "--name", "book", "--idea", "海上冒险", "--chapters", "3", "--volumes", "1"])
    assert failed.exit_code == 2
    assert "第 2 章停止" in failed.output and "已完成 1 章" in failed.output
    assert "全自动流程执行完毕" not in failed.output
    assert "private diagnostic" not in failed.output
    first = (cli.root / "book" / "chapters" / "chapter_001.json").read_bytes()
    cli.fail_chapter = None
    resumed = cli.run(["continue-gen", "--name", "book", "--count", "2"])
    assert resumed.exit_code == 0, resumed.output
    assert sorted(StateManager("book").state.chapters) == [1, 2, 3]
    assert (cli.root / "book" / "chapters" / "chapter_001.json").read_bytes() == first
    assert [call.args[0] for call in cli.pipelines[-1].generate_single_chapter.call_args_list] == [2, 3]


def test_existing_chapter_requires_explicit_rewrite(cli):
    assert cli.run(["auto", "--name", "book", "--idea", "冒险", "--chapters", "1"]).exit_code == 0
    before = tree(cli.root / "book")
    refused = cli.run(["chapter", "--name", "book", "--num", "1"])
    assert refused.exit_code == 1 and "--rewrite" in refused.output
    cli.pipelines[-1].generate_single_chapter.assert_not_called()
    assert tree(cli.root / "book") == before
    allowed = cli.run(["chapter", "--name", "book", "--num", "1", "--rewrite"])
    assert allowed.exit_code == 0, allowed.output
    cli.pipelines[-1].generate_single_chapter.assert_called_once_with(chapter_number=1)
    invalid = cli.run(["chapter", "--name", "book", "--num", "2", "--rewrite"])
    assert invalid.exit_code == 2 and "不存在" in invalid.output
    cli.pipelines[-1].generate_single_chapter.assert_not_called()


@pytest.mark.parametrize("entry", ["resume-plan", "continue-gen", "web"])
def test_planning_failure_can_resume_without_repeating_initial_package(cli, entry):
    cli.fail_outline = True
    failed = cli.run(["plan", "--name", "book", "--idea", "冒险", "--volumes", "2", "--chap-per-vol", "2"])
    assert failed.exit_code == 2 and "resume-plan" in failed.output
    saved = StateManager("book")
    assert saved.state.pending_outline_plan.total_volumes == 2
    concept = (saved.base_dir / "concept.json").read_bytes()
    cli.fail_outline = False
    if entry == "web":
        with TestClient(web_ui.app) as client:
            detail = client.get("/api/novel/book")
            assert detail.status_code == 200
            assert detail.json()["summary"]["planning_pending"] is True
            response = client.post("/api/novel/continue", json={
                "name": "book", "count": 1, "expected_revision": saved.revision,
            })
            assert response.status_code == 200, response.text
            assert "恢复未完成的初始总纲" in response.json()["message"]
        assert cli.status.snapshot()["error"] == ""
        assert not cli.status.is_running
    else:
        args = [entry, "--name", "book"]
        if entry == "continue-gen":
            args += ["--count", "1"]
        result = cli.run(args)
        assert result.exit_code == 0, result.output
    resumed = StateManager("book")
    assert resumed.state.pending_outline_plan is None
    assert len(resumed.state.global_outline.volumes) == 2
    assert len(resumed.state.chapters) == (0 if entry == "resume-plan" else 1)
    assert (saved.base_dir / "concept.json").read_bytes() == concept
    cli.pipelines[-1].concept_planner.plan_novel.assert_not_called()
    assert cli.pipelines[-1].outline_architect.generate_global_outline.call_args.kwargs["chapters_per_volume"] == 2


@pytest.mark.parametrize("failed_stage", ["concept", "world", "cast"])
@pytest.mark.parametrize("entry", ["resume-plan", "continue-gen", "web"])
def test_each_partial_initial_stage_resumes_through_every_public_entry(cli, failed_stage, entry):
    cli.fail_initial_stage = failed_stage
    failed = cli.run([
        "plan", "--name", "partial", "--idea", "原始冒险", "--genre", "奇幻",
        "--volumes", "2", "--chap-per-vol", "2",
    ])
    assert failed.exit_code == 2 and "resume-plan" in failed.output
    assert "private diagnostic" not in failed.output
    saved = StateManager("partial")
    checkpoint = saved.state.pending_initial_planning
    assert checkpoint.next_stage == failed_stage
    assert checkpoint.request.user_idea == "原始冒险"
    assert saved.state.concept is None and saved.state.pending_outline_plan is None
    saved_concept = checkpoint.concept.model_dump_json() if checkpoint.concept else None
    saved_world = checkpoint.world_setting.model_dump_json() if checkpoint.world_setting else None
    status = cli.run(["status", "--name", "partial"])
    assert status.exit_code == 0 and failed_stage in status.output and "resume-plan" in status.output
    with TestClient(web_ui.app) as client:
        shelf = client.get("/api/novels")
    partial_summary = next(item for item in shelf.json()["novels"] if item["novel_id"] == "partial")
    assert partial_summary["planning_pending"] is True
    assert partial_summary["planning_stage"] == failed_stage
    assert partial_summary["title"] == (
        checkpoint.concept.title if checkpoint.concept else "未策划 · partial"
    )

    cli.fail_initial_stage = None
    if entry == "web":
        with TestClient(web_ui.app) as client:
            detail = client.get("/api/novel/partial")
            assert detail.status_code == 200, detail.text
            assert detail.json()["summary"]["planning_stage"] == failed_stage
            response = client.post("/api/novel/continue", json={
                "name": "partial", "count": 1, "expected_revision": saved.revision,
            })
            assert response.status_code == 200, response.text
            assert "主策划" in response.json()["message"] and failed_stage in response.json()["message"]
        assert cli.status.snapshot()["error"] == ""
    else:
        args = [entry, "--name", "partial"]
        if entry == "continue-gen":
            args += ["--count", "1"]
        result = cli.run(args)
        assert result.exit_code == 0, result.output

    resumed = StateManager("partial")
    assert resumed.state.pending_initial_planning is None
    assert resumed.state.pending_outline_plan is None
    assert resumed.state.global_outline is not None
    assert len(resumed.state.chapters) == (0 if entry == "resume-plan" else 1)
    if saved_concept:
        assert resumed.state.concept.model_dump_json() == saved_concept
    if saved_world:
        assert resumed.state.world_setting.model_dump_json() == saved_world
    call = cli.pipelines[-1].concept_planner.plan_novel.call_args.kwargs
    assert (call["concept"] is not None) == (failed_stage != "concept")
    assert (call["world_setting"] is not None) == (failed_stage == "cast")


def test_partial_stage_refuses_new_inputs_and_explicit_resume_dimensions(cli):
    cli.fail_initial_stage = "world"
    assert cli.run(["plan", "--name", "partial", "--idea", "原始冒险"]).exit_code == 2
    before = tree(cli.root / "partial")
    direct = NovelPipeline("partial")
    direct.concept_planner = Mock(side_effect=AssertionError("must not replace checkpoint"))
    with pytest.raises(ValueError, match="resume-plan|替换"):
        direct.initialize_new_novel("另一创意")
    refused = cli.run([
        "resume-plan", "--name", "partial", "--volumes", "3", "--chap-per-vol", "4",
    ])
    assert refused.exit_code == 2 and "省略恢复参数" in refused.output
    assert tree(cli.root / "partial") == before


def test_offline_read_commands_and_export_need_no_key(cli, monkeypatch):
    assert cli.run(["plan", "--name", "book", "--idea", "冒险", "--volumes", "1", "--chap-per-vol", "1"]).exit_code == 0
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    for args in (["status", "--name", "book"], ["list"], ["config"], ["export", "--name", "book"]):
        result = cli.run(args)
        assert result.exit_code == 0, result.output
    blocked = cli.run(["continue-gen", "--name", "book", "--count", "1"])
    assert blocked.exit_code == 1 and "API Key" in blocked.output


def test_unexpected_cli_failure_does_not_dump_provider_details_or_locals(cli, monkeypatch):
    assert cli.run(["plan", "--name", "book", "--idea", "冒险", "--volumes", "1", "--chap-per-vol", "1"]).exit_code == 0
    failure = Mock(side_effect=RuntimeError("secret-provider-token-and-private-prose"))
    monkeypatch.setattr(main, "NovelPipeline", failure)
    result = cli.run(["status", "--name", "book"])
    assert result.exit_code == 2 and "命令未完成" in result.output
    assert "secret-provider-token" not in result.output and "Traceback" not in result.output


@pytest.mark.parametrize("count", [0, True, 51])
def test_direct_worker_validates_count_before_initial_planner(cli, count):
    jobs.run_pipeline_job("book", "航行", "", None, count, 1)
    assert not cli.status.is_running
    assert "1 到 50" in cli.status.snapshot()["error"]
    assert not (cli.root / "book").exists()
    cli.pipelines[-1].concept_planner.plan_novel.assert_not_called()


def test_continuation_range_overflow_is_rejected_before_reserving_worker(cli):
    assert cli.run(["plan", "--name", "book", "--idea", "航行", "--volumes", "1", "--chap-per-vol", "1"]).exit_code == 0
    manager = StateManager("book")
    manager.state.current_chapter_idx = 999999
    manager.save_state()
    before = tree(manager.base_dir)
    with TestClient(web_ui.app) as client:
        response = client.post("/api/novel/continue", json={
            "name": "book", "count": 2, "expected_revision": manager.revision,
        })
    assert response.status_code == 422
    assert "1000000" in response.json()["detail"]
    assert cli.status.snapshot()["task_id"] == ""
    assert tree(manager.base_dir) == before
