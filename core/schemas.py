import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, computed_field, model_validator


class CommercialTarget(BaseModel):
    """商业化定向定制与避坑配置"""
    target_platform: str = Field("FANQIE", description="目标平台: FANQIE(番茄/七猫快爽流), QIDIAN(起点严谨群像流), OVERSEAS_LITRPG(海外出海数值流), GENERAL(通用流)")
    author_style: str = Field("FANQIE_DOPAMINE", description="文风预设 ID，来自 config.author_styles")
    pacing_mode: str = Field("FAST_IMMEDIATE_REWARD", description="节奏模式: FAST_IMMEDIATE_REWARD(极速即时反馈), STEADY_WORLD_BUILDING(慢热厚重布局), EXPLOSIVE_CLIMAX(高频爆发)")
    female_lead_setting: str = Field("NO_FEMALE_LEAD", description="女主设定: NO_FEMALE_LEAD(无女主/一心求道), SINGLE_LEAD(单女主), HAREM(后宫), ANY(顺其自然)")
    protagonist_persona: str = Field("RUTHLESS_DECISIVE", description="主角人设: RUTHLESS_DECISIVE(杀伐果断/绝不圣母), CUNNING_SCHEMER(苟道老六/隐忍布局), INVINCIBLE_CHILL(无敌轻松/装逼犯), HEROIC_DEVOTION(热血重情)")
    avoided_tropes: list[str] = Field(default_factory=list, description="严防毒点与避坑列表（如：严禁虐主、反派绝不降智、严禁送女）")
    word_target_per_chapter: int = Field(2500, ge=500, le=10_000, description="单章目标字数")


class NovelConcept(BaseModel):
    """网文核心策划案与卖点模型"""
    title: str = Field(..., description="小说书名")
    genre: str = Field(..., description="题材分类（如：玄幻升级、诡异修仙、都市高武、无限流等）")
    logline: str = Field(..., description="一句话核心卖点简介（核心看点+戏剧性冲突）")
    golden_finger: str = Field(..., description="主角金手指/作弊器设定及详细运作机制")
    selling_points: list[str] = Field(default_factory=list, description="核心爽点列表（如：极致信息差、越级碾压、系统爆兵）")
    tone: str = Field("热血爽快，快节奏", description="作品基调与行文风格")
    target_audience: str = Field("网文年轻核心受众", description="目标受众画像")
    core_conflict: str = Field(..., description="全书终极大冲突与主角终极目标")
    commercial_target: CommercialTarget | None = Field(default=None, description="商业化定制参数")


class PowerLevel(BaseModel):
    """战力等级体系"""
    level_name: str = Field(..., description="境界/等级名称（如：练气、筑基、金丹）")
    description: str = Field(..., description="该境界表现力与特征能力")
    breakthrough_criteria: str = Field(..., description="突破或晋升条件")


class FactionSetting(BaseModel):
    """门派与势力设定"""
    name: str = Field(..., description="势力名称")
    alignment: str = Field(..., description="立场（友好、敌对、中立、暗藏祸心）")
    description: str = Field(..., description="势力背景与核心底蕴")
    key_figures: list[str] = Field(default_factory=list, description="主要代表人物")


class WorldSetting(BaseModel):
    """世界观设定集"""
    world_name: str = Field(..., description="世界/大陆名称")
    geography_overview: str = Field(..., description="地理版图与核心区域概况")
    power_system: list[PowerLevel] = Field(default_factory=list, description="完整的战力进阶体系")
    factions: list[FactionSetting] = Field(default_factory=list, description="核心势力与阵营分布")
    special_rules: list[str] = Field(default_factory=list, description="核心世界铁律/禁忌设定")
    unique_lore: str = Field("", description="远古秘辛或世界核心未解之谜")


class CharacterState(BaseModel):
    """角色的动态实时状态表"""
    level: str = Field("初始等级", description="当前实力境界/等级")
    location: str = Field("新手村", description="当前所在地理位置")
    hp_status: str = Field("健康良好", description="肉身/精神伤势状态")
    equipment_and_items: list[str] = Field(default_factory=list, description="随身携带的重要道具/武器/底牌")
    skills_and_abilities: list[str] = Field(default_factory=list, description="已掌握的技能/功法/神通")
    current_mood: str = Field("平静警惕", description="当前心理状态与即时意图")
    relations: dict[str, str] = Field(default_factory=dict, description="对其他角色的当前态度/好感度")
    is_alive: bool = Field(True, description="是否存活")


class CharacterCard(BaseModel):
    """角色人设卡"""
    id: str = Field(..., description="角色唯一标识ID")
    name: str = Field(..., description="角色姓名")
    role: str = Field(..., description="角色定位：主角/反派/重要配角/导师/打脸配角")
    personality_tags: list[str] = Field(default_factory=list, description="核心性格标签")
    background: str = Field(..., description="身世与过往经历")
    speaking_style: str = Field("口吻自然", description="说话口吻与标志性语言习惯")
    initial_state: CharacterState = Field(default_factory=CharacterState, description="初始状态")
    current_state: CharacterState = Field(default_factory=CharacterState, description="当前动态状态")
    secrets_and_goals: list[str] = Field(default_factory=list, description="深层秘密与个人动机")


class ForeshadowingRecord(BaseModel):
    """剧情伏笔与悬念账本"""
    hook_id: str = Field(..., description="伏笔唯一编号")
    title: str = Field(..., description="伏笔简述（如：神秘黑戒残魂、灭门仇人玉佩）")
    description: str = Field(..., description="伏笔详情与暗线线索")
    planted_chapter: int = Field(..., ge=1, description="埋下伏笔的章节序号")
    expected_resolution_volume: int = Field(1, ge=1, description="预计回收伏笔的分卷序号")
    status: str = Field("PLANTED", description="状态：PLANTED(已埋下) / DEVELOPING(发酵中) / RESOLVED(已揭晓)")
    resolution_notes: str = Field("", description="伏笔揭晓时的剧情安排说明")


class EmotionalPacingBeat(BaseModel):
    """单章情绪张力与多巴胺心电图排期"""
    chapter_number: int = Field(..., ge=1, description="章节序号")
    tension_level: int = Field(50, ge=0, le=100, description="压抑/危机紧张度 (0~100)")
    anticipation_level: int = Field(60, ge=0, le=100, description="读者期待值/蓄势度 (0~100)")
    dopamine_payoff_level: int = Field(70, ge=0, le=100, description="爽点兑现/多巴胺爆发度 (0~100)")
    pacing_type: str = Field("蓄势铺垫", description="节拍类型: 蓄势铺垫 / 压抑受挫 / 绝地反击 / 爽点爆发 / 收获清点 / 突发异变")
    target_emotion_summary: str = Field("", description="本章情绪控制目标一句话总结")


class PacingSchedule(BaseModel):
    """全书全卷全局情绪心电图排期表"""
    total_chapters_planned: int = Field(30, ge=1, description="规划总章数")
    pacing_beats: list[EmotionalPacingBeat] = Field(default_factory=list, description="各章节情绪波形排期")
    climax_milestone_chapters: list[int] = Field(default_factory=list, description="全书重大高潮章节里程碑序号")


class ChapterBeat(BaseModel):
    """章节内部细纲微节拍（单章3~4个节拍）"""
    beat_index: int = Field(..., ge=1, le=8, description="节拍序号 (1~4)")
    scene_goal: str = Field(..., description="本节拍核心情节目标与推进点")
    action_and_dialogue_plan: str = Field(..., description="具体动作交锋、对话交手与场面调度设计")
    emotional_tone: str = Field(..., description="情绪基调（如：压抑嘲讽、期待蓄力、反转暴击、清点战利品）")
    conflict_type: str = Field(..., description="冲突类型（人际冲突/战力碾压/利益争夺/信息解密）")
    involved_characters: list[str] = Field(default_factory=list, description="出场角色名单")
    expected_words: int = Field(800, ge=100, le=5000, description="预期生成字数")


class ChapterBeatsPlan(BaseModel):
    """单章完整情节点节拍规划方案"""
    chapter_number: int = Field(..., ge=1, description="章节序号")
    chapter_title: str = Field(..., description="本章标题")
    core_expectation: str = Field(..., description="本章给读者的核心期待感与主线推进目标")
    beats: list[ChapterBeat] = Field(default_factory=list, description="本章拆解的3~4个微节拍列表")
    cliffhanger_design: str = Field(..., description="章末留钩子设计（必须停在最高潮或突发悬念瞬间）")


class ChapterOutline(BaseModel):
    """章节大纲定义"""
    chapter_number: int = Field(..., ge=1, description="全局章节序号")
    volume_number: int = Field(..., ge=1, description="所属分卷序号")
    title: str = Field(..., description="章节名称（吸引眼球的网文风格标题）")
    summary: str = Field(..., description="本章核心剧情概要（100~200字）")
    main_conflict: str = Field(..., description="核心矛盾冲突与高潮点")
    expectation_hook: str = Field(..., description="本章结尾的悬念或下一章期待点")
    involved_character_names: list[str] = Field(default_factory=list, description="预计出场角色")
    pacing_beat: EmotionalPacingBeat | None = Field(default=None, description="本章情绪心电图指标")


class VolumeChapterRange(BaseModel):
    """Inclusive global chapter boundaries; never inferred from a block size."""
    model_config = ConfigDict(extra="forbid")
    start: int = Field(strict=True, ge=1, le=1_000_000)
    end: int = Field(strict=True, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def ordered(self):
        if self.end < self.start:
            raise ValueError("分卷终章不能早于起章")
        return self


class VolumeOutline(BaseModel):
    """分卷大纲定义"""
    volume_number: int = Field(..., ge=1, description="分卷序号")
    volume_title: str = Field(..., description="分卷卷名")
    volume_goal: str = Field(..., description="本卷核心主线目标（如：夺取内门第一、探索血煞秘境）")
    main_antagonist: str = Field(..., description="本卷核心反派/主要阻碍势力")
    volume_climax: str = Field(..., description="本卷最终大决战与高潮剧情设计")
    chapter_range: VolumeChapterRange | None = Field(
        None, description="本卷全局章节范围（含首尾）；新总纲必须提供，旧数据可缺省",
    )
    chapters: list[ChapterOutline] = Field(default_factory=list, description="本卷下辖章节列表")


class GlobalOutline(BaseModel):
    """全书宏观总纲"""
    novel_title: str = Field(..., description="全书书名")
    main_theme: str = Field(..., description="全书核心主题与主角心路历程")
    total_volumes: int = Field(..., ge=1, description="规划总卷数")
    final_climax: str = Field(..., description="全书终局高潮决战设计")
    volumes: list[VolumeOutline] = Field(default_factory=list, description="各卷规划大纲")
    pacing_schedule: PacingSchedule | None = Field(default=None, description="全书情绪心电图总排期表")

    @model_validator(mode="after")
    def valid_declared_ranges(self):
        # Legacy outlines remain readable without a speculative migration.
        # Once ranges are declared, load/import must reject partial or corrupt
        # ownership just as generation does, not silently discard the evidence.
        if not any(volume.chapter_range is not None for volume in self.volumes):
            return self
        if self.total_volumes != len(self.volumes) or any(
            volume.volume_number != index for index, volume in enumerate(self.volumes, 1)
        ):
            raise ValueError("分卷范围必须对应连续且完整的卷号")
        next_start = 1
        for volume in self.volumes:
            span = volume.chapter_range
            if span is None or span.start != next_start:
                raise ValueError("所有分卷范围必须从第 1 章开始连续衔接，不得重叠或缺漏")
            previous = span.start - 1
            for chapter in volume.chapters:
                if chapter.volume_number != volume.volume_number or not previous < chapter.chapter_number <= span.end:
                    raise ValueError("细纲章节与声明的分卷范围不一致或章节重复、无序")
                previous = chapter.chapter_number
            next_start = span.end + 1
        return self


class AuditReport(BaseModel):
    """质检审稿报告"""
    passed: bool = Field(..., description="是否通过质检（若存在致命逻辑错误或严重OOC则为False）")
    consistency_score: float = Field(..., ge=0, le=10, description="设定一致性评分（0~10分）")
    pacing_score: float = Field(..., ge=0, le=10, description="节奏与爽点评分（0~10分）")
    logic_issues: list[str] = Field(default_factory=list, description="发现的逻辑漏洞或吃设定问题")
    character_ooc_issues: list[str] = Field(default_factory=list, description="人物性格OOC或动机脱节问题")
    cliffhanger_detected: bool = Field(..., description="章末是否包含有效悬念/钩子")
    critique: str = Field(..., description="审稿专家综合点评")
    revision_directives: list[str] = Field(default_factory=list, description="针对不合格项的具体重写指导意见")
    text_sha256: str = Field("", description="由本地流水线绑定的待审正文摘要；旧报告为空")
    stage: Literal["legacy", "draft", "polished"] = "legacy"
    attempt: int = Field(0, ge=0)
    service_error: bool = False


class HumanChapterReview(BaseModel):
    """An author decision bound to the exact chapter bytes that were reviewed."""
    decision: Literal["approved", "changes_requested"]
    note: str = Field("", max_length=4000)
    reviewed_at: datetime
    text_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")


class ChapterContent(BaseModel):
    """章节最终文本存储模型"""
    chapter_number: int = Field(..., ge=1, description="章节序号")
    volume_number: int = Field(..., ge=1, description="分卷序号")
    title: str = Field(..., description="章节标题")
    word_count: int = Field(0, ge=0, description="正文字数")
    beats_raw_text: list[str] = Field(default_factory=list, description="各节拍扩写原始文本")
    full_text: str = Field(..., description="经过润色合并后的最终正文")
    cliffhanger_ending: str = Field("", description="章末留钩子片段")
    audit_history: list[AuditReport] = Field(default_factory=list, description="本章质检记录")
    final_audit: AuditReport | None = Field(default=None, description="与当前正文逐字匹配的质检报告")
    human_review_history: list[HumanChapterReview] = Field(default_factory=list, description="作者人工审阅记录")
    current_human_review: HumanChapterReview | None = Field(default=None, description="与当前正文逐字匹配的最近人工审阅")

    def refresh_final_audit(self) -> None:
        digest = hashlib.sha256(self.full_text.encode("utf-8")).hexdigest()
        self.final_audit = next(
            (r.model_copy(deep=True) for r in reversed(self.audit_history) if r.text_sha256 == digest), None,
        )

    def refresh_human_review(self) -> None:
        digest = hashlib.sha256(self.full_text.encode("utf-8")).hexdigest()
        self.current_human_review = next(
            (r.model_copy(deep=True) for r in reversed(self.human_review_history) if r.text_sha256 == digest), None,
        )

    @model_validator(mode="after")
    def verify_audit_identity(self, info: ValidationInfo) -> "ChapterContent":
        # Only the internal read-only metadata view lacks a body to verify.
        if not (info.context and info.context.get("metadata_only")):
            self.refresh_final_audit()
            self.refresh_human_review()
        return self

    @computed_field
    @property
    def audit_status(self) -> str:
        if self.final_audit is None:
            return "unverified"
        if self.final_audit.service_error:
            return "unavailable"
        return "passed" if self.final_audit.passed else "failed"

    @computed_field
    @property
    def human_review_status(self) -> str:
        return self.current_human_review.decision if self.current_human_review else "unreviewed"


class RelationUpdate(BaseModel):
    """本章新增的实体关系边（供知识图谱记录人物/势力/道具间关系）"""
    source: str = Field(..., description="关系主体（通常是主角）")
    target: str = Field(..., description="关系客体（角色/势力/道具名）")
    relation_type: str = Field(..., description="KILLED / ALLIED_WITH / ENEMY_OF / WIELDS / SERVES / MASTER_OF / BETRAYED / BLOOD_FEUD / OWES_DEBT_TO")
    description: str = Field("", description="一句话说明关系由来")


class StateUpdateDelta(BaseModel):
    """章节生成后的增量状态更新模型"""
    character_updates: dict[str, dict[str, Any]] = Field(
        default_factory=dict, 
        description="角色状态变更映射"
    )
    newly_introduced_characters: list[CharacterCard] = Field(default_factory=list, description="本章新增角色列表")
    newly_introduced_items: list[str] = Field(default_factory=list, description="本章新增关键物品")
    new_foreshadowings: list[ForeshadowingRecord] = Field(default_factory=list, description="本章埋下的新伏笔")
    resolved_foreshadowing_ids: list[str] = Field(default_factory=list, description="本章已揭晓/解决的伏笔ID")
    new_relations: list[RelationUpdate] = Field(default_factory=list, description="本章新建立的实体关系")
    current_location: str = Field("", description="当前主角所在最新地点")
    summary_of_events: str = Field(..., description="本章发生事实的极简备忘摘要（用于滚动记忆）")


class CommercialDeliveryPackage(BaseModel):
    """全套商业衍生交付包"""
    novel_title: str = Field(..., description="小说书名")
    tiktok_promo_script: str = Field(..., description="短视频/抖音网文推文黄金前三秒爆款解说脚本")
    ai_cover_prompts: dict[str, str] = Field(default_factory=dict, description="Midjourney/SD 4K 电影级封面与角色立绘 Prompt (中英文)")
    marketing_blurbs: list[str] = Field(default_factory=list, description="平台分发推荐文案与核心看点提炼")
    cost_and_token_estimate: dict[str, Any] = Field(default_factory=dict, description="Token 消耗与成本分析")


class InitialOutlinePlan(BaseModel):
    """Durable request needed to resume the unfinished initial outline only."""
    model_config = ConfigDict(extra="forbid")
    total_volumes: int = Field(strict=True, ge=1, le=20)
    chapters_per_volume: int = Field(strict=True, ge=1, le=100)


class InitialPlanningRequest(BaseModel):
    """Original bounded inputs needed to resume any unfinished planner stage."""
    model_config = ConfigDict(extra="forbid")
    user_idea: str = Field(strict=True, min_length=1, max_length=20_000)
    genre_preference: str = Field(default="", strict=True, max_length=200)
    commercial_target: CommercialTarget
    outline_plan: InitialOutlinePlan

    @model_validator(mode="after")
    def valid_text(self):
        for value, label in (
            (self.user_idea, "核心创意"), (self.genre_preference, "题材"),
        ):
            if "\x00" in value:
                raise ValueError(f"{label}不能包含空字符")
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError(f"{label}包含无效 Unicode 字符") from exc
        if not self.user_idea.strip():
            raise ValueError("核心创意不能为空")
        target = self.commercial_target
        target_texts = (
            target.target_platform, target.author_style, target.pacing_mode,
            target.female_lead_setting, target.protagonist_persona,
        )
        if any(not isinstance(value, str) or not value or len(value) > 200 or "\x00" in value for value in target_texts):
            raise ValueError("商业定位字段必须为 1 到 200 字的有效文本")
        if (
            len(target.avoided_tropes) > 50
            or any(
                not isinstance(item, str) or not item.strip() or len(item) > 200 or "\x00" in item
                for item in target.avoided_tropes
            )
        ):
            raise ValueError("避坑项最多 50 条，每条必须为 1 到 200 字的有效文本")
        return self


class InitialPlanningCheckpoint(BaseModel):
    """Durable paid-stage results before they become the canonical foundation."""
    model_config = ConfigDict(extra="forbid")
    request: InitialPlanningRequest
    concept: NovelConcept | None = None
    world_setting: WorldSetting | None = None
    initial_characters: list[CharacterCard] | None = None

    @model_validator(mode="after")
    def ordered_stages(self):
        if self.world_setting is not None and self.concept is None:
            raise ValueError("世界观检查点缺少前序概念策划")
        if self.initial_characters is not None and self.world_setting is None:
            raise ValueError("人物检查点缺少前序世界观")
        if self.initial_characters is not None:
            names = [character.name.strip() for character in self.initial_characters]
            ids = [character.id.strip() for character in self.initial_characters]
            if not names or not all(names) or not all(ids) or len(set(names)) != len(names) or len(set(ids)) != len(ids):
                raise ValueError("人物检查点为空或包含重复、空白身份")
        return self

    @property
    def next_stage(self) -> Literal["concept", "world", "cast", "promotion"]:
        if self.concept is None:
            return "concept"
        if self.world_setting is None:
            return "world"
        if self.initial_characters is None:
            return "cast"
        return "promotion"


class NovelProjectState(BaseModel):
    """整部小说的全局持久化状态快照（支持历史快照时光机回滚）"""
    novel_id: str
    concept: NovelConcept | None = None
    world_setting: WorldSetting | None = None
    global_outline: GlobalOutline | None = None
    pending_initial_planning: InitialPlanningCheckpoint | None = None
    pending_outline_plan: InitialOutlinePlan | None = None
    characters: dict[str, CharacterCard] = Field(default_factory=dict)
    foreshadowings: dict[str, ForeshadowingRecord] = Field(default_factory=dict)
    chapters: dict[int, ChapterContent] = Field(default_factory=dict)
    recent_chapter_summaries: list[str] = Field(default_factory=list, description="最近3章的滚动事件摘要")
    current_volume_idx: int = Field(1, ge=1)
    current_chapter_idx: int = Field(0, ge=0)
    delivery_package: CommercialDeliveryPackage | None = None

    @model_validator(mode="after")
    def valid_pending_plan(self):
        if self.pending_initial_planning is not None and (
            self.concept or self.world_setting or self.characters or self.global_outline
            or self.pending_outline_plan is not None or self.chapters or self.current_chapter_idx
        ):
            raise ValueError("分阶段主策划检查点不能与正式设定、总纲或正文并存")
        if self.pending_outline_plan is not None and (
            not self.concept or not self.world_setting or not self.characters
            or self.global_outline is not None or self.chapters or self.current_chapter_idx
        ):
            raise ValueError("未完成总纲标记必须对应完整初始设定，且不能已有总纲或正文")
        return self
