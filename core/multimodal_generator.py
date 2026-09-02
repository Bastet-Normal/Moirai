
from pydantic import BaseModel, Field

from core.llm import LLMClient, get_llm_client
from core.schemas import ChapterContent, CharacterCard, NovelConcept


class ShortDramaScene(BaseModel):
    """单个短剧分镜头"""
    scene_number: int = Field(..., description="分镜序号 (1~10)")
    shot_type: str = Field(..., description="景别: 特写 / 中景 / 全景 / 快速推拉 / 俯拍")
    visual_action: str = Field(..., description="画面内容与演员微动作调度")
    sound_and_bgm: str = Field(..., description="音效与配乐标注 (如: [重音突响]、[骨碎声]、[心跳骤停BGM])")
    dialogue: str = Field(..., description="台词（带情绪状态标注，如: 楚封（眼神冷冽）:‘给你三息时间跪下。’）")


class ShortDramaScript(BaseModel):
    """竖屏短剧爆款分镜剧本 (1~2分钟高潮切片)"""
    title: str = Field(..., description="短剧集名/分集标题")
    core_hook: str = Field(..., description="黄金前3秒抓人开场视觉钩子")
    scenes: list[ShortDramaScene] = Field(default_factory=list, description="分镜头列表")
    ending_cliffhanger: str = Field(..., description="本集卡点断章瞬间与下集悬念")


class VoiceActorProfile(BaseModel):
    """有声书演播角色音色分配"""
    character_name: str
    voice_type: str = Field(..., description="音色类型: 磁性冷峻青叔音 / 霸道冷艳御姐音 / 阴鸷沙哑老怪音 / 灵动少年音")
    speech_rate: str = Field("标准中速", description="语速与语调特征")
    sample_dialogue_audio_cue: str = Field(..., description="配音情感提示")


class AudioDramaPackage(BaseModel):
    """全套有声书演播配音包"""
    novel_title: str
    voice_cast: list[VoiceActorProfile] = Field(default_factory=list, description="全剧角色配音分配表")
    audio_production_notes: str = Field(..., description="后期混音与环境音效指导")


class MultimodalGenerator:
    """多模态衍生与短剧剧本 / 有声演播全景生成器"""

    def __init__(self, llm_client: LLMClient | None = None):
        self.llm_client = llm_client or get_llm_client()

    def generate_short_drama_script(
        self,
        novel_concept: NovelConcept,
        chapter: ChapterContent,
    ) -> ShortDramaScript:
        """将小说章节转化为工业级竖屏短剧分镜剧本"""
        system_prompt = """你是一位爆款短剧千万级操盘编导。
你的任务是将网文章节浓缩重构为一份节奏极快、视听冲击力拉满、完全符合抖音/快手微短剧拍摄标准的专业分镜剧本：
1. 【景别明确】：合理交替使用特写、推拉镜头、全景与面部微表情。
2. 【音效精确】：精准标注打击音效、BGM变奏点。
3. 【台词精炼】：每句台词不超过 15 字，字字见血、压迫感拉满。
4. 【黄金前3秒与章末钩子】：开头必须是反差极大的视觉冲击，结尾卡在最高潮瞬间。"""

        prompt = f"""【小说基本信息】：
- 书名：《{novel_concept.title}》
- 题材：{novel_concept.genre}
- 金手指：{novel_concept.golden_finger}

【原著章节文本】：
第 {chapter.chapter_number} 章 《{chapter.title}》
\"\"\"{chapter.full_text[:2000]}...\"\"\"

请将其改编为一份专业级竖屏短剧分镜剧本，输出 ShortDramaScript 结构化数据。"""

        return self.llm_client.generate_structured(
            messages=[{"role": "user", "content": prompt}],
            response_model=ShortDramaScript,
            system_prompt=system_prompt,
        )

    def generate_audio_drama_package(
        self,
        novel_concept: NovelConcept,
        characters: list[CharacterCard],
    ) -> AudioDramaPackage:
        """为全书角色分配专属有声书演播音色与音效指导"""
        chars_summary = "\n".join([
            f"- {c.name} ({c.role}): 性格={','.join(c.personality_tags)}, 口吻={c.speaking_style}"
            for c in characters
        ])

        system_prompt = """你是一位国家级有声演播导演、广播剧配音总监。
你的任务是为小说中所有核心角色量身定制最具辨识度的专属音色分配方案（包含旁白与主要出场人物）。"""

        prompt = f"""【小说书名】：《{novel_concept.title}》
【题材】：{novel_concept.genre}

【登场角色名单】：
{chars_summary}

请生成完整的全剧角色音色分配与有声书制作指导方案，输出 AudioDramaPackage 结构化数据。"""

        return self.llm_client.generate_structured(
            messages=[{"role": "user", "content": prompt}],
            response_model=AudioDramaPackage,
            system_prompt=system_prompt,
        )
