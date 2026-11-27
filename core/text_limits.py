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
