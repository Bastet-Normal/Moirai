"""Read canonical chapter shards and pre-sharding manuscripts."""

import re
from collections.abc import Callable, Iterator
from pathlib import Path

from core.schemas import ChapterContent
from core.storage import ProjectReadOnlyError, read_json

_CHAPTER_FILE = re.compile(r"chapter_([0-9]+)\.json")


def read_chapter_file(path: Path, number: int) -> ChapterContent:
    chapter = ChapterContent.model_validate(read_json(path))
    if chapter.chapter_number != number:
        raise ProjectReadOnlyError("正文章节编号与文件身份不符，请检查作品备份")
    return chapter


def iter_saved_chapters(
    directory: Path, legacy: dict[int, ChapterContent], on_error: Callable[[Path, Exception], None],
) -> Iterator[ChapterContent]:
    """Yield in chapter order. A valid shard supersedes the embedded legacy body."""
    paths: dict[int, Path] = {}
    for path in directory.glob("chapter_*.json"):
        match = _CHAPTER_FILE.fullmatch(path.name)
        if not match:
            on_error(path, ValueError("章节文件名无效"))
            continue
        number = int(match[1])
        if number in paths:
            on_error(path, ValueError("章节文件编号重复"))
            # Prefer the file the single-chapter reader and writer address.
            if path.name != f"chapter_{number:03d}.json":
                continue
        paths[number] = path
    for metadata in (directory / "metadata").glob("chapter_*.json"):
        match = _CHAPTER_FILE.fullmatch(metadata.name)
        if match and int(match[1]) not in paths and int(match[1]) not in legacy:
            on_error(metadata, FileNotFoundError("目录中有章节，正文文件缺失"))
    for number in sorted(paths.keys() | legacy.keys()):
        if number in paths:
            try:
                chapter = read_chapter_file(paths[number], number)
            except (OSError, ValueError, RecursionError, ProjectReadOnlyError) as exc:
                on_error(paths[number], exc)
            else:
                yield chapter
                continue
        if number in legacy:
            if legacy[number].chapter_number == number:
                yield legacy[number]
            else:
                on_error(directory.parent / "project_state.json", ValueError("内嵌章节编号不一致"))
