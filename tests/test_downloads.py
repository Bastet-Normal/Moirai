"""Downloads are isolated read snapshots, never editorial project mutations."""

import asyncio
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import web_ui
from api.downloads import build_download
from config.settings import get_settings
from core.schemas import ChapterContent, NovelConcept
from core.state_manager import StateManager
from core.task_status import GenerationTaskStatus


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "projects"))
    get_settings.cache_clear()
    monkeypatch.setattr(web_ui, "generation_status", GenerationTaskStatus())
    manager = StateManager("book")
    manager.set_concept(NovelConcept(
        title="下载测试", genre="测试", logline="测试", golden_finger="测试", core_conflict="测试",
    ))
    manager.save_chapter(ChapterContent(
        chapter_number=1, volume_number=1, title="原章", full_text="第一段。\n\n第二段。",
        word_count=8, beats_raw_text=[], cliffhanger_ending="",
    ))
    with TestClient(web_ui.app) as client:
        yield manager, client
    get_settings.cache_clear()


def project_files(manager):
    return {
        path.relative_to(manager.base_dir): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in manager.base_dir.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize("route", ["export", "export_txt", "export_epub", "export_backup"])
def test_download_preserves_files_revision_and_open_editor(project, route, monkeypatch):
    manager, client = project
    revision = manager.revision
    before = project_files(manager)
    provider = Mock(side_effect=AssertionError("Downloads must not invoke a model"))
    monkeypatch.setattr(web_ui, "get_llm_client", provider)
    responses = []

    def track_download(*args, **kwargs):
        result = build_download(*args, **kwargs)
        responses.append(result)
        return result

    monkeypatch.setattr(web_ui, "build_download", track_download)

    with monkeypatch.context() as download_patch:
        download_patch.setattr(web_ui, "NovelPipeline", provider)
        response = client.get(f"/api/novel/book/{route}")

    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"].startswith("attachment;")
    if route in {"export", "export_txt"}:
        assert "第一段。\n\n第二段。" in response.text
    else:
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert archive.testzip() is None
            if route == "export_epub":
                assert "第一段。" in archive.read("OEBPS/chap_0001.xhtml").decode()
            else:
                assert archive.read("book/chapters/chapter_001.json") == before[Path("chapters/chapter_001.json")][0]
    assert project_files(manager) == before
    assert StateManager("book").revision == revision
    assert client.get("/api/novel/book/snapshots").json()["revision"] == revision
    assert len(responses) == 1 and not Path(responses[0].path).parent.exists()
    if route != "export_backup":
        assert response.headers["x-project-revision"] == revision
    saved = client.patch("/api/novel/book/chapter_text", json={
        "chapter": 1, "text": "导出之后仍可正常保存。", "expected_revision": revision,
    })
    assert saved.status_code == 200, saved.text
    provider.assert_not_called()


@pytest.mark.parametrize("format", ["markdown", "txt", "epub"])
def test_export_uses_complete_snapshot_while_later_edit_commits(project, format, monkeypatch):
    manager, client = project
    before_revision = manager.revision
    name = {"markdown": "export_markdown", "txt": "export_platform_text", "epub": "export_epub"}[format]
    original = getattr(web_ui, name)

    def concurrent_edit(*args, **kwargs):
        # Rendering no longer owns a project write lock or a shared output path.
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(
                manager.replace_chapter_text, 1, "另一次保存的新文。", mark_reconciliation=True,
            ).result(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(web_ui, name, concurrent_edit)
    route = "export" if format == "markdown" else "export_" + format
    response = client.get(f"/api/novel/book/{route}")
    assert response.status_code == 200
    assert response.headers["x-project-revision"] == before_revision
    if format == "epub":
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            body = archive.read("OEBPS/chap_0001.xhtml").decode()
    else:
        body = response.text
    assert "第一段。" in body and "另一次保存的新文。" not in body
    assert StateManager("book").state.chapters[1].full_text == "另一次保存的新文。"
    assert manager.revision != before_revision


@pytest.mark.parametrize("route", ["export", "export_txt", "export_epub"])
def test_corrupt_prose_cannot_silently_produce_incomplete_download(project, route):
    manager, client = project
    (manager.chapters_dir / "chapter_001.json").write_bytes(b"broken")
    before = project_files(manager)
    response = client.get(f"/api/novel/book/{route}")
    assert response.status_code == 409
    assert "暂停正文导出" in response.json()["detail"]
    assert project_files(manager) == before


@pytest.mark.parametrize("route", ["export", "export_txt", "export_epub", "export_backup"])
def test_download_missing_project_does_not_create_it(project, route):
    manager, client = project
    assert client.get(f"/api/novel/missing/{route}").status_code == 404
    assert not (manager.root_dir / "missing").exists()


def test_downloaded_backup_restores_through_import_api_without_overwriting_source(project, tmp_path, monkeypatch):
    manager, client = project
    before = project_files(manager)
    backup = client.get("/api/novel/book/export_backup")
    assert backup.status_code == 200
    # Upload handling and archive validation are exercised through HTTP using
    # generated test data only; never point this fixture at a real library.
    duplicate = client.post("/api/novels/import", files={"file": ("book.zip", backup.content, "application/zip")})
    assert duplicate.status_code == 400 and "已存在同名项目" in duplicate.json()["detail"]
    restored_root = tmp_path / "restored"
    try:
        with monkeypatch.context() as imported_settings:
            imported_settings.setenv("OUTPUT_DIR", str(restored_root))
            get_settings.cache_clear()
            response = client.post("/api/novels/import", files={"file": ("book.zip", backup.content, "application/zip")})
            assert response.status_code == 200, response.text
            assert response.json()["novel_id"] == "book"
            imported = StateManager("book")
            assert imported.load_warnings == []
            assert imported.state.chapters[1].full_text == manager.state.chapters[1].full_text
            assert imported.revision != manager.revision
            assert {p: data[0] for p, data in project_files(imported).items()} == {
                p: data[0] for p, data in before.items()
            }
    finally:
        get_settings.cache_clear()
    assert project_files(manager) == before


def test_empty_book_cannot_produce_epub(project):
    _, client = project
    manager = StateManager("empty")
    manager.save_state()
    before = project_files(manager)
    revision = manager.revision
    response = client.get("/api/novel/empty/export_epub")
    assert response.status_code == 400 and "暂无章节" in response.json()["detail"]
    assert project_files(manager) == before
    assert StateManager("empty").revision == revision


def render_fixture(directory):
    path = directory / "book.txt"
    path.write_bytes(b"disposable contents")
    return path


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_renderer_failure_cleans_partial_download(failure):
    roots = []

    def render(directory):
        roots.append(directory)
        render_fixture(directory)
        raise failure()

    with pytest.raises(failure):
        build_download(render, media_type="text/plain")
    assert len(roots) == 1 and not roots[0].exists()


def test_renderer_cannot_return_external_file(tmp_path):
    outside = tmp_path / "untouched.txt"
    outside.write_bytes(b"outside")
    roots = []

    def render(directory):
        roots.append(directory)
        return outside

    with pytest.raises(ValueError, match="inside its temporary directory"):
        build_download(render, media_type="text/plain")
    assert outside.read_bytes() == b"outside"
    assert not roots[0].exists()


@pytest.mark.parametrize("mode", ["normal", "head", "range", "invalid_range", "disconnect", "cancel"])
def test_response_cleans_temp_on_success_range_error_or_disconnection(mode):
    response = build_download(render_fixture, media_type="text/plain")
    root = Path(response.path).parent
    messages = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        if mode == "disconnect":
            raise OSError("client disconnected")
        if mode == "cancel":
            raise asyncio.CancelledError()

    headers = [(b"range", b"bytes=0-3")] if mode == "range" else []
    if mode == "invalid_range":
        headers = [(b"range", b"bytes=99999-")]
    scope = {
        "type": "http", "method": "HEAD" if mode == "head" else "GET",
        "headers": headers, "extensions": {"http.response.pathsend": {}},
    }
    if mode in {"disconnect", "cancel"}:
        with pytest.raises(OSError if mode == "disconnect" else asyncio.CancelledError):
            asyncio.run(response(scope, receive, send))
    else:
        asyncio.run(response(scope, receive, send))
        expected_status = 416 if mode == "invalid_range" else 206 if mode == "range" else 200
        assert messages[0]["status"] == expected_status
        assert not any(message["type"] == "http.response.pathsend" for message in messages)
        if mode == "normal":
            assert b"".join(message.get("body", b"") for message in messages) == b"disposable contents"
    assert not root.exists()


def test_concurrent_downloads_never_share_output_files():
    first = build_download(render_fixture, media_type="text/plain")
    second = build_download(render_fixture, media_type="text/plain")

    async def receive():
        return {"type": "http.disconnect"}

    async def send(_message):
        pass

    scope = {"type": "http", "method": "GET", "headers": []}
    try:
        assert Path(first.path).parent != Path(second.path).parent
        asyncio.run(first(scope, receive, send))
        assert not Path(first.path).exists() and Path(second.path).is_file()
    finally:
        asyncio.run(second(scope, receive, send))
