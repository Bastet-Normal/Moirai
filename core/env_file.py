"""Atomic updates for the local dotenv configuration file."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from core.storage import atomic_write_text

_ASSIGNMENT = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=")


def _quote(value: object) -> str:
    text = str(value)
    if "\x00" in text or "\r" in text or "\n" in text:
        raise ValueError("配置值不能包含换行或空字符")
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def update_env_file(path: Path, values: Mapping[str, object]) -> None:
    """Update several dotenv keys in one atomic replace.

    Comments, blank lines and unrelated settings are retained. Duplicate target
    assignments are collapsed so a stale definition cannot override the new one.
    """
    path = Path(path)
    pending = {str(key): _quote(value) for key, value in values.items()}
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in pending):
        raise ValueError("配置键名非法")

    if not pending:
        return
    if path.exists():
        with path.open(encoding="utf-8", newline="") as handle:
            original = handle.read()
    else:
        original = ""
    bom = "\ufeff" if original.startswith("\ufeff") else ""
    original = original.removeprefix(bom) if bom else original
    newline = "\r\n" if "\r\n" in original else "\n"
    output: list[str] = []
    written: set[str] = set()
    for line in original.splitlines(keepends=True):
        match = _ASSIGNMENT.match(line)
        key = match.group(1) if match else ""
        if key not in pending:
            output.append(line)
        elif key not in written:
            ending = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
            output.append(f"{key}={pending[key]}{ending}")
            written.add(key)

    additions = [f"{key}={pending[key]}{newline}" for key in pending if key not in written]
    if additions and output and not output[-1].endswith(("\n", "\r")):
        output.append(newline)
    output.extend(additions)
    atomic_write_text(path, bom + "".join(output))
