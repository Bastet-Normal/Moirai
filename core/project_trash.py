"""Safe discovery and atomic restoration of locally trashed projects."""

from __future__ import annotations

import os
import re
import stat
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.error_reporting import SafeUserMessage
from core.project_ids import require_project_id
from core.project_transactions import JOURNAL_FOLDER, TRASH_FOLDER, recover_all_projects, recover_project
from core.storage import (
    ProjectBusyError,
    ProjectConflictError,
    atomic_write_text,
    project_lock,
    project_revision_path,
    read_json,
)

TRASH_ENTRY = re.compile(r"^(.+)-([0-9a-f]{32})$")
MAX_STATE_BYTES = 32 * 1024 * 1024
MAX_TREE_ENTRIES = 100_000


class InvalidTrashEntryError(SafeUserMessage, ValueError):
    """A trash entry cannot be trusted enough to restore."""


class TrashEntryNotFoundError(SafeUserMessage, FileNotFoundError):
    """The requested trash entry no longer exists."""


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _is_link_or_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _entry_identity(entry_id: str) -> tuple[str, str]:
    if not isinstance(entry_id, str) or Path(entry_id).name != entry_id or any(char in entry_id for char in "/\\:\x00"):
        raise InvalidTrashEntryError("回收条目标识非法")
    match = TRASH_ENTRY.fullmatch(entry_id)
    if not match:
        raise InvalidTrashEntryError("回收条目标识格式错误")
    return require_project_id(match.group(1)), match.group(2)


def _inspect_entry(root: Path, entry_id: str) -> tuple[Path, dict[str, Any]]:
    project_id, transaction_id = _entry_identity(entry_id)
    trash_root = root / TRASH_FOLDER
    try:
        trash_info = trash_root.lstat()
        if not stat.S_ISDIR(trash_info.st_mode) or _is_link_or_reparse(trash_info):
            raise InvalidTrashEntryError("作品回收目录不是可信的普通目录")
        entry = trash_root / entry_id
        entry_info = entry.lstat()
    except FileNotFoundError as exc:
        raise TrashEntryNotFoundError("回收条目已不存在，请刷新列表") from exc
    if not stat.S_ISDIR(entry_info.st_mode) or _is_link_or_reparse(entry_info):
        raise InvalidTrashEntryError("回收条目不是可信的普通目录")
    if entry.resolve().parent != trash_root.resolve():
        raise InvalidTrashEntryError("回收条目路径越界")
    if (root / JOURNAL_FOLDER / transaction_id).exists():
        raise ProjectBusyError("该回收条目仍属于正在提交或恢复的事务，请稍后刷新")

    state_file = entry / "project_state.json"
    try:
        state_info = state_file.lstat()
        if not stat.S_ISREG(state_info.st_mode) or _is_link_or_reparse(state_info):
            raise InvalidTrashEntryError("回收项目状态文件不是普通文件")
        if state_info.st_size > MAX_STATE_BYTES:
            raise InvalidTrashEntryError("回收项目状态文件超过 32 MiB 安全上限")
        state = read_json(state_file, max_bytes=MAX_STATE_BYTES)
    except FileNotFoundError as exc:
        raise InvalidTrashEntryError("回收项目缺少 project_state.json") from exc
    except (OSError, ValueError, TypeError) as exc:
        raise InvalidTrashEntryError("回收项目状态文件损坏") from exc
    if not isinstance(state, dict) or state.get("novel_id") != project_id:
        raise InvalidTrashEntryError("回收目录名称与项目状态 ID 不一致")
    current_chapter = state.get("current_chapter_idx", 0)
    if not isinstance(current_chapter, int) or current_chapter < 0:
        raise InvalidTrashEntryError("回收项目的章节进度字段无效")
    return entry, state


def _validate_tree(entry: Path) -> None:
    """Do not restore links, junctions, devices or unbounded directory trees."""
    pending = [entry]
    count = 0
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as children:
                for child in children:
                    count += 1
                    if count > MAX_TREE_ENTRIES:
                        raise InvalidTrashEntryError("回收项目文件数量超过 100000 个安全上限")
                    info = child.stat(follow_symlinks=False)
                    if _is_link_or_reparse(info):
                        raise InvalidTrashEntryError("回收项目包含符号链接或重解析点，拒绝恢复")
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(Path(child.path))
                    elif not stat.S_ISREG(info.st_mode):
                        raise InvalidTrashEntryError("回收项目包含非常规文件，拒绝恢复")
        except InvalidTrashEntryError:
            raise
        except OSError as exc:
            raise InvalidTrashEntryError("无法完整检查回收项目文件树") from exc


def _public_info(root: Path, entry_id: str, entry: Path, state: dict[str, Any]) -> dict[str, Any]:
    project_id, _ = _entry_identity(entry_id)
    concept = state.get("concept") if isinstance(state.get("concept"), dict) else {}
    destination_exists = (root / project_id).exists()
    modified = datetime.fromtimestamp((entry / "project_state.json").stat().st_mtime, UTC).isoformat()
    return {
        "entry_id": entry_id,
        "novel_id": project_id,
        "title": str(concept.get("title") or project_id),
        "genre": str(concept.get("genre") or ""),
        "chapter_count": max(0, int(state.get("current_chapter_idx") or 0)),
        "last_modified_at": modified,
        "restorable": not destination_exists,
        "conflict_reason": "已有同名作品，请先重命名其中一个" if destination_exists else "",
    }


def list_trashed_projects(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    root = Path(root).resolve()
    warnings = list(recover_all_projects(root))
    trash_root = root / TRASH_FOLDER
    if not trash_root.exists():
        return [], warnings
    try:
        trash_info = trash_root.lstat()
        if not stat.S_ISDIR(trash_info.st_mode) or _is_link_or_reparse(trash_info):
            raise InvalidTrashEntryError("作品回收目录不是可信的普通目录")
    except OSError as exc:
        raise InvalidTrashEntryError("无法读取作品回收目录") from exc

    entries = []
    for candidate in sorted(trash_root.iterdir(), key=lambda path: path.name.casefold()):
        try:
            entry, state = _inspect_entry(root, candidate.name)
            entries.append(_public_info(root, candidate.name, entry, state))
        except ProjectBusyError:
            continue
        except (InvalidTrashEntryError, TrashEntryNotFoundError) as exc:
            # The directory name is untrusted local data and may itself contain
            # prose or credential-like text. The exception is application-owned.
            warnings.append(str(exc))
        except ValueError:
            warnings.append("回收条目包含无法解析的字段")
    return entries, warnings


def restore_trashed_project(root: Path, entry_id: str) -> dict[str, Any]:
    """Atomically move one validated trash directory back under its original ID."""
    root = Path(root).resolve()
    project_id, _ = _entry_identity(entry_id)
    recover_project(root, project_id)
    entry, state = _inspect_entry(root, entry_id)
    destination = root / project_id
    with project_lock(destination, timeout=0), project_lock(destination, scope="snapshot"):
        entry, state = _inspect_entry(root, entry_id)
        if destination.exists():
            raise ProjectConflictError("已有同名作品，拒绝覆盖；请先重命名现有作品")
        _validate_tree(entry)
        result = _public_info(root, entry_id, entry, state)
        # Publish a tombstone first, as StateManager mutations do. Directory
        # rename on the same root is atomic: after a crash, exactly one side exists.
        atomic_write_text(project_revision_path(destination), uuid.uuid4().hex)
        entry.rename(destination)
        _sync_directory(entry.parent)
        _sync_directory(destination.parent)
        with suppress(OSError):
            atomic_write_text(project_revision_path(destination), uuid.uuid4().hex)
    return {**result, "restored": True, "restorable": False, "conflict_reason": ""}
