"""Actual ASGI streams, parser cleanup, and validation before side effects."""

import asyncio
import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import api.request_limits as limits
import web_ui
from config.settings import get_settings
from core.project_transactions import JOURNAL_FOLDER
from core.schemas import ChapterContent, NovelConcept
from core.state_manager import StateManager
from core.task_status import GenerationTaskStatus


def asgi_request(path, chunks, headers=(), *, stall_after=False, disconnect_after=False):
    async def run():
        sent, consumed = [], []
        finished = asyncio.Event()
        pending = iter(enumerate(chunks))

        async def receive():
            try:
                index, chunk = next(pending)
            except StopIteration:
                if disconnect_after:
                    return {"type": "http.disconnect"}
                await finished.wait()
                return {"type": "http.disconnect"}
            consumed.append(chunk)
            return {"type": "http.request", "body": chunk, "more_body": stall_after or disconnect_after or index < len(chunks) - 1}

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and not message.get("more_body"):
                finished.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "server": ("testserver", 80), "client": ("127.0.0.1", 1234),
            "headers": [(b"host", b"testserver"), *headers],
        }
        await asyncio.wait_for(web_ui.app(scope, receive, send), timeout=5)
        status = next(message["status"] for message in sent if message["type"] == "http.response.start")
        body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
        return status, body, consumed

    return asyncio.run(run())


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(web_ui, "generation_status", GenerationTaskStatus())
    yield TestClient(web_ui.app)
    get_settings.cache_clear()


def test_declared_oversize_is_rejected_without_reading_body(monkeypatch):
    monkeypatch.setattr(limits, "MAX_JSON_BODY_BYTES", 128)
    status, body, consumed = asgi_request("/api/novel/create", [b"must not be read"], [(b"content-length", b"129")])
    assert status == 413 and not consumed
    assert "上限" in body.decode()


@pytest.mark.parametrize("lengths", [[b"-1"], [b"nan"], [b"1", b"1"], [b"9" * 100]])
def test_ambiguous_or_invalid_length_header_fails_before_consuming(lengths):
    status, _, consumed = asgi_request("/api/novel/create", [b"x"], [(b"content-length", v) for v in lengths])
    assert status == 400 and not consumed


@pytest.mark.parametrize("headers", [[], [(b"content-length", b"1")]])
def test_streamed_or_understated_body_cannot_bypass_limit(monkeypatch, headers):
    monkeypatch.setattr(limits, "MAX_JSON_BODY_BYTES", 128)
    worker = Mock(side_effect=AssertionError("No task should start"))
    monkeypatch.setattr(web_ui, "run_pipeline_job", worker)
    status, body, consumed = asgi_request(
        "/api/novel/create", [b"x" * 80, b"x" * 80, b"unread"],
        [(b"content-type", b"application/json"), *headers],
    )
    assert status == 413, body
    assert len(consumed) == 2
    worker.assert_not_called()


def test_body_at_exact_limit_is_allowed(isolated, monkeypatch):
    data = json.dumps({"name": "missing", "count": 1}).encode()
    monkeypatch.setattr(limits, "MAX_JSON_BODY_BYTES", len(data))
    status, _, _ = asgi_request("/api/novel/continue", [data], [(b"content-type", b"application/json")])
    assert status == 404  # Reached ordinary project validation, not a size rejection.


def test_only_prose_endpoints_get_the_larger_body_budget(isolated, monkeypatch):
    monkeypatch.setattr(limits, "MAX_JSON_BODY_BYTES", 10)
    monkeypatch.setattr(limits, "MAX_TEXT_BODY_BYTES", 500)
    data = json.dumps({"name": "missing", "chapter": 1, "old_text": "a", "new_text": "b"}).encode()
    status, _, _ = asgi_request("/api/novel/patch_paragraph", [data], [(b"content-type", b"application/json")])
    assert status == 404
    status, _, _ = asgi_request("/api/novel/create", [data], [(b"content-type", b"application/json")])
    assert status == 413


def test_streamed_multipart_overflow_closes_partial_spools(monkeypatch):
    import starlette.formparsers as parsers

    monkeypatch.setattr(limits, "MAX_BACKUP_UPLOAD_BYTES", 180)
    monkeypatch.setattr(limits, "MAX_MULTIPART_OVERHEAD_BYTES", 100)
    files = []
    original = parsers.SpooledTemporaryFile

    def record_file(*args, **kwargs):
        file = original(*args, **kwargs)
        files.append(file)
        return file

    monkeypatch.setattr(parsers, "SpooledTemporaryFile", record_file)
    importer = Mock(side_effect=AssertionError("Oversize upload must not reach archive validation"))
    monkeypatch.setattr(web_ui, "import_project_archive", importer)
    opening = b'--test\r\nContent-Disposition: form-data; name="file"; filename="book.zip"\r\nContent-Type: application/zip\r\n\r\n'
    status, body, consumed = asgi_request(
        "/api/novels/import", [opening + b"x" * 20, b"x" * 200, b"\r\n--test--\r\n"],
        [(b"content-type", b"multipart/form-data; boundary=test")],
    )
    assert status == 413, body
    assert files and all(file.closed for file in files)
    assert len(consumed) == 2
    importer.assert_not_called()


@pytest.mark.parametrize("condition", ["idle", "deadline", "disconnect"])
def test_abandoned_multipart_closes_partial_spools(monkeypatch, condition):
    import starlette.formparsers as parsers

    files = []
    original = parsers.SpooledTemporaryFile

    def record_file(*args, **kwargs):
        file = original(*args, **kwargs)
        files.append(file)
        return file

    monkeypatch.setattr(parsers, "SpooledTemporaryFile", record_file)
    monkeypatch.setattr(limits, "BODY_CHUNK_TIMEOUT_SECONDS", 0.05 if condition == "idle" else 10)
    monkeypatch.setattr(limits, "MAX_BODY_READ_SECONDS", 0.05 if condition == "deadline" else 10)
    opening = b'--test\r\nContent-Disposition: form-data; name="file"; filename="book.zip"\r\n\r\nsome bytes'
    status, body, _ = asgi_request(
        "/api/novels/import", [opening], [(b"content-type", b"multipart/form-data; boundary=test")],
        stall_after=condition != "disconnect", disconnect_after=condition == "disconnect",
    )
    assert status == (400 if condition == "disconnect" else 408), body
    assert files and all(file.closed for file in files)


@pytest.mark.parametrize("cancel", [False, True])
def test_incomplete_eof_and_server_cancellation_close_uploads(monkeypatch, cancel):
    import starlette.formparsers as parsers
    from starlette.requests import Request

    files = []
    original = parsers.SpooledTemporaryFile

    def record_file(*args, **kwargs):
        file = original(*args, **kwargs)
        files.append(file)
        return file

    monkeypatch.setattr(parsers, "SpooledTemporaryFile", record_file)
    opening = b'--test\r\nContent-Disposition: form-data; name="file"; filename="book.zip"\r\n\r\nsome bytes'
    headers = [(b"content-type", b"multipart/form-data; boundary=test")]
    if not cancel:
        status, _, _ = asgi_request("/api/novels/import", [opening], headers)
        assert status == 422
    else:
        async def run():
            waiting = asyncio.Event()
            first = True

            async def receive():
                nonlocal first
                if first:
                    first = False
                    return {"type": "http.request", "body": opening, "more_body": True}
                waiting.set()
                await asyncio.Event().wait()

            request = Request({"type": "http", "headers": headers}, receive)
            generator = limits.backup_upload(request)
            task = asyncio.create_task(anext(generator))
            await asyncio.wait_for(waiting.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(run())
    assert files and all(file.closed for file in files)


def test_multipart_headers_have_their_own_small_cap(isolated, monkeypatch):
    monkeypatch.setattr(limits, "MAX_MULTIPART_OVERHEAD_BYTES", 30)
    response = isolated.post("/api/novels/import", files={"file": ("x" * 1000 + ".zip", b"small")})
    assert response.status_code == 413


@pytest.mark.parametrize("content_type,body", [
    ("multipart/form-data; boundary=test", b"not a multipart body"),
    ("multipart/form-data", b"missing boundary"),
    ("multipart/form-data; boundary=test; charset=not-an-encoding", b'--test\r\nContent-Disposition: form-data; name="file"; filename="book.zip"\r\n\r\nx\r\n--test--\r\n'),
])
def test_malformed_multipart_is_a_client_error(isolated, content_type, body):
    response = isolated.post("/api/novels/import", content=body, headers={"content-type": content_type})
    assert response.status_code == 400, response.text


@pytest.mark.parametrize("size", [4, 5])
def test_exact_file_limit_and_upload_stream_reuse(isolated, monkeypatch, size):
    monkeypatch.setattr(limits, "MAX_BACKUP_UPLOAD_BYTES", 4)
    streams = []

    def importer(source, _root):
        assert hasattr(source, "read"), "Pass the existing upload stream, not another file copy"
        assert source.read() == b"x" * 4
        streams.append(source)
        return "book", "Book"

    monkeypatch.setattr(web_ui, "import_project_archive", importer)
    response = isolated.post("/api/novels/import", files={"file": ("book.zip", b"x" * size)})
    assert response.status_code == (200 if size == 4 else 413), response.text
    assert len(streams) == (1 if size == 4 else 0)
    assert all(stream.closed for stream in streams)


def test_upload_limit_does_not_apply_to_post_body_work(monkeypatch):
    from starlette.responses import Response

    monkeypatch.setattr(limits, "MAX_BODY_READ_SECONDS", 0.02)

    async def run():
        async def app(scope, receive, send):
            assert (await receive())["more_body"] is False
            await asyncio.sleep(0.04)
            assert (await receive())["type"] == "http.disconnect"
            await Response(status_code=204)(scope, receive, send)

        events = iter([{"type": "http.request", "body": b"x", "more_body": False}, {"type": "http.disconnect"}])
        sent = []

        async def receive():
            return next(events)

        async def send(message):
            sent.append(message)

        await limits.RequestLimitsMiddleware(app)({"type": "http", "path": "/test", "headers": []}, receive, send)
        assert sent[0]["status"] == 204

    asyncio.run(run())


def test_archive_validation_runs_off_event_loop(isolated, monkeypatch):
    def importing(*_args):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return "book", "Book"

    monkeypatch.setattr(web_ui, "import_project_archive", importing)
    result = isolated.post("/api/novels/import", files={"file": ("book.zip", b"test payload")})
    assert result.status_code == 200, result.text


def test_malformed_origin_is_rejected_without_server_error(isolated):
    assert isolated.get("/api/config", headers={"origin": "http://[invalid"}).status_code == 403


@pytest.mark.parametrize("base_url", ["http://[invalid", "http://example.com:invalid", "https://example.com:99999", "http://:80", "http://local host/v1", "http://example.com/\nx", "http://example.com:0"])
def test_invalid_provider_urls_never_touch_config(isolated, monkeypatch, base_url):
    update = Mock(side_effect=AssertionError("Malformed URL must not update config"))
    client = Mock(side_effect=AssertionError("Malformed URL must not construct a client"))
    monkeypatch.setattr(web_ui, "apply_runtime_config", update)
    monkeypatch.setattr(web_ui, "get_llm_client", client)
    response = isolated.post("/api/config", json={"base_url": base_url, "model": "demo"})
    assert response.status_code == 400, response.text
    update.assert_not_called()
    client.assert_not_called()


@pytest.mark.parametrize("kind", ["two_files", "text_field", "wrong_name", "wrong_type"])
def test_backup_form_shape_is_bounded(isolated, monkeypatch, kind):
    importer = Mock(side_effect=AssertionError("Malformed form cannot import"))
    monkeypatch.setattr(web_ui, "import_project_archive", importer)
    if kind == "two_files":
        response = isolated.post("/api/novels/import", files=[("file", ("a.zip", b"x")), ("file", ("b.zip", b"x"))])
        expected = 400
    elif kind == "text_field":
        response = isolated.post("/api/novels/import", files={"file": ("a.zip", b"x")}, data={"extra": "x"})
        expected = 400
    elif kind == "wrong_name":
        response = isolated.post("/api/novels/import", files={"other": ("a.zip", b"x")})
        expected = 422
    else:
        response = isolated.post("/api/novels/import", json={"file": "not multipart"})
        expected = 415
    assert response.status_code == expected, response.text
    importer.assert_not_called()


@pytest.mark.parametrize("value", [0, -1, 51, True, 2.5, "3"])
def test_invalid_generation_counts_never_reserve_or_invoke_model(isolated, monkeypatch, value):
    reserve = Mock(side_effect=AssertionError("Invalid input must not reserve work"))
    monkeypatch.setattr(web_ui.generation_status, "reserve", reserve)
    for route, body in (
        ("create", {"name": "new", "idea": "idea", "chapters": value}),
        ("continue", {"name": "missing", "count": value}),
    ):
        response = isolated.post("/api/novel/" + route, json=body)
        assert response.status_code == 422, response.text
    reserve.assert_not_called()


def test_validation_errors_are_bounded_and_do_not_echo_secrets(isolated):
    secret = "sensitive-key-" * 400
    response = isolated.post("/api/config", json={"api_key": secret, "base_url": "https://example.invalid", "model": "demo"})
    assert response.status_code == 422
    assert "sensitive-key" not in response.text
    assert "input" not in response.json()["errors"][0]
    assert isinstance(response.json()["detail"], str)


def test_invalid_unicode_field_names_still_return_valid_error_json(isolated):
    response = isolated.post("/api/novel/create", content=json.dumps({"name": "new", "idea": "ok", "\ud800": "bad key"}), headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)


def test_backup_upload_openapi_contract_is_preserved(isolated):
    request = isolated.get("/openapi.json").json()["paths"]["/api/novels/import"]["post"]["requestBody"]
    schema = request["content"]["multipart/form-data"]["schema"]
    assert request["required"] and schema["required"] == ["file"]
    assert schema["properties"]["file"]["format"] == "binary"


def test_bookshelf_returns_sanitized_recovery_diagnostics_instead_of_500(isolated, tmp_path):
    journal_id = "b" * 32
    journal = tmp_path / JOURNAL_FOLDER / journal_id
    journal.mkdir(parents=True)
    (journal / "header.json").write_text("{broken", encoding="utf-8")

    response = isolated.get("/api/novels")
    diagnostics = response.json()["recovery"]

    assert response.status_code == 200
    assert diagnostics["requires_attention"] and diagnostics["blocks_all_projects"]
    assert diagnostics["journals"][0]["journal_id"] == journal_id
    assert diagnostics["journals"][0]["code"] == "corrupt_transaction_header"
    assert str(tmp_path) not in response.text
    read_only = isolated.get("/api/recovery")
    assert read_only.status_code == 200
    assert read_only.json() == diagnostics
    assert journal.exists()


@pytest.mark.parametrize("data", [
    {"name": "new", "idea": "x", "chapterz": 2},
    {"name": "new", "idea": "x" * 20_001},
    {"name": "new", "idea": "bad\x00value"},
    {"name": "new", "idea": "bad\ud800value"},
])
def test_unknown_fields_oversize_and_invalid_unicode_are_rejected(isolated, data):
    response = isolated.post("/api/novel/create", content=json.dumps(data), headers={"content-type": "application/json"})
    assert response.status_code == 422, response.text
    assert not web_ui.generation_status.is_running


def test_custom_words_are_not_silently_truncated_or_merged(isolated):
    for words in ({"category_name_too_long": ["ok"]}, {"test": ["x" * 31]}, {"a": ["one"], " a ": ["two"]}, {"a": ["x"] * 501}):
        response = isolated.post("/api/compliance/custom_words", json={"words": words})
        assert response.status_code == 422, response.text
    assert isolated.get("/api/compliance/custom_words").json()["custom_words"] == {}
    assert isolated.post("/api/compliance/custom_words", json={"words": {" test ": [" one ", "one", "two"]}}).status_code == 200
    assert isolated.get("/api/compliance/custom_words").json()["custom_words"] == {"test": ["one", "two"]}


@pytest.mark.parametrize("content", [
    "{broken",
    json.dumps(["not-a-category-map"]),
    json.dumps({"test": ["bad\x00word"]}),
])
def test_corrupt_custom_words_fail_closed_with_a_safe_error(isolated, tmp_path, content):
    words_file = tmp_path / ".custom_compliance.json"
    words_file.write_text(content, encoding="utf-8")

    response = isolated.get("/api/compliance/custom_words")

    assert response.status_code == 503
    assert "自定义合规词库不可用" in response.json()["detail"]
    assert str(tmp_path) not in response.text


def test_oversize_custom_words_fail_before_json_decode(isolated, tmp_path):
    from config.compliance_words import MAX_CUSTOM_COMPLIANCE_BYTES

    (tmp_path / ".custom_compliance.json").write_bytes(b" " * (MAX_CUSTOM_COMPLIANCE_BYTES + 1))
    response = isolated.get("/api/compliance/custom_words")

    assert response.status_code == 503
    assert "自定义合规词库不可用" in response.json()["detail"]


def test_corrupt_custom_words_cannot_be_reported_as_a_clean_novel(isolated, tmp_path):
    manager = StateManager("book")
    manager.set_concept(NovelConcept(
        title="book", genre="test", logline="test", golden_finger="test", core_conflict="test"
    ))
    manager.save_chapter(ChapterContent(
        chapter_number=1, volume_number=1, title="chapter", full_text="clean", word_count=5
    ))
    (tmp_path / ".custom_compliance.json").write_text("{broken", encoding="utf-8")

    response = isolated.get("/api/novel/book/compliance")

    assert response.status_code == 503
    assert "自定义合规词库不可用" in response.json()["detail"]


def test_excessive_structure_produces_one_bounded_error(isolated):
    response = isolated.post("/api/novel/create", json={str(i): "x" for i in range(1000)})
    assert response.status_code == 422
    assert len(response.json()["errors"]) == 1
    assert len(response.content) < 1000


@pytest.mark.parametrize("route,body", [
    ("compliance_replace", {"chapter": 1, "word": "a", "replacement": "long replacement"}),
    ("para_rewrite_apply", {"chapter": 1, "paragraph_index": 0, "new_text": "b" * 200}),
    ("patch_paragraph", {"name": "book", "chapter": 1, "old_text": "a", "new_text": "b" * 200}),
])
def test_replacement_growth_is_rejected_before_any_project_write(isolated, monkeypatch, route, body):
    import core.text_limits as text_limits

    manager = StateManager("book")
    manager.set_concept(NovelConcept(title="book", genre="test", logline="test", golden_finger="test", core_conflict="test"))
    manager.save_chapter(ChapterContent(chapter_number=1, volume_number=1, title="chapter", full_text="a" * 80, word_count=80))
    revision = manager.revision
    before = {p.relative_to(manager.base_dir): p.read_bytes() for p in manager.base_dir.rglob("*") if p.is_file()}
    monkeypatch.setattr(text_limits, "MAX_CHAPTER_TEXT_CHARS", 100)
    path = "/api/novel/patch_paragraph" if route == "patch_paragraph" else "/api/novel/book/" + route
    response = isolated.post(path, json={**body, "expected_revision": revision})
    assert response.status_code == 422, response.text
    assert StateManager("book").revision == revision
    assert {p.relative_to(manager.base_dir): p.read_bytes() for p in manager.base_dir.rglob("*") if p.is_file()} == before
