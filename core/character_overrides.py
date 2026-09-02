"""Pure, bounded contracts for legacy and dated character-editor records.

This module has no project, filesystem or model-provider side effects. Unknown
legacy metadata is retained; active fields and timeline identities fail closed.
"""

from copy import deepcopy
from datetime import datetime
from typing import Any

STATIC_FIELDS = frozenset({"role", "background", "personality_tags"})
DYNAMIC_FIELDS = frozenset({"level", "location", "hp_status", "current_mood"})
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_HISTORY_EVENTS = 4096

_TEXT_LIMITS = {
    "role": 500, "background": 20_000, "level": 500,
    "location": 500, "hp_status": 1000, "current_mood": 1000,
}


def _text(value: Any, limit: int, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ValueError("invalid or oversized character override text")
    if nonempty and not value.strip():
        raise ValueError("empty character override text")
    value.encode("utf-8")
    return value


def validate_values(values: Any, allowed: frozenset[str]) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ValueError("character override values must be an object")
    result: dict[str, Any] = {}
    for field, value in values.items():
        if field not in allowed:
            raise ValueError(f"unsupported character override field: {field}")
        if field == "personality_tags":
            if not isinstance(value, list) or len(value) > 50:
                raise ValueError("personality_tags override must be a bounded string list")
            result[field] = [_text(item, 200, nonempty=True) for item in value]
        else:
            result[field] = _text(value, _TEXT_LIMITS[field])
    return result


def _chapter(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("invalid dynamic override chapter")
    return value


def _timestamp(value: Any) -> str:
    text = _text(value, 64, nonempty=True)
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("dynamic override timestamp must include timezone")
    return text


def _history(raw: Any, *, retired: bool) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > MAX_HISTORY_EVENTS:
        raise ValueError("invalid dynamic override history")
    result = []
    previous_chapter = -1
    for value in raw:
        if not isinstance(value, dict):
            raise ValueError("dynamic override event must be an object")
        event = deepcopy(value)
        chapter = _chapter(value.get("effective_after_chapter"))
        if not retired and chapter < previous_chapter:
            raise ValueError("dynamic override history is not chronological")
        previous_chapter = chapter
        event["effective_after_chapter"] = chapter
        event["recorded_at"] = _timestamp(value.get("recorded_at"))
        event["updates"] = validate_values(value.get("updates"), DYNAMIC_FIELDS)
        if not event["updates"]:
            raise ValueError("empty dynamic override event")
        if retired:
            event["retired_at"] = _timestamp(value.get("retired_at"))
            event["rollback_target"] = _chapter(value.get("rollback_target"))
            if event["rollback_target"] >= chapter:
                raise ValueError("retired override does not follow its rollback target")
        result.append(event)
    return result


def normalize(raw: Any) -> dict[str, Any]:
    """Return a detached v2 record without inventing dates for legacy edits."""
    if not isinstance(raw, dict):
        raise ValueError("manual override root must be an object")
    version = raw.get("override_version", 1)
    if type(version) is not int or version not in {1, 2}:
        raise ValueError("unsupported manual override version")
    characters = raw.get("characters")
    if not isinstance(characters, dict):
        raise ValueError("manual override characters must be an object")
    normalized = {**deepcopy(raw), "override_version": 2, "characters": {}}
    for name, payload in characters.items():
        _text(name, 200, nonempty=True)
        if not isinstance(payload, dict):
            raise ValueError("character override must be an object")
        if version == 1:
            entry = {
                "static": validate_values({k: v for k, v in payload.items() if k in STATIC_FIELDS}, STATIC_FIELDS),
                "undated_dynamic": validate_values(
                    {k: v for k, v in payload.items() if k in DYNAMIC_FIELDS}, DYNAMIC_FIELDS,
                ),
                "dynamic_history": [], "retired_dynamic_history": [],
            }
            extras = {k: deepcopy(v) for k, v in payload.items() if k not in STATIC_FIELDS | DYNAMIC_FIELDS}
            if extras:
                entry["legacy_unrecognized"] = extras
        else:
            entry = {
                **deepcopy(payload),
                "static": validate_values(payload.get("static", {}), STATIC_FIELDS),
                "undated_dynamic": validate_values(payload.get("undated_dynamic", {}), DYNAMIC_FIELDS),
                "dynamic_history": _history(payload.get("dynamic_history", []), retired=False),
                "retired_dynamic_history": _history(payload.get("retired_dynamic_history", []), retired=True),
            }
        normalized["characters"][name] = entry
    return normalized
