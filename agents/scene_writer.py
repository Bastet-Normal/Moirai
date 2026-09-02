
from agents.base import BaseAgent
from config.anti_ai_rules import BANNED_AI_WORDS, PLATINUM_AESTHETIC_RULES
from config.settings import get_settings
from core.chapter_context import format_chapter_facts
from core.schemas import ChapterBeat


class SceneWriterAgent(BaseAgent):
    """白金级主笔生成 Agent：专注于电影级视听质感、冷峻利落的人物交锋与高级网文叙事美学"""

    def __init__(self, model_name: str = None):
        settings = get_settings()
        system_prompt = f"""你是一位享誉业界的白金级网文宗师，文笔兼具《诡秘之主》的深邃氛围感与《赤心巡天》《道诡异仙》的冷冽张力。

【你的核心行文美学】：
1. 【电影级视听白描（Show, Don't Tell）】：
   - 用光影、声音、温度、气味等物理细节构建沉浸感（如：雨夜青石板反射的幽暗磷光、骨骼被气劲震碎的沉闷声响）。
   - 拒绝一切概括性陈词，将冲突具象化为致命的动作交锋与眼神博弈。
2. 【冷峻高级的人物对话与智商博弈】：
   - 严禁低幼的“土味打脸”与脸谱化叫嚣（严禁写“打耳光”、“打发叫花子”、“连退三步尖叫”等烂俗桥段）。
   - 反派杀伐果断、动机冰冷而现实；主角谋定后动、言简意赅、出手即分生死。
3. 【极具节奏感的极简段落】：
   - 保持 1~3 句话为一段的清爽排版，每句话都有镜头画面推进感。

{PLATINUM_AESTHETIC_RULES}

【严禁出现的AI废话】：
{', '.join(BANNED_AI_WORDS)}"""
        super().__init__(
            name="SceneWriter",
            system_prompt=system_prompt,
            model_name=model_name or settings.WRITER_MODEL,
        )

    def write_beat(
        self,
        beat: ChapterBeat,
        chapter_title: str,
        chapter_number: int,
        novel_title: str,
        golden_finger: str,
        characters_desc: str,
        previous_text_tail: str = "",
        revision_directives: list[str] = None,
        author_style_instruction: str = "",
        chapter_context: dict | None = None,
    ) -> str:
        """扩写单个节拍正文"""
        revision_prompt = ""
        if revision_directives:
            directives_text = "\n".join([f"- {d}" for d in revision_directives])
            revision_prompt = f"\n【质检专家修改意见（本次必须严格执行）】：\n{directives_text}\n"

        prompt = f"""【正在创作】：
- 作品：《{novel_title}》
- 章节：第 {chapter_number} 章 《{chapter_title}》
- 金手指运作机制：{golden_finger}
- 本书文风约束：{author_style_instruction or '冷峻、清晰、以动作与对白推进'}

【登场人物写作前状态】：
{characters_desc}

【历史事实与时间边界】：
{format_chapter_facts(chapter_context)}

【前文承接片段】：
\"\"\"{previous_text_tail or '（本章开篇）'}\"\"\"

【当前节拍任务（第 {beat.beat_index} 节拍）】：
- 核心情节推进：{beat.scene_goal}
- 冲突与博弈：{beat.action_and_dialogue_plan}
- 情绪基调：{beat.emotional_tone}
- 登场人物：{', '.join(beat.involved_characters)}
- 目标字数：{beat.expected_words or 800} 字左右
{revision_prompt}
请以白金级电影质感与冷峻有力的文风，直接输出本节拍正文："""

        return self.call_llm(prompt, temperature=0.75)
