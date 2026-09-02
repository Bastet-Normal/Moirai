"""Concurrency regressions use real OS locks and isolated, disposable projects."""

import io
import json
import multiprocessing
import os
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from config.settings import get_settings
from core.narrative_brancher import BranchingSimulationResult
from core.project_archive import import_project_archive
from core.schemas import ChapterContent, NovelConcept
from core.state_manager import StateManager
from core.storage import (
    ProjectBusyError,
    ProjectConflictError,
    ProjectReadOnlyError,
    append_text_durable,
    atomic_write_text,
    project_lock,
)
from core.task_status import GenerationTaskStatus, TaskReservationMismatch
from pipeline.novel_pipeline import NovelPipeline
from services.generation_jobs import (
    run_audio_package_job,
    run_branch_simulation_job,
    run_delivery_job,
    run_pipeline_job,
    run_reader_simulation_job,
    run_regenerate_job,
    run_short_drama_job,
)


def _hold_lock(path, acquired, release, scope):
    with project_lock(Path(path), timeout=0, scope=scope):
        acquired.set()
        release.wait(20)


@contextmanager
def held_in_child(path, scope="operation"):
    context = multiprocessing.get_context("spawn")
    acquired, release = context.Event(), context.Event()
    child = context.Process(target=_hold_lock, args=(str(path), acquired, release, scope))
    child.start()
    try:
        if not acquired.wait(10):
            raise AssertionError(f"child failed to acquire lock, exit={child.exitcode}")
        yield child
    finally:
        release.set()
        child.join(5)
        if child.is_alive():
            child.terminate()
            child.join(5)
        if child.exitcode != 0:
            raise AssertionError(f"lock helper exited unexpectedly: {child.exitcode}")


def concept(title="第一版"):
    return NovelConcept(title=title, genre="测试", logline="线索", golden_finger="能力", core_conflict="冲突")


def chapter(number=1):
    return ChapterContent(chapter_number=number, volume_number=1, title=f"第{number}章", full_text="正文。")


class IsolatedConcurrencyCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="novelengine-concurrency-")
        self.addCleanup(temporary.cleanup)
        self.addCleanup(get_settings.cache_clear)
        self.enterContext(patch.dict(os.environ, {"OUTPUT_DIR": temporary.name}))
        get_settings.cache_clear()
        self.root = Path(temporary.name)
        self.manager = StateManager("book")
        self.manager.set_concept(concept())


class TestProjectConcurrency(IsolatedConcurrencyCase):
    def test_stale_writer_is_rejected_without_overwriting_new_state(self):
        stale = StateManager("book")
        self.manager.set_concept(concept("已更新"))
        before = (self.manager.base_dir / "project_state.json").read_bytes()
        with self.assertRaises(ProjectConflictError):
            stale.set_concept(concept("陈旧副本"))
        self.assertEqual((self.manager.base_dir / "project_state.json").read_bytes(), before)
        stale.load_state()
        stale.set_concept(concept("刷新后可写"))
        self.assertEqual(StateManager("book").state.concept.title, "刷新后可写")

    def test_delete_tombstone_stops_resurrection_and_stale_deletion(self):
        stale = StateManager("book")
        self.manager.delete_project()
        with self.assertRaises(ProjectConflictError):
            stale.save_state()
        self.assertFalse(self.manager.base_dir.exists())
        recreated = StateManager("book")
        recreated.set_concept(concept("新作品"))
        with self.assertRaises(ProjectConflictError):
            stale.delete_project()
        self.assertEqual(StateManager("book").state.concept.title, "新作品")

    def test_rename_invalidates_both_source_and_destination_old_views(self):
        stale_source = StateManager("book")
        stale_destination = StateManager("renamed")
        self.assertFalse(stale_destination.base_dir.exists())
        self.manager.rename_project("renamed")
        for stale in (stale_source, stale_destination):
            with self.assertRaises(ProjectConflictError):
                stale.save_state()
        self.assertFalse((self.root / "book").exists())
        self.assertEqual(StateManager("renamed").state.novel_id, "renamed")

    def test_rename_refuses_busy_destination(self):
        with held_in_child(self.root / "renamed"), self.assertRaises(ProjectBusyError):
            self.manager.rename_project("renamed")
        self.assertTrue((self.root / "book" / "project_state.json").exists())
        self.assertFalse((self.root / "renamed").exists())

    def test_lightweight_and_corrupt_views_cannot_write(self):
        self.manager.save_chapter(chapter())
        for flag in ("load_chapter_content", "load_memory", "load_derived"):
            with self.subTest(flag=flag):
                view = StateManager("book", **{flag: False})
                with self.assertRaises(ProjectReadOnlyError):
                    view.save_state()
        broken = self.manager.memory_dir / "chapter_001.json"
        atomic_write_text(broken, "not-json")
        view = StateManager("book")
        with self.assertRaises(ProjectReadOnlyError):
            view.set_concept(concept("不能写"))
        self.assertEqual(broken.read_text(encoding="utf-8"), "not-json")

    def test_reload_does_not_accumulate_memory_counts_or_removed_artifacts(self):
        self.manager.save_chapter(chapter())
        light = StateManager("book", load_chapter_content=False, load_memory=False)
        count = light.semantic_memory_chunk_count
        light.load_state()
        self.assertEqual(light.semantic_memory_chunk_count, count)
        self.manager.delete_project()
        light.load_state()
        self.assertEqual(light.semantic_memory_chunk_count, 0)
        self.assertEqual(light.state.chapters, {})
        self.assertIsNone(light.state.concept)
        self.assertFalse(light.base_dir.exists())

    def test_real_process_lock_blocks_writes_but_not_reads(self):
        with held_in_child(self.manager.base_dir):
            view = StateManager("book")
            self.assertEqual(view.state.concept.title, "第一版")
            with self.assertRaises(ProjectBusyError):
                view.set_concept(concept("不能写"))
            with self.assertRaises(ProjectBusyError), project_lock(self.manager.base_dir, timeout=0.05):
                self.fail("competing process must not enter")
        self.manager.set_concept(concept("锁已释放"))

    def test_snapshot_reader_waits_for_actual_write_commit(self):
        started = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.manager.mutation():
                self.manager.set_concept(concept("一致快照"))

                def read():
                    started.set()
                    return StateManager("book").state.concept.title

                pending = executor.submit(read)
                self.assertTrue(started.wait(3))
                self.assertFalse(pending.done())
            self.assertEqual(pending.result(timeout=5), "一致快照")

    def test_state_read_does_not_create_missing_project_directories(self):
        StateManager("missing")
        self.assertFalse((self.root / "missing").exists())

    def test_pipeline_conflict_happens_before_agent_construction(self):
        stale = NovelPipeline("book")
        self.manager.set_concept(concept("新状态"))
        with patch("pipeline.novel_pipeline.BeatDirectorAgent") as agent:
            with self.assertRaises(ProjectConflictError):
                stale.generate_single_chapter(1)
            agent.assert_not_called()

    def test_pipeline_lease_covers_model_call_and_allows_reader(self):
        pipeline = NovelPipeline("book")
        competitor = StateManager("book")

        def simulate(**kwargs):
            with ThreadPoolExecutor(max_workers=1) as executor:
                self.assertEqual(executor.submit(lambda: StateManager("book").state.concept.title).result(5), "第一版")
                competing_write = executor.submit(competitor.save_state)
                with self.assertRaises(ProjectBusyError):
                    competing_write.result(5)
            return BranchingSimulationResult(decision_point_description="独占模型阶段")

        pipeline.__dict__["narrative_brancher"] = Mock(simulate_branches=simulate)
        pipeline.simulate_chapter_branching(1)
        self.assertIn(1, StateManager("book").branch_simulations)

    def test_batch_lease_covers_gap_between_chapters(self):
        pipeline = NovelPipeline("book")

        def generate(number):
            pipeline.state_manager.save_chapter(chapter(number))
            with ThreadPoolExecutor(max_workers=1) as executor:
                competing_write = executor.submit(lambda: StateManager("book").save_state())
                with self.assertRaises(ProjectBusyError):
                    competing_write.result(5)
            return chapter(number)

        pipeline.generate_single_chapter = Mock(side_effect=generate)
        self.assertEqual(len(pipeline.generate_batch(count=2)), 2)

    def test_pipeline_rejects_replanning_and_skipping_chapters(self):
        pipeline = NovelPipeline("book")
        with patch("pipeline.novel_pipeline.ConceptPlannerAgent") as planner:
            with self.assertRaisesRegex(ValueError, "已有设定或正文"):
                pipeline.initialize_new_novel("覆盖?")
            planner.assert_not_called()
        with self.assertRaisesRegex(ValueError, "不能跳章"):
            pipeline.generate_single_chapter(8)

    def test_import_cannot_publish_into_a_busy_project_name(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("newbook/project_state.json", json.dumps({"novel_id": "newbook"}))
        with held_in_child(self.root / "newbook"), self.assertRaises(ProjectBusyError):
            import_project_archive(archive.getvalue(), self.root)
        self.assertFalse((self.root / "newbook").exists())
        self.assertEqual(list(self.root.glob(".novel-import-*")), [])

    def test_durable_append_handles_short_os_writes(self):
        original_write = os.write

        def short_write(fd, data):
            return original_write(fd, data[:2])

        target = self.root / "audit.jsonl"
        with patch("core.storage.os.write", side_effect=short_write):
            append_text_durable(target, "完整中文记录\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "完整中文记录\n")


class TestGenerationJobs(IsolatedConcurrencyCase):
    def setUp(self):
        super().setUp()
        self.status = GenerationTaskStatus()
        self.enterContext(patch("services.generation_jobs.generation_status", self.status))

    def test_busy_process_fails_job_without_constructing_pipeline(self):
        with held_in_child(self.manager.base_dir), patch("services.generation_jobs.NovelPipeline") as pipeline:
            run_delivery_job("book")
            pipeline.assert_not_called()
        self.assertFalse(self.status.is_running)
        self.assertIn("其他进程", self.status.snapshot()["error"])

    def test_deleted_project_does_not_turn_regeneration_into_creation(self):
        token = self.status.reserve("book")
        self.manager.delete_project()
        with patch("services.generation_jobs.NovelPipeline") as pipeline:
            run_regenerate_job("book", 1, token)
            pipeline.assert_not_called()
        self.assertFalse((self.root / "book").exists())
        self.assertIn("已不存在", self.status.snapshot()["error"])

    def test_delivery_success_and_missing_package_are_terminal(self):
        fake = Mock()
        with patch("services.generation_jobs.NovelPipeline", return_value=fake):
            run_delivery_job("book")
            self.assertEqual(self.status.snapshot()["progress"], 100)
            self.assertEqual(self.status.snapshot()["error"], "")
            fake.generate_commercial_package.return_value = None
            run_delivery_job("book")
        self.assertFalse(self.status.is_running)
        self.assertIn("缺少策划案", self.status.snapshot()["error"])

    def test_derived_jobs_recheck_revision_before_any_model_call(self):
        stale_revision = self.manager.revision
        self.manager.set_concept(concept("排队后更新"))
        for runner, method, args in (
            (run_short_drama_job, "generate_short_drama_script", (1,)),
            (run_audio_package_job, "generate_audio_drama_package", ()),
            (run_delivery_job, "generate_commercial_package", ()),
            (run_reader_simulation_job, "simulate_chapter_readers", (1,)),
            (run_branch_simulation_job, "simulate_chapter_branching", (1,)),
        ):
            pipeline = Mock(state_manager=StateManager("book", base_dir=str(self.root)))
            with patch("services.generation_jobs.NovelPipeline", return_value=pipeline):
                runner("book", *args, expected_revision=stale_revision)
            getattr(pipeline, method).assert_not_called()
            self.assertFalse(self.status.is_running)
            self.assertIn("作品已更新", self.status.snapshot()["error"])

    def test_web_continuation_rechecks_revision_before_planning_or_model_calls(self):
        stale_revision = self.manager.revision
        self.manager.set_concept(concept("排队后更新"))
        pipeline = Mock(state_manager=StateManager("book", base_dir=str(self.root)))
        with patch("services.generation_jobs.NovelPipeline", return_value=pipeline):
            run_pipeline_job(
                "book", "", "", None, 1, 3, expected_revision=stale_revision,
            )
        pipeline.resume_initialization.assert_not_called()
        pipeline.resume_initial_planning.assert_not_called()
        pipeline.generate_single_chapter.assert_not_called()
        self.assertFalse(self.status.is_running)
        self.assertIn("作品已更新", self.status.snapshot()["error"])

    def test_replaced_reservation_is_not_hijacked_by_delayed_job(self):
        token = self.status.reserve("book")
        self.status.finish(step="cancelled", task_id=token)
        fresh = self.status.reserve("book")
        before = self.status.snapshot()
        with self.assertRaises(RuntimeError):
            run_delivery_job("book", token)
        self.assertEqual(self.status.snapshot(), before)
        self.status.finish(step="done", task_id=fresh)
        with self.assertRaises(TaskReservationMismatch):
            run_delivery_job("book", token)

    def test_web_batch_records_first_failure_and_keeps_partial_progress(self):
        pipeline = NovelPipeline("book")

        def generate(number):
            if number == 2:
                raise RuntimeError("model unavailable")
            result = chapter(number)
            pipeline.state_manager.save_chapter(result)
            return result

        pipeline.generate_single_chapter = Mock(side_effect=generate)
        with patch("services.generation_jobs.NovelPipeline", return_value=pipeline) as cls:
            cls.plan_chapter_range = NovelPipeline.plan_chapter_range
            run_pipeline_job("book", "", "", None, 3, 1)
        self.assertEqual(pipeline.generate_single_chapter.call_count, 2)
        self.assertFalse(self.status.is_running)
        self.assertEqual(self.status.snapshot()["progress"], 33)
        report = [json.loads(line) for line in (self.manager.base_dir / "generation_report.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(item["chapter"], item["ok"]) for item in report], [(1, True), (2, False)])

    def test_new_job_never_silently_continues_existing_project(self):
        with patch("services.generation_jobs.NovelPipeline") as pipeline:
            run_pipeline_job("book", "新的创意", "", None, 3, 1)
            pipeline.assert_not_called()
        self.assertIn("拒绝", self.status.snapshot()["error"])


class TestConcurrencyEndpoints(IsolatedConcurrencyCase):
    def test_api_returns_busy_for_write_and_backup_but_reader_still_works(self):
        from fastapi.testclient import TestClient

        import web_ui

        with TestClient(web_ui.app) as client, held_in_child(self.manager.base_dir):
            response = client.patch("/api/novel/book/concept", json={"title": "不能写", "expected_revision": self.manager.revision})
            self.assertEqual(response.status_code, 423)
            self.assertEqual(response.headers["retry-after"], "2")
            self.assertEqual(client.get("/api/novel/book/export_backup").status_code, 423)
            self.assertEqual(client.get("/api/novel/book").status_code, 200)

    def test_stale_api_mutation_returns_conflict(self):
        from fastapi.testclient import TestClient

        import web_ui

        stale = NovelPipeline("book")
        self.manager.set_concept(concept("最新"))
        with TestClient(web_ui.app) as client, patch("web_ui.NovelPipeline", return_value=stale):
            response = client.patch("/api/novel/book/concept", json={"title": "陈旧", "expected_revision": stale.state_manager.revision})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(StateManager("book").state.concept.title, "最新")

    def test_cli_reports_busy_before_reaching_model(self):
        from typer.testing import CliRunner

        from main import app

        with held_in_child(self.manager.base_dir):
            result = CliRunner().invoke(app, ["continue-gen", "--name", "book"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("其他进程", result.output)


if __name__ == "__main__":
    unittest.main()
