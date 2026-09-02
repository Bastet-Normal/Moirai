"""Shared bounds for newly written chapter text, including replacement growth."""

from core.error_reporting import SafeUserMessage

MAX_CHAPTER_TEXT_CHARS = 2_000_000


class ChapterTextLimitError(SafeUserMessage, ValueError):
    pass


def check_chapter_length(length: int) -> None:
    if length > MAX_CHAPTER_TEXT_CHARS:
        raise ChapterTextLimitError(f"修改后的正文超过 {MAX_CHAPTER_TEXT_CHARS} 字符上限")


def check_replacement_length(text: str, old: str, new: str, *, count: int | None = None) -> None:
    """Check allocation size before constructing the expanded replacement."""
    matches = text.count(old) if count is None else count
    check_chapter_length(len(text) + matches * (len(new) - len(old)))
