"""Retrieve exact source passages for chapter review without remote indexing."""

import math
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from core.manuscript_facts import alias_pair, contains_name
from core.schemas import AuditRetrievalCoverage, ChapterContent, ManuscriptContinuity
from core.semantic_memory import lexical_tokens

MAX_SOURCE_CHARS = 12_000_000
WINDOW_CHARS = 680
OVERLAP_CHARS = 120
MAX_PASSAGES = 12
ALIAS_PRIORITY_PASSAGES = 2
MAX_QUERIED_CHARS = 12_000


@dataclass
class AuditEvidence:
    passages: list[dict]
    coverage: AuditRetrievalCoverage


def _alias_expansions(
    chapters: Mapping[int, ChapterContent], chapter_number: int, query: str,
    continuity: ManuscriptContinuity | None,
) -> set[str]:
    if continuity is None:
        return set()
    owners: dict[str, set[str]] = defaultdict(set)
    for fact in continuity.facts:
        if fact.source_chapter >= chapter_number or (pair := alias_pair(fact)) is None:
            continue
        source = chapters.get(fact.source_chapter)
        if source and fact.quote in source.full_text:
            name, alias = pair
            owners[alias].add(name)
    canonical_names = {name for names in owners.values() for name in names}
    groups: dict[str, set[str]] = defaultdict(set)
    for alias, names in owners.items():
        if len(names) == 1 and alias not in canonical_names:
            name = next(iter(names))
            groups[name].update((name, alias))

    folded = query.casefold()
    expansions: set[str] = set()
    for names in groups.values():
        mentioned = {name for name in names if contains_name(folded, name)}
        if mentioned:
            expansions.update(names - mentioned)
    return expansions


def retrieve_audit_evidence(
    chapters: Mapping[int, ChapterContent], chapter_number: int, query: str,
    *, continuity: ManuscriptContinuity | None = None,
) -> AuditEvidence:
    """Rank verbatim windows by confirmed names and distinctive lexical overlap."""
    query_truncated = len(query) > MAX_QUERIED_CHARS
    if query_truncated:
        # Keep head/centre/tail separate to avoid creating artificial terms.
        width = max(1, (MAX_QUERIED_CHARS - 2) // 3)
        middle = (len(query) - width) // 2
        query = "\n".join((query[:width], query[middle:middle + width], query[-width:]))
    terms = {term for term in lexical_tokens(query) if len(term) > 1}
    expansions = _alias_expansions(chapters, chapter_number, query, continuity)

    # Visit recent chapters first, then interleave both ends of the earlier
    # history so that a long book's opening remains eligible for review.
    recent = [n for n in range(max(1, chapter_number - 2), chapter_number) if n in chapters]
    earlier = sorted(n for n in chapters if 1 <= n < chapter_number - 2)
    sources = recent[:]
    for index in range((len(earlier) + 1) // 2):
        sources.append(earlier[index])
        opposite = len(earlier) - 1 - index
        if opposite != index:
            sources.append(earlier[opposite])

    candidates = []
    frequencies: Counter[str] = Counter()
    eligible_lengths = [len(chapters[number].full_text) for number in sources if chapters[number].full_text]
    scanned = fully_scanned = 0
    budget = MAX_SOURCE_CHARS
    for number in sources:
        body = chapters[number].full_text
        if budget == 0:
            break
        if not body:
            continue
        sampled = body[:budget]
        scanned += 1
        fully_scanned += len(sampled) == len(body)
        budget -= len(sampled)
        for start in range(0, len(sampled), WINDOW_CHARS - OVERLAP_CHARS):
            excerpt = sampled[start:start + WINDOW_CHARS]
            matches = terms & lexical_tokens(excerpt)
            folded = excerpt.casefold() if expansions else ""
            alias_match = any(contains_name(folded, name) for name in expansions)
            if matches or alias_match:
                candidates.append((number, start, excerpt, matches, alias_match))
                frequencies.update(matches)

    weights = {term: math.log1p(len(candidates) / count) for term, count in frequencies.items()}
    ranked = sorted(
        candidates,
        key=lambda candidate: (
            sum(weights[term] for term in sorted(candidate[3])),
            len(candidate[3]),
            candidate[0],
            -candidate[1],
        ),
        reverse=True,
    )
    passages = [{"chapter": number, "text": chapters[number].full_text[-WINDOW_CHARS:]}
                for number in recent if chapters[number].full_text]
    ranges: dict[int, list[int]] = defaultdict(list)
    # Give confirmed bridges room without displacing all direct lexical matches.
    priority = (candidate for candidate in ranked if candidate[4])
    for pool, limit in ((priority, min(MAX_PASSAGES, len(passages) + ALIAS_PRIORITY_PASSAGES)), (ranked, MAX_PASSAGES)):
        for number, start, excerpt, _, _ in pool:
            if len(passages) >= limit:
                break
            chapter_ranges = ranges[number]
            if len(chapter_ranges) >= 2 or any(abs(start - selected) < WINDOW_CHARS for selected in chapter_ranges):
                continue
            if any(item["chapter"] == number and item["text"] == excerpt for item in passages):
                continue
            chapter_ranges.append(start)
            passages.append({"chapter": number, "text": excerpt})
    return AuditEvidence(passages, AuditRetrievalCoverage(
        eligible_chapters=len(eligible_lengths), searched_chapters=scanned, fully_searched_chapters=fully_scanned,
        eligible_chars=sum(eligible_lengths), searched_chars=MAX_SOURCE_CHARS - budget, query_truncated=query_truncated,
    ))


def find_audit_passages(
    chapters: Mapping[int, ChapterContent], chapter_number: int, query: str,
    *, continuity: ManuscriptContinuity | None = None,
) -> tuple[list[dict], int]:
    """Compatibility wrapper returning the passages and number of chapters touched."""
    result = retrieve_audit_evidence(chapters, chapter_number, query, continuity=continuity)
    return result.passages, result.coverage.searched_chapters
