"""Rolling planning must honor volume ownership, existing work and atomicity."""

import io
import subprocess
import sys
import zipfile
from unittest.mock import Mock

import pytest

from agents.outline_architect import OutlineArchitectAgent, OutlineExtension
from core.llm import LLMClient
from core.project_archive import InvalidProjectArchive, import_project_archive
from core.schemas import (
    AuditReport,
    ChapterBeat,
    ChapterBeatsPlan,
    ChapterContent,
    ChapterOutline,
    EmotionalPacingBeat,
    GlobalOutline,
    NovelConcept,
    PacingSchedule,
    StateUpdateDelta,
    VolumeOutline,
    WorldSetting,
)
from core.state_manager import StateManager
from core.storage import ProjectConflictError, atomic_write_json
from pipeline.novel_pipeline import NovelPipeline


def chapter(number, volume=1):
    return ChapterOutline(
        chapter_number=number, volume_number=volume, title=f"航行{number}",
        summary="离港", main_conflict="风暴", expectation_hook="暗礁",
    )


def outline(specs, *, ranged=True):
    return GlobalOutline(
        novel_title="航行", main_theme="归乡", total_volumes=len(specs), final_climax="抵岸",
        volumes=[VolumeOutline(
            volume_number=index, volume_title=f"航路{index}", volume_goal=f"第{index}卷目标",
            main_antagonist="风暴", volume_climax=f"第{index}卷高潮",
            chapters=[chapter(number, index) for number in numbers],
            **({"chapter_range": {"start": start, "end": end}} if ranged else {}),
        ) for index, (start, end, numbers) in enumerate(specs, 1)],
        pacing_schedule=PacingSchedule(total_chapters_planned=specs[-1][1]),
    )


def extension(**kwargs):
    numbers = range(kwargs["start_chapter"], kwargs["start_chapter"] + kwargs["count"])
    return OutlineExtension(
        chapters=[chapter(number, kwargs["volume"].volume_number) for number in numbers],
        pacing_beats=[EmotionalPacingBeat(chapter_number=number) for number in numbers],
    )


@pytest.fixture(autouse=True)
def forbid_transport(monkeypatch):
    transport = Mock(side_effect=AssertionError("Rolling tests must never call a real provider"))
    monkeypatch.setattr(LLMClient, "_chat", transport)
    yield
    transport.assert_not_called()


@pytest.fixture
def pipeline(tmp_path):
    pipeline = NovelPipeline("rolling", base_dir=str(tmp_path))
    manager = pipeline.state_manager
    manager.set_concept(NovelConcept(title="航行", genre="奇幻", logline="渡海", golden_finger="罗盘", core_conflict="归乡"))
    manager.set_world_setting(WorldSetting(world_name="雾海", geography_overview="岛屿"))
    pipeline.outline_architect = Mock()
    pipeline.outline_architect.extend_outline.side_effect = extension
    return pipeline


def files(manager):
    return {p.relative_to(manager.base_dir): p.read_bytes() for p in manager.base_dir.rglob("*") if p.is_file()}


def test_macro_only_volumes_are_reached_and_each_block_stops_at_its_boundary(pipeline):
    manager = pipeline.state_manager
    manager.set_global_outline(outline([(1, 2, [1, 2]), (3, 4, []), (5, 7, [])]))
    pipeline.extend_outline_to(3)
    call = pipeline.outline_architect.extend_outline.call_args.kwargs
    assert (call["volume"].volume_number, call["start_chapter"], call["count"]) == (2, 3, 2)
    assert manager.get_chapter_outline(5) is None
    pipeline.extend_outline_to(5)
    reloaded = StateManager("rolling", base_dir=str(manager.root_dir))
    assert reloaded.load_warnings == []
    assert [len(volume.chapters) for volume in reloaded.state.global_outline.volumes] == [2, 2, 3]
    assert reloaded.get_chapter_outline(7)[0].volume_number == 3


def test_gap_fill_does_not_duplicate_future_chapters_or_cross_volume_anchors(pipeline):
    manager = pipeline.state_manager
    saved = outline([(1, 4, [1, 4]), (5, 8, [5])])
    future = saved.volumes[0].chapters[-1].model_dump()
    manager.set_global_outline(saved)
    pipeline.extend_outline_to(2)
    call = pipeline.outline_architect.extend_outline.call_args.kwargs
    assert call["count"] == 2
    assert manager.get_chapter_outline(4)[1].model_dump() == future
    assert [c.chapter_number for v in manager.state.global_outline.volumes for c in v.chapters] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("corruption", [
    "duplicate_chapter", "missing_chapter", "extra_chapter", "chapter_order", "wrong_volume", "empty_summary",
    "missing_pacing", "duplicate_pacing", "wrong_pacing", "contradictory_pacing",
])
def test_invalid_extension_is_rejected_without_live_or_disk_changes(pipeline, corruption):
    manager = pipeline.state_manager
    manager.set_global_outline(outline([(1, 5, [1])]))
    before, state, revision = files(manager), manager.state.model_dump(), manager.revision

    def corrupt(**kwargs):
        result = extension(**kwargs)
        if corruption == "duplicate_chapter":
            result.chapters.append(result.chapters[0].model_copy(deep=True))
        elif corruption == "missing_chapter":
            result.chapters.pop()
        elif corruption == "extra_chapter":
            result.chapters.append(chapter(99))
        elif corruption == "chapter_order":
            result.chapters.reverse()
        elif corruption == "wrong_volume":
            result.chapters[0].volume_number = 99
        elif corruption == "empty_summary":
            result.chapters[0].summary = " "
        elif corruption == "missing_pacing":
            result.pacing_beats.pop()
        elif corruption == "duplicate_pacing":
            result.pacing_beats.append(result.pacing_beats[0].model_copy(deep=True))
        elif corruption == "wrong_pacing":
            result.chapters[0].pacing_beat = EmotionalPacingBeat(chapter_number=99)
        else:
            result.chapters[0].pacing_beat = result.pacing_beats[0].model_copy(update={"tension_level": 99})
        return result

    pipeline.outline_architect.extend_outline.side_effect = corrupt
    with pytest.raises(ValueError):
        pipeline.extend_outline_to(2, block_size=3)
    assert files(manager) == before
    assert manager.state.model_dump() == state
    assert manager.revision == revision


@pytest.mark.parametrize("number,size", [
    (True, 10), (0, 10), (1.5, 10), ("2", 10), (1_000_001, 10),
    (1, 0), (1, -1), (1, 51), (1, True), (1, 1.5), (1, "2"),
])
def test_invalid_request_fails_before_even_a_planned_chapter_noop(pipeline, number, size):
    manager = pipeline.state_manager
    manager.set_global_outline(outline([(1, 5, [1])]))
    before, revision = files(manager), manager.revision
    with pytest.raises(ValueError):
        pipeline.extend_outline_to(number, block_size=size)
    assert files(manager) == before and manager.revision == revision
    pipeline.outline_architect.extend_outline.assert_not_called()


def test_existing_outline_is_a_true_noop_and_book_end_stops_without_a_model(pipeline):
    manager = pipeline.state_manager
    manager.set_global_outline(outline([(1, 1, [1])]))
    before, revision = files(manager), manager.revision
    pipeline.extend_outline_to(1)
    with pytest.raises(ValueError, match="终章"):
        pipeline.extend_outline_to(2)
    assert files(manager) == before and manager.revision == revision
    pipeline.outline_architect.extend_outline.assert_not_called()


def test_legacy_anchors_preserve_ownership_and_an_open_final_volume(pipeline):
    manager = pipeline.state_manager
    manager.set_global_outline(outline([(1, 4, [1]), (5, 8, [5])], ranged=False))
    pipeline.extend_outline_to(2)
    assert [c.chapter_number for c in manager.state.global_outline.volumes[0].chapters] == [1, 2, 3, 4]
    pipeline.extend_outline_to(6)
    assert manager.get_chapter_outline(15)[0].volume_number == 2
    assert all(volume.chapter_range is None for volume in manager.state.global_outline.volumes)


def test_ambiguous_legacy_macro_volume_stops_without_guessing_or_migration(pipeline):
    manager = pipeline.state_manager
    manager.set_global_outline(outline([(1, 4, [1]), (5, 8, [])], ranged=False))
    before, revision = files(manager), manager.revision
    with pytest.raises(ValueError, match="转卷位置"):
        pipeline.extend_outline_to(2)
    assert files(manager) == before and manager.revision == revision
    pipeline.outline_architect.extend_outline.assert_not_called()


def test_last_supported_chapter_caps_an_open_legacy_block(pipeline):
    manager = pipeline.state_manager
    manager.set_global_outline(outline([(1, 1_000_000, [1])], ranged=False))
    pipeline.extend_outline_to(1_000_000)
    assert pipeline.outline_architect.extend_outline.call_args.kwargs["count"] == 1
    assert manager.get_chapter_outline(1_000_000) is not None


def test_preserves_saved_pacing_and_protects_live_state_from_model_side_effects(pipeline):
    manager = pipeline.state_manager
    saved = outline([(1, 4, [1, 4])])
    saved.pacing_schedule.pacing_beats = [
        EmotionalPacingBeat(chapter_number=1, tension_level=70),
        EmotionalPacingBeat(chapter_number=4, tension_level=90),
    ]
    saved.pacing_schedule.climax_milestone_chapters = [4]
    manager.set_global_outline(saved)
    before = saved.model_dump()

    def mutate_arguments(**kwargs):
        result = extension(**kwargs)
        kwargs["concept"].title = "不得改名"
        kwargs["world_setting"].special_rules.append("不得添加")
        kwargs["volume"].volume_goal = "不得改目标"
        kwargs["preceding_chapters"][0].summary = "不得改既有细纲"
        kwargs["following_chapter"].summary = "不得改后续细纲"
        return result

    pipeline.outline_architect.extend_outline.side_effect = mutate_arguments
    pipeline.extend_outline_to(2)
    assert manager.state.concept.title == "航行" and manager.state.world_setting.special_rules == []
    assert saved.model_dump() == before
    result = manager.state.global_outline
    assert result.volumes[0].volume_goal == "第1卷目标"
    assert result.volumes[0].chapters[0].summary == result.volumes[0].chapters[-1].summary == "离港"
    assert result.pacing_schedule.pacing_beats[0].tension_level == 70
    assert result.pacing_schedule.pacing_beats[-1].tension_level == 90
    assert result.pacing_schedule.climax_milestone_chapters == [4]
    for number in (2, 3):
        assert manager.get_chapter_outline(number)[1].pacing_beat == result.pacing_schedule.pacing_beats[number - 1]


def test_model_failure_never_changes_project_and_a_fresh_instance_can_retry(pipeline):
    manager = pipeline.state_manager
    manager.set_global_outline(outline([(1, 3, [1])]))
    before, revision = files(manager), manager.revision
    pipeline.outline_architect.extend_outline.side_effect = RuntimeError("offline unavailable")
    with pytest.raises(RuntimeError):
        pipeline.extend_outline_to(2)
    assert files(manager) == before and manager.revision == revision
    resumed = NovelPipeline("rolling", base_dir=str(manager.root_dir))
    resumed.outline_architect = Mock()
    resumed.outline_architect.extend_outline.side_effect = extension
    resumed.extend_outline_to(2)
    assert resumed.state_manager.get_chapter_outline(3) is not None


def test_failed_publication_restores_both_outline_and_aggregate(pipeline, monkeypatch):
    manager = pipeline.state_manager
    manager.set_global_outline(outline([(1, 4, [1])]))
    before, original = files(manager), manager.state.global_outline.model_dump()
    save = manager.save_state

    def fail_after_aggregate():
        save()
        raise OSError("injected after both outline and aggregate publication")

    monkeypatch.setattr(manager, "save_state", fail_after_aggregate)
    with pytest.raises(OSError):
        pipeline.extend_outline_to(2)
    restored = StateManager("rolling", base_dir=str(manager.root_dir))
    assert files(restored) == before
    assert manager.state.global_outline.model_dump() == original
    assert restored.state.global_outline.model_dump() == original
    with pytest.raises(ProjectConflictError):
        pipeline.extend_outline_to(2)


@pytest.mark.parametrize("committed", [False, True])
def test_process_crash_before_or_after_commit_has_one_complete_outline(pipeline, committed):
    manager = pipeline.state_manager
    manager.set_global_outline(outline([(1, 4, [1])]))
    before = files(manager)
    script = """
import os, sys
from types import SimpleNamespace
from agents.outline_architect import OutlineExtension
from core.project_transactions import ProjectTransaction
from pipeline.novel_pipeline import NovelPipeline
p = NovelPipeline('rolling', base_dir=sys.argv[1])
result = OutlineExtension.model_validate_json(sys.argv[3])
p.outline_architect = SimpleNamespace(extend_outline=lambda **_: result)
if sys.argv[2] == 'committed':
    ProjectTransaction._cleanup_after_commit = lambda _: os._exit(81)
else:
    p.state_manager.save_state = lambda: os._exit(81)
p.extend_outline_to(2)
"""
    result = extension(start_chapter=2, count=3, volume=manager.state.global_outline.volumes[0])
    process = subprocess.run(
        [sys.executable, "-c", script, str(manager.root_dir), "committed" if committed else "uncommitted", result.model_dump_json()],
        capture_output=True, timeout=30, check=False,
    )
    assert process.returncode == 81, process.stderr.decode(errors="replace")
    restored = StateManager("rolling", base_dir=str(manager.root_dir))
    assert restored.load_warnings == []
    if not committed:
        assert files(restored) == before
    else:
        assert restored.get_chapter_outline(4) is not None
        assert GlobalOutline.model_validate_json((restored.base_dir / "global_outline.json").read_text(encoding="utf-8")) == restored.state.global_outline


@pytest.mark.parametrize("kind", ["overlap", "gap", "partial", "backwards", "wrong_owner", "boolean", "huge_volume_count"])
def test_corrupt_declared_ranges_are_rejected_on_load_and_backup_import(pipeline, tmp_path, kind):
    manager = pipeline.state_manager
    manager.set_global_outline(outline([(1, 4, [1]), (5, 8, [])]))
    state = manager.state.model_dump(mode="json")
    stored = state["global_outline"]
    span = stored["volumes"][1]["chapter_range"]
    if kind == "overlap":
        span["start"] = 4
    elif kind == "gap":
        span["start"] = 6
    elif kind == "partial":
        stored["volumes"][1]["chapter_range"] = None
    elif kind == "backwards":
        span["end"] = 2
    elif kind == "wrong_owner":
        stored["volumes"][0]["chapters"][0]["chapter_number"] = 6
    elif kind == "boolean":
        stored["volumes"][0]["chapter_range"]["start"] = True
    else:
        stored["total_volumes"] = 10**100
    atomic_write_json(manager.base_dir / "project_state.json", state)
    before = files(manager)
    restored = StateManager("rolling", base_dir=str(manager.root_dir))
    assert restored.load_warnings and files(manager) == before
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in before.items():
            archive.writestr(f"rolling/{name.as_posix()}", content)
    stream.seek(0)
    with pytest.raises(InvalidProjectArchive):
        import_project_archive(stream, tmp_path / "import")
    assert not (tmp_path / "import" / "rolling").exists()


def test_real_generation_publishes_the_new_volume_and_continues_after_reload(pipeline):
    manager = pipeline.state_manager
    manager.set_global_outline(outline([(1, 1, [1]), (2, 3, [])]))
    manager.save_chapter(
        ChapterContent(chapter_number=1, volume_number=1, title="起航", full_text="第一章原文"),
        StateUpdateDelta(summary_of_events="第一章完成"),
    )
    before = (manager.chapters_dir / "chapter_001.json").read_bytes()
    pipeline.beat_director = Mock()
    pipeline.beat_director.plan_chapter_beats.return_value = ChapterBeatsPlan(
        chapter_number=2, chapter_title="航行", core_expectation="前进", cliffhanger_design="下一站",
        beats=[ChapterBeat(beat_index=1, scene_goal="渡海", action_and_dialogue_plan="前进", emotional_tone="期待", conflict_type="风暴")],
    )
    pipeline.scene_writer = Mock()
    pipeline.scene_writer.write_beat.return_value = "第二章新航路正文"
    pipeline.consistency_auditor = Mock()
    pipeline.consistency_auditor.audit_chapter.return_value = AuditReport(
        passed=True, consistency_score=9, pacing_score=9, cliffhanger_detected=True, critique="离线",
    )
    pipeline.stylist_polisher = Mock()
    pipeline.stylist_polisher.polish_chapter.side_effect = lambda **kw: kw["raw_chapter_text"]
    pipeline.state_updater = Mock()
    pipeline.state_updater.extract_state_delta.return_value = StateUpdateDelta(summary_of_events="渡过风暴")
    pipeline.multimodal_generator = Mock()
    pipeline.multimodal_generator.generate_short_drama_script.side_effect = RuntimeError("offline optional stage")
    result = pipeline.generate_single_chapter(2, max_audit_retries=0)
    assert result.volume_number == 2
    context = pipeline.scene_writer.write_beat.call_args.kwargs["chapter_context"]
    assert context["volume_goal"] == "第2卷目标" and context["volume_number"] == 2
    reloaded = StateManager("rolling", base_dir=str(manager.root_dir))
    assert reloaded.state.current_volume_idx == 2 and reloaded.state.current_chapter_idx == 2
    assert reloaded.build_chapter_context(3)["volume_number"] == 2
    assert (manager.chapters_dir / "chapter_001.json").read_bytes() == before


def test_prompt_includes_volume_boundary_future_anchor_and_macro_context(pipeline):
    saved = outline([(1, 2, [1, 2]), (3, 6, [6]), (7, 9, [])])
    architect = OutlineArchitectAgent.__new__(OutlineArchitectAgent)
    architect.call_structured = Mock(return_value=OutlineExtension())
    architect.extend_outline(
        concept=pipeline.state_manager.state.concept, world_setting=pipeline.state_manager.state.world_setting,
        characters=[], volume=saved.volumes[1], start_chapter=3, count=3, recent_summaries=["已发生事实"],
        preceding_chapters=saved.volumes[0].chapters, following_chapter=saved.volumes[1].chapters[0], global_outline=saved,
    )
    prompt = architect.call_structured.call_args.args[0]
    for required in ("第 3 至第 5 章", "第 3 章至第 6 章", "第3卷目标", "归乡", "抵岸", "第6章", "已发生事实", "未来规划"):
        assert required in prompt
