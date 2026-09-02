"""HTTP edit preconditions protect user input across tabs and queued workers."""

from unittest.mock import Mock

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

import web_ui
from api.schemas import RegenerateRequest
from config.settings import get_settings
from core.schemas import ChapterContent, CharacterCard, NovelConcept
from core.state_manager import StateManager
from core.storage import read_json
from core.task_status import GenerationTaskStatus


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(web_ui, "generation_status", GenerationTaskStatus())
    manager = StateManager("book")
    manager.set_concept(NovelConcept(
        title="原书", genre="测试", logline="测试", golden_finger="测试", core_conflict="测试",
    ))
    manager.save_chapter(ChapterContent(
        chapter_number=1, volume_number=1, title="原章", full_text="第一段。\n\n第二段。",
        word_count=8, beats_raw_text=[], cliffhanger_ending="",
    ))
    with TestClient(web_ui.app) as client:
        yield manager, client
    get_settings.cache_clear()


EDIT_ROUTES = [
    ("patch", "/api/novel/book/concept", {"title": "更名"}),
    ("patch", "/api/novel/book/character", {"name": "角色", "level": "新境界"}),
    ("patch", "/api/novel/book/chapter_text", {"chapter": 1, "text": "改文"}),
    ("patch", "/api/novel/book/outline/1", {"title": "新细纲"}),
    ("patch", "/api/novel/book/rename", {"new_name": "renamed"}),
    ("post", "/api/novel/patch_paragraph", {"name": "book", "chapter": 1, "old_text": "第一段。", "new_text": "新段。"}),
    ("post", "/api/novel/rollback", {"name": "book", "target_chapter": 1}),
    ("post", "/api/novel/regenerate", {"name": "book", "chapter": 1}),
    ("post", "/api/novel/book/para_rewrite", {"chapter": 1, "paragraph_index": 0, "instruction": "改短"}),
    ("post", "/api/novel/book/para_rewrite_apply", {"chapter": 1, "paragraph_index": 1, "new_text": "候选"}),
    ("post", "/api/novel/book/chapter/1/human_review", {"decision": "approved", "note": "已通读"}),
    ("post", "/api/novel/book/adopt_branch", {"chapter": 1, "branch_id": "A"}),
    ("post", "/api/novel/book/compliance_replace", {"chapter": 1, "word": "第一", "replacement": "首"}),
    ("post", "/api/novel/book/history_repair", {
        "confirmation": "我已核对第1章开始前状态", "author_note": "人工核对", "characters": [],
    }),
    ("post", "/api/novel/book/history_repair/1/extract", {}),
    ("post", "/api/novel/book/short_drama/1/generate", {}),
    ("post", "/api/novel/book/audio_package/generate", {}),
    ("post", "/api/novel/book/generate_delivery", {}),
    ("post", "/api/novel/reader_simulate", {"name": "book", "chapter": 1}),
    ("post", "/api/novel/branch_simulate", {"name": "book", "chapter": 1}),
    ("post", "/api/novel/continue", {"name": "book", "count": 1}),
]


@pytest.mark.parametrize(("method", "path", "body"), EDIT_ROUTES)
def test_edit_requires_explicit_revision_and_refuses_stale_view(project, method, path, body, monkeypatch):
    manager, client = project
    provider = Mock(side_effect=AssertionError("A stale edit must never invoke a provider"))
    monkeypatch.setattr(web_ui, "get_llm_client", provider)
    before = {p.relative_to(manager.base_dir): p.read_bytes() for p in manager.base_dir.rglob("*") if p.is_file()}
    missing = getattr(client, method)(path, json=body)
    assert missing.status_code == 428, missing.text
    stale = getattr(client, method)(path, json={**body, "expected_revision": "0" * 64})
    assert stale.status_code == 409, stale.text
    after = {p.relative_to(manager.base_dir): p.read_bytes() for p in manager.base_dir.rglob("*") if p.is_file()}
    assert after == before
    provider.assert_not_called()


def test_all_read_views_share_revision_and_old_editor_cannot_overwrite(project):
    manager, client = project
    revision = client.get("/api/novel/book").json()["revision"]
    assert revision == manager.revision
    for suffix in ("/chapter/1", "/snapshots", "/compliance"):
        assert client.get("/api/novel/book" + suffix).json()["revision"] == revision
    first = client.patch("/api/novel/book/chapter_text", json={
        "chapter": 1, "text": "另一个页面已保存的新文", "expected_revision": revision,
    })
    assert first.status_code == 200
    second = client.post("/api/novel/book/para_rewrite_apply", json={
        "chapter": 1, "paragraph_index": 0, "new_text": "过期候选", "expected_revision": revision,
    })
    assert second.status_code == 409
    assert StateManager("book").state.chapters[1].full_text == "另一个页面已保存的新文"


def test_human_review_api_binds_decision_to_current_text_and_revision(project):
    manager, client = project
    revision = manager.revision
    invalid = client.post("/api/novel/book/chapter/1/human_review", json={
        "decision": "changes_requested", "note": "  ", "expected_revision": revision,
    })
    assert invalid.status_code == 422

    response = client.post("/api/novel/book/chapter/1/human_review", json={
        "decision": "approved", "note": "作者已通读", "expected_revision": revision,
    })

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["revision"] != revision
    assert payload["review"]["decision"] == "approved"
    chapter_view = client.get("/api/novel/book/chapter/1").json()["chapter"]
    assert chapter_view["human_review_status"] == "approved"
    assert chapter_view["current_human_review"]["note"] == "作者已通读"
    stale = client.post("/api/novel/book/chapter/1/human_review", json={
        "decision": "approved", "expected_revision": revision,
    })
    assert stale.status_code == 409


def test_history_repair_preview_and_confirmation_are_revision_bound(project):
    manager, client = project
    manager.upsert_character(CharacterCard(id="hero", name="楚封", role="主角", background="山村少年"))
    preview = client.get("/api/novel/book/history_repair")
    assert preview.status_code == 200
    data = preview.json()
    assert data["eligible"] is True
    assert data["timeline_status"]["mode"] == "legacy_snapshots"
    assert data["characters"][0]["name"] == "楚封"
    assert data["confirmation"] == "我已核对第1章开始前状态"

    invalid = client.post("/api/novel/book/history_repair", json={
        "expected_revision": data["revision"], "confirmation": "大概核对过",
        "author_note": "已核对", "characters": [],
    })
    assert invalid.status_code == 422
    response = client.post("/api/novel/book/history_repair", json={
        "expected_revision": data["revision"], "confirmation": data["confirmation"],
        "author_note": "对照第一章原稿逐项核对",
        "characters": [{
            "name": "楚封", "level": "凡人", "location": "山村", "hp_status": "健康",
            "equipment_and_items": ["玉佩"], "skills_and_abilities": [], "current_mood": "困惑",
            "relations": {}, "is_alive": True,
        }],
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "repair_started"
    assert result["revision"] != data["revision"]
    assert result["timeline_status"]["baseline_chapter"] == 0
    assert result["timeline_status"]["pending_chapters"] == [1]
    detail = client.get("/api/novel/book").json()
    assert detail["timeline_status"]["chapter_one_context_available"] is True
    assert detail["state_reconciliation"]["chapters"] == [1]

    stale = client.post("/api/novel/book/history_repair", json={
        "expected_revision": data["revision"], "confirmation": data["confirmation"],
        "author_note": "重复提交", "characters": [],
    })
    assert stale.status_code == 409


def test_state_only_reconciliation_endpoint_is_revision_bound_and_queued(project, monkeypatch):
    manager, client = project
    manager.initialize_history_repair({}, "已核对章前空状态")
    worker = Mock()
    monkeypatch.setattr(web_ui, "run_state_reconciliation_job", worker)

    response = client.post("/api/novel/book/history_repair/1/extract", json={
        "expected_revision": manager.revision,
    })

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "started"
    assert "正文不会被重写" in payload["message"]
    worker.assert_called_once_with(
        name="book", chapter=1, task_id=payload["task_id"], expected_revision=manager.revision,
    )
    web_ui.generation_status.finish(step="test cleanup", task_id=payload["task_id"])


@pytest.mark.parametrize(("path", "worker_name", "extra"), [
    ("/api/novel/book/short_drama/1/generate", "run_short_drama_job", {"chapter": 1}),
    ("/api/novel/book/audio_package/generate", "run_audio_package_job", {}),
    ("/api/novel/book/generate_delivery", "run_delivery_job", {}),
    ("/api/novel/reader_simulate", "run_reader_simulation_job", {"chapter": 1}),
    ("/api/novel/branch_simulate", "run_branch_simulation_job", {"chapter": 1}),
])
def test_derived_rebuild_endpoints_are_revision_bound_and_queued(
    project, monkeypatch, path, worker_name, extra,
):
    manager, client = project
    if worker_name == "run_audio_package_job":
        manager.upsert_character(CharacterCard(id="hero", name="角色", role="主角", background="测试"))
    revision = StateManager("book").revision
    worker = Mock()
    monkeypatch.setattr(web_ui, worker_name, worker)

    body = {"expected_revision": revision}
    if path in {"/api/novel/reader_simulate", "/api/novel/branch_simulate"}:
        body.update({"name": "book", "chapter": 1})
    response = client.post(path, json=body)

    assert response.status_code == 200, response.text
    payload = response.json()
    worker.assert_called_once_with(
        name="book", task_id=payload["task_id"], expected_revision=revision, **extra,
    )
    web_ui.generation_status.finish(step="test cleanup", task_id=payload["task_id"])


def test_continuation_endpoint_is_revision_bound_and_worker_rechecks(project, monkeypatch):
    manager, client = project
    revision = manager.revision
    worker = Mock()
    monkeypatch.setattr(web_ui, "run_pipeline_job", worker)

    response = client.post("/api/novel/continue", json={
        "name": "book", "count": 2, "expected_revision": revision,
    })

    assert response.status_code == 200, response.text
    payload = response.json()
    kwargs = worker.call_args.kwargs
    assert kwargs["name"] == "book"
    assert kwargs["idea"] == ""
    assert kwargs["chapter_count"] == 2
    assert kwargs["task_id"] == payload["task_id"]
    assert kwargs["expected_revision"] == revision
    web_ui.generation_status.finish(step="test cleanup", task_id=payload["task_id"])


def test_character_dynamic_edit_returns_effective_chapter_and_new_revision(project):
    manager, client = project
    manager.upsert_character(CharacterCard(id="hero", name="角色", role="主角", background="测试"))
    revision = manager.revision

    response = client.patch("/api/novel/book/character", json={
        "name": "角色", "level": "人工新境界", "expected_revision": revision,
    })

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["effective_after_chapter"] == 1
    assert payload["revision"] != revision
    assert "第 1 章结束后生效" in payload["message"]
    stored = StateManager("book")
    assert stored.state.characters["角色"].current_state.level == "人工新境界"
    override = read_json(stored._manual_overrides_file)
    assert override["characters"]["角色"]["dynamic_history"][0]["effective_after_chapter"] == 1


def test_character_form_static_edit_does_not_turn_unchanged_state_into_override(project):
    manager, client = project
    character = CharacterCard(id="hero", name="角色", role="主角", background="旧背景")
    manager.upsert_character(character)
    response = client.patch("/api/novel/book/character", json={
        "name": character.name, "background": "新背景", "level": character.current_state.level,
        "location": character.current_state.location, "hp_status": character.current_state.hp_status,
        "current_mood": character.current_state.current_mood, "expected_revision": manager.revision,
    })
    assert response.status_code == 200, response.text
    assert response.json()["effective_after_chapter"] is None
    loaded = StateManager("book")
    entry = read_json(loaded._manual_overrides_file)["characters"][character.name]
    assert entry["static"] == {"background": "新背景"}
    assert entry["dynamic_history"] == []


def test_delete_requires_matching_revision_and_recreated_identity_invalidates_it(project):
    manager, client = project
    revision = manager.revision
    assert client.delete("/api/novel/book").status_code == 428
    assert client.delete("/api/novel/book", headers={"If-Match": "0" * 64}).status_code == 409
    assert client.delete("/api/novel/book", headers={"If-Match": revision}).status_code == 200
    manager = StateManager("book")
    manager.save_state()
    assert manager.revision != revision
    assert client.delete("/api/novel/book", headers={"If-Match": revision}).status_code == 409
    assert manager.base_dir.exists()


def test_queued_rewrite_rechecks_revision_before_any_model_call(project, monkeypatch):
    import services.generation_jobs as jobs

    manager, _ = project
    monkeypatch.setattr(jobs, "generation_status", web_ui.generation_status)
    tasks = BackgroundTasks()
    response = web_ui.regenerate_chapter(RegenerateRequest(
        name="book", chapter=1, expected_revision=manager.revision,
    ), tasks)
    manager.replace_chapter_text(1, "排队后作者保存的新文")
    pipeline = Mock(state_manager=StateManager("book"))
    monkeypatch.setattr(jobs, "NovelPipeline", Mock(return_value=pipeline))
    task = tasks.tasks[0]
    task.func(**task.kwargs)
    pipeline.generate_single_chapter.assert_not_called()
    status = web_ui.generation_status.snapshot()
    assert status["task_id"] == response["task_id"]
    assert not status["is_running"]
    assert "作品已更新" in status["error"]
    assert StateManager("book").state.chapters[1].full_text == "排队后作者保存的新文"


def test_queued_state_reconciliation_rechecks_revision_before_model_call(project, monkeypatch):
    import services.generation_jobs as jobs

    manager, _ = project
    manager.initialize_history_repair({}, "已核对章前空状态")
    expected_revision = manager.revision
    status = GenerationTaskStatus()
    monkeypatch.setattr(jobs, "generation_status", status)
    task_id = status.reserve("book")
    manager.replace_chapter_text(1, "排队后作者又保存了正文", mark_reconciliation=True)
    pipeline = Mock(state_manager=StateManager("book"))
    monkeypatch.setattr(jobs, "NovelPipeline", Mock(return_value=pipeline))

    jobs.run_state_reconciliation_job(
        "book", 1, task_id, expected_revision=expected_revision,
    )

    pipeline.reconcile_existing_chapter_state.assert_not_called()
    assert not status.snapshot()["is_running"]
    assert "作品已更新" in status.snapshot()["error"]


def test_same_version_race_after_api_read_still_cannot_overwrite(project, monkeypatch):
    manager, client = project
    original = web_ui._check_revision

    def race(sm, expected):
        original(sm, expected)
        manager.replace_chapter_text(1, "并发提交")

    monkeypatch.setattr(web_ui, "_check_revision", race)
    response = client.patch("/api/novel/book/chapter_text", json={
        "chapter": 1, "text": "迟到提交", "expected_revision": manager.revision,
    })
    assert response.status_code == 409
    assert StateManager("book").state.chapters[1].full_text == "并发提交"


def test_revision_is_project_bound(project):
    from core.storage import revision_token

    assert revision_token("a", ("same", None)) != revision_token("b", ("same", None))
