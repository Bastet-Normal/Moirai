"""The same bounded factual context is shared by the writer and critic."""

import json
from typing import Any


def format_chapter_facts(context: dict[str, Any] | None) -> str:
    if not context:
        return "未提供额外历史事实；不得自行把后续规划视为已经发生。"
    facts = {key: context.get(key) for key in (
        "characters_status", "recent_summaries", "long_term_memories",
        "knowledge_graph_subgraph", "active_foreshadowings", "special_rules",
    )}
    boundary = context.get("state_as_of_chapter", "前序")
    return (
        f"以下动态事实截至第 {boundary} 章末；本章及后续大纲是待发生目标，不是既成事实。\n"
        + json.dumps(facts, ensure_ascii=False)
    )
