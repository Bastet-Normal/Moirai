"""Versioned prose snapshots associated with one chapter rather than the timeline."""

import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from core.storage import atomic_write_text, read_json, remove_file
from core.text_limits import MAX_CHAPTER_TEXT_CHARS

_MAX_VERSIONS = 30
_VERSION_FILE = re.compile(r"([0-9]{8})\.json")


class ChapterVersion(BaseModel):
    title: str
    text: str = Field(max_length=MAX_CHAPTER_TEXT_CHARS)


def _version_paths(project_dir: Path, chapter: int) -> list[Path]:
    return sorted(path for path in (project_dir / "chapter_versions" / f"{chapter:06d}").glob("*.json")
                  if _VERSION_FILE.fullmatch(path.name))


def list_chapter_versions(project_dir: Path, chapter: int) -> list[dict]:
    return [
        {"number": int(path.stem), "saved_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()}
        for path in reversed(_version_paths(project_dir, chapter))
    ]


def load_chapter_version(project_dir: Path, chapter: int, number: int) -> ChapterVersion | None:
    path = project_dir / "chapter_versions" / f"{chapter:06d}" / f"{number:08d}.json"
    try:
        return ChapterVersion.model_validate(read_json(path, max_bytes=16 * 1024 * 1024))
    except FileNotFoundError:
        return None


def preserve_chapter_version(project_dir: Path, chapter: int, title: str, text: str) -> None:
    paths = _version_paths(project_dir, chapter)
    number = int(paths[-1].stem) + 1 if paths else 1
    if number >= 100_000_000:
        raise ValueError("章节版本超出可保存数量")
    path = project_dir / "chapter_versions" / f"{chapter:06d}" / f"{number:08d}.json"
    atomic_write_text(path, ChapterVersion(title=title, text=text).model_dump_json())
    for expired in paths[:max(0, len(paths) - _MAX_VERSIONS + 1)]:
        remove_file(expired)
