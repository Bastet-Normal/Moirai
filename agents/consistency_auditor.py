from agents.base import BaseAgent
from config.settings import get_settings
from core.chapter_context import format_chapter_facts
from core.schemas import AuditReport


class ConsistencyAuditorAgent(BaseAgent):
    """逻辑与设定质检 Agent：专门审查章节中是否存在吃设定、战力崩溃、人物OOC、逻辑死结及缺乏章末钩子的问题"""

    def __init__(self, model_name: str = None):
        settings = get_settings()
        system_prompt = """你是一位极端挑剔的网文总编与质检专家。
你的职责是严防死守网文质量，挑出正文中所有可能导致读者毒发弃书的逻辑漏洞与人设崩塌：
1. 【战力与规则一致性】：核对战力等级，是否有无缘无故越级碾压或违背世界观铁律的行为。
2. 【人物性格一致性（防OOC）】：主角是否违背杀伐果断/隐忍人设？反派动机是否合乎利益逻辑？
3. 【吃设定检验】：是否出现了死亡角色复活、道具凭空出现或遗忘已有底牌的情况。
4. 【爽点与期待兑现】：打脸反转是否具有逻辑说服力？收获是否明确？
5. 【断章悬念审查】：章节结尾是否停留在悬念最顶点？

如果存在严重硬伤，必须判定 `passed = false`，并在 `revision_directives` 中给出精准的修改指示。若整体流畅且无严重毒点，则 `passed = true`。"""
        super().__init__(
            name="ConsistencyAuditor",
            system_prompt=system_prompt,
            model_name=model_name or settings.CRITIC_MODEL,
        )

    def audit_chapter(
        self,
        chapter_number: int,
        chapter_title: str,
        chapter_text: str,
        world_rules: list[str],
        characters_state: list[dict],
        main_conflict: str,
        chapter_context: dict | None = None,
    ) -> AuditReport:
        """对全章生成正文进行全面逻辑与质量质检"""
        rules_text = "\n".join([f"- {r}" for r in world_rules]) or "无特殊禁忌"
        chars_text = "\n".join([
            f"- {c['name']} (境界: {c.get('level')}, 装备: {c.get('items')}, 性格: {c.get('personality')})"
            for c in characters_state
        ])

        prompt = f"""【正在质检的章节】：第 {chapter_number} 章 《{chapter_title}》
【本章设定目标冲突】：{main_conflict}

【世界观规则约束】：
{rules_text}

【登场角色当前合规状态】：
{chars_text}

【前序事实与关系图谱（必须核对死亡、道具和伏笔状态）】：
{format_chapter_facts(chapter_context)}

【章节待审正文内容】：
\"\"\"{chapter_text}\"\"\"

请从【设定一致性】、【战力逻辑】、【人物OOC】、【爽感节奏】、【章末钩子】五个维度进行最严格的审查，并输出 AuditReport 结构化质检报告。"""

        report = self.call_structured(prompt, AuditReport)
        return self._apply_strictness(report)

    def _apply_strictness(self, report: AuditReport) -> AuditReport:
        """按严格度档位后处理判定：阈值之下强制打回，之上放行（模型判定偏严/偏松的纠偏）"""
        thresholds = {"strict": 8.5, "standard": 7.0, "lenient": 5.5}
        level = (get_settings().AUDIT_STRICTNESS or "standard").lower()
        if level not in thresholds:
            level = "standard"
        avg = (report.consistency_score + report.pacing_score) / 2
        has_issues = bool(report.logic_issues or report.character_ooc_issues or report.revision_directives)
        if not report.passed and not has_issues and avg >= thresholds[level]:
            report.passed = True
        elif report.passed and avg < thresholds[level] - 1.5:
            report.passed = False
        return report
