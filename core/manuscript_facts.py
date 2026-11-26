"""Bounded verbatim-source samples for optional manuscript fact extraction."""

import re
from collections.abc import Mapping

from pydantic import BaseModel, Field

from core.schemas import ChapterContent, ManuscriptFact

MAX_SOURCE_CHAPTERS = 12
_SAMPLE_CHARS = 1_500
_WORD_CHAR = re.compile(r"[a-z0-9_]")


class ManuscriptFacts(BaseModel):
    facts: list[ManuscriptFact] = Field(default_factory=list, max_length=30)


def contains_name(text: str, name: str) -> bool:
    """Match a full casefolded name, not an ASCII word fragment."""
    start = text.find(name)
    while start != -1:
        end = start + len(name)
        joined_left = start > 0 and _WORD_CHAR.fullmatch(name[0]) and _WORD_CHAR.fullmatch(text[start - 1])
        joined_right = end < len(text) and _WORD_CHAR.fullmatch(name[-1]) and _WORD_CHAR.fullmatch(text[end])
        if not joined_left and not joined_right:
            return True
        start = text.find(name, start + 1)
    return False


def alias_pair(fact: ManuscriptFact) -> tuple[str, str] | None:
    """Read only structured alias facts with both names in their quoted evidence."""
    if fact.kind != "alias":
        return None
    name, alias = fact.subject.strip().casefold(), fact.fact.strip().casefold()
    quote = fact.quote.casefold()
    if name and alias and name != alias and contains_name(quote, name) and contains_name(quote, alias):
        return name, alias
    return None


def manuscript_samples(chapters: Mapping[int, ChapterContent], start: int, end: int) -> dict[int, list[str]]:
    samples = {}
    for number in range(start, end + 1):
        text = chapters[number].full_text
        samples[number] = ([text] if len(text) <= _SAMPLE_CHARS
                           else [text[:750], text[-750:]])
    return samples


def verify_sampled_facts(facts: list[ManuscriptFact], samples: dict[int, list[str]]) -> list[ManuscriptFact]:
    verified: dict[tuple, ManuscriptFact] = {}
    for fact in facts:
        if (fact.kind != "alias" or alias_pair(fact)) and any(
            fact.quote in text for text in samples.get(fact.source_chapter, [])
        ):
            verified[(fact.kind, fact.source_chapter, fact.quote)] = fact
    return list(verified.values())
