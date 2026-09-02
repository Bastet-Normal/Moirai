from typing import Any

from agents.base import BaseAgent
from config.settings import get_settings
from core.schemas import ChapterBeat, ChapterBeatsPlan


class BeatDirectorAgent(BaseAgent):
    """章节导演与细纲 Agent：负责将单章剧情拆解为 3~4 个具体的微情节点（Beats），精准控制情绪波动曲线与章末断章悬念"""

    def __init__(self, model_name: str = None):
        settings = get_settings()
        system_prompt = """你是一位精通微观戏剧冲突调度与情绪控制的资深执行导演。
你的任务是将一个 2000~3500 字的网文章节，细致拆解为 3~4 个连贯递进的“情节点节拍（Beats）”。

【节拍编排核心准则】：
1. 【情绪起伏链条】：
   - 节拍 1（入局与蓄势）：承接前文，危机/悬念迫近，或者主角主动谋划，气氛压抑或充满期待。
   - 节拍 2（交锋与激化）：正面交手、言语博弈或惊险试探，冲突升级到临界点。
   - 节拍 3（高潮与兑现）：底牌打出、逆转反杀、或者关键秘密揭开，完成爽点爆发与即时收益结算。
   - 节拍 4（异变与断章）：收尾时必须抛出新的突发状况或未解悬念，制造让读者迫不及待翻下一页的“断章钩子”。
2. 【动作与对白设计】：
   - 每个节拍必须明确“角色要做什么具体的动作”和“核心对白交锋点”，严禁笼统概括。
3. 【字数配比】：
   - 每个节拍大约 700~1000 字，全章总计 2500~3500 字。"""
        super().__init__(
            name="BeatDirector",
            system_prompt=system_prompt,
            model_name=model_name or settings.PLANNER_MODEL,
        )

    def plan_chapter_beats(self, chapter_context: dict[str, Any]) -> ChapterBeatsPlan:
        """根据本章上下文生成 3~4 个情节点节拍方案"""
        chap_num = chapter_context.get("chapter_number", 1)
        chap_title = chapter_context.get("chapter_title", f"第{chap_num}章")
        chap_summary = chapter_context.get("chapter_summary", "")
        main_conflict = chapter_context.get("main_conflict", "")
        expectation_hook = chapter_context.get("expectation_hook", "")
        recent_summaries = "\n".join(chapter_context.get("recent_summaries", [])) or "（前文开端）"
        
        characters_desc = "\n".join([
            f"- {c['name']} ({c['role']}) | 境界: {c.get('level')} | 装备/底牌: {','.join(c.get('items', []))} | 性格: {','.join(c.get('personality', []))}"
            for c in chapter_context.get("characters_status", [])
        ])

        active_foreshadowings_desc = "\n".join([
            f"- [伏笔ID: {h['id']}] {h['title']}: {h['description']}"
            for h in chapter_context.get("active_foreshadowings", [])
        ]) or "暂无活跃伏笔"

        prompt = f"""【小说基本信息】：
- 书名：《{chapter_context.get('novel_title')}》
- 题材：{chapter_context.get('genre')}
- 金手指：{chapter_context.get('golden_finger')}
- 所属分卷：{chapter_context.get('volume_title')} (目标: {chapter_context.get('volume_goal')})

【前情回顾（最近3章）】：
{recent_summaries}

【本章任务大纲】：
- 章节序号：第 {chap_num} 章
- 章节标题：{chap_title}
- 剧情概要：{chap_summary}
- 核心矛盾：{main_conflict}
- 期待悬念：{expectation_hook}

【出场角色当前实时状态】：
{characters_desc}

【当前可利用/推进的剧情伏笔】：
{active_foreshadowings_desc}

请严格按照网文情绪节拍法，将本章拆解为 3~4 个具体的 ChapterBeat，并设计具有致命吸引力的章末断章钩子（cliffhanger_design）。输出完整的 ChapterBeatsPlan JSON。"""

        return self.call_structured(prompt, ChapterBeatsPlan)

    def build_fallback_plan(self, chapter_context: dict[str, Any]) -> ChapterBeatsPlan:
        """LLM 返回空节拍时的本地确定性兜底方案（保证流水线永不产出空章节）"""
        chap_num = chapter_context.get("chapter_number", 1)
        chap_title = chapter_context.get("chapter_title", f"第{chap_num}章")
        summary = chapter_context.get("chapter_summary", "") or "按本章大纲推进核心剧情"
        conflict = chapter_context.get("main_conflict", "") or "主要矛盾正面交锋"
        hook = chapter_context.get("expectation_hook", "") or "突发变故打破局面，悬念留至下章"

        beats = [
            ChapterBeat(
                beat_index=1,
                scene_goal=f"入局蓄势：承接前文，展开「{summary}」",
                action_and_dialogue_plan="通过具体动作与环境压迫感铺陈危机，交代本章冲突的起因与利害。",
                emotional_tone="压抑蓄力",
                conflict_type="信息解密",
                expected_words=800,
            ),
            ChapterBeat(
                beat_index=2,
                scene_goal=f"正面交锋：围绕「{conflict}」短兵相接",
                action_and_dialogue_plan="安排一次具体的力量或智谋交锋，攻防有来有回，局势在交锋中被逼至临界点。",
                emotional_tone="紧张升级",
                conflict_type="战力碾压",
                expected_words=900,
            ),
            ChapterBeat(
                beat_index=3,
                scene_goal="高潮兑现与断章留钩",
                action_and_dialogue_plan="打出关键底牌完成逆转或收益结算，随即以突发异变收束全章。",
                emotional_tone="爆发反杀",
                conflict_type="人际冲突",
                expected_words=900,
            ),
        ]
        return ChapterBeatsPlan(
            chapter_number=chap_num,
            chapter_title=chap_title,
            core_expectation=f"完成「{summary}」，兑现「{conflict}」的对抗与收获",
            beats=beats,
            cliffhanger_design=hook,
        )
