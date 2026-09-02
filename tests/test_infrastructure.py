import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from agents.outline_architect import OutlineExtension
from core.env_file import update_env_file
from core.knowledge_graph import KnowledgeGraph
from core.project_archive import InvalidProjectArchive, import_project_archive, inspect_archive
from core.project_export import export_markdown, export_platform_chapter, export_platform_text
from core.schemas import (
    ChapterContent,
    ChapterOutline,
    CharacterCard,
    EmotionalPacingBeat,
    GlobalOutline,
    NovelConcept,
    NovelProjectState,
    StateUpdateDelta,
    VolumeOutline,
    WorldSetting,
)
from core.semantic_memory import SemanticMemoryIndex
from core.state_manager import StateManager
from core.storage import atomic_write_bytes, atomic_write_text, project_lock, read_json
from core.task_status import GenerationTaskStatus, TaskAlreadyRunning, TaskReservationMismatch
from pipeline.novel_pipeline import NovelPipeline


class TestAtomicStorage(unittest.TestCase):
    def test_atomic_text_write_replaces_content_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "state.json"
            atomic_write_text(target, "first")
            atomic_write_text(target, "second")
            self.assertEqual(target.read_text(encoding="utf-8"), "second")
            self.assertEqual(list(target.parent.glob(".state.json.*.tmp")), [])

    def test_atomic_bytes_preserves_exact_payload_and_rejects_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "evidence.bin"
            payload = b"\x00\xffhistorical-evidence\r\n"
            atomic_write_bytes(target, payload)
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(list(target.parent.glob(".evidence.bin.*.tmp")), [])
            with self.assertRaises(TypeError):
                atomic_write_bytes(target, "not bytes")  # type: ignore[arg-type]
            self.assertEqual(target.read_bytes(), payload)

    def test_project_ids_reject_aliases_and_windows_devices(self):
        for invalid in ("..", "has space", "a:b", "CON", "LPT1"):
            with self.subTest(invalid=invalid):
                self.assertFalse(StateManager.is_valid_project_id(invalid))
        self.assertTrue(StateManager.is_valid_project_id("作品_01-test"))

    def test_env_updates_are_atomic_and_preserve_unrelated_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("# local\nKEEP=value\nMODEL=old\nMODEL=stale\n", encoding="utf-8")
            update_env_file(path, {"MODEL": "new model", "TOKEN": 'a#b"c'})
            text = path.read_text(encoding="utf-8")
            self.assertIn("# local", text)
            self.assertIn("KEEP=value", text)
            self.assertEqual(text.count("MODEL="), 1)
            self.assertIn('MODEL="new model"', text)
            self.assertIn('TOKEN="a#b\\"c"', text)
            self.assertFalse(list(path.parent.glob(".*.tmp")))

            before = path.read_bytes()
            with self.assertRaises(ValueError):
                update_env_file(path, {"MODEL": "bad\nvalue"})
            self.assertEqual(path.read_bytes(), before)

    def test_project_lock_is_reentrant_and_uses_an_external_lock_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "book"
            project.mkdir()
            with project_lock(project), project_lock(project):
                atomic_write_text(project / "state.txt", "safe")
            self.assertEqual((project / "state.txt").read_text(encoding="utf-8"), "safe")
            self.assertEqual(len(list((Path(tmp) / ".novelengine_locks").glob("*.lock"))), 1)


class TestGenerationTaskStatus(unittest.TestCase):
    def test_reservation_is_atomic_and_status_snapshot_isolated(self):
        status = GenerationTaskStatus(max_logs=2)
        first_id = status.reserve("novel_a")
        self.assertTrue(first_id)
        with self.assertRaises(TaskAlreadyRunning):
            status.reserve("novel_b")
        with self.assertRaises(TaskAlreadyRunning):
            status.ensure_reserved("novel_b")
        with self.assertRaises(TaskAlreadyRunning):
            status.ensure_reserved("novel_a")
        self.assertEqual(status.ensure_reserved("novel_a", task_id=first_id), first_id)
        with self.assertRaises(TaskReservationMismatch):
            status.update(task_id="stale", progress=99)

        status.append_log("one")
        status.append_log("two")
        status.append_log("three", agent="Writer")
        snapshot = status.snapshot()
        self.assertEqual(snapshot["logs"], ["two", "three"])
        self.assertEqual(snapshot["active_agent"], "Writer")
        snapshot["logs"].append("external mutation")
        self.assertNotIn("external mutation", status.snapshot()["logs"])

        status.finish(step="done", progress=100)
        self.assertFalse(status.is_running)
        self.assertNotEqual(status.reserve("novel_b"), first_id)


class TestProjectArchiveSafety(unittest.TestCase):
    @staticmethod
    def _archive(entries: dict[str, bytes]) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        return output.getvalue()

    @staticmethod
    def _project_entries(project_dir: Path) -> dict[str, bytes]:
        return {
            path.relative_to(project_dir.parent).as_posix(): path.read_bytes()
            for path in project_dir.rglob("*")
            if path.is_file()
        }

    def test_import_valid_archive(self):
        state = json.dumps({"novel_id": "safe_project"}).encode()
        data = self._archive({"safe_project/project_state.json": state})
        with tempfile.TemporaryDirectory() as tmp:
            project_id, title = import_project_archive(data, Path(tmp))
            self.assertEqual((project_id, title), ("safe_project", "safe_project"))
            self.assertTrue((Path(tmp) / "safe_project" / "project_state.json").is_file())

    def test_rejects_windows_and_posix_traversal(self):
        state = json.dumps({"novel_id": "safe"}).encode()
        for malicious in ("safe/../escape.txt", "safe\\..\\escape.txt"):
            with self.subTest(malicious=malicious):
                data = self._archive({"safe/project_state.json": state, malicious: b"bad"})
                with self.assertRaises(InvalidProjectArchive):
                    inspect_archive(data)

    def test_rejects_state_directory_mismatch(self):
        state = json.dumps({"novel_id": "different"}).encode()
        data = self._archive({"safe/project_state.json": state})
        with self.assertRaises(InvalidProjectArchive):
            inspect_archive(data)

    def test_rejects_reserved_project_directory(self):
        data = self._archive({"CON/project_state.json": json.dumps({"novel_id": "CON"}).encode()})
        with self.assertRaises(InvalidProjectArchive):
            inspect_archive(data)

    def test_rejects_nonportable_nested_member_names(self):
        state = json.dumps({"novel_id": "safe"}).encode()
        for member in ("safe/CON/data.txt", "safe/trailing./data.txt", "safe/name /data.txt"):
            with self.subTest(member=member):
                data = self._archive({"safe/project_state.json": state, member: b"x"})
                with self.assertRaises(InvalidProjectArchive):
                    inspect_archive(data)

    @staticmethod
    def _chapter_project_entries(*, text: str = "正文。") -> dict[str, bytes]:
        state = NovelProjectState(novel_id="safe", current_chapter_idx=1)
        chapter = ChapterContent(
            chapter_number=1,
            volume_number=1,
            title="第一章",
            word_count=len(text),
            full_text=text,
        )
        return {
            "safe/project_state.json": state.model_dump_json().encode(),
            "safe/chapters/chapter_001.json": chapter.model_dump_json().encode(),
            "safe/chapters/chapter_001.txt": f"第1章 第一章\n\n{text}\n".encode(),
        }

    def test_rejects_corrupt_shards_before_publishing_destination(self):
        entries = self._chapter_project_entries()
        entries["safe/chapters/chapter_001.json"] = b'{"chapter_number": 2}'
        data = self._archive(entries)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(InvalidProjectArchive):
                import_project_archive(data, root)
            self.assertFalse((root / "safe").exists())
            self.assertEqual(list(root.glob(".novel-import-*")), [])

    def test_rejects_json_txt_disagreement_and_invalid_timeline_event(self):
        mismatched = self._chapter_project_entries(text="JSON 正文。")
        mismatched["safe/chapters/chapter_001.txt"] = "不同正文。".encode()
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(InvalidProjectArchive, "JSON 与 TXT"):
            import_project_archive(self._archive(mismatched), Path(tmp))

        bad_event = self._chapter_project_entries()
        bad_event["safe/state_events/baseline.json"] = json.dumps(
            {
                "baseline_version": 1,
                "chapter_num": 0,
                "state": {"novel_id": "safe"},
                "knowledge_graph": {},
            }
        ).encode()
        bad_event["safe/state_events/chapter_001.json"] = json.dumps(
            {
                "event_version": 1,
                "chapter_number": 2,
                "volume_number": 1,
                "delta": {"summary_of_events": "发生事件"},
            }
        ).encode()
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(InvalidProjectArchive, "状态事件"):
            import_project_archive(self._archive(bad_event), Path(tmp))

    def test_history_repair_evidence_round_trips_and_checksum_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as target_tmp:
            manager = StateManager("safe", base_dir=source_tmp)
            manager.save_chapter(ChapterContent(
                chapter_number=1, volume_number=1, title="第一章", full_text="正文。",
            ))
            manager._write_timeline_baseline(1)
            manager.initialize_history_repair({}, "作者已核对第一章前的空人物状态")
            entries = self._project_entries(manager.base_dir)
            project_id, _ = import_project_archive(self._archive(entries), Path(target_tmp))
            imported = StateManager(project_id, base_dir=target_tmp)
            self.assertEqual(imported.load_warnings, [])
            self.assertEqual(imported.get_timeline_status()["pending_chapters"], [1])

            retired_baseline = next(
                name for name in entries
                if "/retired_timelines/" in name and name.endswith("/baseline.json")
            )
            entries[retired_baseline] += b" "
            with tempfile.TemporaryDirectory() as rejected_tmp, self.assertRaisesRegex(
                InvalidProjectArchive, "长度|校验和",
            ):
                import_project_archive(self._archive(entries), Path(rejected_tmp))

    def test_rejects_oversized_json_member_before_extraction(self):
        data = self._archive({"safe/project_state.json": json.dumps({"novel_id": "safe"}).encode()})
        with (
            patch("core.project_archive.MAX_JSON_MEMBER_BYTES", 8),
            self.assertRaisesRegex(InvalidProjectArchive, "JSON 文件过大"),
        ):
            inspect_archive(data)


class TestLightweightShardFallback(unittest.TestCase):
    def test_partial_metadata_set_does_not_hide_canonical_chapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = StateManager("partial_metadata", base_dir=tmp)
            for number in (1, 2):
                manager.save_chapter(
                    ChapterContent(
                        chapter_number=number,
                        volume_number=1,
                        title=f"第{number}章",
                        word_count=3,
                        full_text=f"正文{number}",
                    )
                )
            (manager.chapter_meta_dir / "chapter_002.json").unlink()

            light = StateManager(
                "partial_metadata",
                base_dir=tmp,
                load_chapter_content=False,
                load_memory=False,
                load_derived=False,
            )
            self.assertEqual(set(light.state.chapters), {1, 2})
            self.assertEqual(light.load_warnings, [])


class TestSnapshotV3(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = StateManager("snapshot_v3", base_dir=str(self.root))

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _chapter(number: int, text: str) -> ChapterContent:
        return ChapterContent(
            chapter_number=number,
            volume_number=1,
            title=f"chapter {number}",
            word_count=len(text),
            full_text=text,
        )

    def test_snapshot_is_incremental_and_historical_edit_does_not_overwrite_it(self):
        original = "original chapter one " * 100
        self.manager.save_chapter(self._chapter(1, original), StateUpdateDelta(summary_of_events="one"))
        self.manager.save_chapter(
            self._chapter(2, "chapter two " * 100),
            StateUpdateDelta(summary_of_events="two"),
        )

        snapshot_one = self.root / "snapshot_v3" / "snapshots" / "snapshot_after_chap_001.json"
        snapshot_two = self.root / "snapshot_v3" / "snapshots" / "snapshot_after_chap_002.json"
        data = json.loads(snapshot_two.read_text(encoding="utf-8"))
        self.assertEqual(data["snapshot_version"], 3)
        self.assertEqual(data["state"]["chapters"], {})
        self.assertNotIn(original, snapshot_two.read_text(encoding="utf-8"))

        before = snapshot_one.read_bytes()
        self.manager.replace_chapter_text(1, "edited history")
        self.assertEqual(snapshot_one.read_bytes(), before)
        self.assertTrue(self.manager.rollback_to_chapter(1))
        self.assertEqual(self.manager.state.chapters[1].full_text, original)
        self.assertNotIn(2, self.manager.state.chapters)
        reloaded = StateManager("snapshot_v3", base_dir=str(self.root))
        self.assertEqual(reloaded.state.chapters[1].full_text, original)
        self.assertEqual(reloaded.read_chapter(1).full_text, original)
        metadata = read_json(reloaded.chapter_meta_dir / "chapter_001.json")
        self.assertEqual(metadata["word_count"], self._chapter(1, original).word_count)

    def test_project_state_and_memory_are_sharded_for_linear_growth(self):
        self.manager.save_chapter(self._chapter(1, "第一章正文。"))
        self.manager.save_chapter(self._chapter(2, "第二章正文。"))

        aggregate = json.loads((self.manager.base_dir / "project_state.json").read_text(encoding="utf-8"))
        self.assertEqual(aggregate["chapters"], {})
        self.assertFalse((self.manager.base_dir / "semantic_memory.json").exists())
        self.assertTrue((self.manager.memory_dir / "chapter_001.json").is_file())
        self.assertTrue((self.manager.memory_dir / "chapter_002.json").is_file())

        reloaded = StateManager("snapshot_v3", base_dir=str(self.root))
        self.assertEqual(sorted(reloaded.state.chapters), [1, 2])
        self.assertEqual(
            sorted({chunk.chapter_number for chunk in reloaded.semantic_memory.chunks}),
            [1, 2],
        )

    def test_mismatched_snapshot_is_reported_and_rollback_refuses_it(self):
        self.manager.save_chapter(self._chapter(1, "one"), StateUpdateDelta(summary_of_events="one"))
        self.manager.save_chapter(self._chapter(2, "two"), StateUpdateDelta(summary_of_events="two"))
        snapshot = self.manager.snapshots_dir / "snapshot_after_chap_001.json"
        data = json.loads(snapshot.read_text(encoding="utf-8"))
        data["state"]["current_chapter_idx"] = 2
        snapshot.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        before = {
            path.relative_to(self.manager.base_dir): path.read_bytes()
            for path in self.manager.base_dir.rglob("*")
            if path.is_file()
        }

        status = self.manager.snapshot_status(1)
        self.assertTrue(status["exists"])
        self.assertFalse(status["restorable"])
        self.assertFalse(self.manager.rollback_to_chapter(1))
        after = {
            path.relative_to(self.manager.base_dir): path.read_bytes()
            for path in self.manager.base_dir.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_valid_v3_snapshot_status_is_exact(self):
        self.manager.save_chapter(self._chapter(1, "one"), StateUpdateDelta(summary_of_events="one"))
        self.assertEqual(
            self.manager.snapshot_status(1),
            {
                "chapter_number": 1,
                "exists": True,
                "restorable": True,
                "snapshot_version": 3,
                "exact": True,
                "reason": "",
            },
        )

    def test_rollback_prunes_future_derived_files_and_release_text(self):
        self.manager.save_chapter(self._chapter(1, "one"), StateUpdateDelta(summary_of_events="one"))
        self.manager.save_chapter(self._chapter(2, "two"), StateUpdateDelta(summary_of_events="two"))
        future_files = [
            self.manager.multimodal_dir / "short_drama_chap_002.json",
            self.manager.reader_sim_dir / "reader_sim_chap_002.json",
            self.manager.base_dir / "branch_sim_chap_002.json",
        ]
        for path in future_files:
            path.write_text("{}", encoding="utf-8")
        self.manager.short_drama_scripts[2] = object()
        self.manager.reader_simulations[2] = object()
        self.manager.branch_simulations[2] = object()

        self.assertTrue(self.manager.rollback_to_chapter(1))
        self.assertTrue(all(not path.exists() for path in future_files))
        self.assertNotIn(2, self.manager.short_drama_scripts)
        self.assertNotIn(2, self.manager.reader_simulations)
        self.assertNotIn(2, self.manager.branch_simulations)
        releases = list((self.manager.delivery_dir / "platform_txt_release").glob("第*.txt"))
        self.assertEqual(len(releases), 1)
        self.assertIn("第001章", releases[0].name)

    def test_historical_regeneration_replays_following_state_events(self):
        self.manager.upsert_character(CharacterCard(id="hero", name="主角", role="主角", background="test"))
        first = StateUpdateDelta(
            summary_of_events="获得旧剑",
            character_updates={"主角": {"equipment_and_items": ["旧剑"], "level": "一层"}},
        )
        second = StateUpdateDelta(
            summary_of_events="获得护符",
            character_updates={"主角": {"equipment_and_items": ["护符"], "level": "二层"}},
        )
        self.manager.save_chapter(self._chapter(1, "old one"), first)
        self.manager.save_chapter(self._chapter(2, "two"), second)

        replacement = StateUpdateDelta(
            summary_of_events="改为获得新剑",
            character_updates={"主角": {"equipment_and_items": ["新剑"], "level": "一层"}},
        )
        self.manager.save_chapter(self._chapter(1, "new one"), replacement)
        hero = self.manager.state.characters["主角"]
        self.assertEqual(hero.current_state.level, "二层")
        self.assertEqual(hero.current_state.equipment_and_items, ["新剑", "护符"])
        self.assertNotIn("旧剑", self.manager.knowledge_graph.nodes)
        self.assertIn("新剑", self.manager.knowledge_graph.nodes)
        self.assertIn("护符", self.manager.knowledge_graph.nodes)
        self.assertEqual(
            self.manager.state.recent_chapter_summaries,
            ["【第1章】改为获得新剑", "【第2章】获得护符"],
        )

    def test_manual_character_override_survives_event_replay(self):
        self.manager.upsert_character(CharacterCard(id="hero", name="主角", role="主角", background="test"))
        self.manager.save_chapter(
            self._chapter(1, "one"),
            StateUpdateDelta(summary_of_events="one", character_updates={"主角": {"level": "一层"}}),
        )
        self.manager.state.characters["主角"].current_state.location = "人工指定地点"
        self.manager.record_character_override("主角", {"location": "人工指定地点"})
        self.manager.save_chapter(
            self._chapter(1, "one rewritten"),
            StateUpdateDelta(summary_of_events="one rewritten", character_updates={"主角": {"level": "二层"}}),
        )
        self.assertEqual(self.manager.state.characters["主角"].current_state.location, "人工指定地点")

    def test_state_events_remain_chronological_above_999_chapters(self):
        self.manager.upsert_character(CharacterCard(id="hero", name="主角", role="主角", background="test"))
        self.manager.state.current_chapter_idx = 998
        self.manager.save_chapter(
            self._chapter(999, "chapter 999"),
            StateUpdateDelta(summary_of_events="999", character_updates={"主角": {"level": "999层"}}),
        )
        self.manager.save_chapter(
            self._chapter(1000, "chapter 1000"),
            StateUpdateDelta(summary_of_events="1000", character_updates={"主角": {"level": "1000层"}}),
        )
        self.manager.save_chapter(
            self._chapter(999, "chapter 999 rewritten"),
            StateUpdateDelta(summary_of_events="999改写", character_updates={"主角": {"level": "999改写层"}}),
        )
        self.assertEqual(self.manager.state.characters["主角"].current_state.level, "1000层")
        self.assertEqual(self.manager.state.recent_chapter_summaries[-1], "【第1000章】1000")

    def test_manual_text_edit_marks_state_reconciliation(self):
        self.manager.save_chapter(self._chapter(1, "one"))
        self.assertTrue(self.manager.replace_chapter_text(1, "changed", mark_reconciliation=True))
        status = self.manager.get_state_reconciliation_status()
        self.assertTrue(status["required"])
        self.assertEqual(status["chapters"], [1])

    def test_release_filename_is_cross_platform_safe(self):
        chapter = self._chapter(1, "content")
        chapter.title = 'bad:/\\*?"<>| title.'
        self.manager.save_chapter(chapter)
        releases = list((self.manager.delivery_dir / "platform_txt_release").glob("第*.txt"))
        self.assertEqual(len(releases), 1)
        self.assertFalse(any(char in releases[0].name for char in '<>:"/\\|?*'))

    def test_deferred_exports_write_each_shard_once_and_full_books_once(self):
        with (
            patch("core.state_manager.export_markdown", wraps=export_markdown) as markdown,
            patch("core.state_manager.export_platform_text", wraps=export_platform_text) as platform,
            patch("core.state_manager.export_platform_chapter", wraps=export_platform_chapter) as shard,
            self.manager.defer_full_exports(),
            self.manager.defer_full_exports(),
        ):
            for number in range(1, 6):
                self.manager.save_chapter(self._chapter(number, f"chapter {number}"))

        self.assertEqual(markdown.call_count, 1)
        self.assertEqual(platform.call_count, 1)
        self.assertFalse(platform.call_args.kwargs["sync_chapters"])
        self.assertEqual(shard.call_count, 5)
        self.assertEqual(len(list((self.manager.delivery_dir / "platform_txt_release").glob("第*.txt"))), 5)
        self.assertIn("chapter 5", (self.manager.base_dir / "snapshot_v3_全本.md").read_text(encoding="utf-8"))
        self.assertIn(
            "chapter 5",
            (self.manager.delivery_dir / "snapshot_v3_全本发布稿.txt").read_text(encoding="utf-8"),
        )

    def test_deferred_export_flushes_completed_prefix_when_batch_body_fails(self):
        with self.assertRaisesRegex(RuntimeError, "writer failed"), self.manager.defer_full_exports():
            self.manager.save_chapter(self._chapter(1, "durable prefix"))
            raise RuntimeError("writer failed")
        self.assertIn("durable prefix", (self.manager.base_dir / "snapshot_v3_全本.md").read_text(encoding="utf-8"))

    def test_release_sync_removes_generated_orphans_but_preserves_unrelated_notes(self):
        self.manager.save_chapter(self._chapter(1, "one"))
        folder = self.manager.delivery_dir / "platform_txt_release"
        note = folder / "第notes.txt"
        orphan = folder / "第999章_old.txt"
        note.write_text("keep", encoding="utf-8")
        orphan.write_text("remove", encoding="utf-8")
        self.manager.export_platform_delivery_files()
        self.assertTrue(note.is_file())
        self.assertFalse(orphan.exists())


class TestChineseMemoryRecall(unittest.TestCase):
    def test_partial_chinese_phrase_matches_without_entity_hint(self):
        memory = SemanticMemoryIndex()
        memory.add_chapter_memory(1, 1, "旧案", "主角在枯井底发现青铜罗盘，罗盘指向北方雪原。", [])
        memory.add_chapter_memory(2, 1, "冲突", "众人在山门前爆发激烈争执。", [])
        results = memory.search_relevant_memories(
            query="青铜罗盘指向哪里",
            target_entities=[],
            top_k=1,
            exclude_recent_n_chapters=0,
            current_chapter=5,
        )
        self.assertEqual(len(results), 1)
        self.assertIn("青铜罗盘", results[0].content)


class TestKnowledgeGraphIdentity(unittest.TestCase):
    def test_node_ids_are_stable_and_relation_origin_is_preserved(self):
        first = KnowledgeGraph()
        first.upsert_node("青铜罗盘", chapter=1)
        stable_id = first.nodes["青铜罗盘"].id
        first.upsert_node("临时人物", chapter=2)
        first.prune_after_chapter(1)
        first.upsert_node("后来人物", chapter=3)
        self.assertNotEqual(first.nodes["后来人物"].id, stable_id)

        second = KnowledgeGraph()
        second.upsert_node("青铜罗盘", chapter=9)
        self.assertEqual(second.nodes["青铜罗盘"].id, stable_id)

        first.add_relation("主角", "青铜罗盘", "WIELDS", chapter=1)
        first.add_relation("主角", "青铜罗盘", "WIELDS", chapter=5)
        relation = next(edge for edge in first.edges if edge.relation_type == "WIELDS")
        self.assertEqual(relation.established_chapter, 1)


class TestRollingOutline(unittest.TestCase):
    def test_missing_frontier_is_planned_and_persisted_as_a_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = NovelPipeline("rolling", base_dir=tmp)
            manager = pipeline.state_manager
            manager.set_concept(NovelConcept(
                title="rolling", genre="玄幻", logline="test", golden_finger="test", core_conflict="test"
            ))
            manager.set_world_setting(WorldSetting(world_name="world", geography_overview="map"))
            manager.set_global_outline(GlobalOutline(
                novel_title="rolling",
                main_theme="theme",
                total_volumes=1,
                final_climax="final",
                volumes=[VolumeOutline(
                    volume_number=1,
                    volume_title="volume",
                    volume_goal="goal",
                    main_antagonist="enemy",
                    volume_climax="climax",
                    chapters=[ChapterOutline(
                        chapter_number=1,
                        volume_number=1,
                        title="one",
                        summary="one",
                        main_conflict="one",
                        expectation_hook="next",
                    )],
                )],
            ))
            extension = OutlineExtension(
                chapters=[ChapterOutline(
                    chapter_number=number,
                    volume_number=1,
                    title=f"chapter {number}",
                    summary="summary",
                    main_conflict="conflict",
                    expectation_hook="hook",
                ) for number in range(2, 12)],
                pacing_beats=[EmotionalPacingBeat(chapter_number=number) for number in range(2, 12)],
            )
            fake_architect = Mock()
            fake_architect.extend_outline.return_value = extension
            pipeline.__dict__["outline_architect"] = fake_architect

            pipeline.extend_outline_to(2)
            self.assertIsNotNone(manager.get_chapter_outline(11))
            self.assertEqual(manager.get_chapter_outline(2)[1].volume_number, 1)
            reloaded = StateManager("rolling", base_dir=tmp)
            self.assertIsNotNone(reloaded.get_chapter_outline(11))


if __name__ == "__main__":
    unittest.main()
