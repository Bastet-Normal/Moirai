"""Regressions for manuscript migration, reviewed revisions and measured model work."""

import io
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import web_ui
from agents.consistency_auditor import ConsistencyAuditorAgent
from config.settings import get_settings
from core import audit_evidence
from core.audit_evidence import find_audit_passages
from core.chapter_drafts import ChapterDraft, ChapterRewrite, store_chapter_rewrite
from core.llm import LLMClient
from core.manuscript_docx import DOCX_MEDIA_TYPE, read_docx
from core.manuscript_facts import ManuscriptFacts
from core.narrative_brancher import BranchingSimulationResult
from core.schemas import (
    AuditFinding,
    AuditReport,
    ChapterContent,
    ManuscriptContinuity,
    ManuscriptFact,
    StateUpdateDelta,
)
from core.state_manager import StateManager
from core.task_status import GenerationTaskStatus
from pipeline.novel_pipeline import NovelPipeline
from services import generation_jobs

PRIOR = "林岚把星银指环封进漆红的档案盒。"
LATER = "林岚声称星银指环一直留在自己的手上。"


@pytest.fixture
def studio(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://provider.example/v1")
    get_settings.cache_clear()
    status = GenerationTaskStatus()
    monkeypatch.setattr(web_ui, "generation_status", status)
    monkeypatch.setattr(generation_jobs, "generation_status", status)
    try:
        with TestClient(web_ui.app) as client:
            yield client
    finally:
        get_settings.cache_clear()


def import_book(client):
    response = client.post("/api/novels/import_manuscript", json={
        "name": "book", "title": "档案馆", "chapters": [
            {"title": "有雾", "text": PRIOR},
            {"title": "离开", "text": "大雨沿着石阶落下。"},
            {"title": "回到档案馆", "text": LATER},
        ],
    })
    assert response.status_code == 201, response.text
    return StateManager("book")


def test_world_background_is_revision_bound_and_preserves_other_setting_data(studio):
    manager = import_book(studio)
    url = "/api/novel/book/world"
    first = {"world_name": "雾港", "geography_overview": "山间港口。"}
    assert studio.patch(url, json=first).status_code == 428
    first["expected_revision"] = manager.revision
    saved = studio.patch(url, json=first)
    assert saved.status_code == 200, saved.text
    assert studio.patch(url, json=first).status_code == 409
    manager = StateManager("book")
    world = manager.state.world_setting.model_copy(update={"special_rules": ["满潮闭港"]})
    manager.set_world_setting(world)
    update = {"expected_revision": manager.revision, "geography_overview": "海峡以北。"}
    assert studio.patch(url, json={**update, "geography_overview": "   "}).status_code == 422
    assert studio.patch(url, json=update).status_code == 200
    assert StateManager("book").state.world_setting == world.model_copy(update={"geography_overview": "海峡以北。"})


def test_branch_without_outline_uses_bounded_prose_from_the_selected_chapter(studio):
    book = import_book(studio)
    book.replace_chapter_text(1, "头" * 600 + "不送出的中段" + "尾" * 600)
    pipeline = NovelPipeline(book.novel_id)
    brancher = Mock()
    brancher.simulate_branches.return_value = BranchingSimulationResult(decision_point_description="测试")
    pipeline.__dict__["narrative_brancher"] = brancher
    pipeline.simulate_chapter_branching(1)
    situation = brancher.simulate_branches.call_args.kwargs["current_situation"]
    assert "《有雾》" in situation and "不送出的中段" not in situation
    assert situation.endswith("头" * 600 + "\n…\n" + "尾" * 600)


def test_desk_preview_omits_full_prose_and_preserves_the_revision(studio):
    manager = import_book(studio)
    manager.replace_chapter_text(3, "雨" * 200 + "\n\n新的线索")
    view = studio.get("/api/novel/book/chapter/3?preview=true").json()
    assert view["revision"] == manager.revision
    assert view["chapter"] == {"chapter_number": 3, "title": "回到档案馆", "excerpt": "雨" * 160 + "…"}
    assert studio.get("/api/novel/book/chapter/3").json()["chapter"]["full_text"].endswith("新的线索")


def test_docx_round_trip_preview_and_split_txt_is_read_only(studio):
    initial = import_book(studio)
    revision = initial.revision
    word = studio.get("/api/novel/book/export_docx")
    assert word.status_code == 200
    assert word.headers["X-Project-Revision"] == revision
    assert word.headers["content-type"] == DOCX_MEDIA_TYPE
    parsed = read_docx(word.content)
    assert [entry["text"] for entry in parsed if entry["heading"]] == [
        "第1章 有雾", "第2章 离开", "第3章 回到档案馆",
    ]
    result = studio.post("/api/novels/preview_docx", content=word.content, headers={"content-type": DOCX_MEDIA_TYPE})
    assert result.status_code == 200, result.text
    assert result.json()["paragraphs"] == parsed
    bundle = studio.get("/api/novel/book/export_chapter_bundle")
    assert bundle.status_code == 200, bundle.text
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        assert len(archive.namelist()) == 3
        assert archive.read("第001章_有雾.txt").decode() == f"第1章 有雾\n\n{PRIOR}\n"
    preview = studio.get("/api/novel/book/publication_preview?chapter=1").json()
    assert preview["text"] == f"第1章 有雾\n\n{PRIOR}\n"
    assert preview["official_rules"][0]["date"]
    assert preview["revision"] == StateManager("book").revision == revision


def test_docx_rejects_unexpected_xml_declarations(studio):
    file = io.BytesIO()
    with zipfile.ZipFile(file, "w") as archive:
        archive.writestr("word/document.xml", '<!DOCTYPE document [<!ENTITY story "unexpected">]><document/>')
    result = studio.post(
        "/api/novels/preview_docx", content=file.getvalue(), headers={"content-type": DOCX_MEDIA_TYPE},
    )
    assert result.status_code == 422
    assert "XML" in result.json()["detail"]


def test_single_chapter_history_and_selective_rewrite_preserve_other_chapters(studio):
    manager = import_book(studio)
    previous = "林岚关上灯。\n\n雨还在下。\n\n她原地等候。"
    proposed = "林岚点起灯。\n\n雨还在下。\n\n她走下楼梯。"
    manager.replace_chapter_text(2, previous, title="等候")
    revision = manager.revision
    draft = ChapterDraft(
        content=ChapterContent(chapter_number=2, volume_number=1, title="离开", full_text=proposed),
        state_delta=StateUpdateDelta(summary_of_events="候选正文事件"),
        state_extraction_failed=False,
    )
    store_chapter_rewrite(manager.base_dir, ChapterRewrite(
        task_id="a" * 32, novel_id="book", expected_revision=revision, draft=draft,
    ))
    response = studio.get("/api/novel/book/rewrite_candidate")
    changes = response.json()["candidate"]["changes"]
    assert len(changes) == 2
    client_payload = {"task_id": "a" * 32, "expected_revision": revision}
    invalid = studio.post("/api/novel/book/rewrite_candidate", json={**client_payload, "selected_hunks": [1]})
    assert invalid.status_code == 422
    assert StateManager("book").state.chapters[2].full_text == previous
    latest_revision = StateManager("book").revision
    result = studio.post("/api/novel/book/rewrite_candidate", json={
        **client_payload, "expected_revision": latest_revision, "selected_hunks": [changes[0]["index"]],
    })
    # An invalid hunk fails before mutation; a correct retry uses the same valid draft.
    assert result.status_code == 200, result.text
    changed = StateManager("book")
    assert changed.state.chapters[1].full_text == PRIOR
    assert changed.state.chapters[2].full_text == "林岚点起灯。\n\n雨还在下。\n\n她原地等候。"
    assert changed.state.chapters[3].full_text == LATER
    versions = studio.get("/api/novel/book/chapter/2/versions").json()
    assert versions["revision"] == changed.revision
    number = versions["versions"][0]["number"]
    saved = studio.get(f"/api/novel/book/chapter/2/versions/{number}").json()
    assert saved["version"] == {"title": "等候", "text": previous}
    restored = studio.post(f"/api/novel/book/chapter/2/versions/{number}", json={
        "expected_revision": changed.revision,
    })
    assert restored.status_code == 200, restored.text
    assert StateManager("book").state.chapters[2].full_text == previous
    assert studio.post(f"/api/novel/book/chapter/2/versions/{number}", json={
        "expected_revision": changed.revision,
    }).status_code == 409


def test_far_chapter_passage_is_exact_and_future_is_never_considered():
    body = "档案馆很安静。守门人保持沉默。"
    chapters = {n: ChapterContent(chapter_number=n, volume_number=1, title=f"章 {n}", full_text=body)
                for n in range(1, 76)}
    chapters[1].full_text = f"{body}\n\n{PRIOR}"
    chapters[74].full_text = LATER
    chapters[75].full_text = f"未来发生的新事。{PRIOR}"
    passages, scanned = find_audit_passages(chapters, 74, LATER)
    assert scanned == 73
    assert any(item["chapter"] == 1 and PRIOR in item["text"] for item in passages)
    assert all(item["chapter"] < 74 and item["text"] in chapters[item["chapter"]].full_text for item in passages)


def test_frequent_alias_hits_do_not_crowd_out_distinctive_direct_passages():
    direct = "齿轮校准刻度为零。"
    alias_quote = "林岚的代号是白鹭。"
    chapters = {n: ChapterContent(chapter_number=n, volume_number=1, title=f"章 {n}", full_text="林岚走进庭院。")
                for n in range(1, 25)}
    chapters[1].full_text = direct
    chapters[2].full_text = alias_quote
    continuity = ManuscriptContinuity(from_chapter=25, facts=[
        ManuscriptFact(kind="alias", subject="林岚", fact="白鹭", quote=alias_quote, source_chapter=2),
    ])
    passages, _ = find_audit_passages(chapters, 25, "白鹭查阅齿轮校准刻度。", continuity=continuity)
    assert {"chapter": 1, "text": direct} in passages
    assert any(item["chapter"] not in {1, 2, 23, 24} and "林岚" in item["text"] for item in passages)


def test_author_confirmed_alias_recalls_chapter_one_in_chapter_74_preview_and_review(studio, monkeypatch):
    source = "林岚封存星银指环。"
    alias_quote = "林岚的代号是白鹭。"
    target = "白鹭抵达荒漠码头。"
    chapters = [{"title": f"章 {n}", "text": "暮雨穿过庭院。"} for n in range(1, 75)]
    chapters[0]["text"], chapters[1]["text"], chapters[-1]["text"] = source, alias_quote, target
    imported = studio.post("/api/novels/import_manuscript", json={
        "name": "book", "title": "档案馆", "chapters": chapters,
    })
    assert imported.status_code == 201, imported.text
    preview_url = "/api/model_preview?operation=audit&novel_id=book&chapter=74"
    revision = StateManager("book").revision
    alias = ManuscriptFact(kind="alias", subject="林岚", fact="白鹭", source_chapter=2, quote=alias_quote)
    provider = Mock()
    provider.generate_structured.return_value = ManuscriptFacts(facts=[alias.model_copy(update={"fact": "夜枭"}), alias])
    monkeypatch.setattr(web_ui, "get_llm_client", lambda: provider)
    extracted = studio.post("/api/novel/book/manuscript_facts", json={
        "expected_revision": revision, "start": 2, "end": 2,
    })
    assert extracted.status_code == 200, extracted.text
    assert extracted.json()["facts"] == [alias.model_dump()]
    assert 1 not in studio.get(preview_url).json()["source_chapters"]  # A candidate is not canon.
    approved = studio.post("/api/novel/book/continuation_plan", json={
        "expected_revision": revision, "confirmed_facts": extracted.json()["facts"],
        "direction": "追查指环", "next_title": "回程", "next_scene": "调查港口。", "chapters_to_plan": 1,
    })
    assert approved.status_code == 200, approved.text

    preview = studio.get(preview_url).json()
    assert 1 in preview["source_chapters"]
    assert StateManager("book").revision == approved.json()["revision"]

    finding = AuditFinding(issue="回查指环", draft_quote=target, source_chapter=1,
                           source_quote=source, certainty="needs_review", category="ownership")
    auditor = Mock(return_value=AuditReport(passed=True, consistency_score=8, pacing_score=8,
                                          cliffhanger_detected=False, critique="复核。", findings=[finding]))
    monkeypatch.setattr(ConsistencyAuditorAgent, "audit_chapter", auditor)
    audited = studio.post("/api/novel/book/chapter/74/audit", json={"expected_revision": preview["revision"]})
    assert audited.status_code == 200, audited.text
    report = StateManager("book").state.chapters[74].final_audit
    assert report is not None
    assert report.evidence_chapters == sorted(preview["source_chapters"][1:])
    assert report.findings[0].source_quote == source
    assert {"chapter": 1, "text": source} in auditor.call_args.kwargs["chapter_context"]["audit_passages"]

    StateManager("book").replace_chapter_text(2, "暮雨穿过庭院。")
    assert 1 not in studio.get(preview_url).json()["source_chapters"]


def test_author_decision_and_dependent_review_track_source_edits(studio, monkeypatch):
    book = import_book(studio)
    finding = AuditFinding(issue="指环归属冲突", draft_quote=LATER, source_chapter=1,
                           source_quote=PRIOR, certainty="contradiction", category="ownership")
    audited = Mock(side_effect=lambda **params: AuditReport(
        passed=False, consistency_score=5, pacing_score=8, cliffhanger_detected=False, critique="核对指环。",
        findings=[finding] if params["chapter_number"] == 3 else [],
    ))
    monkeypatch.setattr(ConsistencyAuditorAgent, "audit_chapter", audited)
    result = studio.post("/api/novel/book/chapter/3/audit", json={"expected_revision": book.revision})
    assert result.status_code == 200, result.text
    latest = StateManager("book")
    decision = studio.post("/api/novel/book/chapter/3/audit_decision", json={
        "expected_revision": latest.revision, "evidence_id": finding.evidence_id,
        "decision": "intentional", "note": "这是不可靠叙述的线索。",
    })
    assert decision.status_code == 200, decision.text
    persisted = StateManager("book")
    assert persisted.state.chapters[3].finding_decisions[finding.evidence_id].decision == "intentional"
    persisted.replace_chapter_text(1, "林岚把指环放回袖袋。")
    stale = studio.post("/api/novel/book/chapter/3/audit_decision", json={
        "expected_revision": persisted.revision, "evidence_id": finding.evidence_id, "decision": "reopen",
    })
    assert stale.status_code == 409
    rerun = studio.post("/api/novel/book/chapter/1/audit_related", json={"expected_revision": persisted.revision})
    assert rerun.status_code == 200, rerun.text
    assert rerun.json()["chapters"] == [1, 3]
    assert [call.kwargs["chapter_number"] for call in audited.call_args_list] == [3, 1, 3]
    assert StateManager("book").state.chapters[3].final_audit.findings == []


def test_audit_preview_and_report_distinguish_partial_scan_from_sent_excerpts(studio, monkeypatch):
    book = import_book(studio)
    recent_length = len(book.state.chapters[2].full_text)
    monkeypatch.setattr(audit_evidence, "MAX_SOURCE_CHARS", len(PRIOR) + 3)
    monkeypatch.setattr(audit_evidence, "MAX_QUERIED_CHARS", 9)
    preview = studio.get("/api/model_preview?operation=audit&novel_id=book&chapter=3").json()
    assert preview["source_chapters"] == [3, 1, 2]
    coverage = {
        "eligible_chapters": 2, "searched_chapters": 2, "fully_searched_chapters": 1,
        "eligible_chars": recent_length + len(PRIOR), "searched_chars": len(PRIOR) + 3,
        "query_truncated": True,
    }
    assert preview["retrieval_coverage"] == coverage

    auditor = Mock(return_value=AuditReport(passed=True, consistency_score=8, pacing_score=8,
                                          cliffhanger_detected=False, critique="复核。"))
    monkeypatch.setattr(ConsistencyAuditorAgent, "audit_chapter", auditor)
    _, legacy_count = find_audit_passages(book.state.chapters, 3, LATER)
    result = studio.post("/api/novel/book/chapter/3/audit", json={"expected_revision": preview["revision"]})
    assert result.status_code == 200, result.text
    report = StateManager("book").state.chapters[3].final_audit
    assert report is not None
    assert report.searched_chapters == legacy_count == 2
    assert report.retrieval_coverage is not None
    assert report.retrieval_coverage.model_dump() == coverage
    assert report.evidence_chapters == [1, 2]


def test_generated_draft_audit_retrieves_earlier_sources_from_its_actual_prose(studio, monkeypatch):
    book = import_book(studio)
    pipeline = NovelPipeline(book.novel_id)
    auditor = Mock(return_value=AuditReport(passed=True, consistency_score=8, pacing_score=8,
                                          cliffhanger_detected=False, critique="复核。"))
    monkeypatch.setattr(ConsistencyAuditorAgent, "audit_chapter", auditor)
    context = {"chapter_number": 4, "chapter_title": "指环", "audit_passages": [],
               "main_conflict": "官道尽头的突发意外。"}
    report = pipeline._audit_text(LATER, context, stage="draft", attempt=1)
    assert {"chapter": 1, "text": PRIOR} in auditor.call_args.kwargs["chapter_context"]["audit_passages"]
    assert report.retrieval_coverage is not None
    assert report.retrieval_coverage.fully_searched_chapters == 3
    assert context["audit_passages"] == []


def test_extract_verify_and_adopt_facts_then_extend_continuation_plan(studio, monkeypatch):
    manager = import_book(studio)
    fact = ManuscriptFact(kind="character", subject="林岚", fact="曾把星银指环封在档案盒内。",
                          source_chapter=1, quote=PRIOR)
    provider = Mock()
    provider.generate_structured.return_value = ManuscriptFacts(facts=[
        fact, fact.model_copy(update={"quote": "无原文的虚构证据。"}),
    ])
    monkeypatch.setattr(web_ui, "get_llm_client", lambda: provider)
    preview = studio.get("/api/model_preview?operation=facts&novel_id=book&chapter=1&count=1")
    assert preview.json()["source_chapters"] == [1]
    assert preview.json()["host"] == "provider.example"
    assert "fixture-key" not in preview.text
    provider.generate_structured.assert_not_called()
    extracted = studio.post("/api/novel/book/manuscript_facts", json={
        "expected_revision": manager.revision, "start": 1, "end": 1,
    })
    assert extracted.status_code == 200, extracted.text
    assert extracted.json()["facts"] == [fact.model_dump()]
    plan = studio.post("/api/novel/book/continuation_plan", json={
        "expected_revision": manager.revision, "confirmed_facts": extracted.json()["facts"],
        "direction": "查明档案盒的去向", "next_title": "档案目录",
        "next_scene": "林岚寻找空下的格子。", "chapters_to_plan": 1,
    })
    assert plan.status_code == 200, plan.text
    initialized = StateManager("book")
    assert initialized.state.manuscript_continuity.facts == [fact]
    assert any(fact.fact in rule for rule in initialized.build_chapter_context(4)["special_rules"])
    invalid = studio.post("/api/novel/continue", json={"name": "book", "count": 2, "expected_revision": initialized.revision})
    assert invalid.status_code == 422
    expanded = studio.patch("/api/novel/book/continuation_boundary", json={
        "end": 10, "expected_revision": initialized.revision,
    })
    assert expanded.status_code == 200, expanded.text
    assert StateManager("book").state.global_outline.volumes[0].chapter_range.end == 10


def test_worker_usage_keeps_provider_and_estimates_distinct(studio, monkeypatch):
    manager = import_book(studio)
    client = LLMClient.__new__(LLMClient)
    measured = SimpleNamespace(prompt_tokens=23, completion_tokens=11, total_tokens=34)
    report = AuditReport(passed=True, consistency_score=8, pacing_score=8, cliffhanger_detected=False, critique="通过。")

    def audit(_self, **_kwargs):
        messages = [{"role": "user", "content": "123456"}]
        client._record_usage(messages, "甲乙", measured, model="review-model")
        client._record_usage(messages, "甲乙", None, model="review-model")
        return report

    monkeypatch.setattr(LLMClient, "USAGE_TOTALS", {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0, "estimated": False})
    monkeypatch.setattr(ConsistencyAuditorAgent, "audit_chapter", audit)
    result = studio.post("/api/novel/book/chapter/1/audit", json={"expected_revision": manager.revision})
    assert result.status_code == 200, result.text
    accounting = studio.get("/api/llm_usage").json()["recent_tasks"][0]
    assert accounting["task_id"] == result.json()["task_id"]
    assert datetime.fromisoformat(accounting["recorded_at"]).tzinfo == UTC
    assert accounting["models"] == [{
        "model": "review-model", "reported_prompt": 23, "reported_completion": 11,
        "estimated_prompt": 6, "estimated_completion": 2, "reported_calls": 1, "estimated_calls": 1,
    }]
