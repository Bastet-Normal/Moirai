"""Bounded, durable undo journals for multi-file project mutations.

Only the first change to a file copies its previous bytes. A log record is flushed
before the original is touched; a flushed commit marker decides whether recovery
keeps the new state or restores the old one. Recovery itself is checkpointed and
idempotent. All callers must hold operation + snapshot locks while changing files.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import uuid
from contextlib import ExitStack
from pathlib import Path, PurePosixPath

from core.error_reporting import report_internal_error
from core.project_ids import require_project_id
from core.storage import (
    ProjectBusyError,
    ProjectRecoveryError,
    append_text_durable,
    atomic_write_json,
    atomic_write_text,
    has_active_transaction,
    project_lock,
    project_revision_path,
    read_json,
)

logger = logging.getLogger("novelengine.transactions")
JOURNAL_FOLDER = ".novelengine_transactions"
TRASH_FOLDER = ".novelengine_trash"
MAX_RECORDS = 50_000
MAX_LOG_BYTES = 32 * 1024 * 1024
MAX_BACKUP_BYTES = 2 * 1024 * 1024 * 1024
JOURNAL_ID = re.compile(r"^[0-9a-f]{32}$")


def _sync_directory(path: Path) -> None:
    # Windows fsync covers files; POSIX also permits flushing directory entries.
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _safe_path(root: Path, relative: str) -> Path:
    """Validate journal paths before recovery can overwrite, move or remove data."""
    if not isinstance(relative, str) or "\\" in relative or ":" in relative or "\x00" in relative:
        raise ProjectRecoveryError("事务日志包含非法路径，已停止自动恢复")
    parts = relative.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts) or PurePosixPath(relative).is_absolute():
        raise ProjectRecoveryError("事务日志路径越界，已停止自动恢复")
    candidate = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ProjectRecoveryError("项目或事务路径包含符号链接/重解析点，拒绝修改")
    if not candidate.resolve().is_relative_to(root):
        raise ProjectRecoveryError("事务路径越界")
    return candidate


def _copy_durable(source: Path, target: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as reader, target.open("xb") as writer:
        while chunk := reader.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_BACKUP_BYTES:
                raise ProjectRecoveryError("单个事务备份超过 2GB，已停止写入")
            writer.write(chunk)
            digest.update(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    return size, digest.hexdigest()


class ProjectTransaction:
    def __init__(self, root: Path, project_id: str):
        self.root = Path(root).resolve()
        self.project_id = require_project_id(project_id)
        self.transaction_id = uuid.uuid4().hex
        self.participants = [self.project_id]
        parent = _safe_path(self.root, JOURNAL_FOLDER)
        parent.mkdir(parents=True, exist_ok=True)
        self.directory = parent / self.transaction_id
        self.directory.mkdir()
        self.work_dir = self.directory / "work"
        self.work_dir.mkdir()
        self._seen: set[str] = set()
        self._backup_bytes = 0
        self._record_count = 0
        self._write_header()
        _sync_directory(parent)

    @property
    def log_file(self) -> Path:
        return self.directory / "undo.jsonl"

    def _write_header(self) -> None:
        atomic_write_json(self.directory / "header.json", {
            "version": 1,
            "transaction_id": self.transaction_id,
            "project_id": self.project_id,
            "participants": self.participants,
        })
        _sync_directory(self.directory)

    def owns(self, path: Path) -> bool:
        try:
            parts = Path(path).absolute().relative_to(self.root).parts
        except ValueError:
            return False
        return bool(parts) and os.path.normcase(parts[0]) in {os.path.normcase(name) for name in self.participants}

    def _relative(self, path: Path) -> str:
        relative = Path(path).absolute().relative_to(self.root).as_posix()
        _safe_path(self.root, relative)
        if not self.owns(path):
            raise ProjectRecoveryError("事务只能修改已经锁定的作品")
        return relative

    def _record(self, entry: dict) -> None:
        self._record_count += 1
        if self._record_count > MAX_RECORDS:
            raise ProjectRecoveryError("事务文件数量超过安全上限")
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        current_size = self.log_file.stat().st_size if self.log_file.exists() else 0
        if current_size + len(line.encode("utf-8")) > MAX_LOG_BYTES:
            raise ProjectRecoveryError("事务日志超过安全上限")
        append_text_durable(self.log_file, line)
        _sync_directory(self.directory)

    def ensure_directory(self, path: Path) -> None:
        relative = self._relative(path)
        target = _safe_path(self.root, relative)
        if target.is_dir():
            return
        if target.exists():
            raise ProjectRecoveryError("目录被同名文件占用")
        if target.parent != self.root:
            self.ensure_directory(target.parent)
        self._record({"kind": "mkdir", "path": relative})
        target.mkdir()
        _sync_directory(target.parent)

    def capture_file(self, path: Path) -> None:
        relative = self._relative(path)
        key = os.path.normcase(relative)
        if key in self._seen:
            return
        target = _safe_path(self.root, relative)
        entry = {"kind": "file", "path": relative, "backup": None}
        if target.exists():
            if not target.is_file():
                raise ProjectRecoveryError("事务文件路径不是普通文件")
            # Windows accepts case aliases but replace can change the filename's
            # spelling. Undo must restore the original name as well as its bytes.
            entry["path"] = target.resolve().relative_to(self.root).as_posix()
            if self._backup_bytes + target.stat().st_size > MAX_BACKUP_BYTES:
                raise ProjectRecoveryError("事务备份总量超过 2GB，已停止写入")
            backup_name = uuid.uuid4().hex + ".bak"
            size, digest = _copy_durable(target, self.directory / backup_name)
            self._backup_bytes += size
            if self._backup_bytes > MAX_BACKUP_BYTES:
                raise ProjectRecoveryError("事务备份总量超过 2GB，已停止写入")
            entry.update(backup=backup_name, size=size, sha256=digest)
        self._record(entry)
        self._seen.add(key)

    def rename_project(self, source: Path, destination: Path) -> None:
        new_id = require_project_id(destination.name)
        if destination.parent.resolve() != self.root:
            raise ProjectRecoveryError("重命名目标不在作品根目录")
        if new_id not in self.participants:
            if len(self.participants) >= 2:
                raise ProjectRecoveryError("单次事务最多涉及两个项目名，请分次重命名")
            self.participants.append(new_id)
            self._write_header()
        self._move(source, destination)

    def trash_project(self, source: Path) -> Path:
        trash_root = _safe_path(self.root, TRASH_FOLDER)
        trash_root.mkdir(exist_ok=True)
        destination = trash_root / f"{self.project_id}-{self.transaction_id}"
        self._move(source, destination, trash=True)
        return destination

    def _move(self, source: Path, destination: Path, *, trash: bool = False) -> None:
        src = self._relative(source)
        dst = destination.relative_to(self.root).as_posix()
        _safe_path(self.root, dst)
        if destination.exists() or not source.is_dir():
            raise ProjectRecoveryError("目录移动前置条件不满足，拒绝覆盖")
        self._record({"kind": "move", "path": src, "destination": dst, "trash": trash})
        source.rename(destination)
        _sync_directory(source.parent)
        _sync_directory(destination.parent)

    def commit(self) -> None:
        # Flush publication directories before the commit decision on POSIX.
        for record in self._validated_records():
            path = _safe_path(self.root, record["path"])
            if path.parent.is_dir():
                _sync_directory(path.parent)
        try:
            atomic_write_text(self.directory / "COMMITTED", self.transaction_id)
            _sync_directory(self.directory)
        except OSError:
            if not self.committed():
                raise
        self._cleanup_after_commit()

    def committed(self) -> bool:
        marker = _safe_path(self.directory, "COMMITTED")
        try:
            info = marker.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ProjectRecoveryError("事务提交标记无法安全读取，保留日志等待检查") from exc
        try:
            if (
                not stat.S_ISREG(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
                or info.st_size > 128
            ):
                raise ValueError("invalid commit marker")
            marker_value = marker.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError) as exc:
            raise ProjectRecoveryError("事务提交标记损坏，保留日志等待检查") from exc
        if marker_value != self.transaction_id:
            raise ProjectRecoveryError("事务提交标记损坏，保留日志等待检查")
        return True

    def _cleanup_after_commit(self) -> None:
        try:
            self._cleanup()
        except OSError:
            logger.warning("Committed project journal cleanup deferred: %s", self.transaction_id)

    def _validated_records(self) -> list[dict]:
        _safe_path(self.directory, "undo.jsonl")
        try:
            info = self.log_file.lstat()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise ProjectRecoveryError("事务日志无法安全读取，保留现场") from exc
        try:
            if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("journal is not a regular file")
            if info.st_size > MAX_LOG_BYTES:
                raise ProjectRecoveryError("事务日志过大")
            lines = self.log_file.read_bytes().splitlines(keepends=True)
        except ProjectRecoveryError:
            raise
        except (OSError, ValueError) as exc:
            raise ProjectRecoveryError("事务日志无法安全读取，保留现场") from exc
        records = []
        backup_bytes = 0
        for index, line in enumerate(lines):
            # A torn final append cannot have been followed by its mutation.
            if not line.endswith(b"\n") and index == len(lines) - 1:
                break
            try:
                entry = json.loads(line)
                kind, relative = entry["kind"], entry["path"]
                path = _safe_path(self.root, relative)
                if os.path.normcase(path.relative_to(self.root).parts[0]) not in {os.path.normcase(name) for name in self.participants}:
                    raise ValueError("unlocked participant")
                if kind == "file" and ("backup" not in entry or len(path.relative_to(self.root).parts) < 2):
                    raise ValueError("invalid file record")
                if kind == "file" and entry.get("backup") is not None:
                    backup = entry["backup"]
                    if not re.fullmatch(r"[0-9a-f]{32}\.bak", backup):
                        raise ValueError("unsafe backup filename")
                    backup_path = _safe_path(self.directory, backup)
                    size = backup_path.stat().st_size
                    backup_bytes += size
                    if backup_bytes > MAX_BACKUP_BYTES or size != entry["size"]:
                        raise ValueError("backup size mismatch")
                    with backup_path.open("rb") as source:
                        digest = hashlib.file_digest(source, "sha256").hexdigest()
                    if digest != entry["sha256"]:
                        raise ValueError("backup checksum mismatch")
                elif kind == "move":
                    destination = _safe_path(self.root, entry["destination"])
                    if len(path.relative_to(self.root).parts) != 1:
                        raise ValueError("only whole-project directory moves are allowed")
                    if entry.get("trash"):
                        expected = self.root / TRASH_FOLDER / f"{self.project_id}-{self.transaction_id}"
                        if destination != expected:
                            raise ValueError("invalid trash destination")
                    elif destination.parent != self.root or destination.name not in self.participants:
                        raise ValueError("invalid rename destination")
                elif kind not in {"mkdir", "file"}:
                    raise ValueError("unsupported undo action")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ProjectRecoveryError("事务日志或备份损坏，已停止自动恢复并保留现场") from exc
            records.append(entry)
            if len(records) > MAX_RECORDS:
                raise ProjectRecoveryError("事务记录数量过多")
        return records

    def _rollback_completed(self, record_count: int) -> int:
        progress_file = _safe_path(self.directory, "rollback_progress.json")
        try:
            progress_file.lstat()
        except FileNotFoundError:
            completed = 0
        except OSError as exc:
            raise ProjectRecoveryError("事务恢复进度无法安全读取，保留现场") from exc
        else:
            try:
                completed = read_json(progress_file, max_bytes=1024).get("completed", 0)
            except (OSError, ValueError, AttributeError) as exc:
                raise ProjectRecoveryError("事务恢复进度损坏，保留现场") from exc
        if not isinstance(completed, int) or isinstance(completed, bool) or not 0 <= completed <= record_count:
            raise ProjectRecoveryError("事务恢复进度损坏")
        return completed

    def rollback(self) -> None:
        if self.committed():
            self._cleanup_after_commit()
            return
        records = self._validated_records()  # Validate everything before touching originals.
        progress_file = _safe_path(self.directory, "rollback_progress.json")
        completed = self._rollback_completed(len(records))
        for index, entry in enumerate(reversed(records), start=1):
            if index <= completed:
                continue
            self._undo(entry)
            atomic_write_json(progress_file, {"completed": index})
            _sync_directory(self.directory)
        for name in self.participants:
            atomic_write_text(project_revision_path(self.root / name), uuid.uuid4().hex)
        self._cleanup()

    def _undo(self, entry: dict) -> None:
        target = _safe_path(self.root, entry["path"])
        if entry["kind"] == "file":
            if entry.get("backup") is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                _safe_path(self.directory, "work")
                fd, temporary_name = tempfile.mkstemp(dir=self.work_dir, suffix=".restore")
                os.close(fd)
                temporary = Path(temporary_name)
                temporary.unlink()
                try:
                    _copy_durable(self.directory / entry["backup"], temporary)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
        elif entry["kind"] == "mkdir":
            if target.exists():
                target.rmdir()  # Never recursively remove unexpected user data.
        elif entry["kind"] == "move":
            destination = _safe_path(self.root, entry["destination"])
            if destination.exists():
                if target.exists():
                    raise ProjectRecoveryError("恢复目录移动时两端均存在，拒绝覆盖")
                destination.rename(target)
                _sync_directory(destination.parent)
            elif not target.exists():
                raise ProjectRecoveryError("恢复目录移动时两端均缺失")
        if target.parent.exists():
            _sync_directory(target.parent)

    def _cleanup(self) -> None:
        expected = self.root / JOURNAL_FOLDER / self.transaction_id
        if self.directory != expected or not JOURNAL_ID.fullmatch(self.transaction_id):
            raise ProjectRecoveryError("拒绝清理非事务目录")
        _safe_path(self.root, self.directory.relative_to(self.root).as_posix())
        # This exact private directory was created by this journal, never a project.
        shutil.rmtree(self.directory)
        _sync_directory(self.directory.parent)

    @classmethod
    def open(cls, root: Path, directory: Path) -> ProjectTransaction:
        root = root.resolve()
        _safe_path(root, directory.relative_to(root).as_posix())
        try:
            header = read_json(_safe_path(directory, "header.json"), max_bytes=64 * 1024)
            identifier = header["transaction_id"]
            project_id = require_project_id(header["project_id"])
            participants = [require_project_id(name) for name in header["participants"]]
            if header["version"] != 1 or not JOURNAL_ID.fullmatch(identifier):
                raise ValueError("unsupported journal")
            if directory != root / JOURNAL_FOLDER / identifier or project_id not in participants or not 1 <= len(participants) <= 2:
                raise ValueError("invalid participants")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ProjectRecoveryError("事务头损坏，无法安全确定待恢复作品") from exc
        instance = cls.__new__(cls)
        instance.root, instance.directory = root, directory
        instance.project_id, instance.transaction_id = project_id, identifier
        instance.participants = participants
        instance.work_dir = directory / "work"
        return instance


def _diagnostic(
    *,
    journal_id: str,
    code: str,
    severity: str,
    message: str,
    project_ids: list[str] | None = None,
    blocks_all_projects: bool = False,
) -> dict:
    """Return a path-free diagnostic safe to expose through the loopback API."""
    return {
        "journal_id": journal_id,
        "code": code,
        "severity": severity,
        "message": message,
        "project_ids": list(project_ids or []),
        "blocks_all_projects": blocks_all_projects,
        "preserved": True,
    }


def _journal_label(name: str) -> str:
    if JOURNAL_ID.fullmatch(name):
        return name
    digest = hashlib.sha256(name.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return f"unknown-{digest}"


def _pristine_incomplete_journal(directory: Path) -> bool:
    """A crash before publishing header.json cannot have touched project files."""
    try:
        entries = list(directory.iterdir())
        if not entries:
            return True
        if len(entries) != 1 or entries[0].name != "work":
            return False
        info = entries[0].lstat()
        return (
            stat.S_ISDIR(info.st_mode)
            and not getattr(info, "st_file_attributes", 0) & 0x400
            and not any(entries[0].iterdir())
        )
    except OSError:
        return False


def _scan_recovery_journals(root: Path) -> tuple[list[ProjectTransaction], list[dict], set[str], bool]:
    """Inspect every private journal independently without changing user files."""
    root = Path(root).resolve()
    parent = root / JOURNAL_FOLDER
    try:
        parent.lstat()
    except FileNotFoundError:
        return [], [], set(), False
    except OSError as exc:
        report_internal_error(logger, exc, scope="recovery_root_inspection")
        return [], [_diagnostic(
            journal_id="transaction-root",
            code="invalid_transaction_root",
            severity="error",
            message="事务根目录无法安全读取；已停止全部自动恢复",
            blocks_all_projects=True,
        )], set(), True
    diagnostics: list[dict] = []
    transactions: list[ProjectTransaction] = []
    blocked_projects: set[str] = set()
    blocks_all = False
    try:
        parent = _safe_path(root, JOURNAL_FOLDER)
        parent_info = parent.lstat()
        if not stat.S_ISDIR(parent_info.st_mode):
            raise ProjectRecoveryError("事务根路径不是目录")
        directories = sorted(parent.iterdir(), key=lambda item: item.name)
    except (OSError, ProjectRecoveryError) as exc:
        diagnostics.append(_diagnostic(
            journal_id="transaction-root",
            code="invalid_transaction_root",
            severity="error",
            message="事务根目录无法安全读取；已停止全部自动恢复",
            blocks_all_projects=True,
        ))
        report_internal_error(logger, exc, scope="recovery_root_read")
        return [], diagnostics, set(), True

    for directory in directories:
        journal_id = _journal_label(directory.name)
        if not JOURNAL_ID.fullmatch(directory.name):
            diagnostics.append(_diagnostic(
                journal_id=journal_id,
                code="unexpected_transaction_entry",
                severity="error",
                message="事务目录包含无法识别的条目；已停止全部自动恢复",
                blocks_all_projects=True,
            ))
            blocks_all = True
            continue
        try:
            checked = _safe_path(root, f"{JOURNAL_FOLDER}/{directory.name}")
            info = checked.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise ProjectRecoveryError("事务条目不是普通目录")
        except FileNotFoundError:
            continue  # A concurrent committed transaction finished cleanup.
        except (OSError, ProjectRecoveryError) as exc:
            diagnostics.append(_diagnostic(
                journal_id=journal_id,
                code="invalid_transaction_entry",
                severity="error",
                message="事务条目类型或路径异常；已停止全部自动恢复",
                blocks_all_projects=True,
            ))
            report_internal_error(
                logger,
                exc,
                scope="recovery_entry_inspection",
                context={"journal_id": journal_id},
            )
            blocks_all = True
            continue

        header_path = checked / "header.json"
        if not header_path.exists():
            pristine = _pristine_incomplete_journal(checked)
            diagnostics.append(_diagnostic(
                journal_id=journal_id,
                code="incomplete_transaction_initialization" if pristine else "missing_transaction_header",
                severity="warning" if pristine else "error",
                message=(
                    "事务初始化在写入头文件前中断，未发现项目修改记录；现场已保留"
                    if pristine else "事务头缺失且目录包含其他数据；已停止全部自动恢复"
                ),
                blocks_all_projects=not pristine,
            ))
            blocks_all = blocks_all or not pristine
            continue

        try:
            transaction = ProjectTransaction.open(root, checked)
        except ProjectRecoveryError as exc:
            try:
                checked.lstat()
            except FileNotFoundError:
                continue  # A concurrent committed transaction finished cleanup.
            except OSError:
                pass
            diagnostics.append(_diagnostic(
                journal_id=journal_id,
                code="corrupt_transaction_header",
                severity="error",
                message=str(exc),
                blocks_all_projects=True,
            ))
            report_internal_error(
                logger,
                exc,
                scope="recovery_header",
                context={"journal_id": journal_id},
            )
            blocks_all = True
            continue

        try:
            if transaction.committed():
                code = "committed_cleanup_pending"
                message = "事务已提交，正在等待安全清理残留日志"
            else:
                records = transaction._validated_records()
                transaction._rollback_completed(len(records))
                code = "rollback_pending"
                message = "检测到未完成事务，等待自动回滚"
        except ProjectRecoveryError as exc:
            blocked_projects.update(transaction.participants)
            diagnostics.append(_diagnostic(
                journal_id=journal_id,
                code="corrupt_transaction_journal",
                severity="error",
                message=str(exc),
                project_ids=transaction.participants,
            ))
            continue
        transactions.append(transaction)
        diagnostics.append(_diagnostic(
            journal_id=journal_id,
            code=code,
            severity="warning",
            message=message,
            project_ids=transaction.participants,
        ))
    return transactions, diagnostics, blocked_projects, blocks_all


def inspect_recovery_journals(root: Path) -> list[dict]:
    """Return sanitized, read-only journal diagnostics for UI and support."""
    return _scan_recovery_journals(root)[1]


def _recover_transaction(root: Path, candidate: ProjectTransaction) -> None:
    try:
        with ExitStack() as stack:
            for name in sorted(candidate.participants):
                stack.enter_context(project_lock(root / name, timeout=0))
            for name in sorted(candidate.participants):
                stack.enter_context(project_lock(root / name, scope="snapshot"))
            try:
                candidate.directory.lstat()
            except FileNotFoundError:
                return
            fresh = ProjectTransaction.open(root, candidate.directory)
            if fresh.participants != candidate.participants:
                raise ProjectBusyError("事务参与作品刚发生变化，请重试读取")
            fresh.rollback()
    except (ProjectBusyError, ProjectRecoveryError):
        raise
    except OSError as exc:
        raise ProjectRecoveryError("事务恢复遇到本机文件系统错误；现场已保留，请关闭其他程序后重试") from exc


def recover_all_projects_detailed(root: Path) -> list[dict]:
    """Recover what is provably safe, then report every preserved remainder."""
    root = Path(root).resolve()
    transactions, diagnostics, blocked_projects, blocks_all = _scan_recovery_journals(root)
    if blocks_all:
        return diagnostics

    outcome_errors: dict[str, tuple[str, str]] = {}
    for transaction in transactions:
        if blocked_projects.intersection(transaction.participants):
            continue
        try:
            _recover_transaction(root, transaction)
        except ProjectBusyError:
            outcome_errors[transaction.transaction_id] = (
                "recovery_busy", "相关作品正被其他进程使用；关闭其他创作窗口后刷新重试"
            )
        except ProjectRecoveryError as exc:
            blocked_projects.update(transaction.participants)
            outcome_errors[transaction.transaction_id] = ("recovery_blocked", str(exc))

    remaining = inspect_recovery_journals(root)
    for diagnostic in remaining:
        override = outcome_errors.get(diagnostic["journal_id"])
        if override:
            diagnostic["code"], diagnostic["message"] = override
            diagnostic["severity"] = "error" if override[0] == "recovery_blocked" else "warning"
    return remaining


def recover_project(root: Path, project_id: str) -> None:
    """Recover only abandoned transactions; never roll back a live writer."""
    root = Path(root).resolve()
    project_id = require_project_id(project_id)
    if has_active_transaction(root / project_id):
        return
    try:
        (root / JOURNAL_FOLDER).lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ProjectRecoveryError("事务根目录无法安全检查，已停止写入") from exc
    with project_lock(root / project_id, scope="snapshot"):
        transactions, diagnostics, blocked_projects, blocks_all = _scan_recovery_journals(root)
        if blocks_all:
            item = next(diagnostic for diagnostic in diagnostics if diagnostic["blocks_all_projects"])
            raise ProjectRecoveryError(f"{item['message']}（诊断编号 {item['journal_id']}）")
        normalized_project = os.path.normcase(project_id)
        if normalized_project in {os.path.normcase(name) for name in blocked_projects}:
            item = next(
                diagnostic for diagnostic in diagnostics
                if normalized_project in {os.path.normcase(name) for name in diagnostic["project_ids"]}
                and diagnostic["severity"] == "error"
            )
            raise ProjectRecoveryError(f"{item['message']}（诊断编号 {item['journal_id']}）")
        candidates = [
            item for item in transactions
            if normalized_project in {os.path.normcase(name) for name in item.participants}
        ]
    for candidate in candidates:
        _recover_transaction(root, candidate)


def recover_all_projects(root: Path) -> list[str]:
    """List endpoints recover abandoned moves even when the old name is absent."""
    warnings = []
    for item in recover_all_projects_detailed(root):
        projects = ", ".join(item["project_ids"]) or "未知作品"
        warnings.append(f"{projects}: {item['message']}（诊断编号 {item['journal_id']}）")
    return warnings
