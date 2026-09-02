"""Thread-safe lifecycle state for the local generation worker."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from typing import Any

from core.error_reporting import SafeUserMessage, bounded_public_text


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class TaskAlreadyRunning(SafeUserMessage, RuntimeError):
    pass


class TaskReservationMismatch(RuntimeError):
    pass


class GenerationTaskStatus:
    """Owns the single local worker slot and its UI-visible progress snapshot."""

    def __init__(self, *, max_logs: int = 100, max_log_chars: int = 4000):
        self._lock = threading.RLock()
        self._max_logs = max_logs
        self._max_log_chars = max(100, int(max_log_chars))
        self._state: dict[str, Any] = self._idle_state()

    @staticmethod
    def _idle_state() -> dict[str, Any]:
        return {
            "task_id": "",
            "is_running": False,
            "current_novel": "",
            "current_step": "待命",
            "active_agent": "",
            "progress": 0,
            "logs": [],
            "error": "",
            "started_at": "",
            "finished_at": "",
        }

    def reserve(self, novel_id: str, *, step: str = "任务已排队") -> str:
        """Atomically reserve the worker before a background callback is queued."""
        with self._lock:
            if self._state["is_running"]:
                raise TaskAlreadyRunning("当前有正在生成的任务，请等待完成！")
            task_id = uuid.uuid4().hex
            self._state = self._idle_state()
            self._state.update(
                task_id=task_id,
                is_running=True,
                current_novel=novel_id,
                current_step=bounded_public_text(step, fallback="任务已排队", maximum=1000),
                started_at=_now(),
            )
            return task_id

    def ensure_reserved(self, novel_id: str, *, task_id: str | None = None) -> str:
        """Support direct job invocation while preserving endpoint reservations."""
        with self._lock:
            if self._state["is_running"]:
                if (
                    task_id
                    and self._state["current_novel"] == novel_id
                    and self._state["task_id"] == task_id
                ):
                    return task_id
                raise TaskAlreadyRunning("本地生成槽位已被另一个任务占用")
            if task_id:
                raise TaskReservationMismatch("后台任务预约已失效")
        return self.reserve(novel_id)

    def _check_task(self, task_id: str | None) -> None:
        if task_id is not None and self._state["task_id"] != task_id:
            raise TaskReservationMismatch("后台任务令牌与当前预约不匹配")

    def update(self, *, task_id: str | None = None, **fields: Any) -> None:
        allowed = {"current_step", "active_agent", "progress"}
        unknown = set(fields) - allowed
        if unknown:
            raise KeyError(f"unsupported task status fields: {sorted(unknown)}")
        with self._lock:
            self._check_task(task_id)
            if "progress" in fields:
                fields["progress"] = max(0, min(100, int(fields["progress"])))
            if "current_step" in fields:
                fields["current_step"] = bounded_public_text(
                    fields["current_step"], fallback="任务进行中", maximum=1000,
                )
            if "active_agent" in fields:
                fields["active_agent"] = bounded_public_text(
                    fields["active_agent"], fallback="", maximum=200,
                )
            self._state.update(fields)

    def append_log(self, message: str, *, agent: str = "", task_id: str | None = None) -> None:
        with self._lock:
            self._check_task(task_id)
            self._state["logs"].append(
                bounded_public_text(message, fallback="状态更新", maximum=self._max_log_chars)
            )
            self._state["logs"] = self._state["logs"][-self._max_logs :]
            if agent:
                self._state["active_agent"] = bounded_public_text(
                    agent, fallback="", maximum=200,
                )

    def finish(
        self,
        *,
        step: str,
        error: str = "",
        progress: int | None = None,
        task_id: str | None = None,
    ) -> None:
        with self._lock:
            self._check_task(task_id)
            self._state["is_running"] = False
            self._state["active_agent"] = ""
            self._state["current_step"] = bounded_public_text(step, fallback="任务已结束", maximum=1000)
            self._state["error"] = (
                bounded_public_text(error, fallback="任务未完成", maximum=self._max_log_chars)
                if error else ""
            )
            self._state["finished_at"] = _now()
            if progress is not None:
                self._state["progress"] = max(0, min(100, int(progress)))

    @property
    def is_running(self) -> bool:
        with self._lock:
            return bool(self._state["is_running"])

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            result = dict(self._state)
            result["logs"] = list(self._state["logs"])
            return result


generation_status = GenerationTaskStatus()
