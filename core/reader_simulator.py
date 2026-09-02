
from pydantic import BaseModel, Field

from core.llm import LLMClient, get_llm_client
from core.schemas import ChapterContent, NovelConcept


class ReaderPersona(BaseModel):
    """虚拟读者画像"""
    name: str = Field(..., description="读者昵称 (如: '十年老书虫_剑酒', '快节奏爽文狂魔', '考据毒抗极低君')")
    archetype: str = Field(..., description="读者类型: 深度老书虫 / 纯爽快餐党 / 设定考据党 / 逻辑洁癖党 / 追更情绪党")
    reading_preference: str = Field(..., description="喜好倾向")
    drop_off_trigger: str = Field(..., description="容易劝退/弃读的雷区")


class BulletComment(BaseModel):
    """段落级弹幕互动"""
    paragraph_index: int = Field(..., description="对应正文第几段 (从0开始)")
    reader_name: str = Field(..., description="发送弹幕的读者昵称")
    sentiment: str = Field("POSITIVE", description="POSITIVE (狂赞/爽) / NEUTRAL (讨论) / WARNING (毒点预警/吐槽)")
    content: str = Field(..., description="弹幕真实吐槽或赞美内容 (如: '卧槽这波反杀绝了！', '细节好评，前面伏笔收了', '作者这里有点拖了快点打！')")


class DropOffRiskPoint(BaseModel):
    """弃读流失风险点"""
    paragraph_index: int
    risk_level: str = Field("MEDIUM", description="LOW / MEDIUM / HIGH / CRITICAL")
    reason: str = Field(..., description="流失原因深度剖析 (如: '大段设定堆砌过长导致单手滑屏疲劳', '主角反击动作过慢产生压抑窒息感')")
    fix_suggestion: str = Field(..., description="工业级精修修改建议")


class ReaderSimulationReport(BaseModel):
    """全书/单章读者留存与弹幕推演报告"""
    chapter_number: int
    predicted_completion_rate: float = Field(88.5, description="预估单章读完率 (0~100%)")
    three_chapter_retention_score: float = Field(85.0, description="前三章留存吸睛综合评分 (0~100)")
    bullet_comments: list[BulletComment] = Field(default_factory=list, description="段落级实时弹幕")
    drop_off_risks: list[DropOffRiskPoint] = Field(default_factory=list, description="弃读风险点与避坑指南")
    overall_sentiment_summary: str = Field(..., description="虚拟读者群综合口碑与付费追更意愿剖析")


class ReaderSimulator:
    """万级虚拟读者心智仿真与留存率预警引擎"""

    DEFAULT_PERSONAS = [
        ReaderPersona(
            name="十年老书虫_断剑",
            archetype="深度老书虫",
            reading_preference="看重智商博弈、世界观严谨度、战力不崩坏",
            drop_off_trigger="反派强行降智、主角无脑装逼、设定前后矛盾",
        ),
        ReaderPersona(
            name="快节奏爽文狂魔_阿飞",
            archetype="纯爽快餐党",
            reading_preference="喜欢极速破局、即时打脸、战利品连续爆出、短段落单手滑屏",
            drop_off_trigger="连续憋屈超过1章、主角被按在地上摩擦不还手、水文拖沓",
        ),
        ReaderPersona(
            name="考据细节狂_墨竹",
            archetype="设定考据党",
            reading_preference="热衷研究法宝相生相克、功法原理、历史隐秘与伏笔暗线",
            drop_off_trigger="吃了前文设定的常识性硬伤、数值战力膨胀失衡",
        ),
        ReaderPersona(
            name="情绪共鸣党_晚晴",
            archetype="追更情绪党",
            reading_preference="在意人物羁绊、悲壮感、高燃对决与章末悬念",
            drop_off_trigger="人物脸谱化毫无灵魂、死板像机器人、毫无情绪起伏",
        ),
    ]

    def __init__(self, llm_client: LLMClient | None = None):
        self.llm_client = llm_client or get_llm_client()

    def simulate_chapter_readers(
        self,
        novel_concept: NovelConcept,
        chapter: ChapterContent,
    ) -> ReaderSimulationReport:
        """模拟 4 大典型读者画像逐段阅读本章，生成真实弹幕与弃读留存率预警"""
        paragraphs = [p.strip() for p in chapter.full_text.split("\n\n") if p.strip()]
        
        # 挑选前 15 个代表性段落进行索引标注
        indexed_paragraphs = []
        for idx, p in enumerate(paragraphs[:15]):
            indexed_paragraphs.append(f"[段落 {idx}]: {p[:120]}...")

        system_prompt = """你是一位精通网文读者心理学与大数据留存算法的白金主编。
你的任务是模拟 4 类不同偏好的真实读者（老书虫、爽文党、考据党、情绪党）阅读该章节的过程：
1. 【段落级真实弹幕 (BulletComment)】：在关键转折、装逼高潮、伏笔揭晓或尴尬拖沓处发送生动的弹幕吐槽或狂赞。
2. 【弃读流失预警 (DropOffRiskPoint)】：精准定位可能导致读者退出 APP 或滑走的段落，指出致命原因并提供修改方案。
3. 【读完率与留存评分】：结合番茄/书旗/起点平台算法，给出数值化留存预测。"""

        prompt = f"""【小说信息】：
- 书名：《{novel_concept.title}》
- 题材：{novel_concept.genre}
- 金手指：{novel_concept.golden_finger}

【待评测章节】：第 {chapter.chapter_number} 章 《{chapter.title}》 (共 {chapter.word_count} 字)
【章节段落切片】：
{chr(10).join(indexed_paragraphs)}

请进行全真读者心智推演，输出符合 ReaderSimulationReport 的结构化 JSON。"""

        return self.llm_client.generate_structured(
            messages=[{"role": "user", "content": prompt}],
            response_model=ReaderSimulationReport,
            system_prompt=system_prompt,
        )
