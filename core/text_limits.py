"""Shared manuscript validation, Han-character counts and paragraph edits."""

import json
import re
from pathlib import Path

from core.error_reporting import SafeUserMessage

MAX_CHAPTER_TEXT_CHARS = 2_000_000


# Pin the editor and persisted counts to one Han repertoire across OS Unicode versions.
def _han_runs() -> re.Pattern[str]:
    repertoire = json.loads(Path(__file__).with_name("han_repertoire.json").read_text("utf-8"))
    ranges = ["-".join(chr(int(point, 16)) for point in item.split("-")) for item in repertoire["ranges"]]
    return re.compile("[" + "".join(ranges) + "]+")


HAN_RUNS = _han_runs()


def count_han_characters(text: str) -> int:
    return sum(match.end() - match.start() for match in HAN_RUNS.finditer(text))


class ChapterTextError(SafeUserMessage, ValueError):
    """An author edit would leave an invalid chapter body."""


class ChapterTextLimitError(ChapterTextError):
    pass


def check_chapter_length(length: int) -> None:
    if length > MAX_CHAPTER_TEXT_CHARS:
        raise ChapterTextLimitError(f"修改后的正文超过 {MAX_CHAPTER_TEXT_CHARS} 字符上限")


def require_chapter_prose(text: str) -> str:
    check_chapter_length(len(text))
    if not text.strip():
        raise ChapterTextError("章节正文不能为空")
    return text


def check_replacement_length(text: str, old: str, new: str, *, count: int | None = None) -> None:
    """Check allocation size before constructing the expanded replacement."""
    matches = text.count(old) if count is None else count
    check_chapter_length(len(text) + matches * (len(new) - len(old)))


def replace_paragraph_at(
    text: str, index: int, replacement: str, *, expected: str | None = None,
) -> str | None:
    """Use the reader's zero-based numbering and retain whitespace outside the selected paragraph."""
    blocks = text.split("\n\n")
    visible = 0
    for slot, block in enumerate(blocks):
        if not block.strip():
            continue
        if visible == index:
            if expected is not None and block != expected:
                return None
            check_replacement_length(text, block, replacement, count=1)
            blocks[slot] = replacement
            return "\n\n".join(blocks)
        visible += 1
    return None
