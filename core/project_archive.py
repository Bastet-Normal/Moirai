"""Safe import/export helpers for complete Moirai project archives."""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from os import PathLike
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from core.knowledge_graph import KnowledgeGraph
from core.project_ids import WINDOWS_RESERVED_NAMES, is_valid_project_id
from core.project_transactions import recover_project
from core.schemas import ChapterContent, NovelProjectState, StateUpdateDelta
from core.semantic_memory import SemanticMemoryIndex
from core.storage import atomic_write_text, project_lock, project_revision_path, read_json

MAX_ARCHIVE_ENTRIES = 20_000
MAX_UNCOMPRESSED_BYTES = 1_000 * 1024 * 1024
MAX_COMPRESSION_RATIO = 250
MAX_JSON_MEMBER_BYTES = 64 * 1024 * 1024


class InvalidProjectArchive(ValueError):
    pass


def _safe_member_path(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    if not normalized or "\x00" in normalized or normalized.startswith("/"):
        raise InvalidProjectArchive(f"备份包含非法路径: {name}")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise InvalidProjectArchive(f"备份包含非法路径: {name}")
    return path


ArchiveSource = bytes | bytearray | str | PathLike[str] | BinaryIO


def _open_archive(source: ArchiveSource) -> zipfile.ZipFile:
    if isinstance(source, (bytes, bytearray)):
        return zipfile.ZipFile(io.BytesIO(source))
    return zipfile.ZipFile(source)


def inspect_archive(source: ArchiveSource) -> tuple[zipfile.ZipFile, str, NovelProjectState]:
    try:
        archive = _open_archive(source)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise InvalidProjectArchive("无法读取备份文件（非有效 zip）") from exc

    try:
        return _inspect_open_archive(archive)
    except BaseException:
        archive.close()
        raise


def _inspect_open_archive(archive: zipfile.ZipFile) -> tuple[zipfile.ZipFile, str, NovelProjectState]:

    infos = archive.infolist()
    if not infos or len(infos) > MAX_ARCHIVE_ENTRIES:
        archive.close()
        raise InvalidProjectArchive("备份条目数量异常")

    total_size = 0
    safe_paths: list[PurePosixPath] = []
    normalized_names: set[str] = set()
    for info in infos:
        if info.flag_bits & 0x1:
            archive.close()
            raise InvalidProjectArchive("备份不得包含加密条目")
        path = _safe_member_path(info.filename.rstrip("/"))
        normalized = path.as_posix().casefold()
        if normalized in normalized_names:
            archive.close()
            raise InvalidProjectArchive(f"备份包含重复路径: {info.filename}")
        normalized_names.add(normalized)
        if any(
            re.search(r'[<>:"|?*\x00-\x1f]', part)
            or part != part.rstrip(" .")
            or part.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES
            for part in path.parts
        ):
            archive.close()
            raise InvalidProjectArchive(f"备份包含当前平台不支持的文件名: {info.filename}")
        safe_paths.append(path)
        total_size += info.file_size
        if total_size > MAX_UNCOMPRESSED_BYTES:
            archive.close()
            raise InvalidProjectArchive("备份解压后体积过大（上限 1GB）")
        if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            archive.close()
            raise InvalidProjectArchive("备份压缩比异常，疑似压缩炸弹")
        if info.file_size and not info.compress_size:
            archive.close()
            raise InvalidProjectArchive("备份条目压缩信息异常")
        if path.suffix.casefold() == ".json" and info.file_size > MAX_JSON_MEMBER_BYTES:
            archive.close()
            raise InvalidProjectArchive("备份中的单个 JSON 文件过大")
        mode = info.external_attr >> 16
        if mode and stat.S_ISLNK(mode):
            archive.close()
            raise InvalidProjectArchive("备份不得包含符号链接")

    top_dirs = {path.parts[0] for path in safe_paths}
    if len(top_dirs) != 1:
        archive.close()
        raise InvalidProjectArchive("备份结构不合法：应为单项目目录")
    project_id = next(iter(top_dirs))
    if not is_valid_project_id(project_id):
        archive.close()
        raise InvalidProjectArchive(f"备份内项目名非法: {project_id}")
    state_name = f"{project_id}/project_state.json"
    if state_name not in {p.as_posix() for p in safe_paths}:
        archive.close()
        raise InvalidProjectArchive("备份结构不合法：缺少 project_state.json")

    try:
        state = NovelProjectState.model_validate(json.loads(archive.read(state_name)))
    except Exception as exc:
        archive.close()
        raise InvalidProjectArchive("project_state.json 无法解析") from exc
    if state.novel_id != project_id:
        archive.close()
        raise InvalidProjectArchive("备份目录名与项目状态中的 novel_id 不一致")
    return archive, project_id, state


def _numbered_file(path: Path, pattern: str) -> int:
    match = re.fullmatch(pattern, path.name)
    if not match:
        raise InvalidProjectArchive(f"备份包含命名异常的数据文件: {path.name}")
    return int(match.group(1))


def _read_bounded_json(path: Path):
    try:
        if path.stat().st_size > MAX_JSON_MEMBER_BYTES:
            raise InvalidProjectArchive(f"JSON 文件过大: {path.name}")
        return read_json(path, max_bytes=MAX_JSON_MEMBER_BYTES)
    except InvalidProjectArchive:
        raise
    except Exception as exc:
        raise InvalidProjectArchive(f"JSON 文件无法解析: {path.name}") from exc


def _exact_integer(value, *, minimum: int, label: str) -> int:
    if type(value) is not int or value < minimum:
        raise InvalidProjectArchive(f"{label} 必须是有效整数")
    return value


def _validate_retired_timelines(events_dir: Path, project_id: str, current_chapter: int) -> None:
    """Verify author-repair evidence rather than importing an unverifiable audit trail."""
    retired_root = events_dir / "retired_timelines"
    if not retired_root.exists():
        return
    if not retired_root.is_dir():
        raise InvalidProjectArchive("历史修复证据目录无效")
    repair_dirs = sorted(retired_root.iterdir())
    if len(repair_dirs) > 100:
        raise InvalidProjectArchive("历史修复证据归档超过 100 份上限")
    for repair_dir in repair_dirs:
        if not repair_dir.is_dir() or not re.fullmatch(
            r"repair_\d{8}T\d{12}Z_[0-9a-f]{8}", repair_dir.name,
        ):
            raise InvalidProjectArchive("历史修复证据目录名无效")
        manifest = _read_bounded_json(repair_dir / "manifest.json")
        if not isinstance(manifest, dict):
            raise InvalidProjectArchive("历史修复证据清单结构无效")
        if (
            _exact_integer(manifest.get("repair_version"), minimum=1, label="历史修复版本") != 1
            or manifest.get("repair_id") != repair_dir.name
            or manifest.get("novel_id") != project_id
        ):
            raise InvalidProjectArchive("历史修复证据清单身份无效")
        confirmed_at = manifest.get("confirmed_at")
        author_note = manifest.get("author_note")
        selected = manifest.get("selected_initial_characters")
        archived = manifest.get("archived_files")
        if (
            not isinstance(confirmed_at, str) or not confirmed_at or len(confirmed_at) > 128
            or not isinstance(author_note, str) or not 1 <= len(author_note) <= 4000 or "\x00" in author_note
            or not isinstance(manifest.get("previous_timeline_status"), dict)
            or not isinstance(selected, list) or len(selected) > 500
            or any(not isinstance(name, str) or not name or len(name) > 200 or "\x00" in name for name in selected)
            or len(set(selected)) != len(selected)
            or not isinstance(archived, list) or len(archived) > MAX_ARCHIVE_ENTRIES
        ):
            raise InvalidProjectArchive("历史修复证据清单字段无效")

        expected_files = {"manifest.json"}
        for record in archived:
            if not isinstance(record, dict):
                raise InvalidProjectArchive("历史修复证据记录无效")
            name = record.get("file")
            checksum = record.get("sha256")
            byte_count = record.get("bytes")
            if (
                not isinstance(name, str)
                or (name != "baseline.json" and not re.fullmatch(r"chapter_\d+\.json", name))
                or not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum)
                or type(byte_count) is not int or not 0 <= byte_count <= MAX_JSON_MEMBER_BYTES
                or name in expected_files
            ):
                raise InvalidProjectArchive("历史修复证据记录字段无效")
            expected_files.add(name)
            evidence_file = repair_dir / name
            if not evidence_file.is_file() or evidence_file.stat().st_size != byte_count:
                raise InvalidProjectArchive("历史修复证据文件缺失或长度不符")
            payload = evidence_file.read_bytes()
            if hashlib.sha256(payload).hexdigest() != checksum:
                raise InvalidProjectArchive("历史修复证据校验和不符")
            evidence = _read_bounded_json(evidence_file)
            if not isinstance(evidence, dict):
                raise InvalidProjectArchive("历史修复证据内容无效")
            try:
                if name == "baseline.json":
                    if _exact_integer(evidence.get("baseline_version"), minimum=1, label="历史基线版本") != 1:
                        raise InvalidProjectArchive("历史修复基线版本无效")
                    chapter = _exact_integer(evidence.get("chapter_num"), minimum=0, label="历史基线章节")
                    state = NovelProjectState.model_validate(evidence.get("state"))
                    KnowledgeGraph.model_validate(evidence.get("knowledge_graph"))
                    if (
                        chapter > current_chapter
                        or state.current_chapter_idx != chapter
                        or state.novel_id != project_id
                        or evidence.get("novel_id", state.novel_id) != project_id
                    ):
                        raise InvalidProjectArchive("历史修复基线身份无效")
                else:
                    number = _numbered_file(evidence_file, r"chapter_(\d+)\.json")
                    event_version = _exact_integer(
                        evidence.get("event_version"), minimum=1, label="历史事件版本",
                    )
                    if (
                        event_version not in {1, 2}
                        or _exact_integer(evidence.get("chapter_number"), minimum=1, label="历史事件章节") != number
                        or _exact_integer(evidence.get("volume_number"), minimum=1, label="历史事件卷号") < 1
                        or evidence.get("novel_id", project_id) != project_id
                        or number > current_chapter
                    ):
                        raise InvalidProjectArchive("历史修复事件身份无效")
                    source_hash = evidence.get("source_text_sha256")
                    if event_version >= 2 and (
                        not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash)
                    ):
                        raise InvalidProjectArchive("历史修复事件正文身份无效")
                    StateUpdateDelta.model_validate(evidence.get("delta"))
            except InvalidProjectArchive:
                raise
            except Exception as exc:
                raise InvalidProjectArchive(f"历史修复证据内容无法恢复: {name}") from exc
        actual_files = {path.name for path in repair_dir.iterdir() if path.is_file()}
        if any(path.is_dir() for path in repair_dir.iterdir()) or actual_files != expected_files:
            raise InvalidProjectArchive("历史修复证据包含未登记文件")


def validate_project_tree(stage_root: Path, project_id: str) -> list[str]:
    """Load every active shard and validate timeline files before publication."""
    # Imported lazily to avoid coupling the archive inspector to the state manager.
    from core.state_manager import StateManager

    try:
        full = StateManager(project_id, base_dir=str(stage_root))
        light = StateManager(
            project_id,
            base_dir=str(stage_root),
            load_chapter_content=False,
            load_memory=False,
            load_derived=False,
        )
    except Exception as exc:
        raise InvalidProjectArchive("备份项目无法完整加载") from exc
    warnings = [*full.load_warnings, *light.load_warnings]
    if warnings:
        raise InvalidProjectArchive(f"备份包含损坏的数据文件: {warnings[0]}")

    chapter_numbers = set(full.state.chapters)
    expected_current = max(chapter_numbers, default=0)
    if full.state.current_chapter_idx != expected_current:
        raise InvalidProjectArchive("项目进度与实际章节不一致")

    chapters_dir = stage_root / project_id / "chapters"
    for chapter_file in chapters_dir.glob("chapter_*.json"):
        number = _numbered_file(chapter_file, r"chapter_(\d+)\.json")
        try:
            chapter = ChapterContent.model_validate(_read_bounded_json(chapter_file))
        except InvalidProjectArchive:
            raise
        except Exception as exc:
            raise InvalidProjectArchive(f"章节数据无法解析: {chapter_file.name}") from exc
        if chapter.chapter_number != number:
            raise InvalidProjectArchive(f"章节文件名与内容序号不一致: {chapter_file.name}")
        text_file = chapter_file.with_suffix(".txt")
        if text_file.exists():
            try:
                saved_text = text_file.read_text(encoding="utf-8")
            except UnicodeError as exc:
                raise InvalidProjectArchive(f"章节文本编码无效: {text_file.name}") from exc
            expected_text = f"第{chapter.chapter_number}章 {chapter.title}\n\n{chapter.full_text}\n"
            if saved_text != expected_text:
                raise InvalidProjectArchive(f"章节 JSON 与 TXT 正文不一致: {chapter_file.name}")

    metadata_dir = chapters_dir / "metadata"
    for metadata_file in metadata_dir.glob("chapter_*.json"):
        number = _numbered_file(metadata_file, r"chapter_(\d+)\.json")
        raw = _read_bounded_json(metadata_file)
        if not isinstance(raw, dict) or int(raw.get("chapter_number", 0)) != number:
            raise InvalidProjectArchive(f"章节元数据序号不一致: {metadata_file.name}")

    memory_dir = stage_root / project_id / "memory"
    for memory_file in memory_dir.glob("chapter_*.json"):
        number = _numbered_file(memory_file, r"chapter_(\d+)\.json")
        try:
            memory = SemanticMemoryIndex.model_validate(_read_bounded_json(memory_file))
        except InvalidProjectArchive:
            raise
        except Exception as exc:
            raise InvalidProjectArchive(f"章节记忆无法解析: {memory_file.name}") from exc
        if any(chunk.chapter_number != number for chunk in memory.chunks):
            raise InvalidProjectArchive(f"章节记忆归属不一致: {memory_file.name}")

    warnings: list[str] = []
    snapshots_dir = stage_root / project_id / "snapshots"
    for snapshot_file in snapshots_dir.glob("snapshot_after_chap_*.json"):
        number = _numbered_file(snapshot_file, r"snapshot_after_chap_(\d+)\.json")
        data = _read_bounded_json(snapshot_file)
        if not isinstance(data, dict):
            raise InvalidProjectArchive(f"章节快照结构无效: {snapshot_file.name}")
        try:
            version = int(data.get("snapshot_version", 0))
            raw_state = data.get("state", data)
            snapshot_state = NovelProjectState.model_validate(raw_state)
            if version >= 3:
                if int(data.get("chapter_num", 0)) != number:
                    raise ValueError("chapter number mismatch")
                if data.get("chapter"):
                    chapter = ChapterContent.model_validate(data["chapter"])
                    if chapter.chapter_number != number:
                        raise ValueError("chapter payload mismatch")
                SemanticMemoryIndex.model_validate(data.get("semantic_memory_chapter") or {})
            elif data.get("semantic_memory"):
                SemanticMemoryIndex.model_validate(data["semantic_memory"])
            if data.get("knowledge_graph"):
                KnowledgeGraph.model_validate(data["knowledge_graph"])
            if (
                snapshot_state.novel_id != project_id
                or data.get("novel_id", snapshot_state.novel_id) != project_id
            ):
                raise ValueError("snapshot project identity mismatch")
            if snapshot_state.current_chapter_idx != number:
                warnings.append(f"{snapshot_file.name}: 快照进度不匹配，回滚时将被禁用")
        except Exception as exc:
            raise InvalidProjectArchive(f"章节快照无法恢复: {snapshot_file.name}") from exc

    events_dir = stage_root / project_id / "state_events"
    baseline_file = events_dir / "baseline.json"
    baseline_chapter = 0
    if baseline_file.exists():
        baseline = _read_bounded_json(baseline_file)
        try:
            if _exact_integer(baseline.get("baseline_version"), minimum=1, label="状态基线版本") != 1:
                raise ValueError("unsupported baseline")
            baseline_chapter = _exact_integer(
                baseline.get("chapter_num"), minimum=0, label="状态基线章节",
            )
            baseline_state = NovelProjectState.model_validate(baseline["state"])
            KnowledgeGraph.model_validate(baseline.get("knowledge_graph") or {})
            if (
                baseline_state.current_chapter_idx != baseline_chapter
                or baseline_state.novel_id != project_id
                or baseline.get("novel_id", baseline_state.novel_id) != project_id
            ):
                raise ValueError("baseline identity mismatch")
        except Exception as exc:
            raise InvalidProjectArchive("状态事件基线无效") from exc
        if baseline_chapter < 0 or baseline_chapter > expected_current:
            raise InvalidProjectArchive("状态事件基线超出项目进度")

    for event_file in events_dir.glob("chapter_*.json"):
        number = _numbered_file(event_file, r"chapter_(\d+)\.json")
        event = _read_bounded_json(event_file)
        try:
            event_version = _exact_integer(
                event.get("event_version"), minimum=1, label="状态事件版本",
            )
            if (
                event_version not in {1, 2}
                or _exact_integer(event.get("chapter_number"), minimum=1, label="状态事件章节") != number
                or event.get("novel_id", project_id) != project_id
            ):
                raise ValueError("event identity mismatch")
            if _exact_integer(event.get("volume_number"), minimum=1, label="状态事件卷号") < 1:
                raise ValueError("event volume mismatch")
            source_hash = event.get("source_text_sha256")
            if event_version >= 2 and (
                not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash)
            ):
                raise ValueError("event text identity mismatch")
            StateUpdateDelta.model_validate(event.get("delta"))
        except Exception as exc:
            raise InvalidProjectArchive(f"状态事件无效: {event_file.name}") from exc
        if number <= baseline_chapter or number > expected_current:
            raise InvalidProjectArchive(f"状态事件超出有效时间线: {event_file.name}")
    _validate_retired_timelines(events_dir, project_id, expected_current)
    return warnings


def import_project_archive(source: ArchiveSource, output_root: Path) -> tuple[str, str]:
    """Validate, stage and atomically move one archive into the project store."""
    archive, project_id, state = inspect_archive(source)
    stage_root = None
    try:
        output_root = Path(output_root).resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        destination = output_root / project_id
        recover_project(output_root, project_id)
        if destination.exists():
            raise FileExistsError(project_id)
        stage_root = Path(tempfile.mkdtemp(prefix=".novel-import-", dir=output_root))
        extracted_total = 0
        for info in archive.infolist():
            path = _safe_member_path(info.filename.rstrip("/"))
            target = stage_root.joinpath(*path.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            member_total = 0
            with archive.open(info, "r") as member_source, target.open("wb") as sink:
                while chunk := member_source.read(1024 * 1024):
                    member_total += len(chunk)
                    extracted_total += len(chunk)
                    if member_total > info.file_size or extracted_total > MAX_UNCOMPRESSED_BYTES:
                        raise InvalidProjectArchive("备份实际解压体积超过声明上限")
                    sink.write(chunk)
            if member_total != info.file_size:
                raise InvalidProjectArchive("备份条目长度与声明不一致")
        staged_project = stage_root / project_id
        if not (staged_project / "project_state.json").is_file():
            raise InvalidProjectArchive("备份解压后缺少项目状态")
        validate_project_tree(stage_root, project_id)
        with project_lock(destination, timeout=0), project_lock(destination, scope="snapshot"):
            recover_project(output_root, project_id)
            if destination.exists():
                raise FileExistsError(project_id)
            atomic_write_text(project_revision_path(destination), uuid.uuid4().hex)
            staged_project.replace(destination)
            atomic_write_text(project_revision_path(destination), uuid.uuid4().hex)
    except (zipfile.BadZipFile, NotImplementedError) as exc:
        raise InvalidProjectArchive("备份数据损坏或使用了不支持的压缩格式") from exc
    finally:
        archive.close()
        if stage_root is not None:
            shutil.rmtree(stage_root, ignore_errors=True)

    title = state.concept.title if state.concept else project_id
    return project_id, title
