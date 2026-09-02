
from agents.base import BaseAgent
from config.settings import get_settings
from core.schemas import ChapterContent, CharacterCard, CommercialDeliveryPackage, NovelConcept, WorldSetting


class CommercialDeliveryAgent(BaseAgent):
    """商业交付与宣发 Agent：负责将小说一键打包为全套商业化衍生交付物（抖音/快手短视频推文解说脚本、AI电影级封面 Prompt、平台推荐文案与成本预估）"""

    def __init__(self, model_name: str = None):
        settings = get_settings()
        system_prompt = """你是一位拥有千万级粉丝爆款操盘经验的网文商业宣发总监、短视频推文顶级编导。
你的任务是将一部小说转化为极具吸金能力的商业交付包：
1. 【抖音/快手网文推文短视频爆款脚本】：
   - 【黄金前3秒】：第一句话必须是极具冲击力的反差/绝境/炸裂金手指悬念（如：“如果能看到死人死前三天的记忆，你会用它来做什么？”）。
   - 【分镜与对白】：标注【画面/景别】、【情绪音效/BGM建议】、【解说旁白】。
   - 【高潮断章】：精准截在最刺激的打脸瞬间或巨大危机揭开的一刹那，制造最强点击欲。
2. 【AI 电影级封面与立绘 Prompt (Midjourney / SDXL 格式)】：
   - 生成英文专业 Prompt，包含画面主体、光影（Octane Render, Unreal Engine 5, Volumetric Lighting）、构图视角、艺术风格（Cyberpunk x Xianxia / Dark Fantasy Epic）、分辨率及色彩基调。
3. 【平台推荐文案】：输出 3 条极具网感的引流推文话术。"""
        super().__init__(
            name="CommercialDelivery",
            system_prompt=system_prompt,
            model_name=model_name or settings.PLANNER_MODEL,
        )

    def generate_delivery_package(
        self,
        concept: NovelConcept,
        world_setting: WorldSetting,
        protagonist: CharacterCard,
        first_three_chapters: list[ChapterContent],
    ) -> CommercialDeliveryPackage:
        """为小说生成全套商业衍生交付包"""
        chap_summaries = "\n".join([
            f"- 第{c.chapter_number}章 《{c.title}》 (字数: {c.word_count}) | 结尾钩子: {c.cliffhanger_ending}"
            for c in first_three_chapters
        ])

        first_chap_sample = first_three_chapters[0].full_text[:600] if first_three_chapters else ""

        prompt = f"""【小说基本信息】：
- 书名：《{concept.title}》
- 题材：{concept.genre}
- 核心简介：{concept.logline}
- 金手指机制：{concept.golden_finger}
- 核心爽点：{', '.join(concept.selling_points)}
- 主角人设：{protagonist.name} ({','.join(protagonist.personality_tags)})

【黄金前三章剧情走向】：
{chap_summaries}

【第一章开篇片段】：
\"\"\"{first_chap_sample}...\"\"\"

请为该小说定制全套商业交付包（包含短视频推文解说脚本、中英双语 Midjourney 封面 Prompt、分发推文文案），输出符合 CommercialDeliveryPackage 的结构化 JSON。"""

        return self.call_structured(prompt, CommercialDeliveryPackage)
