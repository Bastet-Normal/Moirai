import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from agents.beat_director import BeatDirectorAgent
from config.compliance_words import scan_text
from config.settings import get_output_dir, get_settings
from core.knowledge_graph import KnowledgeGraph
from core.llm import clean_json_string
from core.multimodal_generator import AudioDramaPackage, ShortDramaScript
from core.narrative_brancher import BranchingSimulationResult, PlotBranchCandidate
from core.reader_simulator import BulletComment, ReaderSimulationReport
from core.schemas import (
    ChapterContent,
    CharacterCard,
    NovelConcept,
    StateUpdateDelta,
)
from core.semantic_memory import SemanticMemoryIndex
from core.state_manager import StateManager
from pipeline.novel_pipeline import NovelPipeline


class IsolatedProjectTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="novelengine-test-")
        self.addCleanup(temporary.cleanup)
        self.addCleanup(get_settings.cache_clear)
        self.enterContext(unittest.mock.patch.dict(os.environ, {"OUTPUT_DIR": temporary.name}))
        get_settings.cache_clear()
        self.test_base_dir = Path(temporary.name) / "explicit_projects"


class TestNovelEngineSuite(IsolatedProjectTestCase):

    def setUp(self):
        super().setUp()
        self.test_novel_id = "test_epic_novel"

    def test_knowledge_graph_operations(self):
        """测试实体知识图谱增删查改与死亡状态级联"""
        kg = KnowledgeGraph()
        kg.upsert_node("楚封", "CHARACTER", {"role": "主角", "level": "练气三层"})
        kg.upsert_node("赵管事", "CHARACTER", {"role": "反派", "level": "练气五层"})
        kg.upsert_node("九转吞天诀", "ITEM", {"grade": "天阶功法"})

        kg.add_relation("楚封", "九转吞天诀", "WIELDS", "第1章获得", chapter=1)
        kg.add_relation("赵管事", "楚封", "ENEMY_OF", "矿场剥削仇怨", chapter=1)

        self.assertEqual(len(kg.nodes), 3)
        self.assertEqual(len(kg.edges), 2)

        # 测试子图上下文提取
        subgraph_text = kg.extract_prompt_subgraph(["楚封"])
        self.assertIn("楚封", subgraph_text)
        self.assertIn("九转吞天诀", subgraph_text)

        # 测试击杀级联
        kg.add_relation("楚封", "赵管事", "KILLED", "第2章击杀", chapter=2)
        self.assertEqual(kg.nodes["赵管事"].status, "DEAD")

        cyto = kg.export_cytoscape_data()
        self.assertTrue(len(cyto["elements"]) >= 5)

    def test_semantic_memory_and_reader_simulation(self):
        """测试长程语义记忆检索与读者心智模拟"""
        mem_idx = SemanticMemoryIndex()
        mem_idx.add_chapter_memory(
            chapter_number=1,
            volume_number=1,
            chapter_title="矿坑获宝",
            full_text="楚封获得了天阶法宝九转吞天鼎，威力惊人。\n\n赵管事贪婪逼迫，被楚封一剑逼退。",
            involved_entities=["楚封", "赵管事", "九转吞天鼎"],
        )
        self.assertTrue(len(mem_idx.chunks) >= 1)

        # 跨章检索
        results = mem_idx.search_relevant_memories(
            query="天阶法宝九转吞天鼎",
            target_entities=["楚封"],
            top_k=1,
            exclude_recent_n_chapters=0,
            current_chapter=2,
        )
        self.assertEqual(len(results), 1)
        self.assertIn("九转吞天鼎", results[0].content)

        # 读者弹幕
        comment = BulletComment(
            paragraph_index=1,
            reader_name="老书虫",
            sentiment="POSITIVE",
            content="这波反杀极度舒适！",
        )
        report = ReaderSimulationReport(
            chapter_number=1,
            predicted_completion_rate=92.5,
            three_chapter_retention_score=88.0,
            bullet_comments=[comment],
            drop_off_risks=[],
            overall_sentiment_summary="节奏明快，极具追读欲望",
        )
        self.assertEqual(report.predicted_completion_rate, 92.5)

    def test_state_manager_persistence_and_rollback(self):
        """测试状态管理、快照与时光机回滚与段落微手术"""
        sm = StateManager(novel_id=self.test_novel_id, base_dir=str(self.test_base_dir))
        
        concept = NovelConcept(
            title="生死推演录",
            genre="玄幻脑洞",
            logline="触摸尸体推演记忆",
            selling_points=["快节奏", "不憋屈"],
            golden_finger="生死回放",
            core_conflict="底层矿奴反抗宗门霸权与诸天黑手",
            core_taboos=["严禁虐主"],
        )
        sm.set_concept(concept)

        chap1 = ChapterContent(
            chapter_number=1,
            volume_number=1,
            title="矿道惊魂",
            word_count=2200,
            beats_raw_text=["节拍1", "节拍2"],
            full_text="楚封站在矿道深处，眼神冰冷。\n\n赵管事冷笑连连。",
            cliffhanger_ending="暗处的眼睛缓缓睁开！",
        )
        delta1 = StateUpdateDelta(
            summary_of_events="楚封激活推演系统，突破练气三层",
            character_updates={"楚封": {"level": "练气三层", "location": "废弃矿道"}},
            newly_introduced_characters=[],
            newly_introduced_items=["残破骨片"],
            new_foreshadowings=[],
            resolved_foreshadowing_ids=[],
        )
        sm.save_chapter(chap1, delta1)

        # 测试段落微手术
        patch_ok = sm.patch_paragraph(1, "眼神冰冷", "眼中杀意如雷")
        self.assertTrue(patch_ok)
        self.assertIn("眼中杀意如雷", sm.state.chapters[1].full_text)

        # 保存第 2 章
        chap2 = ChapterContent(
            chapter_number=2,
            volume_number=1,
            title="雷霆斩杀",
            word_count=2300,
            beats_raw_text=["节拍1", "节拍2"],
            full_text="剑气激荡……",
            cliffhanger_ending="执法堂警钟敲响！",
        )
        sm.save_chapter(chap2)
        self.assertEqual(sm.state.current_chapter_idx, 2)

        # 回滚至第 1 章
        rollback_ok = sm.rollback_to_chapter(1)
        self.assertTrue(rollback_ok)
        self.assertEqual(sm.state.current_chapter_idx, 1)


    def test_semantic_memory_replace_and_prune(self):
        """测试记忆块重建（防重复堆积）与按章修剪"""
        idx = SemanticMemoryIndex()
        idx.add_chapter_memory(1, 1, "旧标题", "旧版本文本甲\n\n旧版本文本乙", ["楚封"])
        idx.replace_chapter_memory(1, 1, "新标题", "新版本文本丙\n\n新版本文本丁", ["楚封"])

        all_content = " ".join(c.content for c in idx.chunks)
        self.assertNotIn("旧版本文本甲", all_content)
        self.assertIn("新版本文本丙", all_content)
        # 同章记忆块不因重写而翻倍
        chunk_ids = [c.chunk_id for c in idx.chunks]
        self.assertEqual(len(chunk_ids), len(set(chunk_ids)))

        idx.add_chapter_memory(5, 1, "第五章", "未来剧情", ["楚封"])
        idx.prune_after_chapter(1)
        self.assertTrue(all(c.chapter_number <= 1 for c in idx.chunks))

    def test_knowledge_graph_prune_after_chapter(self):
        """测试图谱按章修剪：删除后置章节的节点与关联边"""
        kg = KnowledgeGraph()
        kg.upsert_node("楚封", "CHARACTER", chapter=1)
        kg.upsert_node("残破骨片", "ITEM", chapter=1)
        kg.upsert_node("神秘黑袍人", "CHARACTER", chapter=5)
        kg.add_relation("楚封", "残破骨片", "WIELDS", "第1章获得", chapter=1)
        kg.add_relation("神秘黑袍人", "楚封", "ENEMY_OF", "第5章结仇", chapter=5)

        kg.prune_after_chapter(1)
        self.assertNotIn("神秘黑袍人", kg.nodes)
        self.assertIn("楚封", kg.nodes)
        self.assertTrue(all(e.established_chapter <= 1 for e in kg.edges))

    def test_snapshot_v2_rollback_restores_graph_and_memory(self):
        """测试快照 v2 回滚：全局状态、知识图谱与语义记忆三者一致还原"""
        sm = StateManager(novel_id="test_rollback_v2", base_dir=str(self.test_base_dir))
        sm.set_concept(NovelConcept(
            title="回滚测试录", genre="玄幻", logline="测试", golden_finger="测试",
            core_conflict="测试",
        ))

        def make_chap(num: int) -> ChapterContent:
            return ChapterContent(
                chapter_number=num, volume_number=1, title=f"第{num}章", word_count=100,
                beats_raw_text=[], full_text=f"第{num}章正文内容。", cliffhanger_ending="钩子",
            )

        delta1 = StateUpdateDelta(summary_of_events="获得骨片", newly_introduced_items=["残破骨片"])
        sm.save_chapter(make_chap(1), delta1)
        delta2 = StateUpdateDelta(summary_of_events="遭遇黑袍人", newly_introduced_items=["噬魂铃"])
        sm.save_chapter(make_chap(2), delta2)

        self.assertIn("噬魂铃", sm.knowledge_graph.nodes)
        self.assertTrue(any(c.chapter_number == 2 for c in sm.semantic_memory.chunks))
        self.assertEqual(sm.state.current_chapter_idx, 2)

        self.assertTrue(sm.rollback_to_chapter(1))
        # 图谱与记忆必须随状态一起回退
        self.assertNotIn("噬魂铃", sm.knowledge_graph.nodes)
        self.assertIn("残破骨片", sm.knowledge_graph.nodes)
        self.assertFalse(any(c.chapter_number == 2 for c in sm.semantic_memory.chunks))
        self.assertEqual(sm.state.current_chapter_idx, 1)
        self.assertFalse((self.test_base_dir / "test_rollback_v2" / "chapters" / "chapter_002.json").exists())

    def test_snapshot_v1_legacy_compat(self):
        """测试旧版 v1 快照（仅状态）的兼容回滚：图谱与记忆按章修剪"""
        sm = StateManager(novel_id="test_rollback_v1", base_dir=str(self.test_base_dir))
        sm.set_concept(NovelConcept(
            title="旧版快照录", genre="玄幻", logline="测试", golden_finger="测试",
            core_conflict="测试",
        ))

        chap1 = ChapterContent(chapter_number=1, volume_number=1, title="壹", word_count=10,
                               beats_raw_text=[], full_text="壹的内容。", cliffhanger_ending="")
        chap2 = ChapterContent(chapter_number=2, volume_number=1, title="贰", word_count=10,
                               beats_raw_text=[], full_text="贰的内容。", cliffhanger_ending="")
        sm.save_chapter(chap1)
        sm.save_chapter(chap2)

        # 手工伪造一个 v1 格式快照（仅含 state，且内容取自第 1 章保存后的状态）
        v2_snapshot_file = self.test_base_dir / "test_rollback_v1" / "snapshots" / "snapshot_after_chap_001.json"
        with open(v2_snapshot_file, encoding="utf-8") as f:
            legacy = json.load(f)["state"]
        legacy_file = v2_snapshot_file
        with open(legacy_file, "w", encoding="utf-8") as f:
            json.dump(legacy, f, ensure_ascii=False)

        self.assertTrue(sm.rollback_to_chapter(1))
        self.assertEqual(sm.state.current_chapter_idx, 1)
        self.assertFalse(any(c.chapter_number > 1 for c in sm.semantic_memory.chunks))

    def test_apply_state_delta_current_location(self):
        """测试 current_location 字段落地：无显式 location 时兜底同步，显式指定时不覆盖"""
        sm = StateManager(novel_id="test_location", base_dir=str(self.test_base_dir))
        sm.upsert_character(CharacterCard(
            id="char_001", name="楚封", role="主角", background="矿奴出身",
        ))

        delta1 = StateUpdateDelta(
            summary_of_events="抵达坊市",
            character_updates={"楚封": {"level": "练气四层"}},
            current_location="青云坊市",
        )
        sm.apply_state_delta(delta1, 1)
        self.assertEqual(sm.state.characters["楚封"].current_state.location, "青云坊市")
        self.assertEqual(sm.state.characters["楚封"].current_state.level, "练气四层")

        delta2 = StateUpdateDelta(
            summary_of_events="返回宗门",
            character_updates={"楚封": {"location": "青云宗大殿", "level": "练气五层"}},
            current_location="青云坊市",
        )
        sm.apply_state_delta(delta2, 2)
        # 显式指定的 location 优先于 current_location 兜底
        self.assertEqual(sm.state.characters["楚封"].current_state.location, "青云宗大殿")

    def test_apply_state_delta_current_location_falls_back_to_protagonist(self):
        """无角色更新时 current_location 落到主角"""
        sm = StateManager(novel_id="test_location2", base_dir=str(self.test_base_dir))
        sm.upsert_character(CharacterCard(
            id="char_001", name="楚封", role="主角", background="矿奴出身",
        ))
        delta = StateUpdateDelta(summary_of_events="赶路", current_location="黑风岭")
        sm.apply_state_delta(delta, 3)
        self.assertEqual(sm.state.characters["楚封"].current_state.location, "黑风岭")

    def test_plan_chapter_range(self):
        """测试批次区间计算：count 语义为"本次新写几章"（修复续写翻倍）"""
        self.assertEqual(NovelPipeline.plan_chapter_range(0, 3), (1, 3))
        self.assertEqual(NovelPipeline.plan_chapter_range(3, 3), (4, 6))
        self.assertEqual(NovelPipeline.plan_chapter_range(3, 1), (4, 4))
        with self.assertRaises(ValueError):
            NovelPipeline.plan_chapter_range(9, 0)

    def test_batch_stops_at_first_failure_and_surfaces_partial_result(self):
        pipeline = NovelPipeline("batch_stop", base_dir=str(self.test_base_dir))
        chapter = ChapterContent(
            chapter_number=1,
            volume_number=1,
            title="成功章",
            word_count=4,
            full_text="成功正文",
        )
        pipeline.generate_single_chapter = unittest.mock.Mock(
            side_effect=[chapter, RuntimeError("transport down"), chapter]
        )
        with self.assertRaisesRegex(RuntimeError, "第 2 章停止"):
            pipeline.generate_batch(start_chapter=1, count=3)
        self.assertEqual(pipeline.generate_single_chapter.call_count, 2)

    def test_clean_json_string(self):
        """测试 JSON 提取与尾部逗号修复"""
        self.assertEqual(clean_json_string('```json\n{"a": 1,}\n```'), '{"a": 1}')
        parsed = json.loads(clean_json_string('前置说明 {"a": [1, 2,]} 后置说明'))
        self.assertEqual(parsed, {"a": [1, 2]})

    def test_beat_director_fallback_plan(self):
        """测试空节拍兜底方案：LLM 失效时流水线也不会产出空章节"""
        agent = BeatDirectorAgent()
        plan = agent.build_fallback_plan({
            "chapter_number": 5,
            "chapter_title": "矿道夺宝",
            "chapter_summary": "深入矿道寻找骨片",
            "main_conflict": "管事压迫",
            "expectation_hook": "黑影逼近",
        })
        self.assertEqual(len(plan.beats), 3)
        self.assertEqual(plan.chapter_number, 5)
        self.assertEqual(plan.cliffhanger_design, "黑影逼近")
        self.assertTrue(all(b.scene_goal for b in plan.beats))

    def test_output_dir_anchored_absolute(self):
        """输出目录必须解析为绝对路径（与启动工作目录解耦）"""
        self.assertTrue(get_output_dir().is_absolute())


    def test_derived_data_reloaded_after_restart(self):
        """分镜/演播/推演等衍生数据必须从磁盘恢复，服务重启不丢失"""
        from core.narrative_brancher import BranchingSimulationResult
        from core.reader_simulator import ReaderSimulationReport

        sm = StateManager(novel_id="test_derived", base_dir=str(self.test_base_dir))
        sm.save_short_drama_script(1, ShortDramaScript(title="分镜", core_hook="钩子", scenes=[], ending_cliffhanger="断章"))
        sm.save_branch_simulation(2, BranchingSimulationResult(decision_point_description="抉择点"))
        sm.save_reader_simulation(3, ReaderSimulationReport(chapter_number=3, overall_sentiment_summary="口碑良好"))
        sm.save_audio_drama_package(AudioDramaPackage(novel_title="测试书", audio_production_notes="混音指导"))

        # 模拟服务重启：新建 StateManager 实例
        sm2 = StateManager(novel_id="test_derived", base_dir=str(self.test_base_dir))
        self.assertIn(1, sm2.short_drama_scripts)
        self.assertEqual(sm2.short_drama_scripts[1].title, "分镜")
        self.assertIn(2, sm2.branch_simulations)
        self.assertIn(3, sm2.reader_simulations)
        self.assertIsNotNone(sm2.audio_package)
        self.assertEqual(sm2.audio_package.novel_title, "测试书")

    def test_project_exists_check(self):
        """project_exists 判定准确，避免为不存在的项目创建目录"""
        from core.state_manager import StateManager as SM
        self.assertFalse(SM.project_exists("__ghost__", base_dir=str(self.test_base_dir)))
        sm = SM(novel_id="real_project", base_dir=str(self.test_base_dir))
        sm.set_concept(NovelConcept(title="存在感", genre="玄幻", logline="l", golden_finger="g", core_conflict="c"))
        self.assertTrue(SM.project_exists("real_project", base_dir=str(self.test_base_dir)))
        self.assertFalse((self.test_base_dir / "__ghost__").exists())


    def test_compliance_scanner(self):
        """合规扫描：分类命中、次数与上下文定位"""
        from config.compliance_words import scan_text
        hits = scan_text("他是世界第一强者，肠子流了一地。这方法100%有效。")
        words = {h["word"]: h for h in hits}
        self.assertIn("世界第一", words)
        self.assertEqual(words["世界第一"]["category"], "极限用语")
        self.assertEqual(words["肠子"]["category"], "血腥风险")
        self.assertIn("first_pos", words["世界第一"])
        self.assertEqual(scan_text("正常文本无命中"), [])

    def test_replace_chapter_text(self):
        """整章人工编辑：字数重算 + 语义记忆重建不残留"""
        sm = StateManager(novel_id="test_edit", base_dir=str(self.test_base_dir))
        chap = ChapterContent(chapter_number=1, volume_number=1, title="旧", word_count=0,
                              beats_raw_text=[], full_text="旧的正文内容。", cliffhanger_ending="")
        sm.save_chapter(chap)
        old_chunks = len(sm.semantic_memory.chunks)
        self.assertTrue(sm.replace_chapter_text(1, "全新的第一段。\n\n全新的第二段内容。"))
        self.assertIn("全新的第一段", sm.state.chapters[1].full_text)
        self.assertGreater(sm.state.chapters[1].word_count, 0)
        new_chunks = [c for c in sm.semantic_memory.chunks if c.chapter_number == 1]
        self.assertTrue(all("旧的正文" not in c.content for c in new_chunks))
        self.assertLessEqual(len(new_chunks), old_chunks + 2)
        self.assertFalse(sm.replace_chapter_text(99, "不存在"))


    def test_relation_updates_applied_to_graph(self):
        """StateUpdateDelta.new_relations 落库为图谱关系边"""
        sm = StateManager(novel_id="test_relations", base_dir=str(self.test_base_dir))
        sm.upsert_character(CharacterCard(id="c1", name="楚封", role="主角", background="b"))
        from core.schemas import RelationUpdate
        delta = StateUpdateDelta(
            summary_of_events="结仇与获得",
            character_updates={"楚封": {"equipment_and_items": ["青钢剑"]}},
            new_relations=[
                RelationUpdate(source="楚封", target="青云宗", relation_type="BETRAYED", description="被逐出师门"),
                RelationUpdate(source="楚封", target="钱掌柜", relation_type="BLOOD_FEUD", description="灭门之仇"),
            ],
        )
        sm.apply_state_delta(delta, 1)
        edges = {(e.source_name, e.target_name, e.relation_type) for e in sm.knowledge_graph.edges}
        self.assertIn(("楚封", "青云宗", "BETRAYED"), edges)
        self.assertIn(("楚封", "钱掌柜", "BLOOD_FEUD"), edges)
        self.assertIn("钱掌柜", sm.knowledge_graph.nodes)

    def test_export_epub(self):
        """EPUB 导出：结构合法、mimetype 首位且未压缩、章节内容齐全"""
        import zipfile
        sm = StateManager(novel_id="test_epub", base_dir=str(self.test_base_dir))
        sm.set_concept(NovelConcept(title="EPUB测试", genre="玄幻", logline="l", golden_finger="g", core_conflict="c"))
        for n in (1, 2):
            sm.save_chapter(ChapterContent(
                chapter_number=n, volume_number=1, title=f"第{n}章", word_count=10,
                beats_raw_text=[], full_text=f"第{n}章 正文内容，包含 <特殊> 字符。", cliffhanger_ending="",
            ))
        path = sm.export_epub()
        self.assertTrue(path.exists())
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            self.assertEqual(infos[0].filename, "mimetype")
            self.assertEqual(infos[0].compress_type, zipfile.ZIP_STORED)
            self.assertEqual(zf.read("mimetype").decode(), "application/epub+zip")
            self.assertIn("OEBPS/content.opf", zf.namelist())
            self.assertIn("OEBPS/nav.xhtml", zf.namelist())
            self.assertIn("urn:moirai:test_epub", zf.read("OEBPS/content.opf").decode())
            chap1 = zf.read("OEBPS/chap_0001.xhtml").decode()
            self.assertIn("第1章", chap1)
            self.assertIn("&lt;特殊&gt;", chap1)

    def test_compliance_word_replace(self):
        """合规替换：命中词全量替换且字数/记忆同步更新"""
        sm = StateManager(novel_id="test_replace", base_dir=str(self.test_base_dir))
        sm.save_chapter(ChapterContent(
            chapter_number=1, volume_number=1, title="t", word_count=0,
            beats_raw_text=[], full_text="他血肉横飞地冲阵，场面血肉横飞。", cliffhanger_ending="",
        ))
        chap = sm.state.chapters[1]
        count = chap.full_text.count("血肉横飞")
        self.assertEqual(count, 2)
        sm.replace_chapter_text(1, chap.full_text.replace("血肉横飞", "×" * 4))
        from config.compliance_words import scan_text
        self.assertEqual(scan_text(sm.state.chapters[1].full_text), [])
        self.assertEqual(sm.state.chapters[1].full_text.count("××××"), 2)

    def test_outline_edit_persists(self):
        """细纲编辑：修改后从磁盘重新加载仍生效"""
        sm = StateManager(novel_id="test_outline", base_dir=str(self.test_base_dir))
        from core.schemas import ChapterOutline, GlobalOutline, VolumeOutline
        sm.set_global_outline(GlobalOutline(
            novel_title="t", main_theme="m", total_volumes=1, final_climax="f",
            volumes=[VolumeOutline(
                volume_number=1, volume_title="v", volume_goal="g", main_antagonist="a", volume_climax="c",
                chapters=[ChapterOutline(chapter_number=1, volume_number=1, title="旧标题", summary="旧概要",
                                         main_conflict="旧矛盾", expectation_hook="旧钩子")],
            )],
        ))
        found = sm.get_chapter_outline(1)
        self.assertIsNotNone(found)
        vol, chap = found
        chap.title = "新标题"
        chap.main_conflict = "新矛盾"
        sm.set_global_outline(sm.state.global_outline)

        sm2 = StateManager(novel_id="test_outline", base_dir=str(self.test_base_dir))
        _, chap2 = sm2.get_chapter_outline(1)
        self.assertEqual(chap2.title, "新标题")
        self.assertEqual(chap2.main_conflict, "新矛盾")


    def test_rename_project(self):
        """项目重命名：目录迁移、状态保留、旧命名产物清理"""
        sm = StateManager(novel_id="test_rename_a", base_dir=str(self.test_base_dir))
        sm.set_concept(NovelConcept(title="改名测试", genre="玄幻", logline="l", golden_finger="g", core_conflict="c"))
        sm.save_chapter(ChapterContent(
            chapter_number=1, volume_number=1, title="壹", word_count=10,
            beats_raw_text=[], full_text="正文内容。", cliffhanger_ending="",
        ))
        old_dir = sm.base_dir
        sm.rename_project("test_rename_b")

        self.assertFalse(old_dir.exists())
        self.assertTrue((sm.base_dir / "project_state.json").exists())
        self.assertFalse((sm.base_dir / "test_rename_a_全本.md").exists())
        self.assertTrue((sm.base_dir / "test_rename_b_全本.md").exists())

        sm2 = StateManager(novel_id="test_rename_b", base_dir=str(self.test_base_dir))
        self.assertEqual(sm2.state.novel_id, "test_rename_b")
        self.assertIn(1, sm2.state.chapters)

        # 自身同名（no-op）与非法名必须报错或安全返回
        with self.assertRaises(ValueError):
            sm2.rename_project("..")
        sm3 = StateManager(novel_id="test_rename_c", base_dir=str(self.test_base_dir))
        with self.assertRaises(ValueError):
            sm3.rename_project("test_rename_b")  # 目标名已被占用

    def test_patch_paragraph_empty_guard(self):
        """空匹配串防护：不再向正文头部注入内容"""
        sm = StateManager(novel_id="test_empty_guard", base_dir=str(self.test_base_dir))
        sm.save_chapter(ChapterContent(
            chapter_number=1, volume_number=1, title="t", word_count=10,
            beats_raw_text=[], full_text="原文内容。", cliffhanger_ending="",
        ))
        self.assertFalse(sm.patch_paragraph(1, "", "注入内容"))
        self.assertNotIn("注入内容", sm.state.chapters[1].full_text)







    def test_adopt_branch_writes_outline(self):
        """分支采纳：推荐分支策略写入章节细纲"""
        from core.schemas import ChapterOutline, GlobalOutline, VolumeOutline
        sm = StateManager(novel_id="test_adopt", base_dir=str(self.test_base_dir))
        sm.set_global_outline(GlobalOutline(
            novel_title="t", main_theme="m", total_volumes=1, final_climax="f",
            volumes=[VolumeOutline(volume_number=1, volume_title="v", volume_goal="g",
                                   main_antagonist="a", volume_climax="c",
                                   chapters=[ChapterOutline(chapter_number=1, volume_number=1, title="旧",
                                                            summary="旧概要", main_conflict="旧矛盾", expectation_hook="旧钩子")])],
        ))
        sm.save_branch_simulation(1, BranchingSimulationResult(
            decision_point_description="抉择",
            candidates=[PlotBranchCandidate(branch_id="A", branch_title="雷击杀",
                                            narrative_strategy="以雷牢困杀强敌，置之死地而后生。" * 3,
                                            immediate_conflict="灵力透支生死线",
                                            recommendation_reason="爽点与自洽双高")],
            selected_branch_id="A",
        ))
        # 模拟 adopt_branch 端点的核心逻辑
        sim = sm.branch_simulations[1]
        candidate = next(c for c in sim.candidates if c.branch_id == "A")
        _, chap = sm.get_chapter_outline(1)
        chap.summary = f"[已采纳分支 A·{candidate.branch_title}] {candidate.narrative_strategy[:400]}"
        chap.main_conflict = candidate.immediate_conflict[:200]
        sm.set_global_outline(sm.state.global_outline)

        sm2 = StateManager(novel_id="test_adopt", base_dir=str(self.test_base_dir))
        _, c2 = sm2.get_chapter_outline(1)
        self.assertIn("已采纳分支 A", c2.summary)
        self.assertIn("灵力透支", c2.main_conflict)


    def test_para_rewrite_validation(self):
        """段落重写端点：空指令 400 / 越界 400 / 幽灵项目 404 / 采纳入库"""
        from fastapi.testclient import TestClient

        import web_ui
        client = TestClient(web_ui.app)
        r = client.post("/api/novel/__ghost_rw__/para_rewrite", json={"chapter": 1, "paragraph_index": 0, "instruction": "x"})
        self.assertEqual(r.status_code, 404)
        # 端点使用默认 OUTPUT_DIR，因此项目要建在 novel_projects 下
        sm = StateManager(novel_id="__rw_test__")
        try:
            sm.save_chapter(ChapterContent(
                chapter_number=1, volume_number=1, title="t", word_count=10,
                beats_raw_text=[], full_text="第一段。\n\n第二段。", cliffhanger_ending="",
            ))
            r = client.post("/api/novel/__rw_test__/para_rewrite", json={"chapter": 1, "paragraph_index": 0, "instruction": "  "})
            self.assertEqual(r.status_code, 400)
            r = client.post("/api/novel/__rw_test__/para_rewrite", json={"chapter": 1, "paragraph_index": 9, "instruction": "更狠厉"})
            self.assertEqual(r.status_code, 400)
            r = client.post("/api/novel/__rw_test__/para_rewrite_apply", json={"chapter": 1, "paragraph_index": 0, "new_text": ""})
            self.assertEqual(r.status_code, 400)
            r = client.post("/api/novel/__rw_test__/para_rewrite_apply", json={"chapter": 1, "paragraph_index": 1, "new_text": "重写后的第二段。", "expected_revision": sm.revision})
            self.assertEqual(r.status_code, 200)
            sm2 = StateManager(novel_id="__rw_test__")
            self.assertIn("重写后的第二段", sm2.state.chapters[1].full_text)
        finally:
            sm.load_state()
            sm.delete_project()


class TestWebAPIContract(IsolatedProjectTestCase):
    """Web API 契约测试：校验规则与错误码（不起后台任务）"""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        import web_ui
        cls.client = TestClient(web_ui.app)
        cls.web_ui = web_ui

    def test_invalid_project_id_rejected(self):
        """路径逃逸与非法项目 ID 统一 404，且不创建任何目录"""
        before = {p.name for p in get_output_dir().iterdir()}
        for bad in ["..", ".", "a:b"]:
            r = self.client.get(f"/api/novel/{bad}/compliance")
            self.assertEqual(r.status_code, 404, f"应拒绝非法 ID: {bad!r}")
        after = {p.name for p in get_output_dir().iterdir()}
        self.assertEqual(before, after, "非法 ID 不得触发目录创建")

    def test_ghost_project_404(self):
        self.assertEqual(self.client.get("/api/novel/__ghost_xyz__/compliance").status_code, 404)

    def test_create_validation(self):
        """非法项目名 / 空创意 / 正常值截断均正确响应"""
        r = self.client.post("/api/novel/create", json={"name": "..", "idea": "x"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/novel/create", json={"name": "ok_name", "idea": "   "})
        self.assertEqual(r.status_code, 400)

    def test_config_requires_model_and_url(self):
        r = self.client.post("/api/config", json={"api_key": "", "base_url": " ", "model": "m"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/config", json={"api_key": "", "base_url": "not-a-url", "model": "m"})
        self.assertEqual(r.status_code, 400)

    def test_config_role_models_exposed(self):
        r = self.client.get("/api/config")
        self.assertEqual(r.status_code, 200)
        self.assertIn("role_models", r.json())
        self.assertEqual(set(r.json()["role_models"].keys()), {"writer", "planner", "critic", "state"})

    def test_local_web_boundary_rejects_cross_origin_and_rebinding(self):
        response = self.client.get("/", headers={"host": "evil.example"})
        self.assertEqual(response.status_code, 421)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-frame-options"], "DENY")

        response = self.client.post(
            "/api/config",
            headers={"origin": "https://evil.example"},
            json={"api_key": "", "base_url": "https://api.example.com/v1", "model": "m"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-frame-options"], "DENY")

        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["pragma"], "no-cache")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])
        self.assertNotIn("'unsafe-eval'", response.headers["content-security-policy"])
        self.assertNotIn("cdn.tailwindcss.com", response.text)
        self.assertNotIn("cdnjs.cloudflare.com", response.text)
        self.assertNotIn("fonts.googleapis.com", response.text)

        for asset in (
            "/assets/tailwind.min.css",
            "/assets/cytoscape.min.js",
            "/assets/fontawesome/all.min.css",
            "/assets/webfonts/fa-solid-900.woff2",
        ):
            with self.subTest(asset=asset):
                asset_response = self.client.get(asset)
                self.assertEqual(asset_response.status_code, 200)
                self.assertGreater(len(asset_response.content), 1_000)

    def test_project_detail_uses_metadata_and_loads_chapter_on_demand(self):
        sm = StateManager("__lazy_detail_test__")
        try:
            sm.set_concept(
                NovelConcept(
                    title="懒加载测试",
                    genre="测试",
                    logline="测试",
                    golden_finger="测试",
                    core_conflict="测试",
                )
            )
            sm.save_chapter(
                ChapterContent(
                    chapter_number=1,
                    volume_number=1,
                    title="第一章",
                    word_count=6,
                    full_text="只在按需接口出现。",
                )
            )

            detail = self.client.get("/api/novel/__lazy_detail_test__")
            self.assertEqual(detail.status_code, 200)
            self.assertNotIn("full_text", detail.json()["chapters"][0])

            chapter = self.client.get("/api/novel/__lazy_detail_test__/chapter/1")
            self.assertEqual(chapter.status_code, 200)
            self.assertEqual(chapter.json()["chapter"]["full_text"], "只在按需接口出现。")
        finally:
            sm.delete_project()

    def test_snapshot_api_disables_mismatched_history(self):
        sm = StateManager("__snapshot_status_test__")
        try:
            sm.save_chapter(
                ChapterContent(
                    chapter_number=1,
                    volume_number=1,
                    title="第一章",
                    word_count=2,
                    full_text="正文",
                ),
                StateUpdateDelta(summary_of_events="第一章事件"),
            )
            snapshot = sm.snapshots_dir / "snapshot_after_chap_001.json"
            data = json.loads(snapshot.read_text(encoding="utf-8"))
            data["state"]["current_chapter_idx"] = 2
            snapshot.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

            response = self.client.get("/api/novel/__snapshot_status_test__/snapshots")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()["snapshots"]), 1)
            self.assertFalse(response.json()["snapshots"][0]["restorable"])
        finally:
            sm.load_state()
            sm.delete_project()


    def test_audit_strictness_levels(self):
        """质检严格度：同分数在宽松档放行、苛刻档打回"""
        from agents.consistency_auditor import ConsistencyAuditorAgent
        from core.schemas import AuditReport
        agent = ConsistencyAuditorAgent()

        report = AuditReport(passed=False, consistency_score=6.0, pacing_score=6.0,
                             logic_issues=[], character_ooc_issues=[], cliffhanger_detected=True,
                             critique="ok", revision_directives=[])
        with unittest.mock.patch.object(agent.settings, "AUDIT_STRICTNESS", "lenient"):
            self.assertTrue(agent._apply_strictness(report).passed)  # 6.0 >= 5.5 放行
        with unittest.mock.patch.object(agent.settings, "AUDIT_STRICTNESS", "strict"):
            self.assertFalse(agent._apply_strictness(report).passed)  # 6.0 < 8.5 打回

        good = AuditReport(passed=True, consistency_score=9.2, pacing_score=9.0,
                           logic_issues=[], character_ooc_issues=[], cliffhanger_detected=True,
                           critique="great", revision_directives=[])
        for level in ("strict", "standard", "lenient"):
            with unittest.mock.patch.object(agent.settings, "AUDIT_STRICTNESS", level):
                self.assertTrue(agent._apply_strictness(good).passed)

    def test_custom_compliance_words(self):
        """自定义词库：保存后参与扫描，删除后不再命中"""
        from config.compliance_words import get_custom_words, save_custom_words
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch(
            "config.compliance_words.get_output_dir", return_value=Path(tmp)
        ):
            save_custom_words({"自定义": ["独门黑话"]})
            self.assertEqual(get_custom_words().get("自定义"), ["独门黑话"])
            hits = scan_text("正文里出现了独门黑话。")
            self.assertTrue(any(h["word"] == "独门黑话" for h in hits))
            save_custom_words({})
            self.assertEqual(scan_text("正文里出现了独门黑话。"), [])

    def test_llm_usage_accounting(self):
        """token 用量统计：估算路径正确累计（无 API usage 时）"""
        from core.llm import LLMClient
        before = dict(LLMClient.USAGE_TOTALS)
        client = LLMClient.__new__(LLMClient)  # 跳过网络客户端构建
        client._record_usage([{"role": "user", "content": "x" * 100}], "y" * 50, None)
        after = LLMClient.USAGE_TOTALS
        self.assertEqual(after["prompt_tokens"], before["prompt_tokens"] + 100)
        self.assertEqual(after["completion_tokens"], before["completion_tokens"] + 50)
        self.assertEqual(after["calls"], before["calls"] + 1)
        self.assertTrue(after["estimated"])
        # 还原，避免污染其它测试
        LLMClient.USAGE_TOTALS.update(before)



    def test_backup_import_roundtrip(self):
        """备份闭环：导出 zip -> 删除 -> 导入 -> 数据完整恢复"""
        import io

        from fastapi.testclient import TestClient

        import web_ui
        client = TestClient(web_ui.app)
        sm = StateManager(novel_id="__bk_test__")
        try:
            sm.save_chapter(ChapterContent(
                chapter_number=1, volume_number=1, title="备份章", word_count=10,
                beats_raw_text=[], full_text="备份正文。", cliffhanger_ending="钩",
            ))
            r = client.get("/api/novel/__bk_test__/export_backup")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers["content-type"], "application/zip")
            zip_bytes = r.content

            sm.delete_project()
            self.assertFalse(StateManager.project_exists("__bk_test__"))

            r2 = client.post("/api/novels/import",
                             files={"file": ("__bk_test__-backup.zip", io.BytesIO(zip_bytes), "application/zip")})
            self.assertEqual(r2.status_code, 200, r2.text)
            self.assertTrue(StateManager.project_exists("__bk_test__"))
            sm2 = StateManager(novel_id="__bk_test__")
            self.assertIn(1, sm2.state.chapters)
            self.assertEqual(sm2.state.chapters[1].title, "备份章")

            # 重复导入同名应 400
            r3 = client.post("/api/novels/import",
                             files={"file": ("__bk_test__-backup.zip", io.BytesIO(zip_bytes), "application/zip")})
            self.assertEqual(r3.status_code, 400)
        finally:
            sm.load_state()
            sm.delete_project()


if __name__ == "__main__":
    unittest.main()
