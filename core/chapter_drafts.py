"""Durable chapter candidates kept beside the manuscript they were based on."""

import re
from difflib import SequenceMatcher
from itertools import zip_longest
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, StringConstraints

from core.schemas import ChapterContent, StateUpdateDelta
from core.storage import atomic_write_bytes, read_json
from core.text_limits import check_chapter_length

REWRITE_FILENAME = ".moirai-rewrite.json"
_MAX_REWRITE_BYTES = 64 * 1024 * 1024


class ChapterDraft(BaseModel):
    content: ChapterContent
    state_delta: StateUpdateDelta
    state_extraction_failed: bool


class ChapterRewrite(BaseModel):
    format_version: Literal[1] = 1
    task_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    novel_id: str
    expected_revision: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    draft: ChapterDraft


def read_chapter_rewrite(project_dir: Path, novel_id: str) -> ChapterRewrite | None:
    try:
        payload = read_json(project_dir / REWRITE_FILENAME, max_bytes=_MAX_REWRITE_BYTES)
    except FileNotFoundError:
        return None
    rewrite = ChapterRewrite.model_validate(payload)
    if rewrite.novel_id != novel_id:
        raise ValueError("重写稿所属作品与当前作品不符")
    return rewrite


def store_chapter_rewrite(project_dir: Path, rewrite: ChapterRewrite) -> None:
    check_chapter_length(len(rewrite.draft.content.full_text))
    data = rewrite.model_dump_json().encode("utf-8")
    if len(data) > _MAX_REWRITE_BYTES:
        raise ValueError("重写稿超过保存上限")
    atomic_write_bytes(project_dir / REWRITE_FILENAME, data)


def chapter_diff(original: str, candidate: str) -> list[tuple[str, str, str]]:
    """Align paragraph-sized blocks while keeping every original line break."""
    def paragraphs(text: str) -> list[str]:
        blocks = re.findall(r"[^\n]*(?:\n+|$)", text)
        return [block for block in blocks if block]

    old, new = paragraphs(original), paragraphs(candidate)
    if len(old) + len(new) > 4_000:
        return [("replace", original, candidate)]
    matcher = SequenceMatcher(None, old, new, autojunk=True)
    hunks = []
    for kind, i, j, a, b in matcher.get_opcodes():
        if kind == "equal":
            hunks.append((kind, "".join(old[i:j]), "".join(new[a:b])))
        else:
            hunks.extend((kind, before, after) for before, after in zip_longest(old[i:j], new[a:b], fillvalue=""))
    return hunks


def apply_chapter_hunks(original: str, candidate: str, selected: list[int]) -> str:
    hunks = chapter_diff(original, candidate)
    changes = {index for index, (kind, _, _) in enumerate(hunks) if kind != "equal"}
    if not selected or len(set(selected)) != len(selected) or not set(selected) <= changes:
        raise ValueError("选择的改稿段落已变动，请重新打开候选稿")
    choices = set(selected)
    return "".join(after if index in choices else before for index, (_, before, after) in enumerate(hunks))
