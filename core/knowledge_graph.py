import hashlib
from typing import Any

from pydantic import BaseModel, Field


class EntityNode(BaseModel):
    """知识图谱实体节点"""
    id: str
    name: str
    entity_type: str = Field("CHARACTER", description="CHARACTER / FACTION / ITEM / LOCATION / SECRET / EVENT")
    properties: dict[str, Any] = Field(default_factory=dict)
    first_seen_chapter: int = 1
    status: str = Field("ACTIVE", description="ACTIVE / DEAD / DESTROYED / RESOLVED")


class RelationEdge(BaseModel):
    """实体间关系有向边"""
    source_name: str
    target_name: str
    relation_type: str = Field(..., description="KILLED / ALLIED_WITH / OWES_DEBT_TO / HIDDEN_IN / WIELDS / SERVES / MASTER_OF / BETRAYED / ENEMY_OF / BLOOD_FEUD")
    description: str = ""
    established_chapter: int = 1
    is_active: bool = True


class KnowledgeGraph(BaseModel):
    """小说世界观与人物动态实体关系图谱"""
    nodes: dict[str, EntityNode] = Field(default_factory=dict)
    edges: list[RelationEdge] = Field(default_factory=list)

    def upsert_node(self, name: str, entity_type: str = "CHARACTER", properties: dict[str, Any] | None = None, chapter: int = 1):
        """插入或更新实体节点"""
        if name not in self.nodes:
            self.nodes[name] = EntityNode(
                # Stable across prune/reload and immune to sequential-ID reuse.
                id=f"ent_{hashlib.sha256(name.encode('utf-8')).hexdigest()[:16]}",
                name=name,
                entity_type=entity_type,
                properties=properties or {},
                first_seen_chapter=chapter,
            )
        else:
            if properties:
                self.nodes[name].properties.update(properties)

    def add_relation(self, source: str, target: str, rel_type: str, description: str = "", chapter: int = 1):
        """添加或更新实体关系"""
        # 确保端点存在
        if source not in self.nodes:
            self.upsert_node(source, "CHARACTER", chapter=chapter)
        if target not in self.nodes:
            self.upsert_node(target, "CHARACTER", chapter=chapter)

        # 检查是否已存在同类型活跃关系
        for e in self.edges:
            if e.source_name == source and e.target_name == target and e.relation_type == rel_type:
                e.description = description
                e.established_chapter = min(e.established_chapter, chapter)
                e.is_active = True
                return

        self.edges.append(RelationEdge(
            source_name=source,
            target_name=target,
            relation_type=rel_type,
            description=description,
            established_chapter=chapter,
            is_active=True,
        ))

        # 若关系为击杀，则将目标节点标记为死亡
        if rel_type.upper() in ["KILLED", "BEHEADED", "EXTERMINATED"] and target in self.nodes:
            self.nodes[target].status = "DEAD"

    def prune_after_chapter(self, keep_up_to_chapter: int):
        """修剪指定章节之后产生的节点与关系（时光机回滚时使用，保证图谱与回滚后剧情一致）"""
        removed_names = {
            name for name, node in self.nodes.items()
            if node.first_seen_chapter > keep_up_to_chapter
        }
        for name in removed_names:
            del self.nodes[name]
        self.edges = [
            e for e in self.edges
            if e.established_chapter <= keep_up_to_chapter
            and e.source_name not in removed_names
            and e.target_name not in removed_names
        ]

    def get_related_entities(self, entity_names: list[str], max_depth: int = 1) -> set[str]:
        """获取与指定实体列表相关联的所有实体名称"""
        result = set(entity_names)
        current_layer = set(entity_names)

        for _ in range(max_depth):
            next_layer = set()
            for e in self.edges:
                if not e.is_active:
                    continue
                if e.source_name in current_layer:
                    next_layer.add(e.target_name)
                if e.target_name in current_layer:
                    next_layer.add(e.source_name)
            new_additions = next_layer - result
            if not new_additions:
                break
            result.update(new_additions)
            current_layer = new_additions

        return result

    def extract_prompt_subgraph(self, involved_entities: list[str]) -> str:
        """提取指定出场实体相关的关系子图，转换为 LLM Prompt 注入文本"""
        if not involved_entities:
            return "暂无图谱关联"

        all_relevant = self.get_related_entities(involved_entities, max_depth=1)
        sub_edges = [
            e for e in self.edges
            if e.is_active and (e.source_name in all_relevant and e.target_name in all_relevant)
        ]

        lines = []
        # 节点状态
        for name in all_relevant:
            if name in self.nodes:
                node = self.nodes[name]
                status_str = " [已死亡/已毁灭]" if node.status == "DEAD" else ""
                lines.append(f"- 实体: {name} ({node.entity_type}){status_str}")

        # 关系边
        for edge in sub_edges:
            desc = f" ({edge.description})" if edge.description else ""
            lines.append(f"  └─ [{edge.source_name}] ──({edge.relation_type})──► [{edge.target_name}]{desc}")

        return "\n".join(lines) if lines else "暂无直接关系网"

    def export_cytoscape_data(self) -> dict[str, Any]:
        """导出用于前端可视化图谱渲染的数据结构"""
        elements = [
            {
                "data": {
                    "id": node.name,
                    "label": node.name,
                    "type": node.entity_type,
                    "status": node.status,
                }
            }
            for node in self.nodes.values()
        ]

        for idx, edge in enumerate(self.edges):
            if edge.is_active:
                elements.append({
                    "data": {
                        "id": f"e_{idx}",
                        "source": edge.source_name,
                        "target": edge.target_name,
                        "label": edge.relation_type,
                        "desc": edge.description,
                    }
                })

        return {"elements": elements}
