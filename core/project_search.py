"""Read-only, bounded search over one loaded novel project."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from core.schemas import NovelProjectState

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


def _match(text: str, query: str) -> re.Match[str] | None:
    return re.search(re.escape(query), text, flags=re.IGNORECASE)


def _snippet(text: str, match: re.Match[str], *, radius: int = 64) -> str:
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"


def search_project(state: NovelProjectState, query: str, *, limit: int = 40) -> list[dict[str, Any]]:
    """Search prose and structured story data, returning at most one hit per entity."""
    normalized_query = _WHITESPACE.sub(" ", query).strip()
    if not normalized_query or limit < 1:
        return []

    candidates: list[dict[str, Any]] = []

    def add(
        *,
        kind: str,
        key: str,
        label: str,
        value: Any,
        tab: str,
        chapter_number: int | None = None,
        search_label: bool = True,
    ) -> None:
        text = _searchable_text(value)
        found = _match(text, normalized_query)
        label_match = _match(label, normalized_query) if search_label else None
        if not found and not label_match:
            return
        source, selected = (label, label_match) if label_match else (text, found)
        assert selected is not None
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

    for number, chapter in sorted(state.chapters.items()):
        # Chapter titles are already available instantly in the command palette;
        # this project search contributes the lazily loaded body text.
        add(
            kind="chapter",
            key=f"chapter:{number}",
            label=f"第{number}章 {chapter.title}",
            value=chapter.full_text,
            tab="chapters",
            chapter_number=number,
            search_label=False,
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
    for item in candidates:
        item.pop("_label_match")
    return candidates[:limit]
