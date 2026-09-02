
from pydantic import BaseModel, Field

from agents.base import BaseAgent
from config.author_styles import AUTHOR_STYLE_PRESETS
from config.settings import get_settings
from core.schemas import (
    ChapterOutline,
    CharacterCard,
    EmotionalPacingBeat,
    GlobalOutline,
    NovelConcept,
    VolumeOutline,
    WorldSetting,
)


class OutlineExtension(BaseModel):
    """A bounded rolling-outline block appended when generation reaches the frontier."""

    chapters: list[ChapterOutline] = Field(default_factory=list)
    pacing_beats: list[EmotionalPacingBeat] = Field(default_factory=list)


class OutlineArchitectAgent(BaseAgent):
    """总纲与分卷架构师 Agent：负责构建百万字长篇宏观主线、全书情绪心电图排期表（PacingSchedule）与各卷详细章节大纲"""

    def __init__(self, model_name: str = None):
        settings = get_settings()
        system_prompt = """你是一位精通网文商业叙事架构、情绪张力心电图编排的总编剧与节奏大师。
你的核心任务是制定百万字总纲，并为每一章精准规划【情绪张力心电图与多巴胺爆发排期（PacingSchedule）】：
1. 【黄金前三章极速破局】：
   - 第1章：绝境困局 + 激活金手指 + 首战告捷（爽度 85）
   - 第2章：实战检验 + 超额收益 + 强烈反差打脸（爽度 90）
   - 第3章：化解眼前危机 + 结算战利品 + 抛出卷主线大危机钩子（期待值 95）
2. 【全书情绪心电图法则】：
   - 严禁连续 3 章压抑（压抑度超过 70 时，下一章必须安排爽点爆发或逆袭）。
   - 每卷包含 1~2 次卷中小高潮（爽度 90+）与 1 次卷末大高潮大清算（爽度 98+）。
3. 【章节大纲要求】：
   - 每章必须具备强烈的戏剧张力标题与清晰的矛盾、出场人物与章末钩子。"""
        super().__init__(
            name="OutlineArchitect",
            system_prompt=system_prompt,
            model_name=model_name or settings.PLANNER_MODEL,
        )

    def generate_global_outline(
        self,
        concept: NovelConcept,
        world_setting: WorldSetting,
        characters: list[CharacterCard],
        total_volumes: int = 3,
        chapters_per_volume: int = 10,
    ) -> GlobalOutline:
        """根据小说策划案与世界观，生成全书宏观总纲、情绪心电图排期与详细章节大纲"""
        chars_summary = "\n".join([
            f"- {c.name} ({c.role}): {','.join(c.personality_tags)} | 初始境界: {c.initial_state.level}"
            for c in characters
        ])
        
        power_system_summary = "\n".join([
            f"- {p.level_name}: {p.description}"
            for p in world_setting.power_system
        ])

        target = concept.commercial_target
        platform_name = target.target_platform if target else "通用商业流"
        style_id = target.author_style if target else "FANQIE_DOPAMINE"
        style_info = AUTHOR_STYLE_PRESETS.get(style_id, AUTHOR_STYLE_PRESETS["FANQIE_DOPAMINE"])
        taboos_text = ", ".join(target.avoided_tropes) if target and target.avoided_tropes else "无"

        prompt = f"""【小说基本信息】：
- 书名：《{concept.title}》
- 题材：{concept.genre}
- 目标平台：{platform_name}
- 文风方向：{style_info['name']}（{style_info['system_instruction']}）
- 核心简介：{concept.logline}
- 金手指机制：{concept.golden_finger}
- 核心爽点：{', '.join(concept.selling_points)}
- 终极冲突：{concept.core_conflict}
- 避坑禁忌：{taboos_text}

【世界观与战力】：
- 世界名称：{world_setting.world_name}
- 战力体系：
{power_system_summary}

【主要初始出场角色】：
{chars_summary}

【架构规划要求】：
1. 规划总卷数：{total_volumes} 卷。
2. 第 1 卷必须规划至少 {chapters_per_volume} 章详细大纲，严格遵循“黄金三章”与“小高潮-中高潮-卷末大高潮”的节奏设计！
3. 为全书各章规划【情绪心电图排期表 (pacing_schedule)】，标注各章的紧张压抑度(tension_level)、期待度(anticipation_level)与爽点爆发度(dopamine_payoff_level 0~100)。
4. volumes 必须按卷号 1 到 {total_volumes} 排列，不能缺卷或重复。第一卷细纲从全局第 1 章连续编号，至少覆盖 {chapters_per_volume} 章；后续卷可先只给宏观规划、将 chapters 留空，后续创作时再滚动细化。
5. 已提供的细纲必须按全局章节号递增，不能重号，volume_number 必须与所在分卷一致。每个情绪节拍的章节身份必须对应正确章节；情绪排期总章数不得少于最高已规划章节号。
6. 每卷必须明确 chapter_range（start/end 为全局首尾章节号，均包含在本卷内），从全局第 1 章开始逐卷连续衔接，不能重叠或留空档。范围代表整卷长度，不是一次滚动细化的区块大小；可依据叙事目标规划较长卷幅，不必把 {chapters_per_volume} 章初始细纲当作全卷长度。所有细纲必须落在本卷范围内；pacing_schedule.total_chapters_planned 不得少于最后一卷的 end。

请输出完整的 GlobalOutline 结构化总纲数据。"""

        return self.call_structured(prompt, GlobalOutline)

    def extend_outline(
        self,
        concept: NovelConcept,
        world_setting: WorldSetting,
        characters: list[CharacterCard],
        volume: VolumeOutline,
        start_chapter: int,
        count: int,
        recent_summaries: list[str],
        preceding_chapters: list[ChapterOutline],
        global_outline: GlobalOutline | None = None,
        following_chapter: ChapterOutline | None = None,
    ) -> OutlineExtension:
        """Plan the next bounded block instead of generating context-free chapters."""
        end_chapter = start_chapter + count - 1
        recent = "\n".join(recent_summaries) or "（尚无已生成章节摘要）"
        preceding = "\n".join(
            f"- 第{chapter.chapter_number}章《{chapter.title}》：{chapter.summary}；章末钩子：{chapter.expectation_hook}"
            for chapter in preceding_chapters[-5:]
        ) or "（这是当前卷首个细纲区块）"
        character_status = "\n".join(
            f"- {character.name}（{character.role}）：{character.current_state.level}，位于{character.current_state.location}"
            for character in characters
        )
        macro = "（旧版调用未提供全书总纲）"
        if global_outline is not None:
            neighbors = "\n".join(
                f"- 第{item.volume_number}卷《{item.volume_title}》：目标={item.volume_goal}；高潮={item.volume_climax}"
                for item in global_outline.volumes if abs(item.volume_number - volume.volume_number) <= 1
            )
            macro = f"全书主题：{global_outline.main_theme}\n全书终局：{global_outline.final_climax}\n相邻卷规划：\n{neighbors}"
        boundary = (
            f"本卷从全局第 {volume.chapter_range.start} 章至第 {volume.chapter_range.end} 章；"
            "到达本卷最后一章时必须完成卷目标与卷高潮，衔接下一卷；若是最后一卷则落实全书终局。"
            if volume.chapter_range else "旧总纲未声明本卷终章，不要擅自宣布全书完结。"
        )
        following = (
            f"第{following_chapter.chapter_number}章《{following_chapter.title}》（第{following_chapter.volume_number}卷）："
            f"{following_chapter.summary}；冲突：{following_chapter.main_conflict}"
            if following_chapter else "（后续尚无细纲）"
        )
        prompt = f"""【滚动扩展任务】
为《{concept.title}》补写第 {start_chapter} 至第 {end_chapter} 章的详细细纲与情绪排期。

【当前卷】第{volume.volume_number}卷《{volume.volume_title}》
- 卷目标：{volume.volume_goal}
- 核心反派：{volume.main_antagonist}
- 卷高潮：{volume.volume_climax}
- 章节边界：{boundary}

【全书与相邻卷宏观规划（规划，不是已发生事实）】
{macro}

【世界铁律】
{'; '.join(world_setting.special_rules)}

【最近已定细纲】
{preceding}

【下一段已定细纲（未来规划，不是已发生事实；须衔接且不得覆盖）】
{following}

【最近正文事实摘要】
{recent}

【人物实时状态】
{character_status}

硬要求：
1. chapters 必须恰好覆盖第 {start_chapter} 至第 {end_chapter} 章，不得跳号或重复；volume_number 全部为 {volume.volume_number}。
2. 每章必须给出可执行的 summary、main_conflict、登场人物和 expectation_hook，承接上一章钩子。
3. pacing_beats 与章节一一对应；避免连续三章高压无回报，并在区块末制造下一阶段强钩子。
4. 不得推翻已经发生的正文事实、人物境界、道具归属和伏笔状态。
5. 不得跨越本卷边界或重写已定细纲。卷首承接上卷结尾，卷末兑现本卷高潮；全书终章完成终局，不能机械制造未完结钩子。

输出 OutlineExtension JSON。"""
        return self.call_structured(prompt, OutlineExtension)
