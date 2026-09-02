from agents.base import BaseAgent
from config.anti_ai_rules import BANNED_AI_WORDS
from config.settings import get_settings


class StylistPolisherAgent(BaseAgent):
    """白金级文学精修与去 AI 味 Agent：负责雕琢语言质感、增强视听张力、剔除低俗套话与优化阅读节奏"""

    def __init__(self, model_name: str = None):
        settings = get_settings()
        system_prompt = f"""你是一位白金级文学精修大师，拥有顶尖的文字审美与视听节奏把控力。
你的使命是将原始文本精修成极具阅读张力、完全看不出 AI 痕迹的高端商业小说正文：

1. 【重塑审美与格调】：
   - 坚决剔除任何粗俗低智的叫嚣与脸谱化冲突。
   - 强化画面的冷冽感、力量感与压迫感，用精简的动词构建电影级镜头。

2. 【优化移动端阅读节奏】：
   - 保持 1~3 句话为一段的通透排版，句式节奏干脆利落。

3. 【全面清理违禁套话】：
   - 严格消除：{', '.join(BANNED_AI_WORDS)}。

4. 【章末悬念定格】：
   - 结尾必须精准停留在最具心理震撼力与命运转折的瞬间！"""
        super().__init__(
            name="StylistPolisher",
            system_prompt=system_prompt,
            model_name=model_name or settings.WRITER_MODEL,
        )

    def polish_chapter(
        self,
        raw_chapter_text: str,
        chapter_title: str,
        chapter_number: int,
        novel_title: str,
        author_style_instruction: str = "",
    ) -> str:
        """对整章文本进行白金级文学精修"""
        prompt = f"""【精修目标】：
- 作品：《{novel_title}》
- 章节：第 {chapter_number} 章 《{chapter_title}》
- 本书文风约束：{author_style_instruction or '保持原作稳定文风'}

【待精修原始文本】：
\"\"\"{raw_chapter_text}\"\"\"

请严格按照白金级文学美学与去 AI 味准则进行精修，直接输出最终正文："""

        return self.call_llm(prompt, temperature=0.70)
