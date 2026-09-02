"""Privacy-preserving error reporting shared by CLI, Web and workers.

Exception messages can contain provider responses, prompts, credentials or file
contents.  Production boundaries therefore log only exception *structure* and
return a short correlation identifier.  Domain errors explicitly marked as safe
may still expose their bounded, application-authored message.
"""

from __future__ import annotations

import hashlib
import logging
import re
import traceback
import uuid
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

_ERROR_ID = re.compile(r"^[0-9a-f]{12}$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_CHAIN = 8
_MAX_FRAMES = 12


class SafeUserMessage(Exception):
    """Marker mixin for exceptions whose text is authored for end users."""


class UserInputError(SafeUserMessage, ValueError):
    """A bounded application-authored validation or workflow error."""


def bounded_public_text(value: object, *, fallback: str, maximum: int = 1000) -> str:
    """Return valid, single-line, bounded text without terminal controls."""
    text = str(value).replace("\x00", "")
    text = _CONTROL_CHARACTERS.sub(" ", text)
    text = " ".join(text.splitlines()).strip()
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = text.encode("utf-8", "backslashreplace").decode("utf-8")
    if maximum < 1:
        raise ValueError("maximum must be positive")
    if not text:
        text = fallback
    return text if len(text) <= maximum else text[: maximum - 1] + "…"


def safe_user_message(error: BaseException, *, fallback: str) -> str | None:
    """Expose only deliberately marked application messages."""
    if not isinstance(error, SafeUserMessage):
        return None
    return bounded_public_text(error, fallback=fallback)


def _exception_chain(error: BaseException) -> list[str]:
    names: list[str] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(names) < _MAX_CHAIN:
        seen.add(id(current))
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", type(current).__name__)
        names.append(name[:100] or "Exception")
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return names


def _traceback_frames(error: BaseException) -> list[str]:
    frames: list[str] = []
    for frame in traceback.extract_tb(error.__traceback__)[-_MAX_FRAMES:]:
        filename = Path(frame.filename).name
        function = re.sub(r"[^A-Za-z0-9_.<>-]", "_", frame.name)[:100]
        frames.append(f"{filename}:{frame.lineno}:{function}")
    return frames


def _context_text(context: Mapping[str, Any] | None) -> str:
    if not context:
        return "-"
    parts: list[str] = []
    for key in sorted(context)[:12]:
        safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(key))[:40]
        value = context[key]
        if isinstance(value, str):
            encoded = value.encode("utf-8", "backslashreplace")
            safe_value = f"str(len={len(value)},sha256={hashlib.sha256(encoded).hexdigest()[:12]})"
        elif isinstance(value, (int, float, bool, type(None))):
            safe_value = bounded_public_text(value, fallback="-", maximum=120)
        else:
            safe_value = f"type={type(value).__name__[:80]}"
        parts.append(f"{safe_key}={safe_value}")
    return ",".join(parts) or "-"


def report_internal_error(
    logger: logging.Logger,
    error: BaseException,
    *,
    scope: str,
    context: Mapping[str, Any] | None = None,
) -> str:
    """Log message-free exception structure and return a reusable error ID."""
    existing = getattr(error, "_novelengine_error_id", None)
    if isinstance(existing, str) and _ERROR_ID.fullmatch(existing):
        return existing

    error_id = uuid.uuid4().hex[:12]
    with suppress(Exception):
        error._novelengine_error_id = error_id
    safe_scope = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(scope))[:120] or "unknown"
    logger.error(
        "Internal error id=%s scope=%s types=%s frames=%s context=%s",
        error_id,
        safe_scope,
        ">".join(_exception_chain(error)),
        "|".join(_traceback_frames(error)) or "-",
        _context_text(context),
    )
    return error_id


def referenced_failure(message: str, error_id: str) -> str:
    """Attach a valid correlation ID to a fixed user-facing failure message."""
    if not _ERROR_ID.fullmatch(error_id):
        raise ValueError("invalid internal error identifier")
    prefix = bounded_public_text(message, fallback="操作未完成", maximum=800).rstrip("。")
    return f"{prefix}（诊断编号 {error_id}）。"
