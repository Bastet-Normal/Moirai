"""Chronological editor state must agree across replay, checkpoints and rollback."""

from copy import deepcopy

import pytest

from core import character_overrides
from core.schemas import ChapterContent, CharacterCard, StateUpdateDelta
from core.state_manager import StateManager
from core.storage import atomic_write_json, read_json


@pytest.fixture
def manager(tmp_path):
    state = StateManager("overrides", base_dir=str(tmp_path))
    state.upsert_character(CharacterCard(id="hero", name="主角", role="主角", background="初始人设"))
    append(state, 1, "剧情一")
    return state


def append(manager, number, level):
    manager.save_chapter(
        ChapterContent(chapter_number=number, volume_number=1, title=str(number), full_text=f"第{number}章正文"),
        StateUpdateDelta(character_updates={"主角": {"level": level}}, summary_of_events=str(number)),
    )


def edit_level(manager, value):
    edited = manager.state.characters["主角"].model_copy(deep=True)
    edited.current_state.level = value
    manager.save_manual_character(edited, {"level": value})


def files(manager):
    return {p.relative_to(manager.base_dir): p.read_bytes() for p in manager.base_dir.rglob("*") if p.is_file()}


@pytest.mark.parametrize("path", ["rollback", "snapshot", "baseline"])
def test_checkpoint_never_reapplies_override_superseded_by_story(manager, path):
    edit_level(manager, "第一章后人工境界")
    append(manager, 2, "第二章剧情境界")
    append(manager, 3, "第三章剧情境界")
    if path == "snapshot":
        manager._timeline_baseline_file.unlink()
        historical, _ = manager.state_before_chapter(3)
        assert historical.characters["主角"].current_state.level == "第二章剧情境界"
    elif path == "baseline":
        manager._reset_timeline_at_current_state(3)
        append(manager, 4, "第四章剧情境界")
        historical, _ = manager.state_before_chapter(4)
        assert historical.characters["主角"].current_state.level == "第三章剧情境界"
    else:
        assert manager.rollback_to_chapter(2)
        assert manager.state.characters["主角"].current_state.level == "第二章剧情境界"


def test_new_dated_override_supersedes_legacy_without_losing_evidence(manager):
    atomic_write_json(manager._manual_overrides_file, {
        "characters": {"主角": {"level": "旧版无日期境界", "location": "旧版地点"}},
    })
    edit_level(manager, "新版人工境界")
    append(manager, 2, "第二章剧情境界")
    append(manager, 1, "第一章重写境界")

    assert manager.state.characters["主角"].current_state.level == "第二章剧情境界"
    assert manager.state.characters["主角"].current_state.location == "旧版地点"
    raw = read_json(manager._manual_overrides_file)
    assert raw["characters"]["主角"]["undated_dynamic"]["level"] == "旧版无日期境界"


def test_saving_unchanged_dynamic_fields_does_not_date_them(manager):
    edited = manager.state.characters["主角"].model_copy(deep=True)
    edited.background = "只修改背景"
    manager.save_manual_character(edited, {"background": edited.background, "level": edited.current_state.level})

    raw = read_json(manager._manual_overrides_file)
    assert raw["characters"]["主角"]["dynamic_history"] == []
    append(manager, 1, "重写的新境界")
    assert manager.state.characters["主角"].current_state.level == "重写的新境界"


def test_saved_override_file_is_never_larger_than_its_read_limit(manager, monkeypatch):
    import core.state_manager as state_module

    monkeypatch.setattr(state_module, "_MAX_MANUAL_OVERRIDE_BYTES", 200)
    before = files(manager)
    with pytest.raises(ValueError, match="上限|大小|limit"):
        edit_level(manager, "一个比较长的人工境界" * 30)
    assert files(manager) == before


def test_repeated_identical_character_save_keeps_revision_and_all_bytes(manager):
    character = manager.state.characters["主角"].model_copy(deep=True)
    before, revision = files(manager), manager.revision

    assert manager.save_manual_character(character, {
        "background": character.background, "level": character.current_state.level,
    }) is None

    assert files(manager) == before
    assert manager.revision == revision


def test_same_chapter_edits_coalesce_and_retired_edits_never_return(manager):
    edit_level(manager, "第一次修改")
    edit_level(manager, "第二次修改")
    history = read_json(manager._manual_overrides_file)["characters"]["主角"]["dynamic_history"]
    assert len(history) == 1
    assert history[0]["updates"]["level"] == "第二次修改"
    append(manager, 2, "第二章旧时间线")
    edit_level(manager, "将被退役的修改")
    assert manager.rollback_to_chapter(1)
    append(manager, 2, "第二章新时间线")
    append(manager, 3, "第三章新时间线")
    append(manager, 2, "重新重放第二章")
    historical, _ = manager.state_before_chapter(3)
    assert historical.characters["主角"].current_state.level == "重新重放第二章"
    assert manager.state.characters["主角"].current_state.level == "第三章新时间线"
    reloaded = StateManager("overrides", base_dir=str(manager.root_dir))
    assert not reloaded.load_warnings


def valid_record():
    return {
        "override_version": 2,
        "characters": {"主角": {
            "static": {"personality_tags": ["谨慎"]}, "undated_dynamic": {},
            "dynamic_history": [{
                "effective_after_chapter": 1, "recorded_at": "2026-08-30T00:00:00+00:00",
                "updates": {"level": "人工境界"},
            }], "retired_dynamic_history": [],
        }},
    }


@pytest.mark.parametrize("malformed", [None, [], {}, {"characters": []},
                                            {"override_version": True, "characters": {}},
                                            {"override_version": 2.0, "characters": {}},
                                            {"override_version": 3, "characters": {}}])
def test_override_document_contract_rejects_malformed_roots(malformed):
    with pytest.raises(ValueError):
        character_overrides.normalize(malformed)


@pytest.mark.parametrize(("field", "value"), [
    ("effective_after_chapter", True), ("effective_after_chapter", -1),
    ("effective_after_chapter", "1"), ("effective_after_chapter", 1.5),
    ("recorded_at", ""), ("recorded_at", "not-a-date"), ("recorded_at", "2026-08-30T00:00:00"),
    ("updates", None), ("updates", {}), ("updates", {"unknown": "text"}),
    ("updates", {"level": 5}), ("updates", {"level": "x" * 501}),
    ("updates", {"level": "has\x00nul"}), ("updates", {"level": "bad\ud800"}),
])
def test_override_event_contract_rejects_invalid_identity_or_values(field, value):
    data = valid_record()
    data["characters"]["主角"]["dynamic_history"][0][field] = value
    with pytest.raises(ValueError):
        character_overrides.normalize(data)


def test_override_normalization_preserves_extra_legacy_evidence_without_aliasing():
    legacy = {"notes": ["保留"], "characters": {"主角": {"level": "未知日期", "custom": ["证据"]}}}
    before = deepcopy(legacy)
    parsed = character_overrides.normalize(legacy)
    assert parsed["characters"]["主角"]["undated_dynamic"] == {"level": "未知日期"}
    assert parsed["characters"]["主角"]["dynamic_history"] == []
    assert parsed["characters"]["主角"]["legacy_unrecognized"] == {"custom": ["证据"]}
    parsed["notes"].append("新记录")
    parsed["characters"]["主角"]["legacy_unrecognized"]["custom"].clear()
    assert legacy == before


def test_override_history_must_be_chronological_and_retirement_must_match_rollback():
    data = valid_record()
    history = data["characters"]["主角"]["dynamic_history"]
    history.append({**deepcopy(history[0]), "effective_after_chapter": 0})
    with pytest.raises(ValueError, match="chronological"):
        character_overrides.normalize(data)
    data = valid_record()
    entry = data["characters"]["主角"]
    entry["retired_dynamic_history"] = [{
        **entry["dynamic_history"].pop(), "retired_at": "2026-08-30T01:00:00+00:00", "rollback_target": 1,
    }]
    with pytest.raises(ValueError, match="rollback target"):
        character_overrides.normalize(data)
