"""Full-project search is useful, bounded, read-only, and version-aware."""

import pytest
from fastapi.testclient import TestClient

import web_ui
from config.settings import get_settings
from core.project_search import search_project
from core.schemas import ChapterContent, CharacterCard, ForeshadowingRecord, NovelConcept
from core.state_manager import StateManager
from core.task_status import GenerationTaskStatus


@pytest.fixture
def searchable_project(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(web_ui, "generation_status", GenerationTaskStatus())
    manager = StateManager("book")
    manager.set_concept(
        NovelConcept(
            title="雾港来信",
            genre="悬疑",
            logline="失踪的摆渡人留下银色潮汐密码",
            golden_finger="回声罗盘",
            core_conflict="在涨潮前找出真凶",
        )
    )
    manager.upsert_character(
        CharacterCard(
            id="hero",
            name="楚封",
            role="主角",
            background="随身带着一枚裂纹玉佩",
        )
    )
    manager.upsert_foreshadowing(
        ForeshadowingRecord(
            hook_id="tide",
            title="倒流的潮钟",
            description="午夜潮钟会逆向敲响三次",
            planted_chapter=1,
            expected_resolution_volume=2,
        )
    )
    manager.save_chapter(
        ChapterContent(
            chapter_number=1,
            volume_number=1,
            title="潮声之前",
            full_text="港口忽然安静下来。楚封在旧船票背面看见银色潮汐四个字。",
            word_count=29,
            cliffhanger_ending="潮钟第一次倒响。",
        )
    )
    with TestClient(web_ui.app) as client:
        yield manager, client
    get_settings.cache_clear()


def test_search_finds_lazy_prose_and_structured_story_entities(searchable_project):
    manager, _ = searchable_project

    prose = search_project(manager.state, "银色潮汐")
    assert [(item["kind"], item["chapter_number"]) for item in prose] == [
        ("chapter", 1),
        ("concept", None),
    ]
    assert "银色潮汐" in prose[0]["snippet"]

    character = search_project(manager.state, "裂纹玉佩")
    assert character[0]["kind"] == "character"
    assert character[0]["tab"] == "characters"

    foreshadowing = search_project(manager.state, "逆向敲响")
    assert foreshadowing[0]["kind"] == "foreshadowing"
    assert foreshadowing[0]["key"] == "foreshadowing:tide"


def test_search_does_not_duplicate_chapter_title_results_and_honors_limit(searchable_project):
    manager, _ = searchable_project
    assert search_project(manager.state, "潮声之前") == []
    assert len(search_project(manager.state, "楚封", limit=1)) == 1
    assert search_project(manager.state, "楚封", limit=0) == []


def test_search_endpoint_is_bounded_versioned_and_read_only(searchable_project):
    manager, client = searchable_project
    before_revision = manager.revision
    before = {
        path.relative_to(manager.base_dir): path.read_bytes() for path in manager.base_dir.rglob("*") if path.is_file()
    }

    response = client.get("/api/novel/book/search", params={"q": "银色潮汐", "limit": 5})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["novel_id"] == "book"
    assert payload["revision"] == before_revision
    assert payload["results"][0]["chapter_number"] == 1

    after = {
        path.relative_to(manager.base_dir): path.read_bytes() for path in manager.base_dir.rglob("*") if path.is_file()
    }
    assert after == before
    assert client.get("/api/novel/book/search", params={"q": "   "}).status_code == 400
    assert client.get("/api/novel/book/search", params={"q": "x", "limit": 51}).status_code == 422
    assert client.get("/api/novel/book/search", params={"q": "x" * 121}).status_code == 422
