"""Offline planning checkpoints preserve paid results and initial story facts."""

import io
import subprocess
import sys
import zipfile
from unittest.mock import Mock

import pytest

from agents.concept_planner import ConceptPlannerAgent, InitialCast, InitialNovelPackage
from core.planning import PlanningIncompleteError
from core.project_archive import InvalidProjectArchive, import_project_archive
from core.schemas import (
    ChapterOutline,
    CharacterCard,
    CharacterState,
    GlobalOutline,
    InitialOutlinePlan,
    NovelConcept,
    VolumeOutline,
    WorldSetting,
)
from core.state_manager import StateManager
from core.storage import atomic_write_json
from pipeline.novel_pipeline import NovelPipeline


def initial_package():
    return InitialNovelPackage(
        concept=NovelConcept(title="原始策划", genre="奇幻", logline="渡海", golden_finger="罗盘", core_conflict="归乡"),
        world_setting=WorldSetting(world_name="雾海", geography_overview="岛屿"),
        initial_characters=[CharacterCard(
            id="hero", name="林岚", role="主角", background="旅人",
            initial_state=CharacterState(level="学徒", location="码头", equipment_and_items=["罗盘"]),
            current_state=CharacterState(level="不应采用的模型默认当前值"),
        )],
    )


def initial_outline(volumes=2, chapters_per_volume=2):
    return GlobalOutline(
        novel_title="原始策划", main_theme="归乡", total_volumes=volumes, final_climax="抵岸",
        volumes=[VolumeOutline(
            volume_number=volume, volume_title=f"航路{volume}", volume_goal="航行",
            main_antagonist="风暴", volume_climax="渡海",
            chapter_range={"start": (volume - 1) * chapters_per_volume + 1, "end": volume * chapters_per_volume},
            chapters=[ChapterOutline(
                chapter_number=(volume - 1) * chapters_per_volume + number, volume_number=volume,
                title="起航", summary="离港", main_conflict="风暴", expectation_hook="暗礁",
            ) for number in range(1, chapters_per_volume + 1)],
        ) for volume in range(1, volumes + 1)],
    )


@pytest.fixture
def pipeline(tmp_path):
    pipeline = NovelPipeline("planning", base_dir=str(tmp_path))
    pipeline.concept_planner = Mock()
    pipeline.concept_planner.plan_novel.return_value = initial_package()
    pipeline.outline_architect = Mock()
    pipeline.outline_architect.generate_global_outline.return_value = initial_outline()
    pipeline.multimodal_generator = Mock()
    pipeline.multimodal_generator.generate_audio_drama_package.side_effect = RuntimeError("offline optional stage")
    return pipeline


def files(manager):
    return {p.relative_to(manager.base_dir): p.read_bytes() for p in manager.base_dir.rglob("*") if p.is_file()}


@pytest.mark.parametrize("stage", ["set_concept", "set_world_setting", "upsert_characters_bulk"])
def test_initial_package_publication_is_atomic(pipeline, monkeypatch, stage):
    manager = pipeline.state_manager
    original = getattr(manager, stage)

    def fail_after_world(world):
        original(world)
        raise OSError("injected disk failure")

    monkeypatch.setattr(manager, stage, fail_after_world)
    with pytest.raises(OSError):
        pipeline.initialize_new_novel("海上冒险", total_volumes=2, chapters_per_volume=2)
    restored = StateManager("planning", base_dir=str(manager.root_dir))
    assert restored.state.concept is None
    assert restored.state.world_setting is None
    assert restored.state.characters == {}
    assert restored.state.pending_initial_planning.next_stage == "promotion"
    assert restored.state.pending_initial_planning.concept.title == "原始策划"
    assert restored.state.pending_initial_planning.initial_characters[0].current_state.level == "学徒"
    assert not (restored.base_dir / "concept.json").exists()
    assert not (restored.base_dir / "world_setting.json").exists()
    assert not (restored.base_dir / "characters.json").exists()
    pipeline.outline_architect.generate_global_outline.assert_not_called()


def test_outline_failure_leaves_resumable_complete_package_with_author_initial_state(pipeline):
    pipeline.outline_architect.generate_global_outline.side_effect = RuntimeError("model unavailable")
    with pytest.raises(RuntimeError):
        pipeline.initialize_new_novel("海上冒险", total_volumes=2, chapters_per_volume=2)
    restored = StateManager("planning", base_dir=str(pipeline.state_manager.root_dir))
    assert restored.state.concept.title == "原始策划"
    assert restored.state.world_setting.world_name == "雾海"
    assert restored.state.characters["林岚"].current_state == restored.state.characters["林岚"].initial_state
    assert restored.state.pending_outline_plan.total_volumes == 2
    assert restored.state.pending_outline_plan.chapters_per_volume == 2
    assert restored.state.global_outline is None


def test_resume_uses_saved_parameters_without_replanning_or_replacing_existing_cast(pipeline):
    pipeline.outline_architect.generate_global_outline.side_effect = RuntimeError("model unavailable")
    with pytest.raises(RuntimeError):
        pipeline.initialize_new_novel("海上冒险", total_volumes=2, chapters_per_volume=2)
    resumed = NovelPipeline("planning", base_dir=str(pipeline.state_manager.root_dir))
    resumed.concept_planner = Mock(side_effect=AssertionError("Must not replan paid results"))
    resumed.outline_architect = Mock()
    resumed.outline_architect.generate_global_outline.return_value = initial_outline()
    resumed.multimodal_generator = pipeline.multimodal_generator
    before_concept = (resumed.state_manager.base_dir / "concept.json").read_bytes()
    before_cast = (resumed.state_manager.base_dir / "characters.json").read_bytes()

    resumed.resume_initial_planning()

    restored = StateManager("planning", base_dir=str(pipeline.state_manager.root_dir))
    assert restored.state.global_outline == initial_outline()
    assert restored.state.pending_outline_plan is None
    assert (restored.base_dir / "concept.json").read_bytes() == before_concept
    assert (restored.base_dir / "characters.json").read_bytes() == before_cast
    assert resumed.outline_architect.generate_global_outline.call_args.kwargs["total_volumes"] == 2
    assert resumed.outline_architect.generate_global_outline.call_args.kwargs["chapters_per_volume"] == 2
    resumed.concept_planner.assert_not_called()


def test_failed_outline_publication_rolls_back_to_resumable_checkpoint(pipeline, monkeypatch):
    pipeline.outline_architect.generate_global_outline.side_effect = RuntimeError("unavailable")
    with pytest.raises(PlanningIncompleteError):
        pipeline.initialize_new_novel("航行", total_volumes=2, chapters_per_volume=2)
    before = files(pipeline.state_manager)
    pipeline.outline_architect.generate_global_outline.side_effect = None
    original = pipeline.state_manager.set_global_outline

    def fail_after_publish(outline):
        original(outline)
        raise OSError("injected publication failure")

    monkeypatch.setattr(pipeline.state_manager, "set_global_outline", fail_after_publish)
    with pytest.raises(PlanningIncompleteError):
        pipeline.resume_initial_planning()
    restored = StateManager("planning", base_dir=str(pipeline.state_manager.root_dir))
    assert files(restored) == before
    assert restored.state.pending_outline_plan is not None
    assert restored.state.global_outline is None


@pytest.mark.parametrize("kind", ["empty", "duplicate_names", "duplicate_ids", "blank_name", "blank_id"])
def test_invalid_cast_never_silently_overwrites_or_publishes_a_partial_package(pipeline, kind):
    package = initial_package()
    if kind == "empty":
        package.initial_characters.clear()
    elif kind.startswith("duplicate"):
        duplicate = package.initial_characters[0].model_copy(deep=True)
        if kind == "duplicate_names":
            duplicate.id = "other"
        else:
            duplicate.name = "另一个名字"
        package.initial_characters.append(duplicate)
    else:
        setattr(package.initial_characters[0], "name" if kind == "blank_name" else "id", " ")
    pipeline.concept_planner.plan_novel.return_value = package
    with pytest.raises(PlanningIncompleteError):
        pipeline.initialize_new_novel("航行", total_volumes=2, chapters_per_volume=2)
    restored = StateManager("planning", base_dir=str(pipeline.state_manager.root_dir))
    assert restored.state.concept is None and restored.state.world_setting is None
    assert restored.state.characters == {}
    assert restored.state.pending_initial_planning.next_stage == "cast"
    assert restored.state.pending_initial_planning.concept.title == "原始策划"
    assert restored.state.pending_initial_planning.world_setting.world_name == "雾海"
    pipeline.outline_architect.generate_global_outline.assert_not_called()


@pytest.mark.parametrize("kind", ["missing_volume", "volume_count", "volume_order", "missing_chapter", "duplicate_chapter", "chapter_order", "wrong_volume"])
def test_invalid_model_outline_stays_pending_and_never_reaches_audio_generation(pipeline, kind):
    outline = initial_outline()
    if kind == "missing_volume":
        outline.volumes.pop()
    elif kind == "volume_count":
        outline.total_volumes = 3
    elif kind == "volume_order":
        outline.volumes.reverse()
    elif kind == "missing_chapter":
        outline.volumes[0].chapters.pop()
    elif kind == "duplicate_chapter":
        outline.volumes[0].chapters[1].chapter_number = 1
    elif kind == "chapter_order":
        outline.volumes[0].chapters.reverse()
    else:
        outline.volumes[0].chapters[0].volume_number = 2
    pipeline.outline_architect.generate_global_outline.return_value = outline
    with pytest.raises(PlanningIncompleteError) as caught:
        pipeline.initialize_new_novel("航行", total_volumes=2, chapters_per_volume=2)
    assert isinstance(caught.value.__cause__, ValueError)
    restored = StateManager("planning", base_dir=str(pipeline.state_manager.root_dir))
    assert restored.state.global_outline is None and restored.state.pending_outline_plan is not None
    assert not (restored.base_dir / "global_outline.json").exists()
    pipeline.multimodal_generator.generate_audio_drama_package.assert_not_called()


def test_legacy_incomplete_plan_requires_explicit_original_dimensions(pipeline):
    package = initial_package()
    manager = pipeline.state_manager
    manager.set_concept(package.concept)
    manager.set_world_setting(package.world_setting)
    manager.upsert_characters_bulk(package.initial_characters)
    before = files(manager)
    with pytest.raises(ValueError, match="显式指定"):
        pipeline.resume_initial_planning()
    assert files(manager) == before
    pipeline.outline_architect.generate_global_outline.assert_not_called()
    pipeline.resume_initial_planning(total_volumes=2, chapters_per_volume=2)
    assert manager.state.global_outline == initial_outline()
    assert manager.state.pending_outline_plan is None


def test_initial_outline_accepts_macro_only_later_volumes_for_rolling_planning(pipeline):
    outline = initial_outline()
    outline.volumes[1].chapters.clear()
    pipeline.outline_architect.generate_global_outline.return_value = outline
    pipeline.initialize_new_novel("航行", total_volumes=2, chapters_per_volume=2)
    restored = StateManager("planning", base_dir=str(pipeline.state_manager.root_dir))
    assert len(restored.state.global_outline.volumes) == 2
    assert restored.state.global_outline.volumes[1].chapters == []
    assert restored.state.pending_outline_plan is None


def test_resume_refuses_changed_request_or_complete_project_before_any_model_call(pipeline):
    pipeline.outline_architect.generate_global_outline.side_effect = RuntimeError("unavailable")
    with pytest.raises(PlanningIncompleteError):
        pipeline.initialize_new_novel("航行", total_volumes=2, chapters_per_volume=2)
    before = files(pipeline.state_manager)
    pipeline.outline_architect.generate_global_outline.reset_mock()
    with pytest.raises(ValueError, match="原始规划"):
        pipeline.resume_initial_planning(total_volumes=3, chapters_per_volume=2)
    assert files(pipeline.state_manager) == before
    pipeline.outline_architect.generate_global_outline.assert_not_called()
    pipeline.outline_architect.generate_global_outline.side_effect = None
    pipeline.resume_initial_planning()
    before = files(pipeline.state_manager)
    pipeline.outline_architect.generate_global_outline.reset_mock()
    with pytest.raises(ValueError, match="已有总纲"):
        pipeline.resume_initial_planning()
    assert files(pipeline.state_manager) == before
    pipeline.outline_architect.generate_global_outline.assert_not_called()


def archive_project(manager):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, data in files(manager).items():
            archive.writestr(f"{manager.novel_id}/{name.as_posix()}", data)
    stream.seek(0)
    return stream


def test_pending_checkpoint_roundtrips_backup_and_rejects_corrupt_requests(pipeline, tmp_path):
    pipeline.outline_architect.generate_global_outline.side_effect = RuntimeError("unavailable")
    with pytest.raises(PlanningIncompleteError):
        pipeline.initialize_new_novel("航行", total_volumes=2, chapters_per_volume=2)
    manager = pipeline.state_manager
    destination = tmp_path / "imported"
    import_project_archive(archive_project(manager), destination)
    restored = StateManager("planning", base_dir=str(destination))
    assert restored.state.pending_outline_plan == InitialOutlinePlan(total_volumes=2, chapters_per_volume=2)
    assert restored.load_warnings == []
    state = manager.state.model_dump(mode="json")
    state["pending_outline_plan"]["total_volumes"] = True
    atomic_write_json(manager.base_dir / "project_state.json", state)
    with pytest.raises(InvalidProjectArchive):
        import_project_archive(archive_project(manager), tmp_path / "rejected")
    corrupt = StateManager("planning", base_dir=str(manager.root_dir))
    assert corrupt.load_warnings


@pytest.mark.parametrize("phase", ["initial_publication", "outline_call"])
def test_process_crash_recovers_initial_atomicity_or_retains_resumable_checkpoint(pipeline, phase):
    script = """
import os, sys
from types import SimpleNamespace
from agents.concept_planner import InitialNovelPackage
from pipeline.novel_pipeline import NovelPipeline
p = NovelPipeline('crash', base_dir=sys.argv[1])
package = InitialNovelPackage.model_validate_json(sys.argv[3])
p.concept_planner = SimpleNamespace(plan_novel=lambda **_: package)
if sys.argv[2] == 'initial_publication':
    original = p.state_manager.set_world_setting
    def fail(world):
        original(world)
        os._exit(43)
    p.state_manager.set_world_setting = fail
else:
    p.outline_architect = SimpleNamespace(generate_global_outline=lambda **_: os._exit(43))
p.initialize_new_novel('offline crash test', total_volumes=2, chapters_per_volume=2)
"""
    root = pipeline.state_manager.root_dir
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), phase, initial_package().model_dump_json()],
        capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 43, result.stderr.decode(errors="replace")
    recovered = StateManager("crash", base_dir=str(root))
    assert recovered.load_warnings == []
    if phase == "initial_publication":
        assert recovered.state.concept is None
        assert recovered.state.pending_initial_planning.next_stage == "promotion"
        assert recovered.state.pending_initial_planning.initial_characters[0].current_state.level == "学徒"
        assert not (recovered.base_dir / "concept.json").exists()
    else:
        assert recovered.state.concept.title == "原始策划"
        assert recovered.state.characters["林岚"].current_state.level == "学徒"
        assert recovered.state.pending_outline_plan.total_volumes == 2
        assert recovered.state.global_outline is None


def staged_agent(*responses):
    agent = ConceptPlannerAgent.__new__(ConceptPlannerAgent)
    agent.call_structured = Mock(side_effect=responses)
    return agent


@pytest.mark.parametrize("failed_stage,expected_next,saved_world", [
    ("concept", "concept", False),
    ("world", "world", False),
    ("cast", "cast", True),
])
def test_each_planner_failure_retains_only_completed_paid_stages(
    pipeline, failed_stage, expected_next, saved_world,
):
    package = initial_package()
    responses = []
    for stage, value in (
        ("concept", package.concept),
        ("world", package.world_setting),
        ("cast", InitialCast(characters=package.initial_characters)),
    ):
        responses.append(RuntimeError(f"offline {stage} failure") if stage == failed_stage else value)
        if stage == failed_stage:
            break
    pipeline.concept_planner = staged_agent(*responses)
    with pytest.raises(PlanningIncompleteError) as caught:
        pipeline.initialize_new_novel("海上冒险", total_volumes=2, chapters_per_volume=2)
    assert failed_stage in str(caught.value)
    restored = StateManager("planning", base_dir=str(pipeline.state_manager.root_dir))
    checkpoint = restored.state.pending_initial_planning
    assert checkpoint.next_stage == expected_next
    assert (checkpoint.concept is not None) == (failed_stage != "concept")
    assert (checkpoint.world_setting is not None) == saved_world
    assert checkpoint.initial_characters is None
    assert restored.state.concept is None and restored.state.characters == {}
    assert not (restored.base_dir / "concept.json").exists()


@pytest.mark.parametrize("failed_stage", ["world", "cast"])
def test_fresh_resume_skips_paid_stages_and_reuses_exact_checkpoint(pipeline, failed_stage):
    package = initial_package()
    if failed_stage == "world":
        pipeline.concept_planner = staged_agent(package.concept, RuntimeError("offline world"))
    else:
        pipeline.concept_planner = staged_agent(
            package.concept, package.world_setting, RuntimeError("offline cast"),
        )
    with pytest.raises(PlanningIncompleteError):
        pipeline.initialize_new_novel("原始脑洞", "奇幻", total_volumes=2, chapters_per_volume=2)
    saved = StateManager("planning", base_dir=str(pipeline.state_manager.root_dir))
    saved_concept = saved.state.pending_initial_planning.concept.model_dump_json()
    saved_world = (
        saved.state.pending_initial_planning.world_setting.model_dump_json()
        if saved.state.pending_initial_planning.world_setting else None
    )

    resumed = NovelPipeline("planning", base_dir=str(saved.root_dir))
    remaining = (
        [package.world_setting, InitialCast(characters=package.initial_characters)]
        if failed_stage == "world" else [InitialCast(characters=package.initial_characters)]
    )
    resumed.concept_planner = staged_agent(*remaining)
    resumed.outline_architect = pipeline.outline_architect
    resumed.multimodal_generator = pipeline.multimodal_generator
    resumed.resume_initialization()
    assert resumed.concept_planner.call_structured.call_count == len(remaining)
    restored = StateManager("planning", base_dir=str(saved.root_dir))
    assert restored.state.pending_initial_planning is None
    assert restored.state.pending_outline_plan is None
    assert restored.state.global_outline == initial_outline()
    assert restored.state.concept.model_dump_json() == saved_concept
    if saved_world:
        assert restored.state.world_setting.model_dump_json() == saved_world


def test_actual_planner_callbacks_are_ordered_and_resume_inputs_are_not_mutated():
    package = initial_package()
    agent = staged_agent(
        package.concept, package.world_setting, InitialCast(characters=package.initial_characters),
    )
    recorded = []
    result = agent.plan_novel(
        "海上冒险", "奇幻", stage_callback=lambda stage, value: recorded.append((stage, value)),
    )
    assert [stage for stage, _ in recorded] == ["concept", "world", "cast"]
    assert result == package
    recorded[0][1].title = "外部污染"
    assert result.concept.title == "原始策划"

    resumed_agent = staged_agent(package.world_setting, InitialCast(characters=package.initial_characters))
    saved_concept = package.concept.model_copy(deep=True)
    resumed_agent.plan_novel("海上冒险", concept=saved_concept)
    assert resumed_agent.call_structured.call_count == 2
    assert saved_concept.title == "原始策划"


@pytest.mark.parametrize("stage", ["concept", "world", "cast"])
def test_process_death_during_each_model_stage_recovers_previous_checkpoint(pipeline, stage):
    script = r'''
import os, sys
from unittest.mock import Mock
from agents.concept_planner import ConceptPlannerAgent, InitialCast, InitialNovelPackage
from pipeline.novel_pipeline import NovelPipeline
package = InitialNovelPackage.model_validate_json(sys.argv[3])
p = NovelPipeline('stage_crash', base_dir=sys.argv[1])
values = {'concept': package.concept, 'world': package.world_setting,
          'cast': InitialCast(characters=package.initial_characters)}
order = ['concept', 'world', 'cast']
target = sys.argv[2]
def call(prompt, schema):
    current = order.pop(0)
    if current == target:
        os._exit(47)
    return values[current]
agent = ConceptPlannerAgent.__new__(ConceptPlannerAgent)
agent.call_structured = Mock(side_effect=call)
p.concept_planner = agent
p.initialize_new_novel('crash idea', total_volumes=2, chapters_per_volume=2)
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(pipeline.state_manager.root_dir), stage, initial_package().model_dump_json()],
        capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 47, result.stderr.decode(errors="replace")
    restored = StateManager("stage_crash", base_dir=str(pipeline.state_manager.root_dir))
    checkpoint = restored.state.pending_initial_planning
    assert checkpoint.next_stage == stage
    assert (checkpoint.concept is not None) == (stage != "concept")
    assert (checkpoint.world_setting is not None) == (stage == "cast")
    assert restored.load_warnings == []


def test_complete_checkpoint_survives_promotion_fault_and_retries_without_planner(pipeline, monkeypatch):
    manager = pipeline.state_manager
    original = manager.set_concept

    def fail_after_concept(value):
        original(value)
        raise OSError("offline promotion fault")

    monkeypatch.setattr(manager, "set_concept", fail_after_concept)
    with pytest.raises(OSError):
        pipeline.initialize_new_novel("海上冒险", total_volumes=2, chapters_per_volume=2)
    saved = StateManager("planning", base_dir=str(manager.root_dir))
    assert saved.state.pending_initial_planning.next_stage == "promotion"
    assert saved.state.concept is None and saved.state.pending_outline_plan is None

    resumed = NovelPipeline("planning", base_dir=str(saved.root_dir))
    resumed.concept_planner = staged_agent()
    resumed.outline_architect = pipeline.outline_architect
    resumed.multimodal_generator = pipeline.multimodal_generator
    resumed.resume_initialization()
    resumed.concept_planner.call_structured.assert_not_called()
    restored = StateManager("planning", base_dir=str(saved.root_dir))
    assert restored.state.global_outline == initial_outline()
    assert restored.state.pending_initial_planning is None


def test_partial_checkpoint_roundtrips_backup_without_exposing_it_as_canonical(pipeline, tmp_path):
    package = initial_package()
    pipeline.concept_planner = staged_agent(package.concept, RuntimeError("offline world"))
    with pytest.raises(PlanningIncompleteError):
        pipeline.initialize_new_novel("保留的原始创意", "奇幻", total_volumes=2, chapters_per_volume=2)
    destination = tmp_path / "partial-import"
    import_project_archive(archive_project(pipeline.state_manager), destination)
    restored = StateManager("planning", base_dir=str(destination))
    checkpoint = restored.state.pending_initial_planning
    assert checkpoint.next_stage == "world"
    assert checkpoint.request.user_idea == "保留的原始创意"
    assert checkpoint.request.genre_preference == "奇幻"
    assert checkpoint.concept.title == "原始策划"
    assert restored.state.concept is None
    assert restored.load_warnings == []


@pytest.mark.parametrize("corruption", [
    "world_without_concept", "cast_without_world", "mixed_canonical", "bad_request_type",
    "blank_idea", "too_many_taboos", "extra_checkpoint_field",
])
def test_corrupt_partial_checkpoint_is_read_only_and_rejected_on_import(pipeline, tmp_path, corruption):
    package = initial_package()
    pipeline.concept_planner = staged_agent(package.concept, RuntimeError("offline world"))
    with pytest.raises(PlanningIncompleteError):
        pipeline.initialize_new_novel("原始创意", total_volumes=2, chapters_per_volume=2)
    manager = pipeline.state_manager
    state = manager.state.model_dump(mode="json")
    checkpoint = state["pending_initial_planning"]
    if corruption == "world_without_concept":
        checkpoint["concept"] = None
        checkpoint["world_setting"] = package.world_setting.model_dump(mode="json")
    elif corruption == "cast_without_world":
        checkpoint["world_setting"] = None
        checkpoint["initial_characters"] = [item.model_dump(mode="json") for item in package.initial_characters]
    elif corruption == "mixed_canonical":
        state["concept"] = package.concept.model_dump(mode="json")
    elif corruption == "bad_request_type":
        checkpoint["request"]["outline_plan"]["total_volumes"] = True
    elif corruption == "blank_idea":
        checkpoint["request"]["user_idea"] = " "
    elif corruption == "too_many_taboos":
        checkpoint["request"]["commercial_target"]["avoided_tropes"] = [f"item-{index}" for index in range(51)]
    else:
        checkpoint["unexpected"] = "must reject"
    atomic_write_json(manager.base_dir / "project_state.json", state)
    before = files(manager)
    corrupt = StateManager("planning", base_dir=str(manager.root_dir))
    assert corrupt.load_warnings and files(manager) == before
    with pytest.raises(InvalidProjectArchive):
        import_project_archive(archive_project(manager), tmp_path / f"rejected-{corruption}")


@pytest.mark.parametrize("current,count", [(0, 0), (0, True), (0, 1.5), (0, "2"), (True, 2), (-1, 2), (0, 51), (999999, 2)])
def test_range_planning_refuses_invalid_counts_instead_of_silently_generating_one(current, count):
    with pytest.raises(ValueError):
        NovelPipeline.plan_chapter_range(current, count)
