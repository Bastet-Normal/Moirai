"""Shared validation rules for portable Moirai project identifiers."""

from __future__ import annotations

import re

PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]{1,64}$")
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def is_valid_project_id(value: object) -> bool:
    """Return whether *value* is safe as a cross-platform directory name."""
    raw = str(value)
    return (
        raw == raw.strip()
        and bool(PROJECT_ID_PATTERN.fullmatch(raw))
        and raw.upper() not in WINDOWS_RESERVED_NAMES
    )


def require_project_id(value: object) -> str:
    """Return a validated project ID or raise a user-facing ``ValueError``."""
    raw = str(value)
    if not is_valid_project_id(raw):
        raise ValueError("项目名非法（1~64位中文/字母/数字/下划线/连字符，且不能使用系统保留名）")
    return raw
