"""Offline end-to-end checks for temporal context and audited text identity."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agents.consistency_auditor import ConsistencyAuditorAgent
from agents.scene_writer import SceneWriterAgent
from core.multimodal_generator import AudioDramaPackage, ShortDramaScript
from core.narrative_brancher import BranchingSimulationResult
from core.reader_simulator import ReaderSimulationReport
from core.schemas import (
    AuditReport,
    ChapterBeat,
    ChapterBeatsPlan,
    ChapterContent,
    ChapterOutline,
    CharacterCard,
    CharacterState,
    CommercialDeliveryPackage,
    ForeshadowingRecord,
    GlobalOutline,
    NovelConcept,
    RelationUpdate,
    StateUpdateDelta,
    VolumeOutline,
    WorldSetting,
)
from core.semantic_memory import MemoryChunk, SemanticMemoryIndex
from core.state_manager import StateManager
from core.storage import ProjectReadOnlyError, atomic_write_json, read_json
from pipeline.novel_pipeline import NovelPipeline


def chapter(number, text="楚封走进古老的山门。"):
    return ChapterContent(chapter_number=number, volume_number=1, title=f"chapter {number}", full_text=text)


def report(passed, score=8):
    return AuditReport(
        passed=passed, consistency_score=score, pacing_score=score,
        cliffhanger_detected=True, critique="offline audit",
        revision_directives=[] if passed else ["修复硬伤"],
    )


def delivery_package():
    return CommercialDeliveryPackage(
        novel_title="离线交付",
        tiktok_promo_script="离线宣发脚本",
        ai_cover_prompts={"主封面": "offline cover prompt"},
    )


def short_drama(label="旧短剧"):
    return ShortDramaScript(title=label, core_hook="旧钩子", ending_cliffhanger="旧悬念")


def audio_package():
    return AudioDramaPackage(novel_title="旧有声包", audio_production_notes="旧配置")


def branch_result(label="旧分支"):
    return BranchingSimulationResult(decision_point_description=label)


def reader_report(number, label="旧读者报告"):
    return ReaderSimulationReport(chapter_number=number, overall_sentiment_summary=label)


def outline(*titles):
    return GlobalOutline(
        novel_title="测试总纲", main_theme="核验衍生数据", total_volumes=1, final_climax="终局",
        volumes=[VolumeOutline(
            volume_number=1, volume_title="第一卷", volume_goal="推进", main_antagonist="旧敌",
            volume_climax="对决", chapters=[
                ChapterOutline(
                    chapter_number=number, volume_number=1, title=title, summary=f"概要 {number}",
                    main_conflict=f"冲突 {number}", expectation_hook=f"钩子 {number}",
                )
                for number, title in enumerate(titles, 1)
            ],
        )],
    )


def save_derived(manager, chapter_numbers):
    for number in chapter_numbers:
        manager.save_short_drama_script(number, short_drama(f"旧短剧 {number}"))
        manager.save_reader_simulation(number, reader_report(number, f"旧读者报告 {number}"))
        manager.save_branch_simulation(number, branch_result(f"旧分支 {number}"))


@pytest.fixture
def manager(tmp_path):
    sm = StateManager("integrity", base_dir=str(tmp_path))
    sm.upsert_character(CharacterCard(id="hero", name="楚封", role="主角", background="山村少年"))
    return sm


@pytest.fixture
def timeline(manager):
    manager.save_chapter(chapter(1), StateUpdateDelta(
        character_updates={"楚封": {"level": "练气", "equipment_and_items": ["木剑"]}},
        newly_introduced_characters=[CharacterCard(id="rival", name="旧敌", role="反派", background="旧怨")],
        new_foreshadowings=[ForeshadowingRecord(hook_id="ring", title="黑戒", description="未知来历", planted_chapter=1)],
        summary_of_events="找到黑戒", current_location="山门",
    ))
    manager.save_chapter(chapter(2, "楚封获得未来仙剑并杀死旧敌。"), StateUpdateDelta(
        character_updates={"楚封": {"level": "金丹", "equipment_and_items": ["未来仙剑"]}},
        new_relations=[RelationUpdate(source="楚封", target="旧敌", relation_type="KILLED")],
        resolved_foreshadowing_ids=["ring"], summary_of_events="未来死亡事件", current_location="仙宫",
    ))
    return manager


def test_historical_context_contains_only_pre_chapter_facts_and_is_read_only(timeline):
    before = {p.relative_to(timeline.base_dir): p.read_bytes() for p in timeline.base_dir.rglob("*") if p.is_file()}
    state_before = timeline.state.model_dump_json()
    graph_before = timeline.knowledge_graph.model_dump_json()
    context = timeline.build_chapter_context(2)
    assert context["characters_status"][0]["level"] == "练气"
    assert context["characters_status"][0]["items"] == ["木剑"]
    assert context["characters_status"][0]["location"] == "山门"
    assert context["active_foreshadowings"][0]["id"] == "ring"
    assert context["recent_summaries"] == ["【第1章】找到黑戒"]
    assert "KILLED" not in context["knowledge_graph_subgraph"]
    assert "未来" not in json.dumps(context, ensure_ascii=False)
    assert timeline.state.model_dump_json() == state_before
    assert timeline.knowledge_graph.model_dump_json() == graph_before
    assert before == {p.relative_to(timeline.base_dir): p.read_bytes() for p in timeline.base_dir.rglob("*") if p.is_file()}
    assert timeline.build_chapter_context(1)["characters_status"][0]["items"] == []


def test_context_returned_lists_do_not_mutate_live_state(timeline):
    context = timeline.build_chapter_context(3)
    context["characters_status"][0]["items"].append("污染")
    context["recent_summaries"].clear()
    assert "污染" not in timeline.state.characters["楚封"].current_state.equipment_and_items
    assert timeline.state.recent_chapter_summaries


def test_history_dates_dynamic_overrides_and_keeps_static_edits(timeline):
    edited = timeline.state.characters["楚封"].model_copy(deep=True)
    edited.personality_tags = ["谨慎"]
    edited.current_state.level = "编辑最新境界"
    effective = timeline.save_manual_character(
        edited, {"personality_tags": ["谨慎"], "level": "编辑最新境界"},
    )
    assert effective == 2
    timeline.save_chapter(chapter(3, "楚封继续前行。"), StateUpdateDelta(
        character_updates={"楚封": {"level": "第三章事件境界"}}, summary_of_events="继续前行",
    ))

    before_override = timeline.build_chapter_context(2)
    after_override = timeline.build_chapter_context(3)
    after_later_event = timeline.build_chapter_context(4)
    assert before_override["characters_status"][0]["personality"] == ["谨慎"]
    assert before_override["characters_status"][0]["level"] == "练气"
    assert after_override["characters_status"][0]["level"] == "编辑最新境界"
    assert after_later_event["characters_status"][0]["level"] == "第三章事件境界"
    stored = read_json(timeline._manual_overrides_file)
    assert stored["override_version"] == 2
    assert stored["characters"]["楚封"]["dynamic_history"][0]["effective_after_chapter"] == 2

    timeline.save_chapter(chapter(1, "第一章重写。"), StateUpdateDelta(
        character_updates={"楚封": {"level": "重写一章境界"}}, summary_of_events="第一章重写",
    ))
    assert timeline.build_chapter_context(3)["characters_status"][0]["level"] == "编辑最新境界"
    assert timeline.build_chapter_context(4)["characters_status"][0]["level"] == "第三章事件境界"
    assert timeline.rollback_to_chapter(1)
    assert timeline.state.characters["楚封"].current_state.level == "重写一章境界"
    rolled_back_overrides = read_json(timeline._manual_overrides_file)["characters"]["楚封"]
    assert rolled_back_overrides["dynamic_history"] == []
    assert rolled_back_overrides["retired_dynamic_history"][0]["effective_after_chapter"] == 2
    assert rolled_back_overrides["retired_dynamic_history"][0]["rollback_target"] == 1
    timeline.save_chapter(chapter(2, "新时间线第二章。"), StateUpdateDelta(
        character_updates={"楚封": {"level": "新时间线境界"}}, summary_of_events="新时间线",
    ))
    assert timeline.state.characters["楚封"].current_state.level == "新时间线境界"


def test_legacy_undated_dynamic_override_stays_current_only(timeline):
    atomic_write_json(timeline._manual_overrides_file, {
        "characters": {"楚封": {"personality_tags": ["旧版静态"], "level": "未知时间境界"}},
    })
    timeline.state.characters["楚封"].current_state.level = "未知时间境界"
    timeline.save_chapter(chapter(3, "楚封继续前行。"), StateUpdateDelta(summary_of_events="继续前行"))

    historical = timeline.build_chapter_context(3)
    assert historical["characters_status"][0]["personality"] == ["旧版静态"]
    assert historical["characters_status"][0]["level"] == "金丹"
    assert timeline.state.characters["楚封"].current_state.level == "未知时间境界"


def test_corrupt_dated_override_refuses_character_save_without_writes(timeline):
    atomic_write_json(timeline._manual_overrides_file, {
        "override_version": 2,
        "characters": {"楚封": {
            "static": {}, "undated_dynamic": {},
            "dynamic_history": [{
                "effective_after_chapter": True,
                "recorded_at": "bad",
                "updates": {"level": "不可覆盖"},
            }],
        }},
    })
    before = {path.relative_to(timeline.base_dir): path.read_bytes()
              for path in timeline.base_dir.rglob("*") if path.is_file()}
    edited = timeline.state.characters["楚封"].model_copy(deep=True)
    edited.current_state.level = "误覆盖"

    with pytest.raises(ValueError, match="覆盖文件损坏"):
        timeline.save_manual_character(edited, {"level": "误覆盖"})

    after = {path.relative_to(timeline.base_dir): path.read_bytes()
             for path in timeline.base_dir.rglob("*") if path.is_file()}
    assert after == before
    reloaded = StateManager("integrity", base_dir=str(timeline.root_dir))
    assert any("manual_overrides.json" in warning for warning in reloaded.load_warnings)
    with pytest.raises(ProjectReadOnlyError, match="无法读取|禁止写入"):
        reloaded.replace_chapter_text(1, "损坏覆盖文件存在时不得写入")


@pytest.mark.parametrize("marker", [
    {"chapters": ["not-a-number"]}, {"chapters": ["1"]}, {"chapters": [False]},
    {"chapters": [True]}, {"chapters": [0]}, {"chapters": [-1]}, {"chapters": [1.5]},
    {"chapters": [999]}, {"chapters": "12"}, {"chapters": {}}, {}, [],
    {"chapters": [1], "reason": []}, {"chapters": [1], "reason": "x" * 4001},
])
def test_corrupt_reconciliation_marker_makes_loaded_project_read_only(timeline, marker):
    atomic_write_json(timeline._reconciliation_file, marker)

    reloaded = StateManager("integrity", base_dir=str(timeline.root_dir))

    assert any("reconciliation_required.json" in warning for warning in reloaded.load_warnings)
    with pytest.raises(ProjectReadOnlyError, match="无法读取|禁止写入"):
        reloaded.replace_chapter_text(1, "损坏核对标记存在时不得写入")


@pytest.mark.parametrize("current", [1, 2, 3, 1000])
@pytest.mark.parametrize("recent_window", [0, 2])
def test_recall_never_returns_current_or_future_chapters(current, recent_window):
    memory = SemanticMemoryIndex(chunks=[
        MemoryChunk(chunk_id=str(n), chapter_number=n, content="同一实体", involved_entities=["主角"])
        for n in [1, 2, 3, 999, 1000, 1001]
    ])
    found = memory.search_relevant_memories("同一实体", ["主角"], top_k=50, current_chapter=current, exclude_recent_n_chapters=recent_window)
    assert all(m.chapter_number < current for m in found)


def test_historical_context_rejects_missing_event_instead_of_using_future_state(timeline):
    atomic_write_json(timeline.state_events_dir / "chapter_001.json", {"chapter_number": 999, "delta": {"summary_of_events": "bad"}})
    with pytest.raises(ValueError, match="状态|事件"):
        timeline.build_chapter_context(2)


def test_timeline_status_proves_complete_contiguous_history(timeline):
    status = timeline.get_timeline_status()
    assert status == {
        "mode": "event_timeline", "baseline_chapter": 0, "current_chapter": 2,
        "event_count": 2, "missing_event_count": 0, "missing_events": [],
        "unacknowledged_missing_event_count": 0,
        "pending_chapters": [], "pending_chapter_count": 0, "complete": True,
        "integrity_valid": True,
        "chapter_one_context_available": True,
    }


@pytest.mark.parametrize("mutation", [
    "missing", "wrong_chapter", "wrong_project", "wrong_text_hash", "bool_volume", "oversize",
])
def test_corrupt_timeline_is_read_only_on_reload(timeline, mutation):
    event_file = timeline.state_events_dir / "chapter_002.json"
    if mutation == "missing":
        event_file.unlink()
    elif mutation == "oversize":
        event_file.write_bytes(b"{" + b" " * (16 * 1024 * 1024) + b"}")
    else:
        data = read_json(event_file)
        if mutation == "wrong_chapter":
            data["chapter_number"] = 1
        elif mutation == "wrong_project":
            data["novel_id"] = "another-book"
        elif mutation == "wrong_text_hash":
            data["source_text_sha256"] = "0" * 64
        else:
            data["volume_number"] = True
        atomic_write_json(event_file, data)

    reloaded = StateManager("integrity", base_dir=str(timeline.root_dir))

    if mutation == "missing":
        status = reloaded.get_timeline_status()
        assert status["complete"] is False
        assert status["integrity_valid"] is False
        assert status["missing_event_count"] == 1
        assert status["missing_events"] == [2]
    assert any("state_events" in warning for warning in reloaded.load_warnings)
    with pytest.raises(ProjectReadOnlyError, match="禁止写入|无法读取"):
        reloaded.replace_chapter_text(1, "损坏时间线存在时不得写入")


def test_acknowledged_future_gap_allows_ordered_prefix_without_publishing_future_aggregate(timeline):
    (timeline.state_events_dir / "chapter_002.json").unlink()
    timeline.mark_state_reconciliation_required(2, "作者尚未核对")
    replacement = chapter(1, "重写第一章")
    timeline.save_chapter(replacement, StateUpdateDelta(
        character_updates={"楚封": {"level": "筑基"}}, summary_of_events="重写",
    ))
    reloaded = StateManager("integrity", base_dir=str(timeline.root_dir))
    assert reloaded.load_warnings == []
    assert reloaded.state.characters["楚封"].current_state.level == "金丹"
    assert reloaded.read_chapter(1).full_text == "重写第一章"
    assert reloaded.build_chapter_context(2)["characters_status"][0]["level"] == "筑基"
    assert reloaded.get_timeline_status()["pending_chapters"] == [2]


def test_replayed_prefix_never_treats_bounded_status_sample_as_full_reconciliation(timeline):
    timeline.mark_state_reconciliation_required(2, "作者尚未核对")
    baseline, state, graph, events, status = timeline._validated_timeline()
    bounded_status = {**status, "pending_chapters": []}

    with pytest.raises(ValueError, match="顺序|前序"):
        timeline._build_replayed_prefix(2, (baseline, state, graph, events, bounded_status))


def test_temporal_evidence_remains_exact_after_project_rename(timeline):
    timeline.rename_project("renamed_timeline")
    renamed = StateManager("renamed_timeline", base_dir=str(timeline.root_dir))
    assert renamed.load_warnings == []
    assert renamed.get_timeline_status()["complete"] is True
    assert renamed.build_chapter_context(1)["state_as_of_chapter"] == 0
    assert renamed.build_chapter_context(2)["characters_status"][0]["level"] == "练气"
    assert all(
        read_json(path).get("novel_id") == "renamed_timeline"
        for path in renamed.state_events_dir.glob("chapter_*.json")
    )
    assert all(
        read_json(path).get("novel_id") == "renamed_timeline"
        for path in renamed.snapshots_dir.glob("snapshot_after_chap_*.json")
    )


def test_author_confirmed_history_repair_is_sequential_and_preserves_retired_evidence(manager):
    manager.save_chapter(chapter(1))
    manager.save_chapter(chapter(2))
    manager._write_timeline_baseline(2)
    seed = CharacterState(
        level="凡人", location="山村", hp_status="健康", current_mood="困惑",
        equipment_and_items=["旧玉佩"], skills_and_abilities=[], relations={}, is_alive=True,
    )
    status = manager.initialize_history_repair({"楚封": seed}, "作者对照第一章原稿逐项核对")
    assert status["baseline_chapter"] == 0
    assert status["integrity_valid"] is True
    assert status["missing_events"] == [1, 2]
    assert status["pending_chapters"] == [1, 2]
    retired = list((manager.state_events_dir / "retired_timelines").glob("repair_*"))
    assert len(retired) == 1
    manifest = read_json(retired[0] / "manifest.json")
    assert manifest["author_note"] == "作者对照第一章原稿逐项核对"
    assert manifest["selected_initial_characters"] == ["楚封"]

    manager.save_chapter(chapter(1, "第一章核对稿"), StateUpdateDelta(
        character_updates={"楚封": {"level": "练气"}}, summary_of_events="踏入修行", current_location="山门",
    ))
    assert manager.get_timeline_status()["pending_chapters"] == [2]
    partial = StateManager("integrity", base_dir=str(manager.root_dir))
    assert partial.load_warnings == []
    assert partial.build_chapter_context(2)["characters_status"][0]["level"] == "练气"
    assert partial.state.current_chapter_idx == 2  # Aggregate remains at the legacy head until repair completes.

    partial.save_chapter(chapter(2, "第二章核对稿"), StateUpdateDelta(
        character_updates={"楚封": {"level": "筑基"}}, summary_of_events="完成筑基", current_location="洞府",
    ))
    assert partial.get_timeline_status()["complete"] is True
    completed = StateManager("integrity", base_dir=str(manager.root_dir))
    assert completed.load_warnings == []
    assert completed.get_state_reconciliation_status()["chapters"] == []
    assert completed.state.characters["楚封"].current_state.level == "筑基"
    assert completed.state.characters["楚封"].current_state.location == "洞府"

    completed.rename_project("renamed_repair")
    renamed = StateManager("renamed_repair", base_dir=str(manager.root_dir))
    retired = next((renamed.state_events_dir / "retired_timelines").glob("repair_*"))
    manifest = read_json(retired / "manifest.json")
    record = next(item for item in manifest["archived_files"] if item["file"] == "baseline.json")
    payload = (retired / "baseline.json").read_bytes()
    evidence = json.loads(payload)
    assert manifest["novel_id"] == "renamed_repair"
    assert record["bytes"] == len(payload)
    assert record["sha256"] == hashlib.sha256(payload).hexdigest()
    assert evidence["novel_id"] == "renamed_repair"
    assert evidence["state"]["novel_id"] == "renamed_repair"


def test_history_repair_rejects_out_of_order_chapter_without_partial_writes(manager):
    manager.save_chapter(chapter(1))
    manager.save_chapter(chapter(2))
    manager.initialize_history_repair({"楚封": manager.state.characters["楚封"].initial_state}, "已人工核对")
    before = {path.relative_to(manager.base_dir): path.read_bytes()
              for path in manager.base_dir.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="顺序|前序"):
        manager.save_chapter(chapter(2, "越序写入"), StateUpdateDelta(summary_of_events="越序"))
    after = {path.relative_to(manager.base_dir): path.read_bytes()
             for path in manager.base_dir.rglob("*") if path.is_file()}
    assert after == before


def test_new_state_event_cannot_skip_chapters_or_leave_partial_writes(manager):
    before = {path.relative_to(manager.base_dir): path.read_bytes()
              for path in manager.base_dir.rglob("*") if path.is_file()}

    with pytest.raises(ValueError, match="连续推进|不能直接写入"):
        manager.save_chapter(chapter(2), StateUpdateDelta(summary_of_events="越过第一章"))

    after = {path.relative_to(manager.base_dir): path.read_bytes()
             for path in manager.base_dir.rglob("*") if path.is_file()}
    assert after == before


def test_history_repair_requires_known_characters_and_contiguous_chapters(manager):
    manager.save_chapter(chapter(1))
    with pytest.raises(ValueError, match="不在当前人物卡"):
        manager.initialize_history_repair({"不存在": CharacterState()}, "已核对")
    broken = StateManager("integrity", base_dir=str(manager.root_dir))
    broken.save_chapter(chapter(3))
    broken = StateManager("integrity", base_dir=str(manager.root_dir))
    with pytest.raises(ValueError, match="不连续"):
        broken.initialize_history_repair({}, "已核对")


def test_state_only_history_reconciliation_preserves_prose_and_advances_in_order(manager):
    manager.save_chapter(chapter(1, "第一章原文，绝不能重写。"))
    manager.save_chapter(chapter(2, "第二章原文，也不能重写。"))
    manager.initialize_history_repair(
        {"楚封": manager.state.characters["楚封"].initial_state}, "已核对章前状态",
    )
    pipeline = NovelPipeline.__new__(NovelPipeline)
    pipeline.state_manager = manager
    pipeline.state_updater = Mock()
    pipeline.state_updater.extract_state_delta.side_effect = [
        StateUpdateDelta(
            character_updates={"楚封": {"level": "练气"}},
            summary_of_events="踏入修行", current_location="山门",
        ),
        StateUpdateDelta(
            character_updates={"楚封": {"level": "筑基"}},
            summary_of_events="完成筑基", current_location="洞府",
        ),
    ]
    original_text = {number: manager.state.chapters[number].full_text for number in (1, 2)}

    pipeline.reconcile_existing_chapter_state(1)
    assert manager.get_state_reconciliation_status()["chapters"] == [2]
    assert manager.state.chapters[1].full_text == original_text[1]
    first_event = read_json(manager.state_events_dir / "chapter_001.json")
    assert first_event["event_version"] == 2
    assert first_event["source_text_sha256"] == hashlib.sha256(original_text[1].encode()).hexdigest()
    first_call = pipeline.state_updater.extract_state_delta.call_args_list[0].kwargs
    assert first_call["chapter_text"] == original_text[1]
    assert first_call["current_characters"]["楚封"]["level"] == manager.state.characters[
        "楚封"
    ].initial_state.level

    pipeline.reconcile_existing_chapter_state(2)
    assert manager.get_state_reconciliation_status()["chapters"] == []
    assert {number: manager.state.chapters[number].full_text for number in (1, 2)} == original_text
    assert manager.state.characters["楚封"].current_state.level == "筑基"
    assert manager.get_timeline_status()["complete"] is True


def test_state_only_history_reconciliation_fails_before_writes_or_out_of_order_model_call(manager):
    manager.save_chapter(chapter(1))
    manager.save_chapter(chapter(2))
    manager.initialize_history_repair({}, "已核对章前空状态")
    pipeline = NovelPipeline.__new__(NovelPipeline)
    pipeline.state_manager = manager
    pipeline.state_updater = Mock()
    before = {path.relative_to(manager.base_dir): path.read_bytes()
              for path in manager.base_dir.rglob("*") if path.is_file()}

    with pytest.raises(ValueError, match="下一章是第 1 章"):
        pipeline.reconcile_existing_chapter_state(2)
    pipeline.state_updater.extract_state_delta.assert_not_called()

    pipeline.state_updater.extract_state_delta.side_effect = RuntimeError("provider offline")
    with pytest.raises(RuntimeError, match="provider offline"):
        pipeline.reconcile_existing_chapter_state(1)
    after = {path.relative_to(manager.base_dir): path.read_bytes()
             for path in manager.base_dir.rglob("*") if path.is_file()}
    assert after == before


def test_legacy_requires_trustworthy_pre_chapter_checkpoint(manager):
    manager.save_chapter(chapter(1))
    manager.save_chapter(chapter(2))
    assert manager.build_chapter_context(2)["state_as_of_chapter"] == 1
    with pytest.raises(ValueError, match="状态|快照"):
        manager.build_chapter_context(1)


def test_reconciliation_blocks_later_generation_but_allows_repairing_target(timeline):
    timeline.mark_state_reconciliation_required(1, "edited")
    with pytest.raises(ValueError, match="补录|核对|状态"):
        timeline.build_chapter_context(2)
    assert timeline.build_chapter_context(1)["state_as_of_chapter"] == 0


def fake_pipeline(manager, texts, reports, polished=None):
    pipeline = NovelPipeline.__new__(NovelPipeline)
    pipeline.state_manager = manager
    pipeline.settings = SimpleNamespace(MAX_AUDIT_RETRIES=1)
    manager.get_chapter_outline = Mock(return_value=(None, None))
    pipeline.beat_director = Mock()
    pipeline.beat_director.plan_chapter_beats.return_value = ChapterBeatsPlan(
        chapter_number=1, chapter_title="测试", core_expectation="开篇", cliffhanger_design="门后是谁",
        beats=[ChapterBeat(beat_index=1, scene_goal="开门", action_and_dialogue_plan="问话", emotional_tone="悬疑", conflict_type="信息")],
    )
    pipeline.scene_writer = Mock()
    pipeline.scene_writer.write_beat.side_effect = texts
    pipeline.consistency_auditor = Mock()
    pipeline.consistency_auditor.audit_chapter.side_effect = reports
    pipeline.stylist_polisher = Mock()
    pipeline.stylist_polisher.polish_chapter.side_effect = (
        (lambda **kw: kw["raw_chapter_text"]) if polished is None else (lambda **kw: polished)
    )
    pipeline.state_updater = Mock()
    pipeline.state_updater.extract_state_delta.return_value = StateUpdateDelta(summary_of_events="开门")
    pipeline.multimodal_generator = Mock()
    pipeline.multimodal_generator.generate_short_drama_script.side_effect = RuntimeError("offline disabled")
    return pipeline


def assert_final_text(pipeline, result, expected, passed):
    assert result.full_text == expected
    assert result.final_audit.passed is passed
    assert result.final_audit.text_sha256 == hashlib.sha256(expected.encode()).hexdigest()
    assert pipeline.state_updater.extract_state_delta.call_args.kwargs["chapter_text"] == expected
    loaded = StateManager("integrity", base_dir=str(pipeline.state_manager.root_dir)).read_chapter(1)
    assert loaded.full_text == expected
    assert loaded.final_audit == result.final_audit


def test_passed_draft_beats_higher_scoring_failed_draft(manager):
    pipeline = fake_pipeline(manager, ["高分硬伤稿", "通过稿"], [report(False, 9), report(True, 8)])
    result = pipeline.generate_single_chapter(1)
    assert_final_text(pipeline, result, "通过稿", True)


def test_best_failed_draft_retains_its_own_audit(manager):
    pipeline = fake_pipeline(manager, ["较佳稿", "较差稿"], [report(False, 8), report(False, 5)])
    result = pipeline.generate_single_chapter(1)
    assert_final_text(pipeline, result, "较佳稿", False)
    assert result.final_audit.consistency_score == 8
    assert len(result.audit_history) == 2


def test_audit_outage_does_not_discard_best_reviewed_draft(manager):
    pipeline = fake_pipeline(manager, ["已审稿", "未审稿"], [report(False, 7), RuntimeError("critic unavailable")])
    assert_final_text(pipeline, pipeline.generate_single_chapter(1), "已审稿", False)


@pytest.mark.parametrize("polish_report", [report(False, 4), RuntimeError("critic offline")])
def test_failed_or_unreviewed_polish_falls_back_to_audited_text(manager, polish_report):
    pipeline = fake_pipeline(manager, ["通过原稿"], [report(True), polish_report], polished="篡改设定润色稿")
    result = pipeline.generate_single_chapter(1)
    assert_final_text(pipeline, result, "通过原稿", True)
    assert len(result.audit_history) == 2


def test_accepted_polish_is_reaudited_and_facts_use_same_text(manager):
    pipeline = fake_pipeline(manager, ["原稿"], [report(True), report(True, 9)], polished="合格润色稿")
    result = pipeline.generate_single_chapter(1)
    assert_final_text(pipeline, result, "合格润色稿", True)
    assert result.final_audit.stage == "polished"
    assert pipeline.consistency_auditor.audit_chapter.call_count == 2


def test_empty_polish_falls_back_and_empty_writer_is_not_saved(manager):
    pipeline = fake_pipeline(manager, ["完整原稿"], [report(True)], polished="   ")
    assert_final_text(pipeline, pipeline.generate_single_chapter(1), "完整原稿", True)
    pipeline = fake_pipeline(manager, ["   "], [report(True)])
    with pytest.raises(ValueError, match="正文|节拍"):
        pipeline.generate_single_chapter(1)
    assert manager.read_chapter(1).full_text == "完整原稿"


def test_manual_edit_invalidates_audit_without_deleting_history(manager):
    pipeline = fake_pipeline(manager, ["已审原文"], [report(True)])
    pipeline.generate_single_chapter(1)
    manager.replace_chapter_text(1, "人工改文")
    loaded = manager.read_chapter(1)
    assert loaded.final_audit is None
    assert loaded.audit_status == "unverified"
    assert len(loaded.audit_history) == 1
    metadata = read_json(manager.chapter_meta_dir / "chapter_001.json")
    assert metadata["final_audit"] is None
    assert metadata["audit_status"] == "unverified"


def test_human_review_is_text_bound_persisted_and_invalidated_by_edit(manager):
    manager.save_chapter(chapter(1, "待人工审阅正文"))
    before_count = read_json(manager.chapter_meta_dir / "chapter_001.json")["memory_chunk_count"]
    no_memory_load = StateManager(
        "integrity", base_dir=str(manager.root_dir), load_memory=False, load_derived=False,
    )

    review = no_memory_load.record_human_review(1, "approved", "作者已通读")

    assert review.text_sha256 == hashlib.sha256("待人工审阅正文".encode()).hexdigest()
    loaded = StateManager("integrity", base_dir=str(manager.root_dir))
    assert loaded.read_chapter(1).current_human_review.note == "作者已通读"
    assert loaded.read_chapter(1).human_review_status == "approved"
    metadata = read_json(manager.chapter_meta_dir / "chapter_001.json")
    assert metadata["human_review_status"] == "approved"
    assert metadata["memory_chunk_count"] == before_count
    lightweight = StateManager(
        "integrity", base_dir=str(manager.root_dir), load_chapter_content=False,
        load_memory=False, load_derived=False,
    )
    assert lightweight.state.chapters[1].human_review_status == "approved"
    assert lightweight.state.chapters[1].current_human_review.note == "作者已通读"

    loaded.replace_chapter_text(1, "人工修改后的正文")
    edited = loaded.read_chapter(1)
    assert edited.current_human_review is None
    assert edited.human_review_status == "unreviewed"
    assert len(edited.human_review_history) == 1


def test_lightweight_human_review_refuses_to_erase_unaccounted_memory(manager):
    manager.save_chapter(chapter(1, "元数据修复前的正文"))
    memory_file = manager.memory_dir / "chapter_001.json"
    atomic_write_json(memory_file, {"chunks": [{"id": "must-survive"}]})
    (manager.chapter_meta_dir / "chapter_001.json").unlink()
    before = {path.relative_to(manager.base_dir): path.read_bytes()
              for path in manager.base_dir.rglob("*") if path.is_file()}
    lightweight = StateManager(
        "integrity", base_dir=str(manager.root_dir), load_memory=False, load_derived=False,
    )

    with pytest.raises(ProjectReadOnlyError, match="元数据缺失"):
        lightweight.record_human_review(1, "approved", "已通读")

    after = {path.relative_to(manager.base_dir): path.read_bytes()
             for path in manager.base_dir.rglob("*") if path.is_file()}
    assert after == before


def test_rewrite_preserves_prior_machine_and_human_review_history(manager):
    old_text, new_text = "旧版正文", "新版正文"
    old_audit = report(True).model_copy(update={
        "text_sha256": hashlib.sha256(old_text.encode()).hexdigest(), "stage": "draft", "attempt": 1,
    })
    manager.save_chapter(chapter(1, old_text).model_copy(update={"audit_history": [old_audit]}))
    manager.record_human_review(1, "changes_requested", "需要重写结尾")
    new_audit = report(True, 9).model_copy(update={
        "text_sha256": hashlib.sha256(new_text.encode()).hexdigest(), "stage": "polished", "attempt": 1,
    })

    manager.save_chapter(chapter(1, new_text).model_copy(update={"audit_history": [new_audit]}))

    loaded = manager.read_chapter(1)
    assert [item.text_sha256 for item in loaded.audit_history] == [old_audit.text_sha256, new_audit.text_sha256]
    assert loaded.final_audit == new_audit
    assert len(loaded.human_review_history) == 1
    assert loaded.current_human_review is None
    assert loaded.human_review_status == "unreviewed"


def test_chapter_source_change_invalidates_delivery_package_and_artifacts(manager):
    manager.save_chapter(chapter(1, "交付包对应的原始正文"))
    manager.set_delivery_package(delivery_package())
    artifacts = [
        manager.delivery_dir / "delivery_package.json",
        manager.delivery_dir / f"{manager.novel_id}_抖音推文爆款脚本.txt",
        manager.delivery_dir / f"{manager.novel_id}_AI封面提示词.txt",
    ]
    assert all(path.is_file() for path in artifacts)

    # State-only or metadata re-publication of identical source must not discard
    # a still-current package.
    manager.save_chapter(manager.read_chapter(1).model_copy(deep=True))
    assert manager.state.delivery_package is not None
    assert all(path.is_file() for path in artifacts)

    manager.replace_chapter_text(1, "正文已经改变，旧宣发内容必须失效")
    assert manager.state.delivery_package is None
    assert not any(path.exists() for path in artifacts)
    assert (manager.delivery_dir / f"{manager.novel_id}_全本发布稿.txt").is_file()
    assert StateManager("integrity", base_dir=str(manager.root_dir)).state.delivery_package is None


@pytest.mark.parametrize("source", ["concept", "world", "character"])
def test_non_chapter_delivery_inputs_invalidate_existing_package(manager, source):
    manager.set_concept(NovelConcept(
        title="旧书名", genre="悬疑", logline="旧简介", golden_finger="旧罗盘", core_conflict="旧冲突",
    ))
    manager.set_world_setting(WorldSetting(world_name="旧世界", geography_overview="旧港区"))
    manager.set_delivery_package(delivery_package())

    if source == "concept":
        changed = manager.state.concept.model_copy(update={"title": "新书名"})
        manager.set_concept(changed)
    elif source == "world":
        changed = manager.state.world_setting.model_copy(update={"world_name": "新世界"})
        manager.set_world_setting(changed)
    else:
        card = manager.state.characters["楚封"].model_copy(deep=True)
        manager.save_manual_character(card, {"background": "新的主角背景"})

    assert manager.state.delivery_package is None
    assert not (manager.delivery_dir / "delivery_package.json").exists()


def test_chapter_edit_invalidates_only_bound_adaptations_and_later_branches(manager):
    for number in (1, 2, 3):
        manager.save_chapter(chapter(number, f"旧正文 {number}"))
    save_derived(manager, (1, 2, 3))

    # Re-publishing identical source is a no-op for every projection.
    manager.save_chapter(manager.read_chapter(2).model_copy(deep=True))
    assert set(manager.short_drama_scripts) == {1, 2, 3}
    assert set(manager.reader_simulations) == {1, 2, 3}
    assert set(manager.branch_simulations) == {1, 2, 3}

    manager.replace_chapter_text(2, "第二章已经人工改写")

    assert set(manager.short_drama_scripts) == {1, 3}
    assert set(manager.reader_simulations) == {1, 3}
    assert set(manager.branch_simulations) == {1}
    assert not (manager.multimodal_dir / "short_drama_chap_002.json").exists()
    assert not (manager.reader_sim_dir / "reader_sim_chap_002.json").exists()
    assert not (manager.base_dir / "branch_sim_chap_002.json").exists()
    assert not (manager.base_dir / "branch_sim_chap_003.json").exists()
    reloaded = StateManager("integrity", base_dir=str(manager.root_dir))
    assert set(reloaded.short_drama_scripts) == {1, 3}
    assert set(reloaded.reader_simulations) == {1, 3}
    assert set(reloaded.branch_simulations) == {1}


def test_concept_change_invalidates_every_concept_bound_projection(manager):
    manager.set_concept(NovelConcept(
        title="旧书", genre="悬疑", logline="旧简介", golden_finger="旧罗盘", core_conflict="旧冲突",
    ))
    manager.save_chapter(chapter(1))
    save_derived(manager, (1,))
    manager.save_audio_drama_package(audio_package())
    manager.set_delivery_package(delivery_package())

    manager.set_concept(manager.state.concept.model_copy(deep=True))
    assert manager.short_drama_scripts and manager.reader_simulations
    assert manager.branch_simulations and manager.audio_package and manager.state.delivery_package

    manager.set_concept(manager.state.concept.model_copy(update={"title": "新书"}))
    assert manager.short_drama_scripts == {}
    assert manager.reader_simulations == {}
    assert manager.branch_simulations == {}
    assert manager.audio_package is None
    assert manager.state.delivery_package is None
    assert not (manager.multimodal_dir / "audio_drama_package.json").exists()


def test_world_and_character_changes_invalidate_only_their_projection_families(manager):
    manager.set_world_setting(WorldSetting(world_name="旧世界", geography_overview="旧港区"))
    manager.save_chapter(chapter(1))
    save_derived(manager, (1,))
    manager.save_audio_drama_package(audio_package())
    manager.set_delivery_package(delivery_package())

    manager.set_world_setting(manager.state.world_setting.model_copy(update={"world_name": "新世界"}))
    assert manager.branch_simulations == {}
    assert manager.state.delivery_package is None
    assert manager.short_drama_scripts and manager.reader_simulations and manager.audio_package

    manager.save_branch_simulation(1, branch_result())
    manager.set_delivery_package(delivery_package())
    unchanged = manager.state.characters["楚封"].model_copy(deep=True)
    assert manager.save_manual_character(unchanged, {"role": unchanged.role}) is None
    assert manager.branch_simulations and manager.audio_package and manager.state.delivery_package

    changed = unchanged.model_copy(update={"role": "第一主角"})
    manager.save_manual_character(changed, {"role": changed.role})
    assert manager.branch_simulations == {}
    assert manager.audio_package is None
    assert manager.state.delivery_package is None
    assert manager.short_drama_scripts and manager.reader_simulations


def test_outline_change_invalidates_only_the_affected_chapter_branch(manager):
    manager.set_global_outline(outline("第一章", "第二章"))
    manager.save_branch_simulation(1, branch_result("分支一"))
    manager.save_branch_simulation(2, branch_result("分支二"))

    manager.set_global_outline(manager.state.global_outline.model_copy(deep=True))
    assert set(manager.branch_simulations) == {1, 2}

    edited = manager.state.global_outline.model_copy(deep=True)
    edited.volumes[0].chapters[1].main_conflict = "第二章的新冲突"
    manager.set_global_outline(edited)
    assert set(manager.branch_simulations) == {1}
    assert (manager.base_dir / "branch_sim_chap_001.json").is_file()
    assert not (manager.base_dir / "branch_sim_chap_002.json").exists()


def test_failed_short_drama_regeneration_never_leaves_old_projection(manager):
    manager.set_concept(NovelConcept(
        title="短剧测试", genre="悬疑", logline="测试", golden_finger="无", core_conflict="开门",
    ))
    first = fake_pipeline(manager, ["第一版正文"], [report(True)])
    first.multimodal_generator.generate_short_drama_script.side_effect = None
    first.multimodal_generator.generate_short_drama_script.return_value = short_drama("第一版短剧")
    first.generate_single_chapter(1)
    assert manager.short_drama_scripts[1].title == "第一版短剧"

    second = fake_pipeline(manager, ["第二版正文"], [report(True)])
    result = second.generate_single_chapter(1)

    assert result.full_text == "第二版正文"
    assert 1 not in manager.short_drama_scripts
    assert not (manager.multimodal_dir / "short_drama_chap_001.json").exists()
    assert 1 not in StateManager("integrity", base_dir=str(manager.root_dir)).short_drama_scripts


def test_metadata_view_preserves_audit_without_loading_text(manager):
    pipeline = fake_pipeline(manager, ["已审原文"], [report(True)])
    result = pipeline.generate_single_chapter(1)
    lightweight = StateManager("integrity", base_dir=str(manager.root_dir), load_chapter_content=False)
    stub = lightweight.state.chapters[1]
    assert stub.full_text == ""
    assert stub.final_audit == result.final_audit
    assert stub.audit_status == "passed"


def test_old_audit_without_text_identity_is_not_treated_as_verified():
    legacy = chapter(1)
    legacy.audit_history = [report(True)]
    loaded = ChapterContent.model_validate(legacy.model_dump())
    assert loaded.final_audit is None
    assert loaded.audit_status == "unverified"


def test_first_audit_outage_is_honestly_marked_unavailable(manager):
    pipeline = fake_pipeline(manager, ["保留原文"], [RuntimeError("offline")])
    result = pipeline.generate_single_chapter(1)
    assert_final_text(pipeline, result, "保留原文", False)
    assert result.audit_status == "unavailable"


def test_provider_cannot_supply_provenance(manager):
    forged = report(True).model_copy(update={"text_sha256": "fake", "stage": "legacy", "service_error": True, "attempt": 99})
    pipeline = fake_pipeline(manager, ["真实正文"], [forged])
    result = pipeline.generate_single_chapter(1)
    assert_final_text(pipeline, result, "真实正文", True)
    assert result.final_audit.stage == "draft"
    assert result.final_audit.attempt == 1
    assert not result.final_audit.service_error


def test_extract_failure_commits_prose_with_marker_and_blocks_continuation(manager):
    pipeline = fake_pipeline(manager, ["完整正文"], [report(True)])
    pipeline.state_updater.extract_state_delta.side_effect = RuntimeError("offline")
    pipeline.generate_single_chapter(1)
    loaded = StateManager("integrity", base_dir=str(manager.root_dir))
    assert loaded.get_state_reconciliation_status()["chapters"] == [1]
    assert loaded.read_chapter(1).full_text == "完整正文"
    with pytest.raises(ValueError, match="状态"):
        loaded.build_chapter_context(2)


def test_marker_write_failure_rolls_back_entire_generation(manager):
    before = {p.relative_to(manager.base_dir): p.read_bytes() for p in manager.base_dir.rglob("*") if p.is_file()}
    pipeline = fake_pipeline(manager, ["完整正文"], [report(True)])
    pipeline.state_updater.extract_state_delta.side_effect = RuntimeError("offline")
    manager.mark_state_reconciliation_required = Mock(side_effect=OSError("disk failure"))
    with pytest.raises(OSError, match="disk failure"):
        pipeline.generate_single_chapter(1)
    assert before == {p.relative_to(manager.base_dir): p.read_bytes() for p in manager.base_dir.rglob("*") if p.is_file()}


def test_missing_history_stops_before_any_agent_invocation(manager):
    manager.save_chapter(chapter(1))
    pipeline = fake_pipeline(manager, ["不应生成"], [report(True)])
    manager.get_chapter_outline.return_value = None
    pipeline.outline_architect = Mock()
    with pytest.raises(ValueError, match="状态"):
        pipeline.generate_single_chapter(1)
    pipeline.outline_architect.extend_outline.assert_not_called()
    pipeline.beat_director.plan_chapter_beats.assert_not_called()
    pipeline.scene_writer.write_beat.assert_not_called()


def test_context_replay_at_chapter_1000_is_numerical(manager):
    manager.state.current_chapter_idx = 998
    manager.save_chapter(chapter(999), StateUpdateDelta(character_updates={"楚封": {"level": "第999章状态"}}, summary_of_events="前章"))
    manager.save_chapter(chapter(1000), StateUpdateDelta(character_updates={"楚封": {"level": "第1000章状态"}}, summary_of_events="本章"))
    context = manager.build_chapter_context(1000)
    assert context["characters_status"][0]["level"] == "第999章状态"
    assert context["recent_summaries"] == ["【第999章】前章"]


def test_corrupt_reconciliation_file_is_not_silently_ignored(timeline):
    atomic_write_json(timeline.state_events_dir / "reconciliation_required.json", {"chapters": ["bad"]})
    with pytest.raises(ValueError, match="标记损坏"):
        timeline.build_chapter_context(3)


@pytest.mark.parametrize("issue_field", ["logic_issues", "character_ooc_issues", "revision_directives"])
def test_strictness_cannot_promote_high_scoring_draft_with_hard_issues(issue_field, monkeypatch):
    monkeypatch.setattr("agents.consistency_auditor.get_settings", lambda: SimpleNamespace(AUDIT_STRICTNESS="lenient"))
    critic = ConsistencyAuditorAgent.__new__(ConsistencyAuditorAgent)
    candidate = report(False, 9)
    candidate.revision_directives = []
    setattr(candidate, issue_field, ["必须修正"])
    assert not critic._apply_strictness(candidate).passed


def test_writer_and_critic_receive_same_pre_chapter_facts(timeline, monkeypatch):
    context = timeline.build_chapter_context(2)
    writer = SceneWriterAgent.__new__(SceneWriterAgent)
    writer.call_llm = Mock(return_value="正文")
    writer.write_beat(
        beat=ChapterBeat(beat_index=1, scene_goal="开门", action_and_dialogue_plan="问话", emotional_tone="悬疑", conflict_type="信息"),
        chapter_title="第二章", chapter_number=2, novel_title="测试", golden_finger="无",
        characters_desc="明确传入的人物状态", chapter_context=context,
    )
    critic = ConsistencyAuditorAgent.__new__(ConsistencyAuditorAgent)
    critic.call_structured = Mock(return_value=report(True))
    monkeypatch.setattr("agents.consistency_auditor.get_settings", lambda: SimpleNamespace(AUDIT_STRICTNESS="standard"))
    critic.audit_chapter(2, "第二章", "正文", [], context["characters_status"], "开门", chapter_context=context)
    writer_prompt = writer.call_llm.call_args.args[0]
    assert "明确传入的人物状态" in writer_prompt
    for prompt in [writer_prompt, critic.call_structured.call_args.args[0]]:
        assert "截至第 1 章末" in prompt
        assert "木剑" in prompt
        assert "找到黑戒" in prompt
        assert "未来仙剑" not in prompt


def test_full_and_lightweight_apis_show_matching_audit(manager, monkeypatch):
    from fastapi.testclient import TestClient

    import web_ui
    from config.settings import get_settings

    manager.set_concept(NovelConcept(title="测试", genre="玄幻", logline="开门", golden_finger="无", core_conflict="旧怨"))
    pipeline = fake_pipeline(manager, ["高分未通过", "低分通过"], [report(False, 9), report(True, 8)])
    pipeline.generate_single_chapter(1)
    monkeypatch.setenv("OUTPUT_DIR", str(manager.root_dir))
    get_settings.cache_clear()
    try:
        with TestClient(web_ui.app) as client:
            detail = client.get("/api/novel/integrity")
            body = client.get("/api/novel/integrity/chapter/1")
            assert detail.status_code == body.status_code == 200
            summary = detail.json()["chapters"][0]
            assert "full_text" not in summary
            assert summary["final_audit"] == body.json()["chapter"]["final_audit"]
            assert summary["audit_status"] == "passed"
            manager.replace_chapter_text(1, "修改正文")
            assert client.get("/api/novel/integrity").json()["chapters"][0]["audit_status"] == "unverified"
    finally:
        get_settings.cache_clear()
