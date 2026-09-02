from typing import Any

from agents.base import BaseAgent
from config.settings import get_settings
from core.schemas import StateUpdateDelta


class StateUpdaterAgent(BaseAgent):
    """状态与记忆更新管家 Agent：负责阅读刚刚完成的章节，自动提取人物状态变更、新出场角色、道具增减与剧情伏笔账本"""

    def __init__(self, model_name: str = None):
        settings = get_settings()
        system_prompt = """你是一位拥有极其严谨逻辑与强迫症级别记忆力的小说设定管家。
你的职责是精准提取最新章节中发生的一切事实性改变，以维护整个小说宇宙的状态机数据库：
1. 【角色状态变更】(character_updates)：键为角色名，值只允许使用以下字段名：
   - level (境界)、location (所在地点)、hp_status (伤势)、current_mood (心理状态)
   - equipment_and_items (新获得的重要道具列表，只列新增项)
   - skills_and_abilities (新掌握的技能/功法列表，只列新增项)
   - is_alive (bool，若角色死亡必须置为 false)
2. 【current_location】：必须填写本章结束时主角所在的确切地点。
3. 【新登场角色】：提取名字、角色定位、性格特点及初始实力。
4. 【实体关系边】(new_relations)：本章新建立/显著推进的关系（结仇、结盟、拜师、获得法宝归属、背叛、血仇等），从主角视角或明确方向填写。
5. 【剧情伏笔追踪】：
   - 若本章埋下了新的秘密、神秘线索或未解危机，生成新的 ForeshadowingRecord。
   - 若本章成功揭晓了之前的伏笔，将其 hook_id 填入 resolved_foreshadowing_ids。
6. 【滚动事件摘要】(summary_of_events)：用 1~2 句话提炼本章核心事实（作为后续章节记忆参考）。"""
        super().__init__(
            name="StateUpdater",
            system_prompt=system_prompt,
            model_name=model_name or settings.STATE_MODEL,
        )

    def extract_state_delta(
        self,
        chapter_number: int,
        chapter_title: str,
        chapter_text: str,
        current_characters: dict[str, Any],
        active_foreshadowings: list[dict[str, Any]],
    ) -> StateUpdateDelta:
        """从最新章节正文中提取状态增量更新"""
        chars_summary = "\n".join([
            f"- {name}: 境界={data.get('level')}, 位置={data.get('location')}, 装备={data.get('items')}, 伤势={data.get('hp_status')}"
            for name, data in current_characters.items()
        ])

        hooks_summary = "\n".join([
            f"- [ID: {h['id']}] {h['title']}: {h['description']}"
            for h in active_foreshadowings
        ]) or "暂无未解决伏笔"

        prompt = f"""【最新完成章节】：第 {chapter_number} 章 《{chapter_title}》

【本章正文内容】：
\"\"\"{chapter_text}\"\"\"

【上一章结束时的已有角色状态】：
{chars_summary}

【当前待追踪的活跃伏笔】：
{hooks_summary}

请深度分析本章正文，提取所有状态变动（注意填写 current_location 与角色 location 字段）、新增角色、新增道具、伏笔账本更新及 1~2 句事实摘要，输出 StateUpdateDelta JSON。"""

        return self.call_structured(prompt, StateUpdateDelta)
