"""Read-only semantic audit for existing Moirai projects.

The source projects are hashed before and after the audit.  Validation and HTTP
smoke tests run against temporary copies, so recovery/lock code cannot mutate the
user-owned source tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class ProjectAuditError(RuntimeError):
    pass


def _is_link_or_reparse(entry: os.DirEntry[str]) -> bool:
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError as exc:
        raise ProjectAuditError(f"无法读取项目文件属性: {entry.path}") from exc
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return entry.is_symlink() or reparse


def _hash_tree(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if _is_link_or_reparse(entry):
                    raise ProjectAuditError(f"项目包含链接或重解析点: {entry.path}")
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    relative = path.relative_to(root).as_posix()
                    digest = hashlib.sha256()
                    with path.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                    result[relative] = digest.hexdigest()
                else:
                    raise ProjectAuditError(f"项目包含非常规文件: {entry.path}")
    return dict(sorted(result.items()))


def _compare_migration_backup(project: Path, backup_dir: Path) -> dict[str, Any]:
    candidates = sorted(backup_dir.glob(f"{project.name}_pre_sharding_*.zip"))
    if not candidates:
        return {"backup": "", "chapter_files_compared": 0, "chapter_files_equal": None}
    backup = candidates[-1]
    compared = 0
    mismatches: list[str] = []
    with zipfile.ZipFile(backup) as archive:
        names = set(archive.namelist())
        for path in sorted((project / "chapters").glob("chapter_*.*")):
            if path.suffix.casefold() not in {".json", ".txt"}:
                continue
            member = f"{project.name}/chapters/{path.name}"
            if member not in names:
                mismatches.append(f"missing:{path.name}")
                continue
            compared += 1
            if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(archive.read(member)).digest():
                mismatches.append(path.name)
    return {
        "backup": backup.name,
        "chapter_files_compared": compared,
        "chapter_files_equal": not mismatches,
        "chapter_file_mismatches": mismatches,
    }


def _http_smoke(copy_root: Path, project_ids: list[str]) -> dict[str, Any]:
    previous = os.environ.get("OUTPUT_DIR")
    os.environ["OUTPUT_DIR"] = str(copy_root)
    try:
        from fastapi.testclient import TestClient

        from config.settings import get_settings

        get_settings.cache_clear()
        import web_ui

        client = TestClient(web_ui.app)
        listing = client.get("/api/novels", headers={"accept-encoding": "gzip"})
        if listing.status_code != 200:
            raise RuntimeError(f"作品列表 API 返回 {listing.status_code}")
        listed = {item["novel_id"] for item in listing.json()["novels"]}
        results: dict[str, Any] = {}
        for project_id in project_ids:
            if project_id not in listed:
                raise RuntimeError(f"作品列表 API 未返回 {project_id}")
            detail = client.get(f"/api/novel/{project_id}", headers={"accept-encoding": "gzip"})
            if detail.status_code != 200:
                raise RuntimeError(f"作品详情 API 返回 {detail.status_code}: {project_id}")
            chapters = detail.json().get("chapters", [])
            for chapter in chapters:
                response = client.get(f"/api/novel/{project_id}/chapter/{chapter['chapter_number']}")
                if response.status_code != 200 or not response.json().get("chapter", {}).get("full_text"):
                    raise RuntimeError(f"章节 API 无法读取: {project_id}/{chapter['chapter_number']}")
            results[project_id] = {
                "detail_status": detail.status_code,
                "chapters_read": len(chapters),
                "detail_gzip": detail.headers.get("content-encoding") == "gzip",
            }
        return results
    finally:
        if previous is None:
            os.environ.pop("OUTPUT_DIR", None)
        else:
            os.environ["OUTPUT_DIR"] = previous
        try:
            from config.settings import get_settings

            get_settings.cache_clear()
        except ImportError:
            pass


def audit_projects(output_root: Path, project_ids: list[str], backup_dir: Path) -> dict[str, Any]:
    from core.project_archive import validate_project_tree
    from core.project_ids import require_project_id

    output_root = output_root.resolve()
    originals: dict[str, dict[str, str]] = {}
    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="novelengine-project-audit-") as tmp:
        copy_root = Path(tmp)
        for raw_id in project_ids:
            project_id = require_project_id(raw_id)
            source = output_root / project_id
            if not (source / "project_state.json").is_file():
                raise FileNotFoundError(f"项目不存在: {project_id}")
            originals[project_id] = _hash_tree(source)
            shutil.copytree(source, copy_root / project_id)
            validation_warnings = validate_project_tree(copy_root, project_id)
            state = json.loads((source / "project_state.json").read_text(encoding="utf-8"))
            results[project_id] = {
                "source_files": len(originals[project_id]),
                "current_chapter": int(state.get("current_chapter_idx", 0)),
                "validation_warnings": validation_warnings,
                **_compare_migration_backup(source, backup_dir),
            }
        api_results = _http_smoke(copy_root, project_ids)

    for project_id in project_ids:
        current = _hash_tree(output_root / project_id)
        if current != originals[project_id]:
            raise RuntimeError(f"只读审计期间源项目发生变化: {project_id}")
        results[project_id]["source_unchanged"] = True
        results[project_id]["http"] = api_results[project_id]
    return {"projects": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("projects", nargs="+", help="Project IDs to audit")
    parser.add_argument("--output-root", type=Path, default=ROOT / "novel_projects")
    parser.add_argument("--backup-dir", type=Path, default=ROOT / "novel_projects" / ".migration_backups")
    args = parser.parse_args()
    try:
        report = audit_projects(args.output_root, args.projects, args.backup_dir)
    except Exception as exc:
        print(f"AUDIT FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
