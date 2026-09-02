import hashlib
import json
import logging
import re
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console

from config.settings import get_output_dir
from core import character_overrides
from core.error_reporting import SafeUserMessage, referenced_failure, report_internal_error
from core.knowledge_graph import KnowledgeGraph
from core.multimodal_generator import AudioDramaPackage, ShortDramaScript
from core.narrative_brancher import BranchingSimulationResult
from core.project_export import export_epub, export_markdown, export_platform_chapter, export_platform_text
from core.project_ids import is_valid_project_id, require_project_id
from core.project_transactions import ProjectTransaction, recover_project
from core.reader_simulator import ReaderSimulationReport
from core.schemas import (
    ChapterContent,
    ChapterOutline,
    CharacterCard,
    CharacterState,
    CommercialDeliveryPackage,
    ForeshadowingRecord,
    GlobalOutline,
    HumanChapterReview,
    NovelConcept,
    NovelProjectState,
    StateUpdateDelta,
    VolumeOutline,
    WorldSetting,
)
from core.semantic_memory import MemoryChunk, SemanticMemoryIndex
from core.storage import (
    ProjectConflictError,
    ProjectReadOnlyError,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    ensure_directory,
    file_transaction,
    project_lock,
    project_mutation,
    project_revision_path,
    project_version,
    read_json,
    remove_file,
    revision_token,
)
from core.text_limits import check_chapter_length, check_replacement_length

console = Console()
logger = logging.getLogger("novelengine.state")

_STATIC_CHARACTER_OVERRIDE_FIELDS = character_overrides.STATIC_FIELDS
_DYNAMIC_CHARACTER_OVERRIDE_FIELDS = character_overrides.DYNAMIC_FIELDS
_MAX_MANUAL_OVERRIDE_BYTES = character_overrides.MAX_FILE_BYTES
_MAX_DYNAMIC_OVERRIDE_EVENTS = character_overrides.MAX_HISTORY_EVENTS
_MAX_TIMELINE_FILE_BYTES = 16 * 1024 * 1024
_MAX_TIMELINE_CHAPTERS = 1_000_000


class ChapterContextError(SafeUserMessage, ValueError):
    """The requested chapter has no trustworthy pre-chapter state."""


class StateManager:
    """小说状态与持久化记忆管理中心（整合知识图谱、多模态、百万字语义记忆与读者心智模拟）"""

    def __init__(
        self,
        novel_id: str,
        base_dir: str | None = None,
        *,
        load_chapter_content: bool = True,
        load_memory: bool = True,
        load_derived: bool = True,
    ):
        self.novel_id = self._sanitize_filename(novel_id)
        root = (Path(base_dir) if base_dir else get_output_dir()).resolve()
        self.root_dir = root
        self._set_project_paths(self.novel_id)
        self._load_chapter_content = load_chapter_content
        self._load_memory = load_memory
        self._load_derived = load_derived
        self._mutation_depth = 0
        self._mutation_failed = False
        self._mutation_stack: ExitStack | None = None
        self._transaction: ProjectTransaction | None = None
        self._export_defer_depth = 0
        self._full_exports_dirty = False
        self.last_trash_path: Path | None = None
        self.load_state()

    def _set_project_paths(self, novel_id: str) -> None:
        self.novel_id = require_project_id(novel_id)
        root = self.root_dir
        self.base_dir = (root / self.novel_id).resolve()
        if self.base_dir.parent != root:
            raise ValueError("项目路径越界")
        if self.base_dir.is_dir():
            self.novel_id = self.base_dir.name
        self.chapters_dir = self.base_dir / "chapters"
        self.snapshots_dir = self.base_dir / "snapshots"
        self.delivery_dir = self.base_dir / "delivery_package"
        self.multimodal_dir = self.base_dir / "multimodal"
        self.reader_sim_dir = self.base_dir / "reader_simulations"
        self.state_events_dir = self.base_dir / "state_events"
        self.memory_dir = self.base_dir / "memory"
        self.chapter_meta_dir = self.chapters_dir / "metadata"

    def _reset_loaded_state(self) -> None:
        """A reload replaces the view; it must not accumulate shards or warnings."""
        self.state = NovelProjectState(novel_id=self.novel_id)
        self.knowledge_graph = KnowledgeGraph()
        self.semantic_memory = SemanticMemoryIndex()
        self.short_drama_scripts: dict[int, ShortDramaScript] = {}
        self.audio_package: AudioDramaPackage | None = None
        self.branch_simulations: dict[int, BranchingSimulationResult] = {}
        self.reader_simulations: dict[int, ReaderSimulationReport] = {}
        self.load_warnings: list[str] = []
        self._legacy_memory_needs_migration = False
        self.semantic_memory_chunk_count = 0
        self._chapter_metadata_ready = False

    def assert_writable_current(self, *, require_full: bool = True) -> None:
        if not self._load_chapter_content or (require_full and not (self._load_memory and self._load_derived)):
            raise ProjectReadOnlyError("当前项目视图缺少本次写入所需数据，请完整加载项目后再修改")
        if self.load_warnings:
            raise ProjectReadOnlyError("项目存在无法读取的数据，已禁止写入以保护原文件；请先检查加载警告")
        if self._mutation_failed:
            raise ProjectConflictError("上次写入未完成，请重新加载并检查项目后再操作")
        if not self._mutation_depth and project_version(self.base_dir) != self._loaded_version:
            raise ProjectConflictError("作品已被其他任务修改、删除或重命名，请刷新后重试")

    @property
    def revision(self) -> str:
        """Revision of the loaded view, never a fresh token for stale data."""
        return revision_token(self.novel_id, self._loaded_version)

    def assert_revision(self, expected: str) -> None:
        if expected != self.revision:
            raise ProjectConflictError("作品已更新；本次修改未保存，请保留输入内容并刷新后重新核对")

    @contextmanager
    def operation(self) -> Iterator[None]:
        """Own the project from context construction through generation and save."""
        with project_lock(self.base_dir, timeout=0):
            self.assert_writable_current()
            yield

    @contextmanager
    def defer_full_exports(self) -> Iterator[None]:
        """Coalesce full-book exports while still publishing each changed shard."""
        self.assert_writable_current()
        self._export_defer_depth += 1
        try:
            yield
        except BaseException as primary:
            self._export_defer_depth -= 1
            if self._export_defer_depth == 0 and self._full_exports_dirty:
                try:
                    self._flush_full_exports()
                except Exception as export_error:
                    error_id = report_internal_error(
                        logger,
                        export_error,
                        scope="deferred_full_export",
                        context={"novel_id": self.novel_id},
                    )
                    primary.add_note(referenced_failure("同时刷新全本导出失败", error_id))
            raise
        else:
            self._export_defer_depth -= 1
            if self._export_defer_depth == 0 and self._full_exports_dirty:
                self._flush_full_exports()

    def _flush_full_exports(self) -> None:
        with self.mutation():
            export_markdown(self.base_dir, self.novel_id, self.state)
            export_platform_text(self.delivery_dir, self.novel_id, self.state, sync_chapters=False)
        self._full_exports_dirty = False

    def _refresh_exports_after_chapter(self, chapter: ChapterContent) -> None:
        export_platform_chapter(self.delivery_dir, chapter)
        if self._export_defer_depth:
            self._full_exports_dirty = True
            return
        export_markdown(self.base_dir, self.novel_id, self.state)
        export_platform_text(self.delivery_dir, self.novel_id, self.state, sync_chapters=False)

    @contextmanager
    def mutation(self, *, require_full: bool = True) -> Iterator[None]:
        """Serialize writes and reject stale aggregates before touching user files.

        The short snapshot lock covers only actual disk mutations, allowing UI
        reads during a long operation's network/model work. Revision tombstones
        are published before writing, so even a crashed writer invalidates old
        in-memory objects. An undo journal restores the complete previous file
        set when a write fails or the process dies before committing.
        """
        with project_lock(self.base_dir, timeout=0):
            if self._mutation_depth:
                yield
                return
            self.assert_writable_current(require_full=require_full)
            original_dir = self.base_dir
            with ExitStack() as stack:
                stack.enter_context(project_lock(original_dir, scope="snapshot"))
                self._mutation_stack = stack
                atomic_write_text(project_revision_path(original_dir), uuid.uuid4().hex)
                transaction = ProjectTransaction(self.root_dir, self.novel_id)
                self._transaction = transaction
                self._mutation_depth = 1
                try:
                    with file_transaction(transaction):
                        self._ensure_directories()
                        yield
                    transaction.commit()
                except BaseException:
                    self._mutation_failed = True
                    if transaction.directory.exists() and not transaction.committed():
                        transaction.rollback()
                        self._set_project_paths(original_dir.name)
                    raise
                finally:
                    try:
                        for path in {self.root_dir / name for name in transaction.participants}:
                            atomic_write_text(project_revision_path(path), uuid.uuid4().hex)
                        self._loaded_version = project_version(self.base_dir)
                    finally:
                        self._mutation_depth = 0
                        self._mutation_stack = None
                        self._transaction = None

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        return require_project_id(name)

    @classmethod
    def is_valid_project_id(cls, name: str) -> bool:
        return is_valid_project_id(name)

    @classmethod
    def project_exists(cls, novel_id: str, base_dir: str | None = None) -> bool:
        """判断项目是否已存在（用于接口层提前 404，避免为不存在的项目创建垃圾目录）"""
        try:
            safe_id = cls._sanitize_filename(novel_id)
        except ValueError:
            return False
        root = (Path(base_dir) if base_dir else get_output_dir()).resolve()
        recover_project(root, safe_id)
        candidate = (root / safe_id).resolve()
        return candidate.parent == root and (candidate / "project_state.json").is_file()

    @classmethod
    def read_project_chapter(
        cls,
        novel_id: str,
        chapter_number: int,
        base_dir: str | None = None,
    ) -> ChapterContent | None:
        """Read one chapter without hydrating the complete project aggregate."""
        chapter, _ = cls.read_project_chapter_versioned(novel_id, chapter_number, base_dir)
        return chapter

    @classmethod
    def read_project_chapter_versioned(
        cls, novel_id: str, chapter_number: int, base_dir: str | None = None,
    ) -> tuple[ChapterContent | None, str]:
        """Read text and its revision under the same short snapshot lock."""
        if chapter_number < 1 or not cls.is_valid_project_id(novel_id):
            return None, ""
        root = (Path(base_dir) if base_dir else get_output_dir()).resolve()
        recover_project(root, str(novel_id))
        project_dir = (root / str(novel_id)).resolve()
        if project_dir.parent != root or not (project_dir / "project_state.json").is_file():
            return None, ""
        with project_lock(project_dir, scope="snapshot"):
            chapter_file = project_dir / "chapters" / f"chapter_{chapter_number:03d}.json"
            token = revision_token(project_dir.name, project_version(project_dir))
            if not chapter_file.is_file():
                return None, token
            return ChapterContent.model_validate(read_json(chapter_file)), token

    def _ensure_directories(self):
        ensure_directory(self.base_dir)
        ensure_directory(self.chapters_dir)
        ensure_directory(self.snapshots_dir)
        ensure_directory(self.delivery_dir)
        ensure_directory(self.multimodal_dir)
        ensure_directory(self.reader_sim_dir)
        ensure_directory(self.state_events_dir)
        ensure_directory(self.memory_dir)
        ensure_directory(self.chapter_meta_dir)

    @staticmethod
    def _chapter_file_number(path: Path) -> int:
        """Zero padding is not a chronological sort once a novel exceeds 999 chapters."""
        match = re.search(r"(\d+)(?=\.json$)", path.name)
        return int(match.group(1)) if match else -1

    def _persist_chapter_content(self, chapter: ChapterContent) -> None:
        self._persist_chapter_record(chapter)
        atomic_write_text(
            self.chapters_dir / f"chapter_{chapter.chapter_number:03d}.txt",
            f"第{chapter.chapter_number}章 {chapter.title}\n\n{chapter.full_text}\n",
        )

    def _persist_chapter_record(self, chapter: ChapterContent) -> None:
        atomic_write_text(
            self.chapters_dir / f"chapter_{chapter.chapter_number:03d}.json",
            chapter.model_dump_json(indent=2),
        )

    @project_mutation
    def save_state(self):
        # Chapters and memory are sharded. Rewriting either aggregate for every
        # chapter makes long-book persistence quadratic in total prose size.
        state_data = self.state.model_dump(mode="json", exclude={"chapters"})
        state_data["chapters"] = {}
        atomic_write_json(self.base_dir / "project_state.json", state_data)
        atomic_write_text(self.base_dir / "knowledge_graph.json", self.knowledge_graph.model_dump_json(indent=2))
        if not self._chapter_metadata_ready:
            self._ensure_chapter_metadata()
        if self._legacy_memory_needs_migration:
            self._persist_memory_shards()
            remove_file(self.base_dir / "semantic_memory.json")
            self._legacy_memory_needs_migration = False

    @project_mutation
    def replace_chapter_text(
        self,
        chapter_number: int,
        new_text: str,
        *,
        mark_reconciliation: bool = False,
    ) -> bool:
        """整章文本人工编辑：重算字数、重建该章语义记忆并导出发布文件"""
        if chapter_number not in self.state.chapters:
            return False
        check_chapter_length(len(new_text))
        chap = self.state.chapters[chapter_number].model_copy(deep=True)
        chap.full_text = new_text
        chap.word_count = len([c for c in new_text if '\u4e00' <= c <= '\u9fff'])
        self.save_chapter(chap)
        if mark_reconciliation:
            self.mark_state_reconciliation_required(chapter_number, "chapter text edited without state extraction")
        return True

    @project_mutation
    def rename_project(self, new_novel_id: str) -> "Path":
        """项目重命名：目录整体迁移、状态与导出文件同步更新，旧命名产物清理"""
        new_id = self._sanitize_filename(new_novel_id)
        if new_id == self.novel_id:
            return self.base_dir
        new_dir = self.base_dir.parent / new_id
        # Reserve both names until the outer mutation has published revisions.
        self._mutation_stack.enter_context(project_lock(new_dir, timeout=0))
        self._mutation_stack.enter_context(project_lock(new_dir, scope="snapshot"))
        recover_project(self.root_dir, new_id)
        if new_dir.exists():
            raise ValueError(f"已存在同名项目: {new_id}")

        old_id = self.novel_id
        self._transaction.rename_project(self.base_dir, new_dir)
        self._set_project_paths(new_id)
        self.state.novel_id = new_id
        self._rewrite_embedded_project_identity(new_id, previous_id=old_id)

        # 清理旧命名导出物（全本 md / 发布 txt / epub）
        for stale in (new_dir / f"{old_id}_全本.md", new_dir / f"{old_id}.epub",
                      self.delivery_dir / f"{old_id}_全本发布稿.txt"):
            remove_file(stale)

        self.save_state()
        self.export_markdown()
        self.export_platform_delivery_files()
        return new_dir

    def _rewrite_embedded_project_identity(self, new_id: str, *, previous_id: str) -> None:
        """Keep rollback/timeline evidence usable after a transactional rename."""
        if self._timeline_baseline_file.is_file():
            baseline = read_json(self._timeline_baseline_file, max_bytes=_MAX_TIMELINE_FILE_BYTES)
            if not isinstance(baseline, dict) or not isinstance(baseline.get("state"), dict):
                raise ValueError("状态事件基线损坏，无法安全重命名")
            baseline["novel_id"] = new_id
            baseline["state"]["novel_id"] = new_id
            atomic_write_json(self._timeline_baseline_file, baseline)
        for event_file in self.state_events_dir.glob("chapter_*.json"):
            event = read_json(event_file, max_bytes=_MAX_TIMELINE_FILE_BYTES)
            if not isinstance(event, dict):
                raise ValueError("状态事件损坏，无法安全重命名")
            event["novel_id"] = new_id
            atomic_write_json(event_file, event)
        for snapshot_file in self.snapshots_dir.glob("snapshot_after_chap_*.json"):
            snapshot = read_json(snapshot_file)
            if not isinstance(snapshot, dict):
                raise ValueError("章节快照损坏，无法安全重命名")
            raw_state = snapshot.get("state", snapshot)
            if not isinstance(raw_state, dict):
                raise ValueError("章节快照状态损坏，无法安全重命名")
            raw_state["novel_id"] = new_id
            if "state" in snapshot:
                snapshot["novel_id"] = new_id
            atomic_write_json(snapshot_file, snapshot)
        retired_root = self.state_events_dir / "retired_timelines"
        if retired_root.is_dir():
            for retired in sorted(retired_root.iterdir()):
                if not retired.is_dir() or not re.fullmatch(
                    r"repair_\d{8}T\d{12}Z_[0-9a-f]{8}", retired.name,
                ):
                    raise ValueError("历史修复证据目录损坏，无法安全重命名")
                manifest_file = retired / "manifest.json"
                manifest = read_json(manifest_file, max_bytes=_MAX_TIMELINE_FILE_BYTES)
                if (
                    not isinstance(manifest, dict)
                    or manifest.get("repair_id") != retired.name
                    or manifest.get("novel_id") != previous_id
                    or not isinstance(manifest.get("archived_files"), list)
                ):
                    raise ValueError("历史修复证据清单损坏，无法安全重命名")
                rewritten: list[dict[str, Any]] = []
                for record in manifest["archived_files"]:
                    if not isinstance(record, dict) or not isinstance(record.get("file"), str):
                        raise ValueError("历史修复证据记录损坏，无法安全重命名")
                    name = record["file"]
                    if name != "baseline.json" and not re.fullmatch(r"chapter_\d+\.json", name):
                        raise ValueError("历史修复证据文件名损坏，无法安全重命名")
                    evidence_file = retired / name
                    evidence = read_json(evidence_file, max_bytes=_MAX_TIMELINE_FILE_BYTES)
                    if not isinstance(evidence, dict):
                        raise ValueError("历史修复证据内容损坏，无法安全重命名")
                    evidence["novel_id"] = new_id
                    if name == "baseline.json":
                        if not isinstance(evidence.get("state"), dict):
                            raise ValueError("历史修复基线损坏，无法安全重命名")
                        evidence["state"]["novel_id"] = new_id
                    encoded = json.dumps(evidence, ensure_ascii=False, indent=2).encode("utf-8")
                    atomic_write_bytes(evidence_file, encoded)
                    rewritten.append({
                        "file": name,
                        "sha256": hashlib.sha256(encoded).hexdigest(),
                        "bytes": len(encoded),
                    })
                manifest["novel_id"] = new_id
                manifest["archived_files"] = rewritten
                atomic_write_json(manifest_file, manifest)

    @project_mutation
    def delete_project(self):
        """Move the project into a recoverable local trash directory."""
        target = self.base_dir.resolve()
        if target.parent != self.root_dir or target == self.root_dir:
            raise ValueError("拒绝删除项目根目录之外的路径")
        if self.base_dir.exists():
            self.last_trash_path = self._transaction.trash_project(self.base_dir)
        self.state = NovelProjectState(novel_id=self.novel_id)
        self.knowledge_graph = KnowledgeGraph()
        self.semantic_memory = SemanticMemoryIndex()
        self.short_drama_scripts.clear()
        self.branch_simulations.clear()
        self.reader_simulations.clear()
        self.audio_package = None

    def _save_memory_chapter(self, chapter_number: int) -> None:
        chunks = [
            chunk.model_dump()
            for chunk in self.semantic_memory.chunks
            if chunk.chapter_number == chapter_number
        ]
        memory_file = self.memory_dir / f"chapter_{chapter_number:03d}.json"
        if chunks:
            atomic_write_json(memory_file, {"chunks": chunks})
        else:
            remove_file(memory_file)
        self.semantic_memory_chunk_count = len(self.semantic_memory.chunks)

    @staticmethod
    def _chapter_metadata(chapter: ChapterContent, memory_chunk_count: int) -> dict[str, Any]:
        final_audit = chapter.final_audit
        human_review = chapter.current_human_review
        return {
            "metadata_version": 3,
            "chapter_number": chapter.chapter_number,
            "volume_number": chapter.volume_number,
            "title": chapter.title,
            "word_count": chapter.word_count,
            "cliffhanger_ending": chapter.cliffhanger_ending,
            "audit_history": [final_audit.model_dump()] if final_audit else [],
            "final_audit": final_audit.model_dump() if final_audit else None,
            "audit_status": chapter.audit_status,
            "human_review_history": [human_review.model_dump(mode="json")] if human_review else [],
            "current_human_review": human_review.model_dump(mode="json") if human_review else None,
            "human_review_status": chapter.human_review_status,
            "memory_chunk_count": memory_chunk_count,
        }

    def _save_chapter_metadata(self, chapter: ChapterContent) -> None:
        metadata_file = self.chapter_meta_dir / f"chapter_{chapter.chapter_number:03d}.json"
        if self._load_memory:
            memory_count = sum(
                1 for chunk in self.semantic_memory.chunks if chunk.chapter_number == chapter.chapter_number
            )
        elif metadata_file.exists():
            raw_count = read_json(metadata_file, max_bytes=1024 * 1024).get("memory_chunk_count", 0)
            if not isinstance(raw_count, int) or isinstance(raw_count, bool) or raw_count < 0:
                raise ProjectReadOnlyError("章节元数据中的记忆计数损坏，拒绝覆盖")
            memory_count = raw_count
        else:
            memory_file = self.memory_dir / f"chapter_{chapter.chapter_number:03d}.json"
            if memory_file.exists():
                raise ProjectReadOnlyError("章节元数据缺失且存在独立记忆，拒绝以轻量模式覆盖")
            memory_count = 0
        atomic_write_json(
            metadata_file,
            self._chapter_metadata(chapter, memory_count),
        )

    @staticmethod
    def _merge_model_history(previous: list, current: list) -> list:
        """Preserve rewrite history while avoiding duplicate retry acknowledgements."""
        merged = []
        seen = set()
        for item in [*previous, *current]:
            key = item.model_dump_json()
            if key not in seen:
                seen.add(key)
                merged.append(item.model_copy(deep=True))
        return merged

    def _ensure_chapter_metadata(self) -> None:
        for chapter in self.state.chapters.values():
            metadata_file = self.chapter_meta_dir / f"chapter_{chapter.chapter_number:03d}.json"
            if not metadata_file.is_file():
                self._save_chapter_metadata(chapter)
        self._chapter_metadata_ready = True

    @staticmethod
    def _chapter_stub(raw: dict[str, Any]) -> ChapterContent:
        data = dict(raw)
        data.pop("metadata_version", None)
        data.pop("memory_chunk_count", None)
        data["beats_raw_text"] = []
        data["full_text"] = ""
        return ChapterContent.model_validate(data, context={"metadata_only": True})

    def read_chapter(self, chapter_number: int) -> ChapterContent | None:
        chapter_file = self.chapters_dir / f"chapter_{chapter_number:03d}.json"
        if not chapter_file.is_file():
            return None
        return ChapterContent.model_validate(read_json(chapter_file))

    def _persist_memory_shards(self) -> None:
        grouped: dict[int, list[MemoryChunk]] = {}
        for chunk in self.semantic_memory.chunks:
            grouped.setdefault(chunk.chapter_number, []).append(chunk)
        for stale in self.memory_dir.glob("chapter_*.json"):
            remove_file(stale)
        for chapter_number, chunks in grouped.items():
            atomic_write_json(
                self.memory_dir / f"chapter_{chapter_number:03d}.json",
                {"chunks": [chunk.model_dump() for chunk in chunks]},
            )
        self.semantic_memory_chunk_count = len(self.semantic_memory.chunks)

    @project_mutation
    def create_snapshot(self, chapter_num: int):
        """快照 v3：全局状态 + 图谱全量，正文与语义记忆按章增量保存。

        v2 在第 N 章重复嵌入前 N 章全部正文和记忆，长篇项目的磁盘占用
        会呈平方增长。v3 回滚时从各章快照重组正文与记忆，语义不变但
        主体数据改为线性增长。
        """
        bundle = self._build_snapshot_bundle(chapter_num, self.state, self.knowledge_graph)
        snapshot_file = self.snapshots_dir / f"snapshot_after_chap_{chapter_num:03d}.json"
        atomic_write_json(snapshot_file, bundle)

    def _build_snapshot_bundle(
        self, chapter_num: int, state: NovelProjectState, graph: KnowledgeGraph,
    ) -> dict[str, Any]:
        """Build a detached checkpoint so replay can finish before publishing anything."""
        state_data = state.model_dump(mode="json", exclude={"chapters"})
        current = state.chapters.get(chapter_num)
        chapter = current.model_dump(mode="json") if current else None
        state_data["chapters"] = {}
        chapter_memory = [
            json.loads(chunk.model_dump_json())
            for chunk in self.semantic_memory.chunks
            if chunk.chapter_number == chapter_num
        ]
        return {
            "snapshot_version": 3,
            "novel_id": self.novel_id,
            "chapter_num": chapter_num,
            "state": state_data,
            "chapter": chapter,
            "knowledge_graph": json.loads(graph.model_dump_json()),
            "semantic_memory_chapter": {"chunks": chapter_memory},
        }

    @property
    def _timeline_baseline_file(self) -> Path:
        return self.state_events_dir / "baseline.json"

    def _write_timeline_baseline(
        self, chapter_num: int, *, state: NovelProjectState | None = None,
        graph: KnowledgeGraph | None = None,
    ) -> None:
        """Freeze dynamic state before subsequent chapter events are applied."""
        source_state = self.state if state is None else state
        source_graph = self.knowledge_graph if graph is None else graph
        state_data = source_state.model_dump(mode="json", exclude={"chapters"})
        state_data["chapters"] = {}
        atomic_write_json(
            self._timeline_baseline_file,
            {
                "baseline_version": 1,
                "novel_id": self.novel_id,
                "chapter_num": chapter_num,
                "state": state_data,
                "knowledge_graph": json.loads(source_graph.model_dump_json()),
            },
        )

    def _ensure_timeline_baseline(self) -> int:
        if not self._timeline_baseline_file.exists():
            self._write_timeline_baseline(self.state.current_chapter_idx)
        try:
            return int(read_json(self._timeline_baseline_file).get("chapter_num", 0))
        except Exception as exc:
            raise ValueError("状态事件基线损坏，拒绝继续写入") from exc

    @staticmethod
    def _timeline_integer(value: Any, *, minimum: int, label: str) -> int:
        if type(value) is not int or value < minimum:  # bool is not a valid stored chapter identity.
            raise ValueError(f"invalid {label}")
        return value

    def _read_timeline_baseline(self) -> tuple[int, NovelProjectState, KnowledgeGraph]:
        data = read_json(self._timeline_baseline_file, max_bytes=_MAX_TIMELINE_FILE_BYTES)
        if not isinstance(data, dict):
            raise ValueError("状态事件基线根节点必须是对象")
        version = self._timeline_integer(data.get("baseline_version"), minimum=1, label="baseline version")
        if version != 1:
            raise ValueError("状态事件基线版本不受支持")
        chapter = self._timeline_integer(data.get("chapter_num"), minimum=0, label="baseline chapter")
        if chapter > self.state.current_chapter_idx or chapter > _MAX_TIMELINE_CHAPTERS:
            raise ValueError("状态事件基线超出作品进度")
        state = NovelProjectState.model_validate(data.get("state"))
        if state.current_chapter_idx != chapter:
            raise ValueError("状态事件基线进度不匹配")
        stored_novel_id = data.get("novel_id", state.novel_id)
        if stored_novel_id != self.novel_id or state.novel_id != self.novel_id:
            raise ValueError("状态事件基线作品身份不匹配")
        graph = KnowledgeGraph.model_validate(data.get("knowledge_graph"))
        return chapter, state, graph

    def _read_timeline_event(
        self, path: Path, expected_chapter: int,
    ) -> tuple[int, StateUpdateDelta, str | None]:
        data = read_json(path, max_bytes=_MAX_TIMELINE_FILE_BYTES)
        if not isinstance(data, dict):
            raise ValueError(f"状态事件 {expected_chapter} 根节点必须是对象")
        version = self._timeline_integer(data.get("event_version"), minimum=1, label="event version")
        chapter = self._timeline_integer(data.get("chapter_number"), minimum=1, label="event chapter")
        volume = self._timeline_integer(data.get("volume_number"), minimum=1, label="event volume")
        if version not in {1, 2} or chapter != expected_chapter:
            raise ValueError(f"第 {expected_chapter} 章状态事件身份不匹配")
        if data.get("novel_id", self.novel_id) != self.novel_id:
            raise ValueError(f"第 {expected_chapter} 章状态事件作品身份不匹配")
        source_hash = data.get("source_text_sha256")
        if version >= 2 and (
            not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash)
        ):
            raise ValueError(f"第 {expected_chapter} 章状态事件缺少正文身份")
        if source_hash is not None and (
            not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash)
        ):
            raise ValueError(f"第 {expected_chapter} 章状态事件正文身份无效")
        return volume, StateUpdateDelta.model_validate(data.get("delta")), source_hash

    def _validated_timeline(self, *, require_complete: bool = False) -> tuple[
        int, NovelProjectState, KnowledgeGraph,
        dict[int, tuple[int, StateUpdateDelta, str | None]], dict[str, Any]
    ]:
        """Validate every timeline record before callers mutate the live aggregate."""
        baseline_chapter, baseline_state, baseline_graph = self._read_timeline_baseline()
        current = self._timeline_integer(
            self.state.current_chapter_idx, minimum=0, label="current chapter",
        )
        if current > _MAX_TIMELINE_CHAPTERS or current - baseline_chapter > _MAX_TIMELINE_CHAPTERS:
            raise ValueError("状态事件时间线超过安全上限")
        events: dict[int, tuple[int, StateUpdateDelta, str | None]] = {}
        for path in self.state_events_dir.glob("chapter_*.json"):
            match = re.fullmatch(r"chapter_(\d+)\.json", path.name)
            if not match:
                raise ValueError(f"状态事件文件名无效: {path.name}")
            number = int(match.group(1))
            if number in events or number <= baseline_chapter or number > current:
                raise ValueError(f"状态事件超出有效时间线: {path.name}")
            events[number] = self._read_timeline_event(path, number)
        expected_count = current - baseline_chapter
        missing_count = expected_count - len(events)
        missing_sample: list[int] = []
        if missing_count:
            for number in range(baseline_chapter + 1, current + 1):
                if number not in events:
                    missing_sample.append(number)
                    if len(missing_sample) == 20:
                        break
        pending = self.get_state_reconciliation_status()["chapters"]
        pending_after_baseline = [number for number in pending if baseline_chapter < number <= current]
        pending_set = set(pending_after_baseline)
        if self._load_chapter_content:
            for number, (_, _, source_hash) in events.items():
                chapter = self.state.chapters.get(number)
                if source_hash and chapter is not None:
                    actual_hash = hashlib.sha256(chapter.full_text.encode("utf-8")).hexdigest()
                    if actual_hash != source_hash and number not in pending_set:
                        raise ValueError(f"第 {number} 章状态事件与当前正文不匹配")
        unacknowledged_missing = sum(
            1 for number in range(baseline_chapter + 1, current + 1)
            if number not in events and number not in pending_set
        )
        complete = missing_count == 0 and not pending_after_baseline
        status = {
            "mode": "event_timeline",
            "baseline_chapter": baseline_chapter,
            "current_chapter": current,
            "event_count": len(events),
            "missing_event_count": missing_count,
            "missing_events": missing_sample,
            "unacknowledged_missing_event_count": unacknowledged_missing,
            "pending_chapters": pending_after_baseline[:100],
            "pending_chapter_count": len(pending_after_baseline),
            "complete": complete,
            "integrity_valid": unacknowledged_missing == 0,
            "chapter_one_context_available": baseline_chapter == 0,
        }
        if require_complete and not complete:
            details = []
            if missing_count:
                suffix = "…" if missing_count > len(missing_sample) else ""
                details.append(f"缺少 {missing_count} 个事件（{missing_sample}{suffix}）")
            if pending_after_baseline:
                details.append(f"待核对章节 {pending_after_baseline[:20]}")
            raise ChapterContextError("状态事件时间线不完整，拒绝发布重放结果：" + "；".join(details))
        return baseline_chapter, baseline_state, baseline_graph, events, status

    def get_timeline_status(self) -> dict[str, Any]:
        """Return bounded, read-only repair diagnostics without guessing missing history."""
        event_files = list(self.state_events_dir.glob("chapter_*.json"))
        if not self._timeline_baseline_file.exists():
            if event_files:
                raise ValueError("存在状态事件但缺少时间线基线")
            pending = self.get_state_reconciliation_status()["chapters"]
            return {
                "mode": "legacy_snapshots",
                "baseline_chapter": None,
                "current_chapter": self.state.current_chapter_idx,
                "event_count": 0,
                "missing_event_count": 0,
                "missing_events": [],
                "unacknowledged_missing_event_count": 0,
                "pending_chapters": pending[:100],
                "pending_chapter_count": len(pending),
                "complete": False,
                "integrity_valid": True,
                "chapter_one_context_available": False,
            }
        return self._validated_timeline()[4]

    def _build_replayed_prefix(
        self, target_chapter: int,
        timeline: tuple[
            int, NovelProjectState, KnowledgeGraph,
            dict[int, tuple[int, StateUpdateDelta, str | None]], dict[str, Any],
        ],
    ) -> tuple[NovelProjectState, KnowledgeGraph]:
        baseline_chapter, baseline_state, baseline_graph, events, status = timeline
        if target_chapter < baseline_chapter or target_chapter > self.state.current_chapter_idx:
            raise ChapterContextError("历史修复目标超出状态事件时间线")
        pending = set(self.get_state_reconciliation_status()["chapters"])
        unresolved = [
            number for number in range(baseline_chapter + 1, target_chapter + 1)
            if number not in events or number in pending
        ]
        if unresolved:
            raise ChapterContextError(f"必须按章节顺序核对状态；前序缺口为 {unresolved[:20]}")
        replayed = baseline_state.model_copy(deep=True)
        replayed.novel_id = self.novel_id
        replayed.concept = self.state.concept
        replayed.world_setting = self.state.world_setting
        replayed.global_outline = self.state.global_outline
        replayed.delivery_package = self.state.delivery_package
        replayed.chapters = dict(self.state.chapters)
        graph = baseline_graph.model_copy(deep=True)
        override_data = self._apply_manual_overrides(
            replayed, graph, dated_chapter=baseline_chapter, include_undated=False,
        )
        for number in range(baseline_chapter + 1, target_chapter + 1):
            volume, delta, _ = events[number]
            self._reduce_state_delta(replayed, graph, delta, number, volume)
            self._apply_manual_overrides(
                replayed, graph, dated_mode="exact", dated_chapter=number,
                include_undated=False, override_data=override_data,
            )
        return replayed, graph

    @project_mutation
    def initialize_history_repair(
        self, character_seeds: dict[str, CharacterState], author_note: str,
    ) -> dict[str, Any]:
        """Start an explicit, sequential chapter-zero repair without inventing history."""
        if not self.state.chapters or self.state.current_chapter_idx < 1:
            raise ChapterContextError("作品尚无章节，不需要历史状态修复")
        chapter_numbers = sorted(self.state.chapters)
        if chapter_numbers != list(range(1, self.state.current_chapter_idx + 1)):
            raise ChapterContextError("现有章节编号不连续，无法建立可顺序核对的历史时间线")
        if len(character_seeds) > 500:
            raise ValueError("章前人物超过 500 个安全上限")
        note = author_note.strip()
        if not note or len(note) > 4000 or "\x00" in note:
            raise ValueError("请填写 1～4000 字的作者核对说明")
        current_status = self.get_timeline_status()
        if current_status.get("baseline_chapter") == 0:
            raise ChapterContextError("第 0 章状态基线已经存在，无需重复初始化")
        unknown = sorted(set(character_seeds) - set(self.state.characters))
        if unknown:
            raise ValueError(f"章前人物不在当前人物卡中：{unknown[:20]}")

        initial_characters: dict[str, CharacterCard] = {}
        graph = KnowledgeGraph()
        for name, seed in character_seeds.items():
            card = self.state.characters[name].model_copy(deep=True)
            card.initial_state = seed.model_copy(deep=True)
            card.current_state = seed.model_copy(deep=True)
            initial_characters[name] = card
            graph.upsert_node(name, "CHARACTER", {
                "role": card.role, "level": seed.level, "location": seed.location,
            }, chapter=0)
        baseline_state = NovelProjectState(
            novel_id=self.novel_id,
            concept=self.state.concept,
            world_setting=self.state.world_setting,
            global_outline=self.state.global_outline,
            characters=initial_characters,
            foreshadowings={}, chapters={}, recent_chapter_summaries=[],
            current_volume_idx=1, current_chapter_idx=0,
            delivery_package=self.state.delivery_package,
        )

        retired_root = self.state_events_dir / "retired_timelines"
        existing = list(retired_root.glob("repair_*")) if retired_root.is_dir() else []
        if len(existing) >= 100:
            raise ValueError("历史修复证据归档已达 100 份安全上限")
        repair_id = f"repair_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}_{uuid.uuid4().hex[:8]}"
        retired = retired_root / repair_id
        ensure_directory(retired)
        archived: list[dict[str, Any]] = []
        active_files = ([self._timeline_baseline_file] if self._timeline_baseline_file.is_file() else []) + list(
            self.state_events_dir.glob("chapter_*.json")
        )
        for source in sorted(active_files, key=lambda path: path.name):
            value = read_json(source, max_bytes=_MAX_TIMELINE_FILE_BYTES)
            encoded = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
            atomic_write_bytes(retired / source.name, encoded)
            archived.append({
                "file": source.name, "sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded),
            })
            remove_file(source)
        atomic_write_json(retired / "manifest.json", {
            "repair_version": 1,
            "repair_id": repair_id,
            "novel_id": self.novel_id,
            "confirmed_at": datetime.now(UTC).isoformat(),
            "author_note": note,
            "previous_timeline_status": current_status,
            "selected_initial_characters": sorted(character_seeds),
            "archived_files": archived,
        })
        self._write_timeline_baseline(0, state=baseline_state, graph=graph)
        chapters = chapter_numbers
        atomic_write_json(self._reconciliation_file, {
            "chapters": chapters,
            "reason": "作者已确认第 0 章状态；现有章节须按顺序重新提取状态",
        })
        return {**self.get_timeline_status(), "repair_id": repair_id}

    def _write_state_event(
        self, chapter_num: int, volume_number: int, delta: StateUpdateDelta, source_text: str,
    ) -> None:
        atomic_write_json(
            self.state_events_dir / f"chapter_{chapter_num:03d}.json",
            {
                "event_version": 2,
                "novel_id": self.novel_id,
                "chapter_number": chapter_num,
                "volume_number": volume_number,
                "source_text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                "delta": delta.model_dump(),
            },
        )
        self._clear_state_reconciliation(chapter_num)

    @property
    def _reconciliation_file(self) -> Path:
        return self.state_events_dir / "reconciliation_required.json"

    def get_state_reconciliation_status(self) -> dict[str, Any]:
        try:
            data = read_json(self._reconciliation_file, max_bytes=1024 * 1024)
            if not isinstance(data, dict):
                raise ValueError("reconciliation root must be an object")
            raw_chapters = data.get("chapters")
            if not isinstance(raw_chapters, list) or len(raw_chapters) > 100_000:
                raise ValueError("reconciliation chapters must be a bounded list")
            if any(type(number) is not int or number < 1 for number in raw_chapters):
                raise ValueError("invalid reconciliation chapter identity")
            reason = data.get("reason", "")
            if not isinstance(reason, str) or "\x00" in reason or len(reason) > 4000:
                raise ValueError("invalid reconciliation reason")
            reason.encode("utf-8")
            chapters = sorted(set(raw_chapters))
            if any(
                number > self.state.current_chapter_idx or number not in self.state.chapters
                for number in chapters
            ):
                raise ValueError("reconciliation chapter does not exist in the current project")
            return {"required": bool(chapters), "chapters": chapters, "reason": reason}
        except FileNotFoundError:
            return {"required": False, "chapters": [], "reason": ""}
        except Exception as exc:
            raise ChapterContextError("状态核对标记损坏，请检查 reconciliation_required.json") from exc

    @project_mutation
    def mark_state_reconciliation_required(self, chapter_num: int, reason: str) -> None:
        if type(chapter_num) is not int or chapter_num < 1:
            raise ValueError("待核对章节必须是正整数")
        if chapter_num > self.state.current_chapter_idx or chapter_num not in self.state.chapters:
            raise ValueError("待核对章节不存在于当前作品")
        if not isinstance(reason, str) or "\x00" in reason or len(reason) > 4000:
            raise ValueError("待核对说明无效或过长")
        status = self.get_state_reconciliation_status()
        chapters = sorted({*status["chapters"], chapter_num})
        serialized = json.dumps({"chapters": chapters, "reason": reason}, ensure_ascii=False, indent=2)
        if len(chapters) > 100_000 or len(serialized.encode("utf-8")) > 1024 * 1024:
            raise ValueError("待核对记录达到安全上限")
        atomic_write_text(self._reconciliation_file, serialized)

    def _clear_state_reconciliation(self, chapter_num: int) -> None:
        status = self.get_state_reconciliation_status()
        chapters = [n for n in status["chapters"] if n != chapter_num]
        if chapters:
            atomic_write_json(
                self._reconciliation_file,
                {"chapters": chapters, "reason": status["reason"]},
            )
        else:
            remove_file(self._reconciliation_file)

    @property
    def _manual_overrides_file(self) -> Path:
        return self.state_events_dir / "manual_overrides.json"

    _validate_override_values = staticmethod(character_overrides.validate_values)

    def _load_manual_overrides(self) -> dict[str, Any]:
        """Validate and normalize legacy undated overrides without writing them."""
        try:
            raw = read_json(self._manual_overrides_file, max_bytes=_MAX_MANUAL_OVERRIDE_BYTES)
        except FileNotFoundError:
            return {"override_version": 2, "characters": {}}
        return character_overrides.normalize(raw)

    def _save_manual_overrides(self, data: dict[str, Any]) -> None:
        """Never publish history that the next bounded reader would reject."""
        serialized = json.dumps(character_overrides.normalize(data), ensure_ascii=False, indent=2)
        if len(serialized.encode("utf-8")) > _MAX_MANUAL_OVERRIDE_BYTES:
            raise ValueError("人工角色覆盖文件达到大小上限，拒绝写入不可重新加载的数据")
        atomic_write_text(self._manual_overrides_file, serialized)

    @project_mutation
    def record_character_override(self, character_name: str, updates: dict[str, Any]) -> int:
        """Persist editor choices, dating dynamic state at the current chapter."""
        try:
            data = self._load_manual_overrides()
        except Exception as exc:
            raise ValueError("人工角色覆盖文件损坏，拒绝覆盖以免丢失编辑内容") from exc
        characters = data.setdefault("characters", {})
        existing = characters.setdefault(character_name, {
            "static": {}, "undated_dynamic": {}, "dynamic_history": [],
            "retired_dynamic_history": [],
        })
        static_updates = self._validate_override_values(
            {key: value for key, value in updates.items() if key in _STATIC_CHARACTER_OVERRIDE_FIELDS},
            _STATIC_CHARACTER_OVERRIDE_FIELDS,
        )
        dynamic_updates = self._validate_override_values(
            {key: value for key, value in updates.items() if key in _DYNAMIC_CHARACTER_OVERRIDE_FIELDS},
            _DYNAMIC_CHARACTER_OVERRIDE_FIELDS,
        )
        unknown = set(updates) - _STATIC_CHARACTER_OVERRIDE_FIELDS - _DYNAMIC_CHARACTER_OVERRIDE_FIELDS
        if unknown:
            raise ValueError(f"不支持的人工角色字段: {', '.join(sorted(unknown))}")
        existing["static"].update(static_updates)
        effective_after_chapter = self.state.current_chapter_idx
        if dynamic_updates:
            history = existing["dynamic_history"]
            event = {
                "effective_after_chapter": effective_after_chapter,
                "recorded_at": datetime.now(UTC).isoformat(),
                "updates": dynamic_updates,
            }
            if history and history[-1]["effective_after_chapter"] == effective_after_chapter:
                history[-1]["updates"].update(dynamic_updates)
                history[-1]["recorded_at"] = event["recorded_at"]
            else:
                if len(history) >= _MAX_DYNAMIC_OVERRIDE_EVENTS:
                    raise ValueError("人工角色动态覆盖历史已达到安全上限，请先归档检查")
                history.append(event)
        self._save_manual_overrides(data)
        return effective_after_chapter

    def _retire_future_character_overrides(self, target_chapter: int) -> None:
        """Keep rollback-discarded editor history as evidence without reapplying it."""
        data = self._load_manual_overrides()
        changed = False
        retired_at = datetime.now(UTC).isoformat()
        for entry in data["characters"].values():
            active = entry["dynamic_history"]
            future = [event for event in active if event["effective_after_chapter"] > target_chapter]
            if not future:
                continue
            retired = entry["retired_dynamic_history"]
            if len(retired) + len(future) > _MAX_DYNAMIC_OVERRIDE_EVENTS:
                raise ValueError("回滚会使人工角色覆盖归档超过安全上限")
            entry["dynamic_history"] = [
                event for event in active if event["effective_after_chapter"] <= target_chapter
            ]
            retired.extend({
                **deepcopy(event),
                "retired_at": retired_at,
                "rollback_target": target_chapter,
            } for event in future)
            changed = True
        if changed:
            self._save_manual_overrides(data)

    def _apply_manual_overrides(
        self, state: NovelProjectState | None = None, graph: KnowledgeGraph | None = None,
        *, dated_mode: str = "exact", dated_chapter: int | None = None,
        include_undated: bool = True, override_data: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if dated_mode not in {"exact", "none"}:
            raise ValueError(f"invalid dated override mode: {dated_mode}")
        if override_data is None:
            try:
                override_data = self._load_manual_overrides()
            except Exception as exc:
                raise ChapterContextError("人工角色覆盖文件损坏，请先检查状态文件") from exc
        state = self.state if state is None else state
        graph = self.knowledge_graph if graph is None else graph
        for character_name, entry in override_data.get("characters", {}).items():
            character = state.characters.get(character_name)
            if not character:
                continue
            for field, value in entry["static"].items():
                setattr(character, field, deepcopy(value))
            dynamic_updates: dict[str, Any] = {}
            if include_undated:
                # Preserve old evidence, but a dated replacement owns that field
                # from then on; legacy values must not defeat later story events.
                replaced = {
                    field for event in entry["dynamic_history"]
                    if event["effective_after_chapter"] <= state.current_chapter_idx
                    for field in event["updates"]
                }
                dynamic_updates.update({
                    key: value for key, value in entry["undated_dynamic"].items() if key not in replaced
                })
            for event in entry["dynamic_history"]:
                effective = event["effective_after_chapter"]
                if dated_mode == "exact" and effective == dated_chapter:
                    dynamic_updates.update(event["updates"])
            for field, value in dynamic_updates.items():
                setattr(character.current_state, field, deepcopy(value))
            graph.upsert_node(
                character.name,
                "CHARACTER",
                {
                    "role": character.role,
                    "level": character.current_state.level,
                    "location": character.current_state.location,
                },
            )
        return override_data

    def _reset_timeline_at_current_state(self, chapter_num: int) -> None:
        """Make a rolled-back legacy/v3 state the new replay checkpoint."""
        for event_file in self.state_events_dir.glob("chapter_*.json"):
            remove_file(event_file)
        remove_file(self._reconciliation_file)
        self._write_timeline_baseline(chapter_num)

    def _replay_state_events(self) -> None:
        """Rebuild characters, graph, hooks and rolling summaries deterministically."""
        baseline_chapter, baseline_state, baseline_graph, events, _ = self._validated_timeline(
            require_complete=True,
        )
        current_static = self.state
        replayed = baseline_state.model_copy(deep=True)
        # Editorial/static data and chapter text are not event-sourced.
        replayed.novel_id = current_static.novel_id
        replayed.concept = current_static.concept
        replayed.world_setting = current_static.world_setting
        replayed.global_outline = current_static.global_outline
        replayed.delivery_package = current_static.delivery_package
        replayed.chapters = dict(current_static.chapters)
        graph = baseline_graph.model_copy(deep=True)
        override_data = self._apply_manual_overrides(
            replayed, graph, dated_chapter=baseline_chapter, include_undated=False,
        )

        snapshots: dict[int, dict[str, Any]] = {}
        for chapter_number in range(baseline_chapter + 1, current_static.current_chapter_idx + 1):
            volume_number, delta, _ = events[chapter_number]
            self._reduce_state_delta(
                replayed, graph, delta, chapter_number, volume_number,
            )
            self._apply_manual_overrides(
                replayed, graph, dated_mode="exact", dated_chapter=chapter_number, include_undated=False,
                override_data=override_data,
            )
            snapshots[chapter_number] = self._build_snapshot_bundle(chapter_number, replayed, graph)

        # Legacy records have no trustworthy date and remain current-only.
        self._apply_manual_overrides(
            replayed, graph, dated_mode="none", include_undated=True, override_data=override_data,
        )
        self.state = replayed
        self.knowledge_graph = graph
        for chapter_number, bundle in snapshots.items():
            atomic_write_json(
                self.snapshots_dir / f"snapshot_after_chap_{chapter_number:03d}.json", bundle,
            )
        self._save_characters()
        self._save_foreshadowings()
        self.save_state()

    def _restore_snapshot_bundle(self, data: dict[str, Any]):
        """从快照数据恢复状态；兼容旧版（仅含 state 字段的 v1 快照，图谱与记忆用修剪近似回退）"""
        if data.get("snapshot_version", 0) >= 3 and "state" in data:
            target = int(data.get("chapter_num") or data["state"].get("current_chapter_idx") or 0)
            self.state = NovelProjectState.model_validate(data["state"])
            self.state.chapters = self._reconstruct_snapshot_chapters(target)
            self.knowledge_graph = KnowledgeGraph.model_validate(data.get("knowledge_graph") or {})
            self.semantic_memory = self._reconstruct_snapshot_memory(target)
        elif "state" in data:
            self.state = NovelProjectState.model_validate(data["state"])
            if data.get("knowledge_graph"):
                self.knowledge_graph = KnowledgeGraph.model_validate(data["knowledge_graph"])
            else:
                self.knowledge_graph = KnowledgeGraph()
            if data.get("semantic_memory"):
                self.semantic_memory = SemanticMemoryIndex.model_validate(data["semantic_memory"])
            else:
                self.semantic_memory = SemanticMemoryIndex()
        else:
            # v1 旧格式快照：只有 NovelProjectState，图谱/记忆无法精确还原，按章节修剪
            self.state = NovelProjectState.model_validate(data)
            if not self.state.chapters:
                self.state.chapters = self._reconstruct_snapshot_chapters(self.state.current_chapter_idx)
            self.knowledge_graph.prune_after_chapter(self.state.current_chapter_idx)
            self.semantic_memory.prune_after_chapter(self.state.current_chapter_idx)

    def snapshot_status(self, chapter_num: int) -> dict[str, Any]:
        """Validate one rollback checkpoint without changing the live aggregate."""
        result: dict[str, Any] = {
            "chapter_number": chapter_num,
            "exists": False,
            "restorable": False,
            "snapshot_version": 0,
            "exact": False,
            "reason": "缺少快照文件",
        }
        if chapter_num < 1:
            result["reason"] = "章节序号无效"
            return result
        snapshot_file = self.snapshots_dir / f"snapshot_after_chap_{chapter_num:03d}.json"
        if not snapshot_file.is_file():
            return result
        result["exists"] = True
        try:
            data = read_json(snapshot_file)
            if not isinstance(data, dict):
                raise ValueError("snapshot root is not an object")
            version = int(data.get("snapshot_version", 0))
            result["snapshot_version"] = version
            raw_state = data.get("state", data)
            state = NovelProjectState.model_validate(raw_state)
            if data.get("novel_id", state.novel_id) != self.novel_id:
                raise ValueError("snapshot project mismatch")
            if state.novel_id != self.novel_id:
                raise ValueError("snapshot project mismatch")
            if state.current_chapter_idx != chapter_num:
                raise ValueError("snapshot progress mismatch")

            if version >= 3:
                if int(data.get("chapter_num", 0)) != chapter_num:
                    raise ValueError("snapshot chapter mismatch")
                if data.get("chapter") is not None:
                    chapter = ChapterContent.model_validate(data["chapter"])
                    if chapter.chapter_number != chapter_num:
                        raise ValueError("snapshot chapter payload mismatch")
                KnowledgeGraph.model_validate(data.get("knowledge_graph") or {})
                memory = SemanticMemoryIndex.model_validate(data.get("semantic_memory_chapter") or {})
                if any(chunk.chapter_number != chapter_num for chunk in memory.chunks):
                    raise ValueError("snapshot memory chapter mismatch")
                result.update(restorable=True, exact=True, reason="")
            elif "state" in data:
                if not isinstance(data.get("knowledge_graph"), dict):
                    raise ValueError("v2 snapshot lacks knowledge graph")
                KnowledgeGraph.model_validate(data["knowledge_graph"])
                if data.get("semantic_memory") is not None:
                    memory = SemanticMemoryIndex.model_validate(data["semantic_memory"])
                    if any(chunk.chapter_number > chapter_num for chunk in memory.chunks):
                        raise ValueError("v2 snapshot contains future memory")
                if any(number > chapter_num for number in state.chapters):
                    raise ValueError("v2 snapshot contains future chapters")
                result.update(restorable=True, exact=True, reason="")
            else:
                if any(number > chapter_num for number in state.chapters):
                    raise ValueError("v1 snapshot contains future chapters")
                result.update(
                    restorable=True,
                    exact=False,
                    reason="旧版快照只能近似回退图谱与记忆，正文和项目状态可恢复",
                )
        except Exception:
            result["reason"] = "快照内容与目标章节不一致或已损坏"
        return result

    def _reconstruct_snapshot_chapters(self, target_chapter: int) -> dict[int, ChapterContent]:
        """Rebuild chapter versions from v3 deltas, with disk/v1/v2 fallbacks."""
        chapters: dict[int, ChapterContent] = {}

        # Current chapter files cover legacy projects and any gap in snapshot history.
        for chapter_file in sorted(self.chapters_dir.glob("chapter_*.json"), key=self._chapter_file_number):
            num_match = re.search(r"chapter_(\d+)", chapter_file.name)
            if not num_match or int(num_match.group(1)) > target_chapter:
                continue
            try:
                chapter = ChapterContent.model_validate(read_json(chapter_file))
                chapters[chapter.chapter_number] = chapter
            except Exception:
                continue

        # Snapshot copies win over mutable current chapter files.
        for snapshot_file in sorted(self.snapshots_dir.glob("snapshot_after_chap_*.json"), key=self._chapter_file_number):
            num_match = re.search(r"(\d+)(?=\.json$)", snapshot_file.name)
            if not num_match:
                continue
            number = int(num_match.group(1))
            if number > target_chapter:
                continue
            try:
                snapshot = read_json(snapshot_file)
                raw_chapter = snapshot.get("chapter")
                if raw_chapter:
                    chapter = ChapterContent.model_validate(raw_chapter)
                    chapters[chapter.chapter_number] = chapter
                    continue
                raw_state = snapshot.get("state", snapshot)
                raw_chapters = raw_state.get("chapters", {}) if isinstance(raw_state, dict) else {}
                raw_chapter = raw_chapters.get(str(number)) or raw_chapters.get(number)
                if raw_chapter:
                    chapters[number] = ChapterContent.model_validate(raw_chapter)
            except Exception:
                continue
        return {number: chapters[number] for number in sorted(chapters) if number <= target_chapter}

    def _reconstruct_snapshot_memory(self, target_chapter: int) -> SemanticMemoryIndex:
        """Rebuild per-chapter memory chunks across mixed snapshot versions."""
        by_chapter: dict[int, list[MemoryChunk]] = {}
        for chunk in self.semantic_memory.chunks:
            if chunk.chapter_number <= target_chapter:
                by_chapter.setdefault(chunk.chapter_number, []).append(chunk)

        for snapshot_file in sorted(self.snapshots_dir.glob("snapshot_after_chap_*.json"), key=self._chapter_file_number):
            num_match = re.search(r"(\d+)(?=\.json$)", snapshot_file.name)
            if not num_match:
                continue
            number = int(num_match.group(1))
            if number > target_chapter:
                continue
            try:
                snapshot = read_json(snapshot_file)
                if snapshot.get("snapshot_version", 0) >= 3:
                    raw_chunks = (snapshot.get("semantic_memory_chapter") or {}).get("chunks", [])
                    by_chapter[number] = [MemoryChunk.model_validate(raw) for raw in raw_chunks]
                elif snapshot.get("semantic_memory"):
                    legacy = SemanticMemoryIndex.model_validate(snapshot["semantic_memory"])
                    for chapter_number in {c.chapter_number for c in legacy.chunks}:
                        by_chapter[chapter_number] = [
                            c for c in legacy.chunks if c.chapter_number == chapter_number
                        ]
            except Exception:
                continue

        chunks = [
            chunk
            for chapter_number in sorted(by_chapter)
            for chunk in by_chapter[chapter_number]
            if chapter_number <= target_chapter
        ]
        return SemanticMemoryIndex(chunks=chunks)

    @project_mutation
    def rollback_to_chapter(self, target_chapter_num: int) -> bool:
        snapshot_file = self.snapshots_dir / f"snapshot_after_chap_{target_chapter_num:03d}.json"
        status = self.snapshot_status(target_chapter_num)
        if not status["restorable"]:
            console.print(f"[red]❌ 第 {target_chapter_num} 章快照不可安全恢复：{status['reason']}[/red]")
            return False

        try:
            data = read_json(snapshot_file)
            self._restore_snapshot_bundle(data)

            for chap_file in self.chapters_dir.glob("chapter_*.json"):
                num_match = re.search(r"chapter_(\d+)", chap_file.name)
                if num_match and int(num_match.group(1)) > target_chapter_num:
                    remove_file(chap_file)
            for chap_file in self.chapters_dir.glob("chapter_*.txt"):
                num_match = re.search(r"chapter_(\d+)", chap_file.name)
                if num_match and int(num_match.group(1)) > target_chapter_num:
                    remove_file(chap_file)
            for metadata_file in self.chapter_meta_dir.glob("chapter_*.json"):
                num_match = re.search(r"chapter_(\d+)", metadata_file.name)
                if num_match and int(num_match.group(1)) > target_chapter_num:
                    remove_file(metadata_file)

            # 回滚是时间线截断：章节之后的快照和绑定正文的按章衍生物一并清理。
            # 分支推演还读取当前人物状态；回滚会恢复整条人物时间线，因此所有旧分支都失效。
            patterns = (
                (self.snapshots_dir, "snapshot_after_chap_*.json"),
                (self.multimodal_dir, "short_drama_chap_*.json"),
                (self.reader_sim_dir, "reader_sim_chap_*.json"),
            )
            for folder, pattern in patterns:
                for derived_file in folder.glob(pattern):
                    num_match = re.search(r"(\d+)(?=\.json$)", derived_file.name)
                    if num_match and int(num_match.group(1)) > target_chapter_num:
                        remove_file(derived_file)
            self.short_drama_scripts = {
                n: value for n, value in self.short_drama_scripts.items() if int(n) <= target_chapter_num
            }
            self.reader_simulations = {
                n: value for n, value in self.reader_simulations.items() if int(n) <= target_chapter_num
            }
            self._invalidate_branch_simulations()

            self._retire_future_character_overrides(target_chapter_num)
            self._apply_manual_overrides(
                dated_chapter=target_chapter_num, include_undated=True,
            )
            self._reset_timeline_at_current_state(target_chapter_num)
            self._persist_memory_shards()
            # Sharded aggregates do not embed prose: restoring only self.state
            # would appear successful until the next process reads old files.
            for chapter in self.state.chapters.values():
                self._persist_chapter_content(chapter)
                self._save_chapter_metadata(chapter)
            self._save_characters()
            self._save_foreshadowings()
            for filename, model in (
                ("concept.json", self.state.concept),
                ("world_setting.json", self.state.world_setting),
                ("global_outline.json", self.state.global_outline),
            ):
                path = self.base_dir / filename
                if model is None:
                    remove_file(path)
                else:
                    atomic_write_text(path, model.model_dump_json(indent=2))
            remove_file(self.base_dir / "semantic_memory.json")
            self._legacy_memory_needs_migration = False
            self.save_state()
            self.export_markdown()
            self.export_platform_delivery_files()
            remove_file(self.base_dir / f"{self.novel_id}.epub")
            console.print(f"[bold green]✔ 成功回滚小说状态至第 {target_chapter_num} 章快照！[/bold green]")
            return True
        except Exception as e:
            raise ProjectConflictError("回滚未完成，请重新加载并检查项目文件后重试") from e

    def load_state(self) -> NovelProjectState:
        """Load one consistent disk view without reserving the generation slot."""
        recover_project(self.root_dir, self.novel_id)
        with project_lock(self.base_dir, scope="snapshot"):
            self._reset_loaded_state()
            state = self._load_state_files()
            self._loaded_version = project_version(self.base_dir)
            self._mutation_failed = False
            return state

    def _load_state_files(self) -> NovelProjectState:
        def warn(path: Path, exc: Exception) -> None:
            relative = str(path.relative_to(self.base_dir))
            label = (
                "state_events（状态事件）"
                if relative == str(self.state_events_dir.relative_to(self.base_dir))
                else relative
            )
            error_id = report_internal_error(
                logger,
                exc,
                scope="project_data_load",
                context={"novel_id": self.novel_id, "file": relative},
            )
            message = f"{label}: {type(exc).__name__}: 诊断编号 {error_id}"
            self.load_warnings.append(message)
            console.print(f"[yellow]⚠️ 跳过损坏的项目数据 {message}[/yellow]")

        state_file = self.base_dir / "project_state.json"
        if state_file.exists():
            try:
                self.state = NovelProjectState.model_validate(read_json(state_file))
            except Exception as e:
                warn(state_file, e)

        # New projects keep prose in independent chapter files. Lightweight
        # callers use tiny metadata shards and fetch one chapter only on demand.
        legacy_chapters = dict(self.state.chapters)
        chapters: dict[int, ChapterContent] = {}
        available_metadata_files = sorted(self.chapter_meta_dir.glob("chapter_*.json"), key=self._chapter_file_number)
        if self._load_chapter_content:
            chapters.update(legacy_chapters)
            for chapter_file in sorted(self.chapters_dir.glob("chapter_*.json"), key=self._chapter_file_number):
                try:
                    chapter = ChapterContent.model_validate(read_json(chapter_file))
                    chapters[chapter.chapter_number] = chapter
                except Exception as exc:
                    warn(chapter_file, exc)
        else:
            if available_metadata_files:
                for metadata_file in available_metadata_files:
                    try:
                        raw_metadata = read_json(metadata_file)
                        chapter = self._chapter_stub(raw_metadata)
                        chapters[chapter.chapter_number] = chapter
                        self.semantic_memory_chunk_count += int(raw_metadata.get("memory_chunk_count", 0))
                    except Exception as exc:
                        warn(metadata_file, exc)
                # A crash or an interrupted old migration may leave only part of
                # the metadata set. Fill every missing chapter from canonical
                # content instead of silently hiding it from lightweight views.
                for number, chapter in legacy_chapters.items():
                    chapters.setdefault(number, self._chapter_stub(chapter.model_dump(mode="json")))
                for chapter_file in sorted(self.chapters_dir.glob("chapter_*.json"), key=self._chapter_file_number):
                    try:
                        chapter = ChapterContent.model_validate(read_json(chapter_file))
                        chapters.setdefault(
                            chapter.chapter_number,
                            self._chapter_stub(chapter.model_dump(mode="json")),
                        )
                    except Exception as exc:
                        warn(chapter_file, exc)
            elif legacy_chapters:
                chapters = {
                    number: self._chapter_stub(chapter.model_dump(mode="json"))
                    for number, chapter in legacy_chapters.items()
                }
            else:
                # Compatibility fallback for a partially migrated project.
                for chapter_file in sorted(self.chapters_dir.glob("chapter_*.json"), key=self._chapter_file_number):
                    try:
                        chapter = ChapterContent.model_validate(read_json(chapter_file))
                        chapters[chapter.chapter_number] = self._chapter_stub(chapter.model_dump(mode="json"))
                    except Exception as exc:
                        warn(chapter_file, exc)
        self.state.chapters = chapters
        self._chapter_metadata_ready = all(
            (self.chapter_meta_dir / f"chapter_{number:03d}.json").is_file()
            for number in chapters
        )

        kg_file = self.base_dir / "knowledge_graph.json"
        if kg_file.exists():
            try:
                self.knowledge_graph = KnowledgeGraph.model_validate(read_json(kg_file))
            except Exception as exc:
                warn(kg_file, exc)

        if self._load_memory:
            memory_files = sorted(self.memory_dir.glob("chapter_*.json"), key=self._chapter_file_number)
            if memory_files:
                chunks: list[MemoryChunk] = []
                for memory_file in memory_files:
                    try:
                        shard = SemanticMemoryIndex.model_validate(read_json(memory_file))
                        chunks.extend(shard.chunks)
                    except Exception as exc:
                        warn(memory_file, exc)
                self.semantic_memory = SemanticMemoryIndex(chunks=chunks)
                self.semantic_memory_chunk_count = len(chunks)
            else:
                mem_file = self.base_dir / "semantic_memory.json"
                if mem_file.exists():
                    try:
                        self.semantic_memory = SemanticMemoryIndex.model_validate(read_json(mem_file))
                        self.semantic_memory_chunk_count = len(self.semantic_memory.chunks)
                        self._legacy_memory_needs_migration = True
                    except Exception as exc:
                        warn(mem_file, exc)

        try:
            self._load_manual_overrides()
        except Exception as exc:
            warn(self._manual_overrides_file, exc)
        try:
            self.get_state_reconciliation_status()
        except Exception as exc:
            warn(self._reconciliation_file, exc)
        try:
            timeline_status = self.get_timeline_status()
            if not timeline_status["integrity_valid"]:
                raise ValueError("状态事件无待核对标记却出现缺口")
        except Exception as exc:
            warn(self.state_events_dir, exc)

        if not self._load_derived:
            return self.state

        # 恢复分镜 / 有声演播 / 分支推演 / 读者仿真等衍生数据（否则服务重启后这些数据从接口中消失）
        for drama_file in sorted(self.multimodal_dir.glob("short_drama_chap_*.json")):
            num_match = re.search(r"short_drama_chap_(\d+)", drama_file.name)
            if not num_match:
                continue
            try:
                self.short_drama_scripts[int(num_match.group(1))] = ShortDramaScript.model_validate(read_json(drama_file))
            except Exception as exc:
                warn(drama_file, exc)

        audio_file = self.multimodal_dir / "audio_drama_package.json"
        if audio_file.exists():
            try:
                self.audio_package = AudioDramaPackage.model_validate(read_json(audio_file))
            except Exception as exc:
                warn(audio_file, exc)

        for branch_file in sorted(self.base_dir.glob("branch_sim_chap_*.json")):
            num_match = re.search(r"branch_sim_chap_(\d+)", branch_file.name)
            if not num_match:
                continue
            try:
                self.branch_simulations[int(num_match.group(1))] = BranchingSimulationResult.model_validate(read_json(branch_file))
            except Exception as exc:
                warn(branch_file, exc)

        for sim_file in sorted(self.reader_sim_dir.glob("reader_sim_chap_*.json")):
            num_match = re.search(r"reader_sim_chap_(\d+)", sim_file.name)
            if not num_match:
                continue
            try:
                self.reader_simulations[int(num_match.group(1))] = ReaderSimulationReport.model_validate(read_json(sim_file))
            except Exception as exc:
                warn(sim_file, exc)

        return self.state

    @project_mutation
    def set_concept(self, concept: NovelConcept):
        changed = self.state.concept != concept
        self.state.concept = concept.model_copy(deep=True)
        if changed:
            self._invalidate_delivery_package()
            self._invalidate_all_chapter_adaptations()
            self._invalidate_branch_simulations()
            self._invalidate_audio_package()
        atomic_write_text(self.base_dir / "concept.json", self.state.concept.model_dump_json(indent=2))
        self.save_state()

    @project_mutation
    def set_world_setting(self, world_setting: WorldSetting):
        changed = self.state.world_setting != world_setting
        self.state.world_setting = world_setting.model_copy(deep=True)
        if changed:
            self._invalidate_delivery_package()
            self._invalidate_branch_simulations()
        atomic_write_text(self.base_dir / "world_setting.json", self.state.world_setting.model_dump_json(indent=2))

        # 录入世界观初始图谱节点
        self.knowledge_graph.upsert_node(self.state.world_setting.world_name, "LOCATION", {"type": "WORLD"})
        for fac in self.state.world_setting.factions:
            self.knowledge_graph.upsert_node(fac.name, "FACTION", {"alignment": fac.alignment, "desc": fac.description})

        self.save_state()

    @project_mutation
    def set_global_outline(self, outline: GlobalOutline):
        previous = self.state.global_outline
        outline_file = self.base_dir / "global_outline.json"
        # Legacy callers could have edited the live model before handing it back.
        # Compare against the last committed copy so that such an edit cannot hide
        # stale branch simulations. New callers should still submit a detached copy.
        if previous is outline and outline_file.is_file():
            previous = GlobalOutline.model_validate(read_json(outline_file))
        changed_chapters = self._changed_outline_chapters(previous, outline)
        self.state.global_outline = outline.model_copy(deep=True)
        if changed_chapters:
            self._invalidate_branch_simulations(chapters=changed_chapters)
        atomic_write_text(outline_file, self.state.global_outline.model_dump_json(indent=2))
        self.save_state()

    @project_mutation
    def set_delivery_package(self, pkg: CommercialDeliveryPackage):
        self.state.delivery_package = pkg
        atomic_write_text(self.delivery_dir / "delivery_package.json", pkg.model_dump_json(indent=2))
        
        script_file = self.delivery_dir / f"{self.novel_id}_抖音推文爆款脚本.txt"
        atomic_write_text(script_file, pkg.tiktok_promo_script)

        prompt_file = self.delivery_dir / f"{self.novel_id}_AI封面提示词.txt"
        prompt_text = "".join(f"【{k}】：\n{v}\n\n" for k, v in pkg.ai_cover_prompts.items())
        atomic_write_text(prompt_file, prompt_text)

        self.save_state()

    def _invalidate_delivery_package(self) -> None:
        """Remove locally derived promotion data when any of its inputs change."""
        self.state.delivery_package = None
        for artifact in (
            self.delivery_dir / "delivery_package.json",
            self.delivery_dir / f"{self.novel_id}_抖音推文爆款脚本.txt",
            self.delivery_dir / f"{self.novel_id}_AI封面提示词.txt",
        ):
            remove_file(artifact)

    @staticmethod
    def _changed_outline_chapters(
        previous: GlobalOutline | None, current: GlobalOutline,
    ) -> set[int]:
        """Return chapters whose branch-simulation input changed."""
        previous_chapters = {
            chapter.chapter_number: chapter
            for volume in previous.volumes
            for chapter in volume.chapters
        } if previous else {}
        current_chapters = {
            chapter.chapter_number: chapter
            for volume in current.volumes
            for chapter in volume.chapters
        }
        return {
            number
            for number in previous_chapters.keys() | current_chapters.keys()
            if previous_chapters.get(number) != current_chapters.get(number)
        }

    def _invalidate_chapter_adaptations(self, chapter_num: int) -> None:
        """Drop prose-bound short-drama and reader projections for one chapter."""
        self.short_drama_scripts.pop(chapter_num, None)
        self.reader_simulations.pop(chapter_num, None)
        remove_file(self.multimodal_dir / f"short_drama_chap_{chapter_num:03d}.json")
        remove_file(self.reader_sim_dir / f"reader_sim_chap_{chapter_num:03d}.json")

    def _invalidate_all_chapter_adaptations(self) -> None:
        self.short_drama_scripts.clear()
        self.reader_simulations.clear()
        for artifact in self.multimodal_dir.glob("short_drama_chap_*.json"):
            remove_file(artifact)
        for artifact in self.reader_sim_dir.glob("reader_sim_chap_*.json"):
            remove_file(artifact)

    def _invalidate_audio_package(self) -> None:
        self.audio_package = None
        remove_file(self.multimodal_dir / "audio_drama_package.json")

    def _invalidate_branch_simulations(self, *, chapters: set[int] | None = None) -> None:
        """Drop branch projections globally or for an explicit chapter set."""
        if chapters is None:
            self.branch_simulations.clear()
            for artifact in self.base_dir.glob("branch_sim_chap_*.json"):
                remove_file(artifact)
            return
        for chapter_num in chapters:
            self.branch_simulations.pop(chapter_num, None)
            remove_file(self.base_dir / f"branch_sim_chap_{chapter_num:03d}.json")

    @staticmethod
    def _branch_character_inputs(characters: dict[str, CharacterCard]) -> tuple[str, ...]:
        """Stable identity for every character value supplied to the brancher."""
        return tuple(
            json.dumps(
                {
                    "name": characters[name].name,
                    "role": characters[name].role,
                    "personality_tags": characters[name].personality_tags,
                    "level": characters[name].current_state.level,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            for name in sorted(characters)
        )

    @project_mutation
    def save_short_drama_script(self, chapter_num: int, script: ShortDramaScript):
        self.short_drama_scripts[chapter_num] = script
        script_file = self.multimodal_dir / f"short_drama_chap_{chapter_num:03d}.json"
        atomic_write_text(script_file, script.model_dump_json(indent=2))

    @project_mutation
    def save_audio_drama_package(self, pkg: AudioDramaPackage):
        self.audio_package = pkg
        pkg_file = self.multimodal_dir / "audio_drama_package.json"
        atomic_write_text(pkg_file, pkg.model_dump_json(indent=2))

    @project_mutation
    def save_branch_simulation(self, chapter_num: int, branch_result: BranchingSimulationResult):
        self.branch_simulations[chapter_num] = branch_result
        branch_file = self.base_dir / f"branch_sim_chap_{chapter_num:03d}.json"
        atomic_write_text(branch_file, branch_result.model_dump_json(indent=2))

    @project_mutation
    def save_reader_simulation(self, chapter_num: int, report: ReaderSimulationReport):
        self.reader_simulations[chapter_num] = report
        report_file = self.reader_sim_dir / f"reader_sim_chap_{chapter_num:03d}.json"
        atomic_write_text(report_file, report.model_dump_json(indent=2))

    @project_mutation
    def upsert_character(self, character: CharacterCard):
        changed = self.state.characters.get(character.name) != character
        audio_changed = changed and (
            self.state.characters.get(character.name) is None
            or any(
                getattr(self.state.characters[character.name], field) != getattr(character, field)
                for field in ("name", "role", "personality_tags", "speaking_style")
            )
        )
        stored = character.model_copy(deep=True)
        self.state.characters[stored.name] = stored
        if changed:
            self._invalidate_delivery_package()
            self._invalidate_branch_simulations()
            if audio_changed:
                self._invalidate_audio_package()
        self.knowledge_graph.upsert_node(
            stored.name,
            "CHARACTER", 
            {"role": stored.role, "level": stored.current_state.level, "location": stored.current_state.location}
        )
        self._save_characters()
        self.save_state()

    @project_mutation
    def upsert_characters_bulk(self, characters: list[CharacterCard]):
        changed = any(self.state.characters.get(char.name) != char for char in characters)
        audio_changed = any(
            self.state.characters.get(char.name) is None
            or any(
                getattr(self.state.characters[char.name], field) != getattr(char, field)
                for field in ("name", "role", "personality_tags", "speaking_style")
            )
            for char in characters
        )
        for char in characters:
            stored = char.model_copy(deep=True)
            self.state.characters[stored.name] = stored
            self.knowledge_graph.upsert_node(
                stored.name,
                "CHARACTER", 
                {"role": stored.role, "level": stored.current_state.level, "location": stored.current_state.location}
            )
        if changed:
            self._invalidate_delivery_package()
            self._invalidate_branch_simulations()
            if audio_changed:
                self._invalidate_audio_package()
        self._save_characters()
        self.save_state()

    def save_manual_character(self, character: CharacterCard, updates: dict[str, Any]) -> int | None:
        """Apply only changed editor fields; unchanged forms do not create events."""
        with self.operation():
            self.assert_writable_current()
            original = self.state.characters.get(character.name)
            if original is None:
                raise ValueError("人工角色编辑对象不存在")
            validated = self._validate_override_values(
                updates, _STATIC_CHARACTER_OVERRIDE_FIELDS | _DYNAMIC_CHARACTER_OVERRIDE_FIELDS,
            )
            changed = {
                field: value for field, value in validated.items()
                if value != getattr(
                    original if field in _STATIC_CHARACTER_OVERRIDE_FIELDS else original.current_state, field,
                )
            }
            if not changed:
                return None
            edited = original.model_copy(deep=True)
            for field, value in changed.items():
                target = edited if field in _STATIC_CHARACTER_OVERRIDE_FIELDS else edited.current_state
                setattr(target, field, value)
            with self.mutation():
                effective = self.record_character_override(character.name, changed)
                self.state.characters[edited.name] = edited
                self._invalidate_delivery_package()
                self._invalidate_branch_simulations()
                if set(changed) & {"role", "personality_tags", "speaking_style"}:
                    self._invalidate_audio_package()
                self.knowledge_graph.upsert_node(edited.name, "CHARACTER", {
                    "role": edited.role,
                    "level": edited.current_state.level,
                    "location": edited.current_state.location,
                })
                self._save_characters()
                self.save_state()
            return effective if set(changed) & _DYNAMIC_CHARACTER_OVERRIDE_FIELDS else None

    def _save_characters(self):
        data = {k: v.model_dump() for k, v in self.state.characters.items()}
        atomic_write_json(self.base_dir / "characters.json", data)

    @project_mutation
    def upsert_foreshadowing(self, hook: ForeshadowingRecord):
        self.state.foreshadowings[hook.hook_id] = hook
        self.knowledge_graph.upsert_node(hook.title, "SECRET", {"desc": hook.description, "status": hook.status}, chapter=hook.planted_chapter)
        self._save_foreshadowings()
        self.save_state()

    def _save_foreshadowings(self):
        data = {k: v.model_dump() for k, v in self.state.foreshadowings.items()}
        atomic_write_json(self.base_dir / "foreshadowings.json", data)

    def get_chapter_outline(self, chapter_num: int) -> tuple[VolumeOutline, ChapterOutline] | None:
        if not self.state.global_outline:
            return None
        for vol in self.state.global_outline.volumes:
            for chap in vol.chapters:
                if chap.chapter_number == chapter_num:
                    return vol, chap
        return None

    @project_mutation
    def apply_state_delta(self, delta: StateUpdateDelta, chapter_num: int, volume_number: int = 1):
        """Apply one event and persist its resulting checkpoint."""
        branch_inputs = self._branch_character_inputs(self.state.characters)
        self._apply_state_delta_in_memory(delta, chapter_num, volume_number)
        if branch_inputs != self._branch_character_inputs(self.state.characters):
            self._invalidate_branch_simulations()
        self._save_characters()
        self._save_foreshadowings()
        self.save_state()
        self.create_snapshot(chapter_num)

    def _apply_state_delta_in_memory(
        self,
        delta: StateUpdateDelta,
        chapter_num: int,
        volume_number: int = 1,
    ) -> None:
        self._reduce_state_delta(self.state, self.knowledge_graph, delta, chapter_num, volume_number)

    @staticmethod
    def _reduce_state_delta(
        state: NovelProjectState, graph: KnowledgeGraph, delta: StateUpdateDelta,
        chapter_num: int, volume_number: int = 1,
    ) -> None:
        """Shared reducer: only the supplied objects change; no I/O or live view swap."""
        location_explicitly_set: set = set()
        for char_name, updates in delta.character_updates.items():
            if char_name in state.characters:
                char = state.characters[char_name]
                if "location" in updates:
                    location_explicitly_set.add(char_name)
                for k, v in updates.items():
                    if hasattr(char.current_state, k):
                        if k == "equipment_and_items" and isinstance(v, list):
                            char.current_state.equipment_and_items = list(dict.fromkeys(
                                [*char.current_state.equipment_and_items, *v]
                            ))
                            for itm in v:
                                graph.upsert_node(itm, "ITEM", chapter=chapter_num)
                                graph.add_relation(char_name, itm, "WIELDS", f"第{chapter_num}章获得", chapter=chapter_num)
                        elif k == "skills_and_abilities" and isinstance(v, list):
                            char.current_state.skills_and_abilities = list(dict.fromkeys(
                                [*char.current_state.skills_and_abilities, *v]
                            ))
                        else:
                            setattr(char.current_state, k, v)
                state.characters[char_name] = char
                graph.upsert_node(char_name, "CHARACTER", {"level": char.current_state.level, "hp": char.current_state.hp_status}, chapter=chapter_num)

        # 本章没有角色更新时，current_location 落到主角身上
        location_targets = list(delta.character_updates)
        if not location_targets and delta.current_location:
            protagonist = next((c for c in state.characters.values() if "主角" in c.role or c.role.lower() == "protagonist"), None)
            if protagonist:
                location_targets = [protagonist.name]

        # current_location 兜底：为状态发生变动、但 LLM 未显式给出位置的角色同步位置
        if delta.current_location:
            for char_name in location_targets:
                if char_name in location_explicitly_set:
                    continue
                char = state.characters.get(char_name)
                if char:
                    char.current_state.location = delta.current_location
                    graph.upsert_node(char_name, "CHARACTER", {"location": delta.current_location}, chapter=chapter_num)

        for new_char in delta.newly_introduced_characters:
            if new_char.name not in state.characters:
                state.characters[new_char.name] = new_char.model_copy(deep=True)
                graph.upsert_node(new_char.name, "CHARACTER", {"role": new_char.role}, chapter=chapter_num)

        for itm in delta.newly_introduced_items:
            graph.upsert_node(itm, "ITEM", chapter=chapter_num)

        for rel in delta.new_relations:
            if rel.source and rel.target and rel.relation_type:
                graph.add_relation(rel.source, rel.target, rel.relation_type, rel.description, chapter=chapter_num)

        for hook in delta.new_foreshadowings:
            state.foreshadowings[hook.hook_id] = hook.model_copy(deep=True)
            graph.upsert_node(hook.title, "SECRET", {"desc": hook.description, "status": "PLANTED"}, chapter=chapter_num)

        for hook_id in delta.resolved_foreshadowing_ids:
            if hook_id in state.foreshadowings:
                state.foreshadowings[hook_id].status = "RESOLVED"
                state.foreshadowings[hook_id].resolution_notes += f" (在第 {chapter_num} 章揭晓)"

        summary_entry = f"【第{chapter_num}章】{delta.summary_of_events}"
        state.recent_chapter_summaries.append(summary_entry)
        if len(state.recent_chapter_summaries) > 3:
            state.recent_chapter_summaries = state.recent_chapter_summaries[-3:]

        state.current_chapter_idx = max(state.current_chapter_idx, chapter_num)
        state.current_volume_idx = max(state.current_volume_idx, volume_number)

    @project_mutation
    def save_chapter(self, chapter: ChapterContent, delta: StateUpdateDelta | None = None):
        check_chapter_length(len(chapter.full_text))
        previous_chapter = self.state.chapters.get(chapter.chapter_number)
        adaptation_inputs_changed = previous_chapter is None or any(
            getattr(previous_chapter, field) != getattr(chapter, field)
            for field in ("title", "full_text", "word_count")
        )
        delivery_inputs_changed = previous_chapter is None or any(
            getattr(previous_chapter, field) != getattr(chapter, field)
            for field in ("title", "full_text", "word_count", "cliffhanger_ending")
        )
        if previous_chapter is not None and previous_chapter is not chapter:
            chapter.audit_history = self._merge_model_history(
                previous_chapter.audit_history, chapter.audit_history,
            )
            chapter.human_review_history = self._merge_model_history(
                previous_chapter.human_review_history, chapter.human_review_history,
            )
        chapter.refresh_final_audit()
        chapter.refresh_human_review()
        previous_current = self.state.current_chapter_idx
        branch_character_inputs = self._branch_character_inputs(self.state.characters)
        was_existing = chapter.chapter_number in self.state.chapters
        is_new_progress = not was_existing and chapter.chapter_number > previous_current
        baseline_chapter = self._ensure_timeline_baseline() if delta else None
        if delta and is_new_progress and chapter.chapter_number != previous_current + 1:
            raise ChapterContextError(
                f"带状态事件的新章节必须连续推进：当前为第 {previous_current} 章，"
                f"不能直接写入第 {chapter.chapter_number} 章",
            )
        self.state.chapters[chapter.chapter_number] = chapter
        
        self._persist_chapter_content(chapter)

        # 录入长程语义记忆库（重建式写入，重写/微手术后旧记忆不会残留）
        vol_info, chap_info = self.get_chapter_outline(chapter.chapter_number) or (None, None)
        entities = chap_info.involved_character_names if chap_info else []
        self.semantic_memory.replace_chapter_memory(
            chapter_number=chapter.chapter_number,
            volume_number=chapter.volume_number,
            chapter_title=chapter.title,
            full_text=chapter.full_text,
            involved_entities=entities,
        )
        self._save_memory_chapter(chapter.chapter_number)
        self._save_chapter_metadata(chapter)

        # 推进当前卷号游标（供无大纲兜底场景使用），必须在快照写入之前完成
        if chapter.volume_number:
            self.state.current_volume_idx = max(self.state.current_volume_idx, chapter.volume_number)

        if delta:
            self._write_state_event(
                chapter.chapter_number, chapter.volume_number, delta, chapter.full_text,
            )
            if is_new_progress:
                self.apply_state_delta(delta, chapter.chapter_number, volume_number=chapter.volume_number)
            elif baseline_chapter is not None and chapter.chapter_number > baseline_chapter:
                # Replacing an event requires replaying all following events; otherwise
                # removed items/hooks and changed relationships leak from the old draft.
                timeline = self._validated_timeline()
                if timeline[4]["complete"]:
                    self._replay_state_events()
                elif timeline[4]["integrity_valid"]:
                    replayed, graph = self._build_replayed_prefix(chapter.chapter_number, timeline)
                    atomic_write_json(
                        self.snapshots_dir / f"snapshot_after_chap_{chapter.chapter_number:03d}.json",
                        self._build_snapshot_bundle(chapter.chapter_number, replayed, graph),
                    )
                else:
                    raise ChapterContextError("状态事件存在未标记缺口，拒绝发布重放结果")
            else:
                # Legacy projects start their replay baseline at the first post-upgrade
                # chapter. Earlier snapshots lack pre-chapter deltas, so their dynamic
                # state cannot be safely guessed. The new text is still saved, and all
                # chapters after the baseline are fully replayable.
                remove_file(self.state_events_dir / f"chapter_{chapter.chapter_number:03d}.json")
                self.mark_state_reconciliation_required(
                    chapter.chapter_number,
                    f"legacy chapter predates event baseline {baseline_chapter}",
                )
        else:
            self.state.current_chapter_idx = max(self.state.current_chapter_idx, chapter.chapter_number)
            if is_new_progress:
                self.create_snapshot(chapter.chapter_number)

        if adaptation_inputs_changed:
            self._invalidate_chapter_adaptations(chapter.chapter_number)
            self._invalidate_branch_simulations(chapters={
                number for number in self.branch_simulations if int(number) >= chapter.chapter_number
            })
        if branch_character_inputs != self._branch_character_inputs(self.state.characters):
            self._invalidate_branch_simulations()
        if delivery_inputs_changed:
            self._invalidate_delivery_package()
        self._refresh_exports_after_chapter(chapter)
        self.save_state()

    def record_human_review(
        self,
        chapter_number: int,
        decision: str,
        note: str = "",
        *,
        reviewed_at: datetime | None = None,
    ) -> HumanChapterReview:
        """Record an author decision without rewriting prose or semantic memory."""
        with self.mutation(require_full=False):
            chapter = self.state.chapters.get(chapter_number)
            if chapter is None:
                raise ValueError(f"章节不存在: 第 {chapter_number} 章")
            note = note.strip()
            if decision == "changes_requested" and not note:
                raise ValueError("要求修改时请填写具体意见")
            review = HumanChapterReview(
                decision=decision,
                note=note,
                reviewed_at=reviewed_at or datetime.now(UTC),
                text_sha256=hashlib.sha256(chapter.full_text.encode("utf-8")).hexdigest(),
            )
            chapter.human_review_history.append(review)
            chapter.refresh_human_review()
            self._persist_chapter_record(chapter)
            self._save_chapter_metadata(chapter)
            return review.model_copy(deep=True)

    @project_mutation
    def patch_paragraph(
        self,
        chapter_number: int,
        old_text_snippet: str,
        new_text_snippet: str,
        *,
        mark_reconciliation: bool = False,
    ) -> bool:
        """段落级微手术局部修改"""
        if chapter_number not in self.state.chapters:
            return False
        # 空匹配串会在位置 0 命中并向正文头部注入内容，必须拒绝
        if not old_text_snippet:
            return False
        chap = self.state.chapters[chapter_number].model_copy(deep=True)
        if old_text_snippet in chap.full_text:
            check_replacement_length(chap.full_text, old_text_snippet, new_text_snippet, count=1)
            chap.full_text = chap.full_text.replace(old_text_snippet, new_text_snippet, 1)
            chap.word_count = len([c for c in chap.full_text if '\u4e00' <= c <= '\u9fff'])
            self.save_chapter(chap)
            if mark_reconciliation:
                self.mark_state_reconciliation_required(chapter_number, "paragraph edited without state extraction")
            return True
        return False

    def state_before_chapter(self, chapter_num: int) -> tuple[NovelProjectState, KnowledgeGraph]:
        """Return a detached pre-chapter view, never infer history from current facts.

        Event baselines take precedence over snapshots. Legacy projects need an
        exact prior snapshot with its graph. Undated dynamic editor overrides
        remain current-only; static character edits apply to known past characters.
        """
        with project_lock(self.base_dir, scope="snapshot"):
            self.assert_writable_current()
            if not 1 <= chapter_num <= self.state.current_chapter_idx + 1:
                raise ChapterContextError("章节超出已知状态范围，请先生成前序章节")
            pending = [n for n in self.get_state_reconciliation_status()["chapters"] if n < chapter_num]
            if pending:
                raise ChapterContextError(f"前序章节 {pending} 的状态待核对，请先重写或补录这些章节")
            target = chapter_num - 1
            if target == self.state.current_chapter_idx:
                # Avoid copying every chapter body in a long-book project.
                return (
                    NovelProjectState.model_validate(self.state.model_dump(exclude={"chapters"})),
                    self.knowledge_graph.model_copy(deep=True),
                )

            try:
                timeline = self._validated_timeline() if self._timeline_baseline_file.exists() else None
                if timeline is not None and timeline[0] <= target:
                    checkpoint_chapter, state, graph, events, _ = timeline
                    state = state.model_copy(deep=True)
                    graph = graph.model_copy(deep=True)
                else:
                    checkpoint = read_json(self.snapshots_dir / f"snapshot_after_chap_{target:03d}.json")
                    checkpoint_chapter = target
                    if checkpoint.get("snapshot_version", 0) < 2:
                        raise ValueError("legacy snapshot lacks exact graph")
                    if not isinstance(checkpoint.get("knowledge_graph"), dict):
                        raise ValueError("missing checkpoint graph")
                    state = NovelProjectState.model_validate({**checkpoint["state"], "chapters": {}})
                    graph = KnowledgeGraph.model_validate(checkpoint["knowledge_graph"])
                    if state.novel_id != self.novel_id:
                        raise ValueError("checkpoint project mismatch")
                    events = {}
                if state.current_chapter_idx != checkpoint_chapter:
                    raise ValueError("checkpoint chapter mismatch")
                override_data = self._apply_manual_overrides(
                    state, graph, dated_chapter=checkpoint_chapter,
                    include_undated=False,
                )
                for number in range(checkpoint_chapter + 1, chapter_num):
                    if number not in events:
                        raise ValueError(f"missing event {number}")
                    volume_number, delta, _ = events[number]
                    self._reduce_state_delta(
                        state, graph, delta, number, volume_number=volume_number,
                    )
                    self._apply_manual_overrides(
                        state, graph, dated_mode="exact", dated_chapter=number,
                        include_undated=False, override_data=override_data,
                    )
                return state, graph
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ChapterContextError(
                    f"无法可靠恢复第 {chapter_num} 章写作前的状态；请检查前序事件或第 {target} 章快照，"
                    "不会使用后续章节状态替代。旧项目可继续手动编辑正文。"
                ) from exc

    def build_chapter_context(self, chapter_num: int) -> dict[str, Any]:
        historical_state, historical_graph = self.state_before_chapter(chapter_num)
        vol_info, chap_info = self.get_chapter_outline(chapter_num) or (None, None)

        involved_names = chap_info.involved_character_names if chap_info else []
        active_characters_info = []
        for char_name in involved_names:
            if char_name in historical_state.characters:
                c = historical_state.characters[char_name]
                active_characters_info.append({
                    "name": c.name,
                    "role": c.role,
                    "personality": c.personality_tags,
                    "speaking_style": c.speaking_style,
                    "level": c.current_state.level,
                    "location": c.current_state.location,
                    "items": c.current_state.equipment_and_items,
                    "skills": c.current_state.skills_and_abilities,
                    "hp_status": c.current_state.hp_status,
                    "is_alive": c.current_state.is_alive,
                })

        if not active_characters_info:
            for c in historical_state.characters.values():
                if "主角" in c.role or c.role.lower() == "protagonist":
                    active_characters_info.append({
                        "name": c.name,
                        "role": c.role,
                        "personality": c.personality_tags,
                        "speaking_style": c.speaking_style,
                        "level": c.current_state.level,
                        "location": c.current_state.location,
                        "items": c.current_state.equipment_and_items,
                        "skills": c.current_state.skills_and_abilities,
                        "hp_status": c.current_state.hp_status,
                        "is_alive": c.current_state.is_alive,
                    })
                    break

        active_hooks = [
            {"id": h.hook_id, "title": h.title, "description": h.description}
            for h in historical_state.foreshadowings.values()
            if h.status in ["PLANTED", "DEVELOPING"] and h.planted_chapter < chapter_num
        ]

        power_levels_summary = []
        if self.state.world_setting:
            power_levels_summary = [
                f"{p.level_name}: {p.description}"
                for p in self.state.world_setting.power_system[:6]
            ]

        pacing_beat = None
        if self.state.global_outline and self.state.global_outline.pacing_schedule:
            for pb in self.state.global_outline.pacing_schedule.pacing_beats:
                if pb.chapter_number == chapter_num:
                    pacing_beat = pb
                    break

        # 提取知识图谱子图
        graph_context = historical_graph.extract_prompt_subgraph([c["name"] for c in active_characters_info])

        # 百万字长程记忆语义召回
        main_conflict = chap_info.main_conflict if chap_info else ""
        long_term_memories = self.semantic_memory.search_relevant_memories(
            query=main_conflict,
            target_entities=[c["name"] for c in active_characters_info],
            top_k=2,
            current_chapter=chapter_num,
        )
        long_term_memory_texts = [m.content for m in long_term_memories]

        return deepcopy({
            "state_as_of_chapter": chapter_num - 1,
            "novel_title": self.state.concept.title if self.state.concept else "未命名网文",
            "genre": self.state.concept.genre if self.state.concept else "",
            "golden_finger": self.state.concept.golden_finger if self.state.concept else "",
            "world_name": self.state.world_setting.world_name if self.state.world_setting else "",
            "special_rules": self.state.world_setting.special_rules if self.state.world_setting else [],
            "power_levels_summary": power_levels_summary,
            "volume_title": vol_info.volume_title if vol_info else f"第{historical_state.current_volume_idx}卷",
            "volume_number": vol_info.volume_number if vol_info else historical_state.current_volume_idx,
            "volume_goal": vol_info.volume_goal if vol_info else "",
            "volume_climax": vol_info.volume_climax if vol_info else "",
            "chapter_number": chapter_num,
            "chapter_title": chap_info.title if chap_info else f"第{chapter_num}章",
            "chapter_summary": chap_info.summary if chap_info else "",
            "main_conflict": chap_info.main_conflict if chap_info else "",
            "expectation_hook": chap_info.expectation_hook if chap_info else "",
            "recent_summaries": historical_state.recent_chapter_summaries,
            "long_term_memories": long_term_memory_texts,
            "characters_status": active_characters_info,
            "active_foreshadowings": active_hooks,
            "pacing_beat": pacing_beat.model_dump() if pacing_beat else None,
            "knowledge_graph_subgraph": graph_context,
            "commercial_target": self.state.concept.commercial_target.model_dump() if (self.state.concept and self.state.concept.commercial_target) else None,
        })

    @project_mutation
    def export_markdown(self) -> Path:
        return export_markdown(self.base_dir, self.novel_id, self.state)

    @project_mutation
    def export_platform_delivery_files(self) -> Path:
        return export_platform_text(self.delivery_dir, self.novel_id, self.state)

    @project_mutation
    def export_epub(self) -> Path:
        return export_epub(self.base_dir, self.novel_id, self.state)
