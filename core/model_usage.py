"""Task-scoped accounting for supplier-reported and estimated model usage."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

Usage = dict[str, dict[str, int]]
_SCOPED_USAGE: ContextVar[Usage | None] = ContextVar("model_usage", default=None)


@contextmanager
def collect_model_usage() -> Iterator[Usage]:
    usage: Usage = {}
    token = _SCOPED_USAGE.set(usage)
    try:
        yield usage
    finally:
        _SCOPED_USAGE.reset(token)


def record_model_usage(model: str, prompt: int, completion: int, *, estimated: bool) -> None:
    usage = _SCOPED_USAGE.get()
    if usage is None:
        return
    values = usage.setdefault(model, {
        "reported_prompt": 0, "reported_completion": 0,
        "estimated_prompt": 0, "estimated_completion": 0,
        "reported_calls": 0, "estimated_calls": 0,
    })
    kind = "estimated" if estimated else "reported"
    values[f"{kind}_calls"] += 1
    values[f"{kind}_prompt"] += prompt
    values[f"{kind}_completion"] += completion


def usage_rows(usage: Usage) -> list[dict[str, Any]]:
    return [{"model": model, **values} for model, values in usage.items()]
