"""Sensitive exception content never crosses production error boundaries."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import zipfile
from datetime import datetime

from fastapi.testclient import TestClient
from starlette.requests import Request

import web_ui
from config.settings import get_settings
from core.error_reporting import (
    UserInputError,
    bounded_public_text,
    report_internal_error,
    safe_user_message,
)
from core.state_manager import StateManager
from core.task_status import GenerationTaskStatus
from pipeline.novel_pipeline import NovelPipeline
from services.generation_jobs import run_delivery_job

SECRET = "sk-private-token-and-unpublished-prose"


def _raised_error() -> RuntimeError:
    try:
        try:
            raise ValueError(f"provider body: {SECRET}")
        except ValueError as cause:
            error = RuntimeError(f"outer response: {SECRET}")
            error.add_note(f"note: {SECRET}")
            raise error from cause
    except RuntimeError as error:
        return error


def test_internal_report_is_message_free_reuses_id_and_hashes_string_context(caplog):
    logger = logging.getLogger("novelengine.test.error_reporting")
    error = _raised_error()
    with caplog.at_level(logging.ERROR, logger=logger.name):
        first = report_internal_error(
            logger,
            error,
            scope="unit_boundary",
            context={"untrusted": SECRET, "chapter": 3},
        )
        second = report_internal_error(logger, error, scope="outer_boundary")

    assert first == second and re.fullmatch(r"[0-9a-f]{12}", first)
    assert len(caplog.records) == 1
    assert SECRET not in caplog.text
    assert "RuntimeError>ValueError" in caplog.text
    assert "chapter=3" in caplog.text
    assert "untrusted=str(len=" in caplog.text and "sha256=" in caplog.text


def test_only_marked_messages_can_be_exposed_and_text_is_single_line_bounded():
    assert safe_user_message(ValueError(SECRET), fallback="失败") is None
    assert safe_user_message(UserInputError("可重试\n请刷新"), fallback="失败") == "可重试 请刷新"
    assert len(bounded_public_text("x" * 100, fallback="失败", maximum=10)) == 10


def test_background_job_status_and_logs_never_copy_unexpected_exception(
    tmp_path, monkeypatch, caplog,
):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "projects"))
    get_settings.cache_clear()
    StateManager("book").save_state()
    status = GenerationTaskStatus()
    monkeypatch.setattr("services.generation_jobs.generation_status", status)

    class BrokenPipeline:
        def __init__(self, *args, **kwargs):
            raise RuntimeError(SECRET)

    monkeypatch.setattr("services.generation_jobs.NovelPipeline", BrokenPipeline)
    with caplog.at_level(logging.ERROR, logger="novelengine.jobs"):
        run_delivery_job("book")

    snapshot = status.snapshot()
    combined = json.dumps(snapshot, ensure_ascii=False) + caplog.text
    assert SECRET not in combined
    assert not snapshot["is_running"]
    assert re.search(r"诊断编号 [0-9a-f]{12}", combined)
    get_settings.cache_clear()


def test_failed_chapter_report_contains_only_correlation_id(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "projects"))
    get_settings.cache_clear()
    pipeline = NovelPipeline("book")
    error = RuntimeError(SECRET)
    with caplog.at_level(logging.ERROR, logger="novelengine.pipeline"):
        pipeline._append_generation_log(7, False, error=error)

    report = (pipeline.state_manager.base_dir / "generation_report.jsonl").read_text(encoding="utf-8")
    assert SECRET not in report + caplog.text
    entry = json.loads(report)
    assert entry["chapter"] == 7 and entry["ok"] is False
    generated_at = datetime.fromisoformat(entry["time"])
    assert generated_at.tzinfo is not None and generated_at.utcoffset().total_seconds() == 0
    assert re.search(r"诊断编号 [0-9a-f]{12}", entry["detail"])
    get_settings.cache_clear()


def test_web_fallback_and_archive_error_do_not_echo_exception_or_member_name(
    tmp_path, monkeypatch, caplog,
):
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/private",
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1),
    })
    with caplog.at_level(logging.ERROR, logger="novelengine.web"):
        response = asyncio.run(web_ui.unhandled_exception_handler(request, RuntimeError(SECRET)))
    assert SECRET not in response.body.decode() + caplog.text
    assert re.fullmatch(r"[0-9a-f]{12}", json.loads(response.body)["request_id"])

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("safe/project_state.json", json.dumps({"novel_id": "safe"}))
        archive.writestr(f"safe/{SECRET}:bad.txt", "x")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "projects"))
    get_settings.cache_clear()
    with TestClient(web_ui.app) as client, caplog.at_level(logging.ERROR, logger="novelengine.web"):
        imported = client.post(
            "/api/novels/import",
            files={"file": ("book.zip", output.getvalue(), "application/zip")},
        )
    assert imported.status_code == 400
    assert SECRET not in imported.text + caplog.text
    assert re.search(r"诊断编号 [0-9a-f]{12}", imported.json()["detail"])
    get_settings.cache_clear()
