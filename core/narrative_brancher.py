
from pydantic import BaseModel, Field

from core.llm import LLMClient, get_llm_client
from core.schemas import CharacterCard, NovelConcept, WorldSetting


class PlotBranchCandidate(BaseModel):
    """单个剧情分支候选方案"""
    branch_id: str = Field(..., description="分支代号: A / B / C")
    branch_title: str = Field(..., description="分支名称 (如: 杀伐果断·雷霆灭门 / 隐忍布局·借刀杀人 / 金蝉脱壳·远赴新图)")
    narrative_strategy: str = Field(..., description="具体执行策略与剧情发展推演 (200~300字)")
    immediate_conflict: str = Field(..., description="直接引发的即时冲突与交锋")
    dopamine_score: int = Field(80, description="爽感回报与即时反馈得分 (0~100)")
    logic_consistency_score: int = Field(90, description="战力与逻辑自洽性得分 (0~100)")
    longterm_potential_score: int = Field(85, description="长线伏笔与世界观扩展潜力 (0~100)")
    overall_score: float = Field(85.0, description="加权综合推荐分")
    recommendation_reason: str = Field(..., description="编剧专家推荐理由与潜在毒点避坑分析")


class BranchingSimulationResult(BaseModel):
    """多分支沙盒推演结果"""
    decision_point_description: str = Field(..., description="当前面临的命运十字路口与生死抉择核心")
    candidates: list[PlotBranchCandidate] = Field(default_factory=list, description="3个不同维度的分支推演方案")
    selected_branch_id: str = Field("A", description="系统综合评分最高推荐采纳的分支")


class NarrativeBrancher:
    """Tree-of-Thoughts 剧情分支沙盒推演引擎"""

    def __init__(self, llm_client: LLMClient | None = None):
        self.llm_client = llm_client or get_llm_client()

    def simulate_branches(
        self,
        chapter_number: int,
        novel_concept: NovelConcept,
        world_setting: WorldSetting,
        characters: list[CharacterCard],
        current_situation: str,
    ) -> BranchingSimulationResult:
        """在剧情转折点同时推演 3 种完全不同的命运走向"""
        chars_summary = "\n".join([
            f"- {c.name} ({c.role}): 境界={c.current_state.level}, 性格={','.join(c.personality_tags)}"
            for c in characters
        ])

        system_prompt = """你是一位精通博弈论与多线叙事架构的白金级总编剧。
你的任务是在小说主人公面临重大抉择的命运关口，运用 Tree-of-Thoughts（思维树）推演出 3 条截然不同但皆具备极致商业价值的剧情分支：
- 分支 A（刚猛无敌/杀伐果断）：以绝对力量或极速手段正面碾碎敌人，给予读者最直接的代偿爽感。
- 分支 B（智谋布局/借力打力）：运用信息差与多方势力博弈，设局坑杀强敌，坐收渔翁之利。
- 分支 C（金蝉脱壳/反套路破局）：打破常规逻辑，以出人意料的方式化解危局，顺势引出全新大地图或隐秘副本。

对每个分支严格从【爽感回报】、【战力自洽】、【长线扩展】三个维度进行深度评分并给出综合推荐。"""

        prompt = f"""【小说信息】：
- 书名：《{novel_concept.title}》
- 题材：{novel_concept.genre}
- 金手指机制：{novel_concept.golden_finger}

【当前登场角色状态】：
{chars_summary}

【当前面临的重大转折危机（第 {chapter_number} 章）】：
{current_situation}

请深度推演 3 个具有颠覆性的剧情分支，并输出符合 BranchingSimulationResult 的结构化 JSON。"""

        return self.llm_client.generate_structured(
            messages=[{"role": "user", "content": prompt}],
            response_model=BranchingSimulationResult,
            system_prompt=system_prompt,
        )
