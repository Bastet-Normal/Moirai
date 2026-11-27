"""Read-only, bounded search over one loaded novel project."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from core.chapter_store import iter_saved_chapters
from core.error_reporting import report_internal_error
from core.schemas import ChapterContent, NovelProjectState
from core.storage import ProjectReadOnlyError, project_lock, project_version, read_json, revision_token

logger = logging.getLogger("novelengine.search")

_WHITESPACE = re.compile(r"\s+")
_KIND_ORDER = {
    "chapter": 0,
    "character": 1,
    "foreshadowing": 2,
    "outline": 3,
    "world": 4,
    "concept": 5,
}


def _text_values(value: Any) -> Iterable[str]:
    """Yield user-authored strings without indexing keys, numbers or booleans."""
    if isinstance(value, str):
        if value.strip():
            yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _text_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _text_values(item)


def _searchable_text(value: Any) -> str:
    return _WHITESPACE.sub(" ", " ".join(_text_values(value))).strip()


def _snippet(text: str, match: re.Match[str], *, radius: int = 64) -> str:
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"


def search_project(
    state: NovelProjectState, query: str, *, limit: int = 40, ordered_chapters: Iterable[ChapterContent] | None = None,
) -> list[dict[str, Any]]:
    """Search prose and structured story data, returning at most one hit per entity."""
    normalized_query = _WHITESPACE.sub(" ", query).strip()
    if not normalized_query or limit < 1:
        return []

    pattern = re.compile(re.escape(normalized_query), re.IGNORECASE)
    candidates: list[dict[str, Any]] = []

    def add(
        *,
        kind: str,
        key: str,
        label: str,
        value: Any,
        tab: str,
        chapter_number: int | None = None,
    ) -> None:
        label_match = pattern.search(label)
        source = label if label_match else _searchable_text(value)
        selected = label_match or pattern.search(source)
        if selected is None:
            return
        candidates.append(
            {
                "kind": kind,
                "key": key,
                "label": label,
                "snippet": _snippet(source, selected),
                "tab": tab,
                "chapter_number": chapter_number,
                "_label_match": label_match is not None,
            }
        )

    for character in sorted(state.characters.values(), key=lambda item: (item.name, item.id)):
        add(
            kind="character",
            key=f"character:{character.id}",
            label=f"人物 · {character.name}",
            value=character.model_dump(mode="json"),
            tab="characters",
        )

    for hook in sorted(state.foreshadowings.values(), key=lambda item: (item.planted_chapter, item.hook_id)):
        add(
            kind="foreshadowing",
            key=f"foreshadowing:{hook.hook_id}",
            label=f"伏笔 · {hook.title}",
            value=hook.model_dump(mode="json"),
            tab="foreshadowing",
        )

    outline = state.global_outline
    if outline:
        add(
            kind="outline",
            key="outline:global",
            label="总纲 · 全书主线",
            value={
                "main_theme": outline.main_theme,
                "final_climax": outline.final_climax,
            },
            tab="outline",
        )
        for volume in outline.volumes:
            add(
                kind="outline",
                key=f"outline:volume:{volume.volume_number}",
                label=f"大纲 · 第{volume.volume_number}卷 {volume.volume_title}",
                value={
                    "goal": volume.volume_goal,
                    "antagonist": volume.main_antagonist,
                    "climax": volume.volume_climax,
                },
                tab="outline",
            )
            for chapter in volume.chapters:
                add(
                    kind="outline",
                    key=f"outline:chapter:{chapter.chapter_number}",
                    label=f"细纲 · 第{chapter.chapter_number}章 {chapter.title}",
                    value=chapter.model_dump(mode="json"),
                    tab="outline",
                    chapter_number=chapter.chapter_number,
                )

    if state.world_setting:
        add(
            kind="world",
            key="world:setting",
            label=f"世界观 · {state.world_setting.world_name}",
            value=state.world_setting.model_dump(mode="json"),
            tab="world",
        )

    if state.concept:
        add(
            kind="concept",
            key="concept:main",
            label=f"策划案 · {state.concept.title}",
            value=state.concept.model_dump(mode="json"),
            tab="outline",
        )

    candidates.sort(
        key=lambda item: (
            not item["_label_match"],
            _KIND_ORDER.get(item["kind"], 99),
            item["chapter_number"] or 0,
            item["label"],
        )
    )
    # In the established ordering, prose follows all label hits and precedes the
    # remaining structured matches. Stop opening shards as soon as that space is filled.
    labels = []
    other = []
    for item in candidates[:limit]:
        (labels if item.pop("_label_match") else other).append(item)
    if len(labels) == limit:
        return labels
    results = labels
    if ordered_chapters is None:
        ordered_chapters = (state.chapters[n] for n in sorted(state.chapters))
    single_term = " " not in normalized_query
    for chapter in ordered_chapters:
        # Whitespace normalization cannot create a single-term match. Avoid copying
        # the entire manuscript when searching for an uncommon name or identifier.
        if single_term and not pattern.search(chapter.full_text):
            continue
        text = _WHITESPACE.sub(" ", chapter.full_text).strip()
        match = pattern.search(text)
        if match:
            number = chapter.chapter_number
            results.append({
                "kind": "chapter", "key": f"chapter:{number}", "label": f"第{number}章 {chapter.title}",
                "snippet": _snippet(text, match), "tab": "chapters", "chapter_number": number,
            })
            if len(results) == limit:
                break
    return results + other[:limit - len(results)]


def search_saved_project(project_dir: Path, query: str, *, limit: int) -> dict[str, Any]:
    """Bind ranked results and revision to the same committed manuscript."""
    root = project_dir.parent.resolve()
    project_dir = project_dir.resolve()
    if project_dir.parent != root:
        raise ProjectReadOnlyError("作品目录不在当前作品库内")
    incomplete = False

    def skipped(path: Path, exc: Exception) -> None:
        nonlocal incomplete
        incomplete = True
        report_internal_error(logger, exc, scope="project_search",
                              context={"novel_id": project_dir.name, "file": path.name})

    with project_lock(project_dir, scope="snapshot"):
        try:
            state = NovelProjectState.model_validate(read_json(project_dir / "project_state.json"))
        except (ValueError, OSError, RecursionError) as exc:
            raise ProjectReadOnlyError("作品目录暂时无法读取，请刷新作品库后再搜索") from exc
        chapters = iter_saved_chapters(project_dir / "chapters", state.chapters, skipped)
        results = search_project(state, query, limit=limit, ordered_chapters=chapters)
        return {"novel_id": project_dir.name, "revision": revision_token(project_dir.name, project_version(project_dir)),
                "query": query, "results": results, "incomplete": incomplete}
