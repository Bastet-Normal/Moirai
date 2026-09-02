import math
import re

from pydantic import BaseModel, Field

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_WORD = re.compile(r"[A-Za-z0-9_]+")


def lexical_tokens(text: str) -> set[str]:
    """Tokenize mixed Chinese/Latin text without an external NLP dependency.

    ``\\w+`` treats an entire Chinese sentence as one token, which made the old
    memory search miss almost every partial phrase. Character bi/tri-grams give
    stable lexical recall while keeping this offline and deterministic.
    """
    tokens = {word.casefold() for word in _LATIN_WORD.findall(text or "")}
    for run in _CJK_RUN.findall(text or ""):
        if len(run) == 1:
            tokens.add(run)
            continue
        tokens.update(run[index : index + 2] for index in range(len(run) - 1))
        if len(run) >= 3:
            tokens.update(run[index : index + 3] for index in range(len(run) - 2))
        if len(run) <= 8:
            tokens.add(run)
    return tokens


class MemoryChunk(BaseModel):
    """语义记忆片段"""
    chunk_id: str
    chapter_number: int
    volume_number: int = 1
    content: str
    involved_entities: list[str] = Field(default_factory=list)
    event_type: str = Field("PLOT", description="PLOT / DIALOGUE / BATTLE / LORE / ITEM_ACQUISITION")
    importance_weight: float = 1.0


class SemanticMemoryIndex(BaseModel):
    """百万字超长篇长程语义记忆检索索引"""
    chunks: list[MemoryChunk] = Field(default_factory=list)

    def add_chapter_memory(
        self,
        chapter_number: int,
        volume_number: int,
        chapter_title: str,
        full_text: str,
        involved_entities: list[str],
    ):
        """将单个章节切片为语义记忆块并存入索引"""
        paragraphs = [p.strip() for p in full_text.split("\n\n") if len(p.strip()) > 5]
        
        # 按每 2~3 个自然段合并为一个记忆块
        chunk_size = 3
        for i in range(0, len(paragraphs), chunk_size):
            chunk_text = "\n".join(paragraphs[i:i + chunk_size])
            chunk_id = f"mem_c{chapter_number:03d}_p{i:02d}"
            
            # 判断事件类型
            event_type = "PLOT"
            if any(w in chunk_text for w in ["说", "道", "冷笑", "喝问"]):
                event_type = "DIALOGUE"
            if any(w in chunk_text for w in ["杀", "斩", "轰", "剑气", "崩裂", "鲜血"]):
                event_type = "BATTLE"
            if any(w in chunk_text for w in ["获得", "储物袋", "法宝", "功法", "丹药"]):
                event_type = "ITEM_ACQUISITION"

            self.chunks.append(MemoryChunk(
                chunk_id=chunk_id,
                chapter_number=chapter_number,
                volume_number=volume_number,
                content=f"【第{chapter_number}章 《{chapter_title}》】：{chunk_text}",
                involved_entities=involved_entities,
                event_type=event_type,
            ))

    def replace_chapter_memory(
        self,
        chapter_number: int,
        volume_number: int,
        chapter_title: str,
        full_text: str,
        involved_entities: list[str],
    ):
        """重建某章的记忆块：先清除该章旧块再写入（用于重写/局部修改后，防止旧版本文本残留污染检索）"""
        self.chunks = [c for c in self.chunks if c.chapter_number != chapter_number]
        self.add_chapter_memory(
            chapter_number=chapter_number,
            volume_number=volume_number,
            chapter_title=chapter_title,
            full_text=full_text,
            involved_entities=involved_entities,
        )

    def prune_after_chapter(self, keep_up_to_chapter: int):
        """修剪指定章节之后的所有记忆块（时光机回滚时使用）"""
        self.chunks = [c for c in self.chunks if c.chapter_number <= keep_up_to_chapter]

    def search_relevant_memories(
        self,
        query: str,
        target_entities: list[str] | None = None,
        top_k: int = 3,
        exclude_recent_n_chapters: int = 2,
        current_chapter: int = 1,
    ) -> list[MemoryChunk]:
        """长程检索：跨卷/跨百章检索与当前情节、实体强相关的历史记忆"""
        if not self.chunks or top_k <= 0:
            return []

        scored_chunks = []
        target_entities_set = set(target_entities or [])
        query_words = lexical_tokens(query)
        if not query_words and not target_entities_set:
            return []

        for chunk in self.chunks:
            # A rewrite must never recall its old body or any future chapter,
            # including chapters 1/2 where the recent-window shortcut once leaked.
            if chunk.chapter_number >= current_chapter:
                continue
            # 过滤掉刚发生过的最近 2 章（已有 recent_summaries 覆盖）
            if current_chapter - chunk.chapter_number <= max(0, exclude_recent_n_chapters):
                continue

            score = 0.0
            # 1. 实体匹配加权
            chunk_entities_set = set(chunk.involved_entities)
            entity_overlap = len(target_entities_set.intersection(chunk_entities_set))
            score += entity_overlap * 3.0

            # 2. 关键词交集与词频打分
            chunk_words = lexical_tokens(chunk.content)
            word_overlap = len(query_words.intersection(chunk_words))
            if query_words:
                score += (word_overlap / math.sqrt(len(query_words))) * 2.0

            # 3. 道具获取与重大战役加权
            # 事件类型只作为相关结果的微调，不能让无关战斗凭空入选。
            if entity_overlap or word_overlap:
                if chunk.event_type == "ITEM_ACQUISITION":
                    score += 1.5
                if chunk.event_type == "BATTLE":
                    score += 1.0
                score *= max(0.1, chunk.importance_weight)

            if score > 0:
                scored_chunks.append((score, chunk))

        # 按得分从高到低排序
        scored_chunks.sort(key=lambda x: (x[0], x[1].chapter_number), reverse=True)
        return [c for _, c in scored_chunks[:top_k]]
