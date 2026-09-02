import json
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from config.settings import get_settings
from core.project_transactions import JOURNAL_FOLDER, TRASH_FOLDER
from core.project_trash import (
    InvalidTrashEntryError,
    TrashEntryNotFoundError,
    list_trashed_projects,
    restore_trashed_project,
)
from core.schemas import ChapterContent, NovelConcept
from core.state_manager import StateManager
from core.storage import ProjectBusyError, ProjectConflictError, ProjectRecoveryError


def concept(title="可恢复作品"):
    return NovelConcept(title=title, genre="玄幻", logline="测试", golden_finger="无", core_conflict="测试")


def tree(root):
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def trashed(tmp_path):
    manager = StateManager("trash_book", base_dir=str(tmp_path))
    manager.set_concept(concept())
    manager.save_chapter(ChapterContent(
        chapter_number=1, volume_number=1, title="第一章", full_text="原始正文",
    ))
    before = tree(manager.base_dir)
    manager.delete_project()
    return tmp_path, manager.last_trash_path.name, before


def test_list_and_restore_roundtrip_is_byte_exact(trashed):
    root, entry_id, before = trashed
    entries, warnings = list_trashed_projects(root)
    assert warnings == []
    assert entries == [{
        **entries[0],
        "entry_id": entry_id,
        "novel_id": "trash_book",
        "title": "可恢复作品",
        "genre": "玄幻",
        "chapter_count": 1,
        "restorable": True,
        "conflict_reason": "",
    }]
    restored = restore_trashed_project(root, entry_id)
    assert restored["restored"] and restored["novel_id"] == "trash_book"
    assert tree(root / "trash_book") == before
    assert not (root / TRASH_FOLDER / entry_id).exists()
    assert list_trashed_projects(root)[0] == []
    loaded = StateManager("trash_book", base_dir=str(root))
    assert loaded.load_warnings == []
    assert loaded.read_chapter(1).full_text == "原始正文"


def test_restore_never_overwrites_a_new_same_name_project(trashed):
    root, entry_id, _ = trashed
    replacement = StateManager("trash_book", base_dir=str(root))
    replacement.set_concept(concept("新作品"))
    entries, _ = list_trashed_projects(root)
    assert not entries[0]["restorable"]
    with pytest.raises(ProjectConflictError, match="拒绝覆盖"):
        restore_trashed_project(root, entry_id)
    assert (root / TRASH_FOLDER / entry_id).is_dir()
    assert StateManager("trash_book", base_dir=str(root)).state.concept.title == "新作品"


@pytest.mark.parametrize("entry_id", ["../escape", "x", "trash_book-" + "g" * 32, "a\\b"])
def test_restore_rejects_invalid_entry_identifiers(tmp_path, entry_id):
    with pytest.raises((InvalidTrashEntryError, ValueError, TrashEntryNotFoundError)):
        restore_trashed_project(tmp_path, entry_id)
    assert not (tmp_path.parent / "escape").exists()


def test_tampered_state_is_reported_and_never_moved(trashed):
    root, entry_id, _ = trashed
    state_file = root / TRASH_FOLDER / entry_id / "project_state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["novel_id"] = "another"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    entries, warnings = list_trashed_projects(root)
    assert entries == [] and any("状态 ID 不一致" in warning for warning in warnings)
    with pytest.raises(InvalidTrashEntryError, match="状态 ID 不一致"):
        restore_trashed_project(root, entry_id)
    assert (root / TRASH_FOLDER / entry_id).is_dir()


def test_pending_transaction_entry_is_not_offered(trashed):
    root, entry_id, _ = trashed
    transaction_id = entry_id.rsplit("-", 1)[1]
    (root / JOURNAL_FOLDER / transaction_id).mkdir(parents=True)
    entries, _ = list_trashed_projects(root)
    assert entries == []
    with pytest.raises(ProjectBusyError):
        restore_trashed_project(root, entry_id)


def test_unknown_corrupt_journal_blocks_trash_restore_and_preserves_entry(trashed):
    root, entry_id, before = trashed
    journal = root / JOURNAL_FOLDER / ("d" * 32)
    journal.mkdir(parents=True)
    (journal / "header.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(ProjectRecoveryError, match="诊断编号"):
        restore_trashed_project(root, entry_id)

    assert tree(root / TRASH_FOLDER / entry_id) == before
    assert not (root / "trash_book").exists()
    assert journal.exists()


def test_nested_link_is_visible_for_diagnosis_but_refused_on_restore(trashed):
    root, entry_id, _ = trashed
    link = root / TRASH_FOLDER / entry_id / "unsafe-link"
    try:
        os.symlink(root / "outside", link, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not permitted on this host")
    assert list_trashed_projects(root)[0]
    with pytest.raises(InvalidTrashEntryError, match="符号链接|重解析点"):
        restore_trashed_project(root, entry_id)
    assert link.is_symlink()


def test_move_failure_leaves_valid_trash_and_no_half_project(trashed):
    root, entry_id, before = trashed
    with (
        patch("pathlib.Path.rename", side_effect=OSError("simulated rename failure")),
        pytest.raises(OSError, match="simulated"),
    ):
        restore_trashed_project(root, entry_id)
    assert tree(root / TRASH_FOLDER / entry_id) == before
    assert not (root / "trash_book").exists()


def test_trash_web_api_lists_restores_and_reports_conflict(trashed, monkeypatch):
    root, entry_id, _ = trashed
    monkeypatch.setenv("OUTPUT_DIR", str(root))
    get_settings.cache_clear()
    import web_ui

    try:
        with TestClient(web_ui.app) as client:
            listing = client.get("/api/trash")
            assert listing.status_code == 200
            assert listing.json()["entries"][0]["entry_id"] == entry_id
            restored = client.post(f"/api/trash/{entry_id}/restore")
            assert restored.status_code == 200
            assert restored.json()["project"]["novel_id"] == "trash_book"
            assert client.get("/api/novel/trash_book").status_code == 200
            assert client.post(f"/api/trash/{entry_id}/restore").status_code == 404
    finally:
        get_settings.cache_clear()
