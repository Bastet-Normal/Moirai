"""Crash and fault injection tests; all files live in disposable test roots."""

import io
import json
import multiprocessing
import os
import shutil
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from core.project_archive import import_project_archive
from core.project_transactions import (
    JOURNAL_FOLDER,
    TRASH_FOLDER,
    ProjectTransaction,
    inspect_recovery_journals,
    recover_all_projects,
    recover_all_projects_detailed,
    recover_project,
)
from core.schemas import ChapterContent, NovelConcept, StateUpdateDelta
from core.state_manager import StateManager
from core.storage import (
    ProjectRecoveryError,
    atomic_write_text,
    file_transaction,
    project_lock,
    read_json,
)


def _chapter(number, text):
    return ChapterContent(chapter_number=number, volume_number=1, title=f"chapter {number}", full_text=text)


def _tree(project):
    return {path.relative_to(project).as_posix(): path.read_bytes() for path in project.rglob("*") if path.is_file()}


def _crash_writer(root, operation, boundary=3):
    manager = StateManager("book", base_dir=root)
    original_replace = os.replace
    count = 0

    def replace(source, destination):
        nonlocal count
        original_replace(source, destination)
        if Path(destination).is_relative_to(Path(root) / "book"):
            count += 1
            if count == boundary:
                os._exit(91)

    def move_then_die(transaction, *args, **kwargs):
        original_move(transaction, *args, **kwargs)
        os._exit(91)

    original_move = ProjectTransaction._move
    if operation == "chapter":
        with patch("core.storage.os.replace", side_effect=replace):
            manager.save_chapter(_chapter(3, "must never partially survive"), StateUpdateDelta(summary_of_events="three"))
    elif operation in {"rename", "trash"}:
        with patch.object(ProjectTransaction, "_move", move_then_die):
            if operation == "rename":
                manager.rename_project("renamed")
            else:
                manager.delete_project()
    elif operation == "commit":
        with patch.object(ProjectTransaction, "_cleanup_after_commit", side_effect=lambda: os._exit(91)):
            manager.save_chapter(_chapter(3, "fully committed"), StateUpdateDelta(summary_of_events="three"))
    elif operation == "rename_after_files":
        with manager.mutation():
            manager.rename_project("renamed")
            os._exit(91)


def _crash_recovery(root, move_only):
    original = ProjectTransaction._undo

    def undo_then_die(transaction, entry):
        original(transaction, entry)
        if not move_only or entry["kind"] == "move":
            os._exit(92)

    with patch.object(ProjectTransaction, "_undo", undo_then_die):
        recover_project(Path(root), "book")


def _live_writer(root, acquired, release):
    manager = StateManager("book", base_dir=root)
    with manager.mutation():
        manager.set_concept(NovelConcept(title="live writer committed", genre="g", logline="l", golden_finger="g", core_conflict="c"))
        acquired.set()
        if not release.wait(15):
            raise RuntimeError("reader failed to release test writer")


def _run_child(target, *args, expected):
    process = multiprocessing.get_context("spawn").Process(target=target, args=args)
    process.start()
    process.join(15)
    if process.is_alive():
        process.terminate()
        process.join(5)
        raise AssertionError("fault-injection child timed out")
    if process.exitcode != expected:
        raise AssertionError(f"expected injected exit {expected}, got {process.exitcode}")


class TestRecovery(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="novelengine-recovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.manager = StateManager("book", base_dir=temporary.name)
        self.manager.set_concept(NovelConcept(title="Original", genre="test", logline="l", golden_finger="g", core_conflict="c"))
        self.manager.save_chapter(_chapter(1, "first original"), StateUpdateDelta(summary_of_events="one"))
        self.manager.save_chapter(_chapter(2, "second original"), StateUpdateDelta(summary_of_events="two"))
        self.before = _tree(self.manager.base_dir)

    def assertRestored(self):
        reloaded = StateManager("book", base_dir=str(self.root))
        self.assertEqual(_tree(reloaded.base_dir), self.before)
        self.assertEqual(sorted(reloaded.state.chapters), [1, 2])
        self.assertEqual(list((self.root / JOURNAL_FOLDER).iterdir()), [])

    def test_every_chapter_publication_failure_restores_the_exact_file_set(self):
        # First measure actual boundaries rather than guessing a convenient few.
        trial_root = self.root / "trial"
        shutil.copytree(self.manager.base_dir, trial_root / "book")
        trial = StateManager("book", base_dir=str(trial_root))
        real_replace = os.replace
        publications = []

        def record(source, destination):
            real_replace(source, destination)
            if Path(destination).is_relative_to(trial.base_dir):
                publications.append(str(destination))

        with patch("core.storage.os.replace", side_effect=record):
            trial.save_chapter(_chapter(3, "third"), StateUpdateDelta(summary_of_events="three"))
        self.assertGreater(len(publications), 10)

        for failure_at in range(1, len(publications) + 1):
            with self.subTest(publication=failure_at):
                count = 0
                failed = False

                def fail_after_replace(source, destination, failure_at=failure_at):
                    nonlocal count, failed
                    real_replace(source, destination)
                    if Path(destination).is_relative_to(self.manager.base_dir) and not failed:
                        count += 1
                        if count == failure_at:
                            failed = True
                            raise OSError("injected post-publication failure")

                self.manager.load_state()
                with patch("core.storage.os.replace", side_effect=fail_after_replace), self.assertRaises(OSError):
                    self.manager.save_chapter(_chapter(3, "third"), StateUpdateDelta(summary_of_events="three"))
                self.assertTrue(failed)
                self.assertRestored()

    def test_rollback_deletion_failure_restores_future_chapters_and_snapshots(self):
        from core.state_manager import remove_file

        count = 0

        def fail_after_delete(path, **kwargs):
            nonlocal count
            remove_file(path, **kwargs)
            count += 1
            if count == 2:
                raise OSError("injected deletion failure")

        with patch("core.state_manager.remove_file", side_effect=fail_after_delete), self.assertRaises(RuntimeError):
            self.manager.rollback_to_chapter(1)
        self.assertRestored()

    def test_failed_new_project_does_not_leave_partial_directories(self):
        manager = StateManager("newbook", base_dir=str(self.root))
        with self.assertRaises(RuntimeError), manager.mutation():
            manager.set_concept(NovelConcept(title="X", genre="g", logline="l", golden_finger="g", core_conflict="c"))
            raise RuntimeError("fail before commit")
        self.assertFalse(manager.base_dir.exists())
        self.assertEqual(list((self.root / JOURNAL_FOLDER).iterdir()), [])

    def test_process_crash_before_commit_recovers_on_load(self):
        _run_child(_crash_writer, str(self.root), "chapter", expected=91)
        self.assertTrue(list((self.root / JOURNAL_FOLDER).iterdir()))
        self.assertNotEqual(_tree(self.manager.base_dir), self.before)
        self.assertRestored()

    def test_process_crash_after_commit_preserves_the_complete_new_state(self):
        _run_child(_crash_writer, str(self.root), "commit", expected=91)
        reloaded = StateManager("book", base_dir=str(self.root))
        self.assertEqual(reloaded.state.chapters[3].full_text, "fully committed")
        self.assertEqual(reloaded.state.current_chapter_idx, 3)
        self.assertTrue((reloaded.memory_dir / "chapter_003.json").is_file())
        self.assertTrue((reloaded.state_events_dir / "chapter_003.json").is_file())
        self.assertEqual(list((self.root / JOURNAL_FOLDER).iterdir()), [])

    def test_interrupted_rename_is_recovered_even_when_old_name_is_missing(self):
        _run_child(_crash_writer, str(self.root), "rename", expected=91)
        self.assertFalse(self.manager.base_dir.exists())
        self.assertTrue((self.root / "renamed").exists())
        self.assertTrue(StateManager.project_exists("book", base_dir=str(self.root)))
        self.assertFalse((self.root / "renamed").exists())
        self.assertRestored()

    def test_reading_new_name_can_also_recover_an_interrupted_rename(self):
        _run_child(_crash_writer, str(self.root), "rename_after_files", expected=91)
        recover_project(self.root, "renamed")
        self.assertFalse((self.root / "renamed").exists())
        self.assertRestored()

    def test_interrupted_delete_restores_project_before_listing(self):
        _run_child(_crash_writer, str(self.root), "trash", expected=91)
        self.assertFalse(self.manager.base_dir.exists())
        self.assertEqual(recover_all_projects(self.root), [])
        self.assertRestored()

    def test_committed_delete_keeps_recoverable_original_files(self):
        self.manager.delete_project()
        trash = self.manager.last_trash_path
        self.assertEqual(trash.parent, self.root / TRASH_FOLDER)
        self.assertEqual(_tree(trash), self.before)
        self.assertFalse(self.manager.base_dir.exists())
        self.assertEqual(list((self.root / JOURNAL_FOLDER).iterdir()), [])

    def test_recovery_is_idempotent_when_it_crashes_after_a_file_restore(self):
        _run_child(_crash_writer, str(self.root), "chapter", expected=91)
        _run_child(_crash_recovery, str(self.root), False, expected=92)
        self.assertRestored()
        recover_project(self.root, "book")
        self.assertRestored()

    def test_recovery_is_idempotent_when_it_crashes_after_undoing_a_rename(self):
        _run_child(_crash_writer, str(self.root), "rename_after_files", expected=91)
        _run_child(_crash_recovery, str(self.root), True, expected=92)
        self.assertRestored()

    def _abandoned_transaction(self):
        with project_lock(self.manager.base_dir), project_lock(self.manager.base_dir, scope="snapshot"):
            transaction = ProjectTransaction(self.root, "book")
            with file_transaction(transaction):
                atomic_write_text(self.manager.base_dir / "concept.json", "temporary damaged value")
        return transaction

    def test_recovery_validates_all_backup_checksums_before_touching_user_files(self):
        transaction = self._abandoned_transaction()
        backup = next(transaction.directory.glob("*.bak"))
        backup.write_bytes(b"corrupted backup")
        before_attempt = _tree(self.manager.base_dir)
        with self.assertRaises(ProjectRecoveryError):
            recover_project(self.root, "book")
        self.assertEqual(_tree(self.manager.base_dir), before_attempt)
        self.assertTrue(transaction.directory.exists())

    def test_corrupt_header_returns_path_free_guidance_instead_of_crashing_listing(self):
        transaction = self._abandoned_transaction()
        (transaction.directory / "header.json").write_text("{broken", encoding="utf-8")
        before_attempt = _tree(self.manager.base_dir)

        diagnostics = recover_all_projects_detailed(self.root)

        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["journal_id"], transaction.transaction_id)
        self.assertEqual(diagnostics[0]["code"], "corrupt_transaction_header")
        self.assertTrue(diagnostics[0]["blocks_all_projects"])
        self.assertEqual(diagnostics[0]["project_ids"], [])
        self.assertNotIn(str(self.root), json.dumps(diagnostics, ensure_ascii=False))
        self.assertEqual(_tree(self.manager.base_dir), before_attempt)
        self.assertTrue(transaction.directory.exists())
        self.assertTrue(any(transaction.transaction_id in warning for warning in recover_all_projects(self.root)))

    def test_read_only_diagnostics_identify_corrupt_known_project_and_preserve_evidence(self):
        transaction = self._abandoned_transaction()
        next(transaction.directory.glob("*.bak")).write_bytes(b"corrupted backup")
        before_attempt = _tree(self.manager.base_dir)

        diagnostics = inspect_recovery_journals(self.root)

        self.assertEqual(diagnostics[0]["code"], "corrupt_transaction_journal")
        self.assertEqual(diagnostics[0]["project_ids"], ["book"])
        self.assertFalse(diagnostics[0]["blocks_all_projects"])
        self.assertTrue(diagnostics[0]["preserved"])
        self.assertEqual(_tree(self.manager.base_dir), before_attempt)

    def test_pristine_pre_header_crash_is_reported_but_does_not_block_safe_recovery(self):
        journal_id = "a" * 32
        work = self.root / JOURNAL_FOLDER / journal_id / "work"
        work.mkdir(parents=True)

        diagnostics = recover_all_projects_detailed(self.root)

        self.assertEqual(diagnostics[0]["code"], "incomplete_transaction_initialization")
        self.assertFalse(diagnostics[0]["blocks_all_projects"])
        self.assertTrue(work.parent.exists())

    def test_nonpristine_headerless_journal_blocks_all_recovery_without_touching_projects(self):
        journal_id = "c" * 32
        journal = self.root / JOURNAL_FOLDER / journal_id
        journal.mkdir(parents=True)
        (journal / "undo.jsonl").write_text("unknown ownership\n", encoding="utf-8")
        before_attempt = _tree(self.manager.base_dir)

        diagnostics = recover_all_projects_detailed(self.root)

        self.assertEqual(diagnostics[0]["code"], "missing_transaction_header")
        self.assertTrue(diagnostics[0]["blocks_all_projects"])
        with self.assertRaisesRegex(ProjectRecoveryError, journal_id):
            recover_project(self.root, "book")
        self.assertEqual(_tree(self.manager.base_dir), before_attempt)
        self.assertTrue(journal.exists())

    def test_corrupt_known_journal_blocks_only_its_declared_project(self):
        other = StateManager("other", base_dir=str(self.root))
        other.set_concept(NovelConcept(title="Other", genre="g", logline="l", golden_finger="g", core_conflict="c"))
        transaction = self._abandoned_transaction()
        next(transaction.directory.glob("*.bak")).write_bytes(b"corrupted backup")

        with self.assertRaises(ProjectRecoveryError):
            StateManager("book", base_dir=str(self.root))
        reloaded_other = StateManager("other", base_dir=str(self.root))
        self.assertEqual(reloaded_other.state.concept.title, "Other")
        self.assertTrue(transaction.directory.exists())

    def test_oversized_commit_marker_is_bounded_and_never_rolls_back(self):
        transaction = self._abandoned_transaction()
        (transaction.directory / "COMMITTED").write_text("x" * 129, encoding="utf-8")
        before_attempt = _tree(self.manager.base_dir)

        diagnostics = recover_all_projects_detailed(self.root)

        self.assertEqual(diagnostics[0]["code"], "corrupt_transaction_journal")
        self.assertIn("提交标记损坏", diagnostics[0]["message"])
        self.assertEqual(_tree(self.manager.base_dir), before_attempt)
        self.assertTrue(transaction.directory.exists())

    def test_nonregular_transaction_root_blocks_writes_without_touching_project(self):
        transaction_root = self.root / JOURNAL_FOLDER
        self.assertEqual(list(transaction_root.iterdir()), [])
        transaction_root.rmdir()
        transaction_root.write_text("external obstruction", encoding="utf-8")
        before_attempt = _tree(self.manager.base_dir)

        diagnostics = recover_all_projects_detailed(self.root)

        self.assertEqual(diagnostics[0]["code"], "invalid_transaction_root")
        self.assertTrue(diagnostics[0]["blocks_all_projects"])
        with self.assertRaises(ProjectRecoveryError):
            recover_project(self.root, "book")
        self.assertEqual(_tree(self.manager.base_dir), before_attempt)

    def test_nonregular_undo_log_is_rejected_and_preserved(self):
        transaction = self._abandoned_transaction()
        transaction.log_file.unlink()
        transaction.log_file.mkdir()
        before_attempt = _tree(self.manager.base_dir)

        diagnostics = recover_all_projects_detailed(self.root)

        self.assertEqual(diagnostics[0]["code"], "corrupt_transaction_journal")
        self.assertIn("无法安全读取", diagnostics[0]["message"])
        self.assertEqual(_tree(self.manager.base_dir), before_attempt)
        self.assertTrue(transaction.log_file.is_dir())

    def test_recovery_io_error_becomes_retryable_diagnostic_and_keeps_journal(self):
        transaction = self._abandoned_transaction()
        before_attempt = _tree(self.manager.base_dir)

        with patch.object(ProjectTransaction, "_undo", side_effect=OSError("temporarily locked")):
            diagnostics = recover_all_projects_detailed(self.root)

        self.assertEqual(diagnostics[0]["code"], "recovery_blocked")
        self.assertIn("文件系统错误", diagnostics[0]["message"])
        self.assertEqual(_tree(self.manager.base_dir), before_attempt)
        self.assertTrue(transaction.directory.exists())

    def test_recovery_rejects_path_escape_without_touching_any_target(self):
        transaction = self._abandoned_transaction()
        sentinel = self.root / "sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with transaction.log_file.open("ab") as output:
            output.write(json.dumps({"kind": "file", "path": "../sentinel.txt", "backup": None}).encode() + b"\n")
        before_attempt = _tree(self.manager.base_dir)
        with self.assertRaises(ProjectRecoveryError):
            recover_project(self.root, "book")
        self.assertEqual(_tree(self.manager.base_dir), before_attempt)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_torn_last_journal_append_is_ignored_before_unperformed_mutation(self):
        transaction = self._abandoned_transaction()
        with transaction.log_file.open("ab") as output:
            output.write(b'{"kind":"file","path":')
        self.assertRestored()

    def test_first_change_only_is_backed_up_and_storage_budget_fails_safely(self):
        transaction = self._abandoned_transaction()
        self.assertEqual(len(list(transaction.directory.glob("*.bak"))), 1)
        with file_transaction(transaction):
            atomic_write_text(self.manager.base_dir / "concept.json", "second change")
        self.assertEqual(len(list(transaction.directory.glob("*.bak"))), 1)
        self.assertRestored()
        self.manager.load_state()
        with patch("core.project_transactions.MAX_BACKUP_BYTES", 4), self.assertRaises(ProjectRecoveryError):
            self.manager.set_concept(NovelConcept(title="Too big", genre="g", logline="l", golden_finger="g", core_conflict="c"))
        self.assertRestored()

    def test_same_thread_read_never_recovers_its_live_transaction(self):
        with self.manager.mutation():
            self.manager.set_concept(NovelConcept(title="Committed later", genre="g", logline="l", golden_finger="g", core_conflict="c"))
            self.assertTrue(StateManager.project_exists("book", base_dir=str(self.root)))
            self.assertEqual(read_json(self.manager.base_dir / "concept.json")["title"], "Committed later")
        self.assertEqual(StateManager("book", base_dir=str(self.root)).state.concept.title, "Committed later")

    def test_other_process_reader_waits_without_rolling_back_live_writer(self):
        context = multiprocessing.get_context("spawn")
        acquired, release = context.Event(), context.Event()
        process = context.Process(target=_live_writer, args=(str(self.root), acquired, release))
        process.start()
        try:
            self.assertTrue(acquired.wait(8))
            with ThreadPoolExecutor(max_workers=1) as executor:
                started = threading.Event()

                def read():
                    started.set()
                    return StateManager("book", base_dir=str(self.root)).state.concept.title

                reader = executor.submit(read)
                self.assertTrue(started.wait(3))
                self.assertFalse(reader.done())
                release.set()
                self.assertEqual(reader.result(8), "live writer committed")
        finally:
            release.set()
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)
        self.assertEqual(process.exitcode, 0)

    def test_commit_marker_failure_before_publication_undoes_changes(self):
        real_replace = os.replace

        def fail_commit(source, destination):
            if Path(destination).name == "COMMITTED":
                raise OSError("commit marker unavailable")
            real_replace(source, destination)

        with patch("core.storage.os.replace", side_effect=fail_commit), self.assertRaises(OSError):
            self.manager.save_chapter(_chapter(3, "not committed"), StateUpdateDelta(summary_of_events="three"))
        self.assertRestored()

    def test_commit_marker_lost_ack_keeps_successfully_committed_state(self):
        real_replace = os.replace

        def lose_ack(source, destination):
            real_replace(source, destination)
            if Path(destination).name == "COMMITTED":
                raise OSError("commit succeeded but acknowledgement lost")

        with patch("core.storage.os.replace", side_effect=lose_ack):
            self.manager.save_chapter(_chapter(3, "committed"), StateUpdateDelta(summary_of_events="three"))
        self.assertEqual(StateManager("book", base_dir=str(self.root)).state.chapters[3].full_text, "committed")

    def test_import_recovers_abandoned_delete_before_checking_name_collision(self):
        _run_child(_crash_writer, str(self.root), "trash", expected=91)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("book/project_state.json", self.before["project_state.json"])
        with self.assertRaises(FileExistsError):
            import_project_archive(archive.getvalue(), self.root)
        self.assertRestored()

    def test_rename_does_not_remove_unrelated_files_with_the_old_prefix(self):
        user_file = self.manager.base_dir / "book_research_notes.txt"
        user_file.write_text("user notes", encoding="utf-8")
        destination = self.manager.rename_project("renamed")
        self.assertEqual((destination / user_file.name).read_text(encoding="utf-8"), "user notes")

    @unittest.skipUnless(os.name == "nt", "case aliases are a Windows filesystem boundary")
    def test_windows_case_alias_still_journals_and_recovers_writes(self):
        aliased = StateManager("BOOK", base_dir=str(self.root))
        with self.assertRaises(RuntimeError), aliased.mutation():
            aliased.replace_chapter_text(1, "alias must not bypass the journal")
            raise RuntimeError("fail alias write")
        self.assertRestored()


if __name__ == "__main__":
    unittest.main()
