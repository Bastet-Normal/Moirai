"""Versioned prose snapshots associated with one chapter rather than the timeline."""

import re
from datetime import UTC, datetime
from pathlib import Path, PurePath

from pydantic import BaseModel, Field

from core.schemas import ChapterContent
from core.storage import ProjectReadOnlyError, atomic_write_text, read_json, remove_file
from core.text_limits import MAX_CHAPTER_TEXT_CHARS

_MAX_VERSIONS = 30
_VERSION_FILE = re.compile(r"([0-9]{8})\.json")
_ARCHIVED_VERSION = re.compile(r"chapter_versions/[0-9]{6,}/(?:[0-9a-f]{32}/)?[0-9]{8}\.json")


class ChapterVersion(BaseModel):
    title: str
    text: str = Field(max_length=MAX_CHAPTER_TEXT_CHARS)


def is_version_record(relative_path: PurePath) -> bool:
    return _ARCHIVED_VERSION.fullmatch(relative_path.as_posix()) is not None


def read_version_record(path: Path) -> ChapterVersion:
    return ChapterVersion.model_validate(read_json(path, max_bytes=16 * 1024 * 1024))


def _version_dir(project_dir: Path, chapter: ChapterContent) -> Path:
    slot = project_dir / "chapter_versions" / f"{chapter.chapter_number:06d}"
    # Pre-identity chapter records keep their original, flat version directory.
    return slot / chapter.chapter_id if chapter.chapter_id else slot


def _version_paths(directory: Path) -> list[Path]:
    return sorted(path for path in directory.glob("*.json")
                  if _VERSION_FILE.fullmatch(path.name))


def list_chapter_versions(project_dir: Path, chapter: ChapterContent) -> list[dict]:
    return [
        {"number": int(path.stem), "saved_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()}
        for path in reversed(_version_paths(_version_dir(project_dir, chapter)))
    ]


def load_chapter_version(project_dir: Path, chapter: ChapterContent, number: int) -> ChapterVersion | None:
    path = _version_dir(project_dir, chapter) / f"{number:08d}.json"
    try:
        return read_version_record(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError) as exc:
        raise ProjectReadOnlyError("这份历史稿无法读取，请检查作品备份。") from exc


def preserve_chapter_version(project_dir: Path, chapter: ChapterContent) -> None:
    directory = _version_dir(project_dir, chapter)
    paths = _version_paths(directory)
    number = int(paths[-1].stem) + 1 if paths else 1
    if number >= 100_000_000:
        raise ValueError("章节版本超出可保存数量")
    path = directory / f"{number:08d}.json"
    atomic_write_text(path, ChapterVersion(title=chapter.title, text=chapter.full_text).model_dump_json())
    for expired in paths[:max(0, len(paths) - _MAX_VERSIONS + 1)]:
        remove_file(expired)
