"""Small, dependency-free persistence primitives used by the project store.

Project files are user data.  A process crash must never leave a half-written
JSON document behind, so normal writes go through a temporary sibling followed
by an atomic replace.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

from core.error_reporting import SafeUserMessage

_LOCKS_GUARD = threading.Lock()
_PROJECT_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCK_STATE = threading.local()
_TRANSACTION_STATE = threading.local()
P = ParamSpec("P")
R = TypeVar("R")
MAX_JSON_READ_BYTES = 128 * 1024 * 1024


class ProjectBusyError(SafeUserMessage, RuntimeError):
    """Another thread or process owns the project's operation lock."""


class ProjectConflictError(SafeUserMessage, RuntimeError):
    """The in-memory project no longer matches the version on disk."""


class ProjectReadOnlyError(SafeUserMessage, RuntimeError):
    """A lightweight or incomplete project view must not be persisted."""


class ProjectRecoveryError(ProjectReadOnlyError):
    """A damaged or incomplete journal must be retained for recovery."""


@contextmanager
def file_transaction(transaction) -> Iterator[None]:
    if getattr(_TRANSACTION_STATE, "active", None) is not None:
        raise RuntimeError("nested file transactions must share the outer project transaction")
    _TRANSACTION_STATE.active = transaction
    try:
        yield
    finally:
        _TRANSACTION_STATE.active = None


def _transaction_for(path: Path):
    active = getattr(_TRANSACTION_STATE, "active", None)
    return active if active is not None and active.owns(path) else None


def has_active_transaction(path: Path) -> bool:
    """A same-thread read inside a mutation must not recover its own live journal."""
    return _transaction_for(path) is not None


def ensure_directory(path: Path) -> None:
    path = Path(path)
    transaction = _transaction_for(path)
    if transaction:
        transaction.ensure_directory(path)
    else:
        path.mkdir(parents=True, exist_ok=True)


def remove_file(path: Path, *, missing_ok: bool = True) -> None:
    path = Path(path)
    transaction = _transaction_for(path)
    if transaction:
        transaction.capture_file(path)
    path.unlink(missing_ok=missing_ok)


def _lock_identity(project_dir: Path) -> tuple[Path, str]:
    resolved = Path(project_dir).resolve()
    return resolved, os.path.normcase(str(resolved))


def project_revision_path(project_dir: Path) -> Path:
    """Keep revision tombstones outside the project, surviving delete/rename."""
    resolved, key = _lock_identity(project_dir)
    name = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return resolved.parent / ".novelengine_locks" / f"{name}.revision"


def project_version(project_dir: Path) -> tuple[Any, ...]:
    """Read a cheap optimistic concurrency token, including legacy state writes."""
    revision_file = project_revision_path(project_dir)
    try:
        revision = revision_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        revision = ""
    try:
        state_stat = (Path(project_dir) / "project_state.json").stat()
        state_version = (state_stat.st_mtime_ns, state_stat.st_size, state_stat.st_ino)
    except FileNotFoundError:
        state_version = None
    return revision, state_version


def revision_token(novel_id: str, version: tuple[Any, ...]) -> str:
    """Opaque, project-bound revision for optimistic HTTP write preconditions."""
    serialized = json.dumps([novel_id, version], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write *text* without exposing readers to a partially written file."""
    path = Path(path)
    ensure_directory(path.parent)
    transaction = _transaction_for(path)
    if transaction:
        transaction.capture_file(path)
    temp_dir = transaction.work_dir if transaction else path.parent
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=temp_dir)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: Any, *, indent: int = 2) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=indent))


def append_text_durable(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Append one audit record under the project lock and flush it to disk."""
    path = Path(path)
    ensure_directory(path.parent)
    transaction = _transaction_for(path)
    if transaction:
        transaction.capture_file(path)
    data = text.encode(encoding)
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        remaining = memoryview(data)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("audit log append made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def atomic_output_path(path: Path) -> Iterator[Path]:
    """Yield a temporary sibling and atomically publish it on success."""
    path = Path(path)
    ensure_directory(path.parent)
    transaction = _transaction_for(path)
    if transaction:
        transaction.capture_file(path)
    temp_dir = transaction.work_dir if transaction else path.parent
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=temp_dir)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        yield temp_path
        with temp_path.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Publish exact bytes with the same transaction/durability rules as text writes."""
    if not isinstance(data, bytes):
        raise TypeError("atomic_write_bytes requires bytes")
    with atomic_output_path(path) as temporary:
        temporary.write_bytes(data)


def read_json(path: Path, *, max_bytes: int = MAX_JSON_READ_BYTES) -> Any:
    """Read one bounded regular JSON file without following POSIX symlinks."""
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("JSON 读取上限必须是非负整数")
    path = Path(path)
    path_info = path.lstat()
    path_is_reparse = bool(getattr(path_info, "st_file_attributes", 0) & 0x400)
    if not stat.S_ISREG(path_info.st_mode) or path_is_reparse:
        raise ValueError(f"拒绝读取非普通 JSON 文件: {path.name}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        is_reparse = bool(getattr(info, "st_file_attributes", 0) & 0x400)
        if not stat.S_ISREG(info.st_mode) or is_reparse:
            raise ValueError(f"拒绝读取非普通 JSON 文件: {path.name}")
        if info.st_size > max_bytes:
            raise ValueError(f"JSON 文件超过 {max_bytes} 字节上限: {path.name}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            return json.load(handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


@contextmanager
def project_lock(project_dir: Path, *, timeout: float = 5.0, scope: str = "operation") -> Iterator[None]:
    """Serialize mutations for one project across threads and local processes.

    The lock is re-entrant because high-level operations intentionally compose
    lower-level save methods. The OS lock prevents a simultaneously launched CLI
    and Web process from writing the same project at once.
    """
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("lock timeout must be finite and non-negative")
    if scope not in {"operation", "snapshot"}:
        raise ValueError("unsupported project lock scope")
    resolved, key = _lock_identity(project_dir)
    if scope != "operation":
        key += f":{scope}"
    deadline = time.monotonic() + timeout
    with _LOCKS_GUARD:
        lock = _PROJECT_LOCKS.setdefault(key, threading.RLock())
    if not lock.acquire(timeout=timeout):
        raise ProjectBusyError(f"作品「{resolved.name}」正在被其他任务使用，请稍后重试")
    try:
        counts = getattr(_THREAD_LOCK_STATE, "counts", None)
        if counts is None:
            counts = _THREAD_LOCK_STATE.counts = {}

        if counts.get(key, 0):
            counts[key] += 1
            try:
                yield
            finally:
                counts[key] -= 1
            return

        lock_dir = resolved.parent / ".novelengine_locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_name = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".lock"
        handle = (lock_dir / lock_name).open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ProjectBusyError(
                            f"作品「{resolved.name}」正在被其他进程使用，请稍后重试"
                        ) from exc
                    time.sleep(min(0.05, remaining))
            counts[key] = 1
            try:
                yield
            finally:
                counts.pop(key, None)
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
    finally:
        lock.release()


def project_mutation(method: Callable[P, R]) -> Callable[P, R]:
    """Serialize a StateManager mutation for the lifetime of the whole method."""
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        if not args or not hasattr(args[0], "mutation"):
            raise TypeError("project_mutation requires a project mutation context")
        with args[0].mutation():
            return method(*args, **kwargs)

    return wrapped
