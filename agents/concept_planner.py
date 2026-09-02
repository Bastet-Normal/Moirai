
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field

from agents.base import BaseAgent
from config.author_styles import AUTHOR_STYLE_PRESETS
from config.platform_presets import PLATFORM_PRESETS, TABOO_PRESETS
from config.settings import get_settings
from core.schemas import CharacterCard, CommercialTarget, NovelConcept, WorldSetting


class InitialNovelPackage(BaseModel):
    """初版小说设定打包数据"""
    concept: NovelConcept
    world_setting: WorldSetting
    initial_characters: list[CharacterCard] = Field(..., description="初始核心角色人设（包含主角、初始反派、重要盟友/导师等）")


class InitialCast(BaseModel):
    """初始角色阵容（独立小 Schema，降低单次生成体积）"""
    characters: list[CharacterCard] = Field(..., description="初始核心角色：主角、初始冲突反派、以及一位重要导师或关键配角")


class ConceptPlannerAgent(BaseAgent):
    """主策划与世界观 Agent：负责将用户的一句话灵感转化为符合平台商业标准的策划案、金手指机制、世界观与人设"""

    def __init__(self, model_name: str = None):
        settings = get_settings()
        system_prompt = """你是一位拥有十年经验的白金级网络小说总策划、商业总编剧。
你精通各大头部网文平台的定向商业打法（番茄/七猫极速脑洞、起点严谨群像、海外LitRPG）。

你的任务是根据用户的创意核心与商业化定制要求，深度策划出一部具备商业爆款潜力的网文小说全套基础设定：
1. 【严格遵守避坑指南与硬约束】：
   - 严禁虐主、反派绝不无脑降智、主角行事杀伐果断、严禁送女狗血。
2. 【核心卖点与金手指】：金手指机制必须规则清晰、具有不对称优势与极速正反馈。
3. 【战力等级体系】：境界阶梯清晰，每个境界有明确的突破门槛与质变表现。
4. 【主角与配角】：主角动机必须极其强烈，反派智商在线合乎利益逻辑。"""
        super().__init__(
            name="ConceptPlanner",
            system_prompt=system_prompt,
            model_name=model_name or settings.PLANNER_MODEL,
        )

    def plan_novel(
        self,
        user_idea: str,
        genre_preference: str = "",
        commercial_target: CommercialTarget | None = None,
        *,
        concept: NovelConcept | None = None,
        world_setting: WorldSetting | None = None,
        initial_characters: list[CharacterCard] | None = None,
        stage_callback: Callable[
            [Literal["concept", "world", "cast"], NovelConcept | WorldSetting | list[CharacterCard]], None,
        ] | None = None,
    ) -> InitialNovelPackage:
        """从用户的一句话灵感与商业化定制参数，分三步生成策划案、世界观与初始阵容

        拆分为三次小体量结构化调用：单次输出过大会触发中转网关超时（504），
        且小 Schema 的模型遵从度显著更高。
        """
        target = commercial_target or CommercialTarget()
        platform_info = PLATFORM_PRESETS.get(target.target_platform, PLATFORM_PRESETS["GENERAL"])
        style_info = AUTHOR_STYLE_PRESETS.get(target.author_style, AUTHOR_STYLE_PRESETS["FANQIE_DOPAMINE"])

        taboos_text = "\n".join([f"- {t}" for t in (target.avoided_tropes or TABOO_PRESETS)])
        platform_features = "\n".join([f"- {f}" for f in platform_info["pacing_features"]])

        shared_header = f"""【用户核心创意/脑洞】：
{user_idea}

【商业化平台定位】：
- 目标平台模式：{platform_info['name']} ({platform_info['description']})
- 主角人设定位：{target.protagonist_persona}
- 女主设定模式：{target.female_lead_setting}
- 核心题材偏好：{genre_preference or '根据创意自动匹配最优商业题材'}
- 文风方向：{style_info['name']}
- 文风执行要求：{style_info['system_instruction']}

【平台节奏硬要求】：
{platform_features}

【严防毒点与避坑硬约束（违背将直接判定不合格）】：
{taboos_text}"""

        # Completed inputs are durable checkpoints supplied by the pipeline.
        # They are copied so prompt assembly and callers cannot mutate evidence.
        concept_missing = concept is None
        concept = concept.model_copy(deep=True) if concept is not None else self.call_structured(
            shared_header + "\n\n请策划这部小说的核心策划案（书名、题材、一句话卖点、金手指机制与运作规则、核心爽点、基调、受众、终极大冲突），输出 NovelConcept JSON。",
            NovelConcept,
        )
        concept.commercial_target = target
        if stage_callback is not None and concept_missing:
            stage_callback("concept", concept.model_copy(deep=True))

        # Step 2: 世界观与战力体系（依托已定策划）
        world_missing = world_setting is None
        world = world_setting.model_copy(deep=True) if world_setting is not None else self.call_structured(
            shared_header + f"""

【已定核心策划案】：
- 书名：《{concept.title}》（{concept.genre}）
- 核心简介：{concept.logline}
- 金手指：{concept.golden_finger}
- 终极冲突：{concept.core_conflict}

请为该策划设计配套世界观：世界名称、地理版图、6~8 级战力进阶体系（每级含描述与突破条件）、3~4 个势力阵营、世界铁律、一条远古秘辛，输出 WorldSetting JSON。""",
            WorldSetting,
        )
        if stage_callback is not None and world_missing:
            stage_callback("world", world.model_copy(deep=True))

        # Step 3: 初始角色阵容（依托策划与世界观）
        cast = InitialCast(characters=[item.model_copy(deep=True) for item in initial_characters]) if initial_characters is not None else self.call_structured(
            shared_header + f"""

【已定核心策划案】：
- 书名：《{concept.title}》（{concept.genre}）
- 核心简介：{concept.logline}
- 金手指：{concept.golden_finger}
- 终极冲突：{concept.core_conflict}
- 世界名称：{world.world_name}

请设计至少 3 位初始核心人物（主角、初始冲突反派、一位重要导师或关键配角）：含姓名、角色定位、性格标签、身世背景、说话口吻、初始状态与深层秘密，输出 InitialCast JSON。""",
            InitialCast,
        )
        if stage_callback is not None and initial_characters is None:
            stage_callback("cast", [item.model_copy(deep=True) for item in cast.characters])

        return InitialNovelPackage(concept=concept, world_setting=world, initial_characters=cast.characters)

