import argparse
import logging
import threading
import zipfile
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import uuid4

import uvicorn
from fastapi import BackgroundTasks, Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile

from api.downloads import build_download
from api.request_limits import BACKUP_REQUEST_BODY, RequestLimitsMiddleware, backup_upload
from api.schemas import (
    AdoptBranchRequest,
    AppendChapterRequest,
    BranchSimRequest,
    ChapterRewriteRequest,
    ChapterTextRequest,
    CharacterUpdateRequest,
    ComplianceReplaceRequest,
    ConceptUpdateRequest,
    ConfigRequest,
    ContinuationBoundaryRequest,
    ContinuationPlanRequest,
    CreateNovelRequest,
    CustomWordsRequest,
    FindingDecisionRequest,
    GenerateChaptersRequest,
    HistoryRepairRequest,
    HumanReviewRequest,
    ImportManuscriptRequest,
    ManuscriptFactsRequest,
    OutlineUpdateRequest,
    ParaRewriteApplyRequest,
    ParaRewriteRequest,
    PatchParagraphRequest,
    ProjectRevisionRequest,
    ReaderSimRequest,
    RegenerateRequest,
    RenameRequest,
    RollbackRequest,
)
from config.compliance_words import (
    COMPLIANCE_CATEGORIES,
    ComplianceWordsError,
    compliance_terms,
    get_custom_words,
    save_custom_words,
    scan_text,
)
from config.settings import BASE_DIR, get_output_dir, get_settings
from core.audit_evidence import retrieve_audit_evidence
from core.chapter_drafts import REWRITE_FILENAME, apply_chapter_hunks, chapter_diff, read_chapter_rewrite
from core.chapter_versions import list_chapter_versions, load_chapter_version
from core.error_reporting import SafeUserMessage, report_internal_error, safe_user_message
from core.llm import get_llm_client
from core.manuscript_docx import DOCX_MEDIA_TYPE, InvalidManuscriptDocument, export_docx, read_docx
from core.manuscript_facts import (
    MAX_SOURCE_CHAPTERS,
    ManuscriptFacts,
    alias_pair,
    manuscript_samples,
    verify_sampled_facts,
)
from core.model_usage import collect_model_usage, usage_rows
from core.project_archive import InvalidProjectArchive, import_project_archive
from core.project_export import export_epub, export_markdown, export_platform_chapter, export_platform_text
from core.project_search import search_project
from core.project_transactions import inspect_recovery_journals, recover_all_projects_detailed, recover_project
from core.project_trash import (
    InvalidTrashEntryError,
    TrashEntryNotFoundError,
    list_trashed_projects,
    restore_trashed_project,
)
from core.runtime_config import apply_runtime_config
from core.schemas import (
    ChapterOutline,
    CharacterState,
    CommercialTarget,
    GlobalOutline,
    ManuscriptContinuity,
    NovelConcept,
    VolumeChapterRange,
    VolumeOutline,
    WorldSetting,
)
from core.state_manager import ChapterContextError, StateManager
from core.storage import ProjectBusyError, ProjectConflictError, ProjectReadOnlyError, project_lock, remove_file
from core.task_status import TaskAlreadyRunning, generation_status
from core.text_limits import ChapterTextLimitError, check_chapter_length, check_replacement_length
from pipeline.novel_pipeline import NovelPipeline
from services.generation_jobs import (
    run_audio_package_job,
    run_branch_simulation_job,
    run_chapter_audit_job,
    run_delivery_job,
    run_pipeline_job,
    run_reader_simulation_job,
    run_regenerate_job,
    run_related_audit_job,
    run_short_drama_job,
    run_state_reconciliation_job,
)

app = FastAPI(title="Moirai")
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
app.add_middleware(RequestLimitsMiddleware)
app.mount("/assets", StaticFiles(directory=BASE_DIR / "web" / "assets", check_dir=False), name="assets")
logger = logging.getLogger("novelengine.web")
_CONFIG_ENDPOINT_LOCK = threading.Lock()
OFFLINE_DEMO = False
LOCAL_WEB_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}
RECOVERY_GUIDANCE = [
    "先关闭其他 Moirai 窗口和命令行任务，再刷新诊断。",
    "处理前复制整个作品根目录和 .novelengine_transactions 目录作为离线备份。",
    "不要直接删除、重命名或编辑仍待恢复的事务目录。",
    "若问题仍在，请保留诊断编号并连同服务端日志交给熟悉文件事务的人员检查。",
]


def _recovery_payload(diagnostics: list[dict]) -> dict:
    return {
        "requires_attention": bool(diagnostics),
        "blocks_all_projects": any(item.get("blocks_all_projects") for item in diagnostics),
        "journals": diagnostics,
        "guidance": RECOVERY_GUIDANCE if diagnostics else [],
    }


@app.middleware("http")
async def protect_local_web_boundary(request: Request, call_next):
    """Reject DNS rebinding and cross-origin browser requests to the local API."""
    def harden_response(response):
        # This is a local stateful application: stale HTML or JSON can conceal
        # recovery locks, revisions and job state, so browser caching is unsafe.
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Moirai"] = "studio"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "font-src 'self' data:; "
            "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        )
        return response

    try:
        request_host = (request.url.hostname or "").casefold()
    except ValueError:
        return harden_response(JSONResponse(status_code=400, content={"detail": "Host 请求头非法"}))
    if request_host not in LOCAL_WEB_HOSTS:
        return harden_response(JSONResponse(status_code=421, content={"detail": "仅允许通过本机地址访问"}))

    origin = request.headers.get("origin")
    if origin:
        try:
            parsed_origin = urlparse(origin)
            origin_host = (parsed_origin.hostname or "").casefold()
        except ValueError:
            return harden_response(JSONResponse(status_code=403, content={"detail": "Origin 请求头非法"}))
        request_origin = f"{request.url.scheme}://{request.headers.get('host', '')}".rstrip("/")
        normalized_origin = f"{parsed_origin.scheme}://{parsed_origin.netloc}".rstrip("/")
        if origin_host not in LOCAL_WEB_HOSTS or normalized_origin.casefold() != request_origin.casefold():
            return harden_response(JSONResponse(status_code=403, content={"detail": "拒绝跨来源访问本机创作台"}))

    response = await call_next(request)
    return harden_response(response)

@app.get("/api/config")
def get_current_config():
    settings = get_settings()
    masked_key = ""
    if settings.OPENAI_API_KEY:
        masked_key = settings.OPENAI_API_KEY[:6] + "..." + settings.OPENAI_API_KEY[-4:] if len(settings.OPENAI_API_KEY) > 10 else "***"
    return {
        "has_key": bool(settings.OPENAI_API_KEY),
        "masked_key": masked_key,
        "base_url": settings.OPENAI_BASE_URL,
        "model": settings.DEFAULT_MODEL,
        "role_models": {
            "writer": settings.WRITER_MODEL,
            "planner": settings.PLANNER_MODEL,
            "critic": settings.CRITIC_MODEL,
            "state": settings.STATE_MODEL,
        },
        "audit_strictness": settings.AUDIT_STRICTNESS,
    }


@app.post("/api/config")
def update_config(req: ConfigRequest):
    if not req.base_url.strip() or not req.model.strip():
        raise HTTPException(status_code=400, detail="Base URL 与模型名不能为空")
    raw_url = req.base_url.strip()
    try:
        parsed_url = urlparse(raw_url)
        port = parsed_url.port  # Validates malformed or out-of-range ports.
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Base URL 地址或端口非法") from exc
    if (
        parsed_url.scheme not in ("http", "https") or not parsed_url.hostname
        or port == 0 or any(ord(char) <= 32 or ord(char) == 127 for char in raw_url)
    ):
        raise HTTPException(status_code=400, detail="Base URL 必须是有效的 http(s) 地址")
    if parsed_url.username or parsed_url.password or parsed_url.fragment:
        raise HTTPException(status_code=400, detail="Base URL 不得包含账号密码或 URL 片段")
    env_path = BASE_DIR / ".env"

    new_key = req.api_key.strip()
    base_url = req.base_url.strip().rstrip("/")
    default_model = req.model.strip()
    role_models = {
        "WRITER_MODEL": req.writer_model.strip() or default_model,
        "PLANNER_MODEL": req.planner_model.strip() or default_model,
        "CRITIC_MODEL": req.critic_model.strip() or default_model,
        "STATE_MODEL": req.state_model.strip() or default_model,
    }
    requested = {
        "OPENAI_BASE_URL": base_url,
        "DEFAULT_MODEL": default_model,
        **role_models,
    }
    if new_key:
        requested["OPENAI_API_KEY"] = new_key
    if req.audit_strictness in ("strict", "standard", "lenient"):
        requested["AUDIT_STRICTNESS"] = req.audit_strictness

    # Saving an unchanged form must not rewrite user formatting/comments in
    # .env or churn the live SDK client. Treat a trailing slash on the base URL
    # as semantically equivalent because requests normalize it below anyway.
    # 配置更新与生成任务争用同一个原子槽位；先开始的一方获胜。
    with _CONFIG_ENDPOINT_LOCK:
        current = get_settings()
        env_updates = {
            key: value
            for key, value in requested.items()
            if (
                current.OPENAI_BASE_URL.rstrip("/") != value.rstrip("/")
                if key == "OPENAI_BASE_URL"
                else str(getattr(current, key)) != value
            )
        }
        if not env_updates:
            return {"status": "unchanged", "message": "配置未变化，无需写入。"}
        try:
            task_id = generation_status.reserve("__settings__", step="正在更新模型配置")
        except TaskAlreadyRunning as exc:
            raise HTTPException(
                status_code=400,
                detail=safe_user_message(exc, fallback="当前有任务正在运行"),
            ) from exc
        try:
            apply_runtime_config(env_path, env_updates, get_llm_client())
        except ValueError as exc:
            generation_status.finish(step="配置更新失败", error="配置值无效", task_id=task_id)
            error_id = report_internal_error(
                logger,
                exc,
                scope="runtime_config_validation",
                context={"fields": ",".join(sorted(env_updates))},
            )
            raise HTTPException(
                status_code=400,
                detail=f"配置值无效（诊断编号 {error_id}）",
            ) from exc
        except Exception:
            generation_status.finish(step="配置更新失败", error="运行时配置未生效", task_id=task_id)
            raise
        else:
            generation_status.finish(step="配置已更新", progress=100, task_id=task_id)
    return {"status": "success", "message": "API 配置已成功保存并即时生效！"}


@app.get("/api/novels")
def list_novels():
    base_dir = get_output_dir()
    novels = []
    warnings = []
    recovery_diagnostics = recover_all_projects_detailed(base_dir)
    blocks_all_projects = any(item.get("blocks_all_projects") for item in recovery_diagnostics)
    if base_dir.exists() and not blocks_all_projects:
        for p in base_dir.iterdir():
            if p.is_dir() and (p / "project_state.json").exists():
                try:
                    pipeline = NovelPipeline(novel_id=p.name, lightweight=True, load_derived=False)
                    novels.append(pipeline.get_status_summary())
                except Exception as exc:
                    error_id = report_internal_error(
                        logger,
                        exc,
                        scope="bookshelf_project_load",
                        context={"novel_id": p.name},
                    )
                    warnings.append({
                        "novel_id": p.name,
                        "message": f"项目状态无法读取（诊断编号 {error_id}）",
                    })
    return {
        "novels": novels,
        "warnings": warnings,
        "recovery": _recovery_payload(recovery_diagnostics),
    }


@app.get("/api/recovery")
def get_recovery_diagnostics():
    """Read-only, path-free diagnostics; this endpoint never edits a journal."""
    return _recovery_payload(inspect_recovery_journals(get_output_dir()))


@app.get("/api/novel/{novel_id}")
def get_novel_detail(novel_id: str):
    _require_project(novel_id)
    pipeline = NovelPipeline(novel_id=novel_id, lightweight=True)
    sm = pipeline.state_manager
    state = sm.state
    checkpoint = state.pending_initial_planning
    if not state.concept and checkpoint is None:
        raise HTTPException(status_code=404, detail="未找到该小说项目")
    display_concept = state.concept or (checkpoint.concept if checkpoint else None)
    display_world = state.world_setting or (checkpoint.world_setting if checkpoint else None)
    display_characters = list(state.characters.values()) or (
        (checkpoint.initial_characters or []) if checkpoint else []
    )
    
    try:
        candidate = read_chapter_rewrite(sm.base_dir, sm.novel_id)
    except (OSError, ValueError):
        candidate = None
    pacing_beats = []
    if state.global_outline and state.global_outline.pacing_schedule:
        pacing_beats = [b.model_dump() for b in state.global_outline.pacing_schedule.pacing_beats]

    short_dramas = {k: v.model_dump() for k, v in sm.short_drama_scripts.items()}
    branch_sims = {k: v.model_dump() for k, v in sm.branch_simulations.items()}
    reader_sims = {k: v.model_dump() for k, v in sm.reader_simulations.items()}
    try:
        timeline_status = sm.get_timeline_status()
    except Exception:
        timeline_status = {
            "mode": "invalid", "complete": False, "chapter_one_context_available": False,
            "message": "状态时间线损坏，项目已进入只读保护；请先备份并检查加载警告。",
        }

    return {
        "novel_id": sm.novel_id,
        "revision": sm.revision,
        "concept": display_concept.model_dump() if display_concept else None,
        "world_setting": display_world.model_dump() if display_world else None,
        "manuscript_facts": [fact.model_dump() for fact in state.manuscript_continuity.facts] if state.manuscript_continuity else [],
        "global_outline": state.global_outline.model_dump() if state.global_outline else None,
        "pending_outline_plan": state.pending_outline_plan is not None,
        "imported_manuscript": state.imported_manuscript,
        "rewrite_candidate_chapter": candidate.draft.content.chapter_number if candidate else None,
        "pacing_beats": pacing_beats,
        "characters": [c.model_dump() for c in display_characters],
        "foreshadowings": [f.model_dump() for f in state.foreshadowings.values()],
        "chapters": [
            c.model_dump(exclude={"full_text", "beats_raw_text"})
            for c in state.chapters.values()
        ],
        "delivery_package": state.delivery_package.model_dump() if state.delivery_package else None,
        "knowledge_graph": sm.knowledge_graph.model_dump(),
        "cytoscape_data": sm.knowledge_graph.export_cytoscape_data(),
        "short_dramas": short_dramas,
        "audio_package": sm.audio_package.model_dump() if sm.audio_package else None,
        "branch_simulations": branch_sims,
        "reader_simulations": reader_sims,
        "state_reconciliation": sm.get_state_reconciliation_status(),
        "timeline_status": timeline_status,
        "load_warnings": sm.load_warnings,
        "planning_checkpoint": {"next_stage": checkpoint.next_stage} if checkpoint else None,
        "summary": pipeline.get_status_summary(),
    }


@app.get("/api/novel/{novel_id}/chapter/{chapter_num}")
def get_chapter_detail(novel_id: str, chapter_num: int):
    _require_project(novel_id)
    if chapter_num < 1:
        raise HTTPException(status_code=400, detail="章节序号必须 ≥ 1")
    chapter, revision = StateManager.read_project_chapter_versioned(novel_id, chapter_num)
    if not chapter:
        raise HTTPException(status_code=404, detail=f"章节不存在: 第 {chapter_num} 章")
    return {"chapter": chapter.model_dump(), "revision": revision, "novel_id": novel_id}


@app.get("/api/novel/{novel_id}/search")
def search_novel(
    novel_id: str,
    q: Annotated[str, Query(min_length=1, max_length=120)],
    limit: Annotated[int, Query(ge=1, le=50)] = 40,
):
    """Search one project without changing its revision or loading derived artifacts."""
    _require_project(novel_id)
    normalized_query = " ".join(q.split())
    if not normalized_query:
        raise HTTPException(status_code=400, detail="搜索词不能为空")
    state_manager = StateManager(
        novel_id=novel_id,
        load_memory=False,
        load_derived=False,
    )
    return {
        "novel_id": novel_id,
        "revision": state_manager.revision,
        "query": normalized_query,
        "results": search_project(state_manager.state, normalized_query, limit=limit),
    }


@app.get("/api/novel/{novel_id}/snapshots")
def get_snapshot_statuses(novel_id: str):
    _require_project(novel_id)
    pipeline = NovelPipeline(novel_id=novel_id, lightweight=True, load_derived=False)
    sm = pipeline.state_manager
    statuses = [sm.snapshot_status(number) for number in sorted(sm.state.chapters)]
    return {"snapshots": statuses, "revision": sm.revision}


@app.get("/api/novel/{novel_id}/history_repair")
def get_history_repair_preview(novel_id: str):
    _require_project(novel_id)
    sm = StateManager(novel_id, load_chapter_content=False, load_memory=False, load_derived=False)
    try:
        status = sm.get_timeline_status()
    except Exception:
        status = {"mode": "invalid", "integrity_valid": False, "complete": False}
    candidates = [
        {
            "name": card.name,
            "role": card.role,
            "initial_state": card.initial_state.model_dump(),
        }
        for card in sorted(sm.state.characters.values(), key=lambda item: item.name)
    ]
    chapter_numbers = sorted(sm.state.chapters)
    chapters_contiguous = chapter_numbers == list(range(1, sm.state.current_chapter_idx + 1))
    return {
        "novel_id": sm.novel_id,
        "revision": sm.revision,
        "eligible": bool(sm.state.chapters) and chapters_contiguous and status.get("baseline_chapter") != 0
                    and status.get("integrity_valid", True) and not sm.load_warnings,
        "timeline_status": status,
        "characters": candidates,
        "confirmation": "我已核对第1章开始前状态",
        "warnings": [
            "系统只展示人物卡中已保存的 initial_state 候选值，不会自动认定它们属于第 1 章之前。",
            "只有勾选并逐项核对的人物会进入第 0 章基线；关系、伏笔和后续出场人物不会被猜测。",
            "确认后，现有章节状态须从第 1 章起按顺序重新提取；旧时间线会保留在证据归档中。",
        ],
        "chapters_contiguous": chapters_contiguous,
        "load_warnings": sm.load_warnings,
    }


@app.post("/api/novel/{novel_id}/history_repair")
def initialize_history_repair(novel_id: str, req: HistoryRepairRequest):
    _require_project(novel_id)
    sm = StateManager(novel_id)
    _check_revision(sm, req.expected_revision)
    seeds = {
        item.name: CharacterState.model_validate(item.model_dump(exclude={"name"}))
        for item in req.characters
    }
    timeline = sm.initialize_history_repair(seeds, req.author_note)
    return {
        "status": "repair_started",
        "message": "第 0 章状态已按作者确认建立；请从第 1 章起依次提取现有正文状态。",
        "novel_id": sm.novel_id,
        "revision": sm.revision,
        "timeline_status": timeline,
    }


@app.post("/api/novel/{novel_id}/history_repair/{chapter_num}/extract")
def extract_historical_chapter_state(
    novel_id: str, chapter_num: int, req: ProjectRevisionRequest, background_tasks: BackgroundTasks,
):
    """Queue state-only extraction; accepted prose is never regenerated by this route."""
    _require_project(novel_id)
    if not 1 <= chapter_num <= 1_000_000:
        raise HTTPException(status_code=400, detail="章节序号必须在 1 到 1000000 之间")
    sm = StateManager(
        novel_id, load_chapter_content=False, load_memory=False, load_derived=False,
    )
    _check_revision(sm, req.expected_revision)
    if chapter_num not in sm.state.chapters:
        raise HTTPException(status_code=404, detail=f"章节不存在: 第 {chapter_num} 章")
    timeline = sm.get_timeline_status()
    pending = sm.get_state_reconciliation_status()["chapters"]
    if timeline.get("baseline_chapter") != 0 or not timeline.get("integrity_valid"):
        raise HTTPException(status_code=409, detail="缺少可信的第 0 章状态基线")
    if not pending:
        raise HTTPException(status_code=409, detail="当前没有待核对的章节状态")
    if chapter_num != pending[0]:
        raise HTTPException(status_code=409, detail=f"必须按顺序核对；下一章是第 {pending[0]} 章")
    try:
        task_id = generation_status.reserve(
            novel_id, step=f"第 {chapter_num} 章状态提取已排队",
        )
    except TaskAlreadyRunning as exc:
        raise HTTPException(
            status_code=400,
            detail=safe_user_message(exc, fallback="当前有任务正在运行"),
        ) from exc
    background_tasks.add_task(
        run_state_reconciliation_job,
        name=novel_id,
        chapter=chapter_num,
        task_id=task_id,
        expected_revision=req.expected_revision,
    )
    return {
        "status": "started", "task_id": task_id,
        "message": f"已启动第 {chapter_num} 章状态提取；正文不会被重写",
    }


@app.post("/api/novel/create")
def create_novel(req: CreateNovelRequest, background_tasks: BackgroundTasks):
    if not _valid_project_id(req.name):
        raise HTTPException(status_code=400, detail="项目名非法：1~64位中文/字母/数字/下划线/连字符")
    if not req.idea.strip():
        raise HTTPException(status_code=400, detail="核心创意不能为空")
    if StateManager.project_exists(req.name):
        raise HTTPException(status_code=409, detail=f"已存在同名项目: {req.name}")

    commercial_target = CommercialTarget(
        target_platform=req.target_platform,
        author_style=req.author_style,
        protagonist_persona=req.protagonist_persona,
        female_lead_setting=req.female_lead_setting,
        avoided_tropes=req.avoided_tropes,
    )

    try:
        task_id = generation_status.reserve(req.name, step="新建作品", can_stop=True)
    except TaskAlreadyRunning as exc:
        raise HTTPException(
            status_code=400,
            detail=safe_user_message(exc, fallback="当前有任务正在运行"),
        ) from exc
    background_tasks.add_task(
        run_pipeline_job,
        name=req.name,
        idea=req.idea,
        genre=req.genre,
        target=commercial_target,
        chapter_count=req.chapters,
        volumes=req.volumes,
        task_id=task_id,
        include_adaptations=req.include_adaptations,
    )
    return {"status": "started", "task_id": task_id, "message": "生产流水线已启动！"}


@app.post("/api/novel/continue")
def continue_novel(req: GenerateChaptersRequest, background_tasks: BackgroundTasks):
    _require_project(req.name)

    pipeline = NovelPipeline(novel_id=req.name, lightweight=True, load_derived=False)
    _check_revision(pipeline.state_manager, req.expected_revision)
    state = pipeline.state_manager.state
    if not state.concept and state.pending_initial_planning is None:
        raise HTTPException(status_code=404, detail="未找到该小说项目的策划案，无法续写！")
    if state.imported_manuscript and state.global_outline is None:
        raise HTTPException(status_code=409, detail="请先为存稿建立续写规划")

    current_idx = state.current_chapter_idx
    count = req.count
    try:
        _, target_chapters = NovelPipeline.plan_chapter_range(current_idx, count)
    except SafeUserMessage as exc:
        raise HTTPException(
            status_code=422,
            detail=safe_user_message(exc, fallback="生成范围无效"),
        ) from exc

    if state.global_outline and state.global_outline.volumes:
        last_range = state.global_outline.volumes[-1].chapter_range
        if last_range is not None and target_chapters > last_range.end:
            raise HTTPException(status_code=422, detail=f"续写会超过总纲终章（第 {last_range.end} 章），请调整章数或终章")

    try:
        task_id = generation_status.reserve(req.name, step="批量续写", can_stop=True)
    except TaskAlreadyRunning as exc:
        raise HTTPException(
            status_code=400,
            detail=safe_user_message(exc, fallback="当前有任务正在运行"),
        ) from exc
    background_tasks.add_task(
        run_pipeline_job,
        name=req.name,
        idea="",
        genre="",
        target=(
            state.concept.commercial_target or CommercialTarget()
            if state.concept else state.pending_initial_planning.request.commercial_target
        ),
        chapter_count=count,
        volumes=3,
        task_id=task_id,
        expected_revision=req.expected_revision,
        include_adaptations=req.include_adaptations,
    )
    if state.pending_initial_planning is not None:
        prefix = f"将从已保存的主策划 {state.pending_initial_planning.next_stage} 阶段恢复，再完成总纲并开始续写。"
    elif state.pending_outline_plan is not None:
        prefix = "将先恢复未完成的初始总纲，再开始续写。"
    else:
        prefix = ""
    return {"status": "started", "task_id": task_id, "message": prefix + f"已启动续写任务 (从第 {current_idx + 1} 章新写 {count} 章，至第 {target_chapters} 章)！"}


def _ensure_idle():
    """写状态类接口统一并发防护：后台生产进行中时拒绝并发操作，避免多线程同时写同一批状态文件"""
    if generation_status.is_running:
        raise HTTPException(status_code=400, detail="当前有正在生成的任务，请等待完成后再操作！")


def _valid_project_id(novel_id: str) -> bool:
    """项目 ID 白名单校验：阻断 .. 等路径逃逸与非法字符"""
    return StateManager.is_valid_project_id(novel_id)


def _require_project(novel_id: str):
    """项目不存在（或 ID 非法）时提前 404，避免 StateManager 为不存在的项目创建空目录"""
    if not _valid_project_id(novel_id) or not StateManager.project_exists(novel_id):
        raise HTTPException(status_code=404, detail=f"小说项目不存在: {novel_id}")


def _check_revision(sm: StateManager, expected: str | None) -> None:
    if not expected:
        raise HTTPException(status_code=428, detail="缺少作品版本，请先读取最新内容再提交修改")
    sm.assert_revision(expected)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # Do not echo submitted API keys, prose or arbitrary payloads back in errors.
    errors = [
        {"field": ".".join(str(part)[:80].encode("utf-8", "backslashreplace").decode("utf-8") for part in error["loc"]),
         "message": error["msg"][:200], "type": error["type"]}
        for error in exc.errors()[:10]
    ]
    detail = "请求参数无效：" + "；".join(f"{item['field']}: {item['message']}" for item in errors)
    return JSONResponse(status_code=422, content={"detail": detail, "errors": errors})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Log message-free exception structure and return a correlation ID only."""
    route = request.scope.get("route")
    request_id = report_internal_error(
        logger,
        exc,
        scope="http_request",
        context={
            "method": request.method,
            "route": getattr(route, "path", "unmatched"),
        },
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "服务器内部错误，请查看服务端日志", "request_id": request_id},
    )


@app.exception_handler(ProjectBusyError)
async def project_busy_handler(request: Request, exc: ProjectBusyError):
    detail = safe_user_message(exc, fallback="项目正在被其他任务使用")
    return JSONResponse(status_code=423, content={"detail": detail}, headers={"Retry-After": "2"})


@app.exception_handler(ProjectConflictError)
@app.exception_handler(ProjectReadOnlyError)
@app.exception_handler(ChapterContextError)
async def project_conflict_handler(request: Request, exc: RuntimeError):
    detail = safe_user_message(exc, fallback="项目状态冲突，请刷新后重试")
    return JSONResponse(status_code=409, content={"detail": detail})


@app.exception_handler(ChapterTextLimitError)
async def chapter_text_limit_handler(request: Request, exc: ChapterTextLimitError):
    detail = safe_user_message(exc, fallback="正文超过安全上限")
    return JSONResponse(status_code=422, content={"detail": detail})


@app.exception_handler(ComplianceWordsError)
async def compliance_words_handler(request: Request, exc: ComplianceWordsError):
    detail = safe_user_message(exc, fallback="自定义合规词库不可用")
    return JSONResponse(status_code=503, content={"detail": detail})


@app.post("/api/novel/branch_simulate")
def branch_simulate(req: BranchSimRequest, background_tasks: BackgroundTasks):
    _require_project(req.name)
    pipeline = NovelPipeline(novel_id=req.name, lightweight=True, load_derived=False)
    _check_revision(pipeline.state_manager, req.expected_revision)
    if not pipeline.state_manager.state.concept:
        raise HTTPException(status_code=400, detail="项目缺少策划案，无法推演剧情分支")
    if req.chapter not in pipeline.state_manager.state.chapters:
        raise HTTPException(status_code=404, detail=f"章节不存在: 第 {req.chapter} 章")
    try:
        task_id = generation_status.reserve(req.name, step=f"第 {req.chapter} 章剧情分支推演已排队")
    except TaskAlreadyRunning as exc:
        raise HTTPException(
            status_code=400,
            detail=safe_user_message(exc, fallback="当前有任务正在运行"),
        ) from exc
    background_tasks.add_task(
        run_branch_simulation_job,
        name=req.name,
        chapter=req.chapter,
        task_id=task_id,
        expected_revision=req.expected_revision,
    )
    return {"status": "started", "task_id": task_id, "message": f"第 {req.chapter} 章剧情分支推演已启动"}


@app.post("/api/novel/reader_simulate")
def reader_simulate(req: ReaderSimRequest, background_tasks: BackgroundTasks):
    _require_project(req.name)
    pipeline = NovelPipeline(novel_id=req.name, lightweight=True, load_derived=False)
    _check_revision(pipeline.state_manager, req.expected_revision)
    if not pipeline.state_manager.state.concept:
        raise HTTPException(status_code=400, detail="项目缺少策划案，无法推演读者留存")
    if req.chapter not in pipeline.state_manager.state.chapters:
        raise HTTPException(status_code=404, detail=f"章节不存在: 第 {req.chapter} 章")
    try:
        task_id = generation_status.reserve(req.name, step=f"第 {req.chapter} 章读者推演已排队")
    except TaskAlreadyRunning as exc:
        raise HTTPException(
            status_code=400,
            detail=safe_user_message(exc, fallback="当前有任务正在运行"),
        ) from exc
    background_tasks.add_task(
        run_reader_simulation_job,
        name=req.name,
        chapter=req.chapter,
        task_id=task_id,
        expected_revision=req.expected_revision,
    )
    return {"status": "started", "task_id": task_id, "message": f"第 {req.chapter} 章读者推演已启动"}


@app.post("/api/novel/patch_paragraph")
def patch_paragraph(req: PatchParagraphRequest):
    _ensure_idle()
    _require_project(req.name)
    if not req.old_text.strip():
        raise HTTPException(status_code=400, detail="目标段落内容不能为空")
    pipeline = NovelPipeline(novel_id=req.name)
    _check_revision(pipeline.state_manager, req.expected_revision)
    chapter = pipeline.state_manager.state.chapters.get(req.chapter)
    if chapter and req.old_text in chapter.full_text:
        check_replacement_length(chapter.full_text, req.old_text, req.new_text, count=1)
    ok = pipeline.state_manager.patch_paragraph(
        req.chapter, req.old_text, req.new_text, mark_reconciliation=True
    )
    if not ok:
        raise HTTPException(status_code=400, detail="未找到目标段落内容，修改失败！")
    return {"status": "success", "message": "段落已精准微手术更新！"}


@app.post("/api/novel/rollback")
def rollback_novel(req: RollbackRequest):
    _ensure_idle()
    _require_project(req.name)
    if req.target_chapter < 1:
        raise HTTPException(status_code=400, detail="目标章节必须 ≥ 1")

    pipeline = NovelPipeline(novel_id=req.name)
    _check_revision(pipeline.state_manager, req.expected_revision)
    success = pipeline.state_manager.rollback_to_chapter(req.target_chapter)
    if not success:
        raise HTTPException(status_code=400, detail=f"无法回滚至第 {req.target_chapter} 章快照")
    return {"status": "success", "message": f"已成功回滚至第 {req.target_chapter} 章！"}


@app.patch("/api/novel/{novel_id}/concept")
def update_concept(novel_id: str, req: ConceptUpdateRequest):
    _ensure_idle()
    _require_project(novel_id)
    pipeline = NovelPipeline(novel_id=novel_id)
    _check_revision(pipeline.state_manager, req.expected_revision)
    stored_concept = pipeline.state_manager.state.concept
    if not stored_concept:
        raise HTTPException(status_code=404, detail="项目缺少策划案")
    concept = stored_concept.model_copy(deep=True)
    for field in ("title", "genre", "logline", "golden_finger"):
        value = getattr(req, field)
        if value is not None and value.strip():
            setattr(concept, field, value.strip())
    pipeline.state_manager.set_concept(concept)
    return {"status": "success", "message": "策划案已更新"}


@app.patch("/api/novel/{novel_id}/character")
def update_character(novel_id: str, req: CharacterUpdateRequest):
    _ensure_idle()
    _require_project(novel_id)
    pipeline = NovelPipeline(novel_id=novel_id)
    sm = pipeline.state_manager
    _check_revision(sm, req.expected_revision)
    char = sm.state.characters.get(req.name)
    if not char:
        raise HTTPException(status_code=404, detail=f"角色不存在: {req.name}")
    char = char.model_copy(deep=True)
    manual_updates = {}
    if req.role is not None and req.role.strip():
        char.role = req.role.strip()
        manual_updates["role"] = char.role
    if req.background is not None:
        char.background = req.background.strip()
        manual_updates["background"] = char.background
    if req.personality_tags is not None:
        char.personality_tags = [t.strip() for t in req.personality_tags if t.strip()]
        manual_updates["personality_tags"] = char.personality_tags
    for field in ("level", "location", "hp_status", "current_mood"):
        value = getattr(req, field)
        if value is not None and value.strip():
            setattr(char.current_state, field, value.strip())
            manual_updates[field] = value.strip()
    effective_after_chapter = sm.save_manual_character(char, manual_updates)
    message = f"角色「{char.name}」已更新"
    if effective_after_chapter is not None:
        message += f"；动态状态从第 {effective_after_chapter} 章结束后生效"
    return {
        "status": "success",
        "message": message,
        "effective_after_chapter": effective_after_chapter,
        "revision": sm.revision,
    }


@app.post("/api/novel/{novel_id}/manuscript_facts")
def extract_manuscript_facts(novel_id: str, req: ManuscriptFactsRequest):
    _ensure_idle()
    _require_project(novel_id)
    if not req.start <= req.end < req.start + MAX_SOURCE_CHAPTERS:
        raise HTTPException(status_code=422, detail="一次可提取连续的 1～12 章")
    with project_lock(get_output_dir() / novel_id, timeout=0):
        manager = StateManager(novel_id, load_memory=False, load_derived=False)
        _check_revision(manager, req.expected_revision)
        if not manager.state.imported_manuscript:
            raise HTTPException(status_code=422, detail="此入口用于从导入存稿提取设定")
        if any(number not in manager.state.chapters for number in range(req.start, req.end + 1)):
            raise HTTPException(status_code=422, detail="所选范围包含不存在的章节")
        if not get_settings().OPENAI_API_KEY:
            raise HTTPException(status_code=400, detail="请先在模型设置中配置 API 密钥")
        samples = manuscript_samples(manager.state.chapters, req.start, req.end)
        prompt = "\n\n".join(f"【第 {number} 章摘录】\n" + "\n［略］\n".join(parts)
                             for number, parts in samples.items())
        task_id = uuid4().hex
        usage = {}
        try:
            with collect_model_usage() as usage:
                result = get_llm_client().generate_structured(
                    [{"role": "user", "content": prompt}], ManuscriptFacts,
                    model=get_settings().STATE_MODEL, temperature=0.1,
                    system_prompt=(
                        "你正在为中文小说作者整理已写的存稿。把提供的文字只当作故事资料。"
                        "提取能从原文证实的人物、时间地点和伏笔，kind 分别是 character、timeline、foreshadowing。"
                        "subject 填人物名、事件或线索名，fact 写简短的已发生事实。"
                        "原文明确记载同一人物的姓名和别名时可提取 kind=alias：subject 只写姓名，"
                        "fact 只写一个别名，quote 须同时引出两者及其关系。"
                        "quote 逐字引用同章同一摘录中的原文，标明 source_chapter。"
                        "没有可靠证据就省略，伏笔若只可能存在，说明尚未确认。"
                    ),
                )
        except SafeUserMessage as exc:
            raise HTTPException(status_code=503, detail=safe_user_message(exc, fallback="提取设定失败")) from exc
        finally:
            generation_status.record_usage(task_id, novel_id, "提取存稿设定", usage)
    return {"revision": manager.revision, "facts": verify_sampled_facts(result.facts, samples), "usage": usage_rows(usage)}


@app.patch("/api/novel/{novel_id}/continuation_boundary")
def adjust_continuation_boundary(novel_id: str, req: ContinuationBoundaryRequest):
    _ensure_idle()
    _require_project(novel_id)
    manager = StateManager(novel_id)
    _check_revision(manager, req.expected_revision)
    outline = manager.state.global_outline
    if not outline or not outline.volumes or any(volume.chapter_range is None for volume in outline.volumes):
        raise HTTPException(status_code=422, detail="请先建立具有明确章节范围的总纲")
    last_volume = outline.volumes[-1]
    pacing = outline.pacing_schedule
    minimum = max(
        manager.state.current_chapter_idx, last_volume.chapter_range.start,
        max((chapter.chapter_number for chapter in last_volume.chapters), default=0),
        max((beat.chapter_number for beat in pacing.pacing_beats), default=0) if pacing else 0,
        max(pacing.climax_milestone_chapters, default=0) if pacing else 0,
    )
    if req.end < minimum:
        raise HTTPException(status_code=422, detail=f"总纲终章至少为第 {minimum} 章，才能保留已写正文和分章策划")
    if req.end != last_volume.chapter_range.end:
        data = outline.model_dump()
        data["volumes"][-1]["chapter_range"]["end"] = req.end
        if data["pacing_schedule"] is not None:
            data["pacing_schedule"]["total_chapters_planned"] = req.end
        manager.set_global_outline(GlobalOutline.model_validate(data))
    return {"revision": manager.revision, "end": req.end}


@app.post("/api/novel/{novel_id}/continuation_plan")
def define_continuation_plan(novel_id: str, req: ContinuationPlanRequest):
    """Start a bounded outline from author-confirmed facts for an existing manuscript."""
    _ensure_idle()
    _require_project(novel_id)
    manager = StateManager(novel_id)
    _check_revision(manager, req.expected_revision)
    state = manager.state
    if state.global_outline or state.pending_initial_planning or state.pending_outline_plan:
        raise ProjectConflictError("作品已有总纲或处于策划恢复中，请在原大纲中继续")
    if not state.concept or not state.chapters or state.current_volume_idx != 1:
        raise HTTPException(status_code=422, detail="续写规划需要现有的单卷存稿")
    start = state.current_chapter_idx + 1
    end = start + req.chapters_to_plan - 1
    if end > 1_000_000:
        raise HTTPException(status_code=422, detail="规划超出章节安全上限")
    manager.state_before_chapter(start)
    concept = state.concept.model_copy(update={
        "logline": state.concept.logline or req.direction,
        "core_conflict": req.direction,
    })
    world = state.world_setting or WorldSetting(world_name=concept.title, geography_overview="")
    fact_keys = set()
    for fact in req.confirmed_facts:
        source = state.chapters.get(fact.source_chapter)
        key = (fact.kind, fact.source_chapter, fact.quote)
        if (source is None or fact.quote not in source.full_text or key in fact_keys
                or (fact.kind == "alias" and alias_pair(fact) is None)):
            raise HTTPException(status_code=422, detail="待确认设定引用不存在、已改动或重复")
        fact_keys.add(key)
    outline = GlobalOutline(
        novel_title=concept.title, main_theme=req.direction, total_volumes=1, final_climax=req.direction,
        volumes=[VolumeOutline(
            volume_number=1, volume_title=concept.title, volume_goal=req.direction,
            main_antagonist="", volume_climax=req.direction, chapter_range=VolumeChapterRange(start=1, end=end),
            chapters=[ChapterOutline(
                chapter_number=start, volume_number=1, title=req.next_title,
                summary=req.next_scene, main_conflict=req.next_scene, expectation_hook="",
            )],
        )],
    )
    with manager.mutation():
        state.manuscript_continuity = ManuscriptContinuity(
            background=req.story_so_far, from_chapter=start, facts=req.confirmed_facts,
        )
        manager.set_concept(concept)
        manager.set_world_setting(world)
        manager.set_global_outline(outline)
    return {"chapter": start, "end": end, "revision": manager.revision}


@app.post("/api/novel/{novel_id}/chapters", status_code=201)
def append_manual_chapter(novel_id: str, req: AppendChapterRequest):
    _ensure_idle()
    _require_project(novel_id)
    manager = StateManager(novel_id)
    _check_revision(manager, req.expected_revision)
    chapter = manager.append_manual_chapter(req.title, req.text)
    return {"chapter": chapter.chapter_number, "revision": manager.revision}


@app.patch("/api/novel/{novel_id}/chapter_text")
def update_chapter_text(novel_id: str, req: ChapterTextRequest):
    _ensure_idle()
    _require_project(novel_id)
    check_chapter_length(len(req.text))
    pipeline = NovelPipeline(novel_id=novel_id)
    _check_revision(pipeline.state_manager, req.expected_revision)
    ok = pipeline.state_manager.replace_chapter_text(
        req.chapter, req.text, mark_reconciliation=True, title=req.title,
    )
    if not ok:
        raise HTTPException(status_code=404, detail=f"章节不存在: 第 {req.chapter} 章")
    return {"status": "success", "message": f"第 {req.chapter} 章已更新"}


@app.get("/api/novel/{novel_id}/chapter/{chapter_num}/versions")
def chapter_versions(novel_id: str, chapter_num: int):
    _require_project(novel_id)
    project_dir = get_output_dir() / novel_id
    recover_project(project_dir.parent, novel_id)
    with project_lock(project_dir, scope="snapshot"):
        chapter, revision = StateManager.read_project_chapter_versioned(novel_id, chapter_num, recover=False)
        if chapter is None:
            raise HTTPException(status_code=404, detail="章节不存在")
        return {"revision": revision, "versions": list_chapter_versions(project_dir, chapter_num)}


@app.get("/api/novel/{novel_id}/chapter/{chapter_num}/versions/{number}")
def chapter_version(novel_id: str, chapter_num: int, number: int):
    _require_project(novel_id)
    project_dir = get_output_dir() / novel_id
    recover_project(project_dir.parent, novel_id)
    with project_lock(project_dir, scope="snapshot"):
        chapter, revision = StateManager.read_project_chapter_versioned(novel_id, chapter_num, recover=False)
        previous = load_chapter_version(project_dir, chapter_num, number) if chapter else None
        if previous is None:
            raise HTTPException(status_code=404, detail="历史稿不存在")
        return {"revision": revision, "version": previous.model_dump()}


@app.post("/api/novel/{novel_id}/chapter/{chapter_num}/versions/{number}")
def restore_chapter_version(novel_id: str, chapter_num: int, number: int, req: ProjectRevisionRequest):
    _ensure_idle()
    _require_project(novel_id)
    project_dir = get_output_dir() / novel_id
    with project_lock(project_dir, timeout=0):
        manager = StateManager(novel_id)
        _check_revision(manager, req.expected_revision)
        previous = load_chapter_version(project_dir, chapter_num, number)
        if previous is None or chapter_num not in manager.state.chapters:
            raise HTTPException(status_code=404, detail="历史稿不存在")
        manager.replace_chapter_text(chapter_num, previous.text, title=previous.title, mark_reconciliation=True)
    return {"revision": manager.revision, "chapter": chapter_num}


@app.post("/api/novel/{novel_id}/chapter/{chapter_num}/audit")
def audit_chapter(
    novel_id: str, chapter_num: int, req: ProjectRevisionRequest, background_tasks: BackgroundTasks,
):
    _require_project(novel_id)
    manager = StateManager(novel_id, load_chapter_content=False, load_memory=False, load_derived=False)
    _check_revision(manager, req.expected_revision)
    if chapter_num not in manager.state.chapters:
        raise HTTPException(status_code=404, detail="章节不存在")
    try:
        task_id = generation_status.reserve(novel_id, step=f"第 {chapter_num} 章审稿已排队")
    except TaskAlreadyRunning as exc:
        raise HTTPException(status_code=400, detail=safe_user_message(exc, fallback="创作任务运行中")) from exc
    background_tasks.add_task(
        run_chapter_audit_job, novel_id, chapter_num, task_id, expected_revision=req.expected_revision,
    )
    return {"status": "started", "task_id": task_id}


@app.post("/api/novel/{novel_id}/chapter/{chapter_num}/audit_related")
def audit_related_chapters(
    novel_id: str, chapter_num: int, req: ProjectRevisionRequest, background_tasks: BackgroundTasks,
):
    _require_project(novel_id)
    manager = StateManager(novel_id, load_chapter_content=False, load_memory=False, load_derived=False)
    _check_revision(manager, req.expected_revision)
    if chapter_num not in manager.state.chapters:
        raise HTTPException(status_code=404, detail="章节不存在")
    related = [chapter_num] + sorted(
        number for number, chapter in manager.state.chapters.items()
        if number > chapter_num and chapter.final_audit
        and any(finding.source_chapter == chapter_num for finding in chapter.final_audit.findings)
    )
    if len(related) > 20:
        raise HTTPException(status_code=422, detail="相关章节超过 20 章，请按章复查")
    try:
        task_id = generation_status.reserve(novel_id, step="相关章节复查已排队", can_stop=True)
    except TaskAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    background_tasks.add_task(run_related_audit_job, novel_id, related, task_id, expected_revision=manager.revision)
    return {"task_id": task_id, "chapters": related}


@app.post("/api/novel/{novel_id}/chapter/{chapter_num}/audit_decision")
def update_audit_decision(novel_id: str, chapter_num: int, req: FindingDecisionRequest):
    _ensure_idle()
    _require_project(novel_id)
    manager = StateManager(novel_id, load_memory=False, load_derived=False)
    _check_revision(manager, req.expected_revision)
    chapter = manager.record_finding_decision(chapter_num, req.evidence_id, req.decision, req.note.strip())
    return {"revision": manager.revision, "decisions": chapter.finding_decisions}


@app.post("/api/novel/{novel_id}/chapter/{chapter_num}/human_review")
def record_chapter_human_review(novel_id: str, chapter_num: int, req: HumanReviewRequest):
    _ensure_idle()
    _require_project(novel_id)
    if chapter_num < 1:
        raise HTTPException(status_code=400, detail="章节序号必须 ≥ 1")
    state_manager = StateManager(novel_id=novel_id, load_memory=False, load_derived=False)
    _check_revision(state_manager, req.expected_revision)
    try:
        review = state_manager.record_human_review(chapter_num, req.decision, req.note)
    except ValueError as exc:
        error_id = report_internal_error(
            logger,
            exc,
            scope="human_review",
            context={"novel_id": novel_id, "chapter": chapter_num},
        )
        raise HTTPException(
            status_code=400,
            detail=f"人工审阅未能保存（诊断编号 {error_id}）",
        ) from exc
    chapter = state_manager.state.chapters[chapter_num]
    return {
        "status": "success",
        "message": "人工审阅已记录，并与当前正文版本绑定",
        "review": review.model_dump(mode="json"),
        "human_review_status": chapter.human_review_status,
        "history_count": len(chapter.human_review_history),
        "revision": state_manager.revision,
        "novel_id": novel_id,
        "chapter_number": chapter_num,
    }


@app.get("/api/novel/{novel_id}/compliance")
def get_compliance(novel_id: str):
    _require_project(novel_id)
    state_manager = StateManager(novel_id=novel_id, load_memory=False, load_derived=False)
    chapters = []
    if not state_manager.state.chapters:
        return {"chapters": chapters, "revision": state_manager.revision}
    terms = compliance_terms()
    for num, chap in sorted(state_manager.state.chapters.items()):
        hits = scan_text(chap.full_text, terms=terms)
        if hits:
            chapters.append({"chapter_number": num, "title": chap.title, "hits": hits})
    return {"chapters": chapters, "revision": state_manager.revision}


@app.get("/api/novel/{novel_id}/export_backup")
def export_backup(novel_id: str):
    """项目完整备份：打包整个项目目录（含章节/快照/图谱/记忆/宣发全部数据）"""
    _ensure_idle()
    _require_project(novel_id)
    base_dir = (get_output_dir() / novel_id).resolve()
    def render(directory: Path) -> Path:
        zip_path = directory / f"{novel_id}_backup.zip"
        with project_lock(base_dir, timeout=0), project_lock(base_dir, scope="snapshot"):
            _require_project(novel_id)
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in sorted(base_dir.rglob("*")):
                    if p.is_symlink() or not p.resolve().is_relative_to(base_dir):
                        raise HTTPException(status_code=400, detail="项目包含链接到其他位置的文件，拒绝打包")
                    if p.is_file():
                        zf.write(p, p.relative_to(base_dir.parent))
        return zip_path

    return build_download(render, media_type="application/zip")


@app.post("/api/novels/preview_docx")
def preview_docx(document: Annotated[bytes, Body(media_type=DOCX_MEDIA_TYPE)]):
    try:
        return {"paragraphs": read_docx(document)}
    except InvalidManuscriptDocument as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/novels/import_manuscript", status_code=201)
def import_manuscript(req: ImportManuscriptRequest):
    """Create a local manuscript project from the author's confirmed chapter split."""
    _ensure_idle()
    if not _valid_project_id(req.name):
        raise HTTPException(status_code=422, detail="项目标识须为 1～64 位中文、字母、数字、下划线或连字符")
    root = get_output_dir()
    with project_lock(root / req.name, timeout=0):
        recover_project(root, req.name)
        if (root / req.name).exists():
            raise HTTPException(status_code=409, detail="同名项目已存在，请使用其他项目标识")
        manager = StateManager(req.name, base_dir=str(root))
        with manager.mutation(), manager.defer_full_exports():
            manager.state.imported_manuscript = True
            manager.set_concept(NovelConcept(
                title=req.title, genre=req.genre.strip(), logline="",
                golden_finger="", core_conflict="",
            ))
            for chapter in req.chapters:
                manager.append_manual_chapter(chapter.title, chapter.text)
    return {"novel_id": manager.novel_id, "chapters": len(req.chapters)}


@app.post("/api/novels/import", openapi_extra=BACKUP_REQUEST_BODY)
def import_novel(file: Annotated[UploadFile, Depends(backup_upload)]):
    """从备份 zip 导入项目：校验结构合法后解压到作品库"""
    _ensure_idle()
    if not file.filename or not file.filename.casefold().endswith(".zip"):
        raise HTTPException(status_code=400, detail="请上传 .zip 备份文件")
    try:
        # A sync endpoint runs in FastAPI's worker pool. Reuse the already
        # bounded upload spool instead of copying another 500 MiB temporary file.
        src_id, title = import_project_archive(file.file, get_output_dir())
    except FileExistsError as exc:
        raise HTTPException(status_code=400, detail=f"已存在同名项目: {exc.args[0]}（请先重命名或删除）") from exc
    except InvalidProjectArchive as exc:
        error_id = report_internal_error(
            logger,
            exc,
            scope="project_archive_import",
        )
        raise HTTPException(
            status_code=400,
            detail=f"备份文件无效或包含不受支持的数据（诊断编号 {error_id}）",
        ) from exc
    return {"status": "success", "message": f"已导入《{title}》（{src_id}）", "novel_id": src_id}


@app.delete("/api/novel/{novel_id}")
def delete_novel(novel_id: str, if_match: Annotated[str | None, Header()] = None):
    _ensure_idle()
    _require_project(novel_id)
    pipeline = NovelPipeline(novel_id=novel_id)
    _check_revision(pipeline.state_manager, if_match)
    pipeline.state_manager.delete_project()
    return {"status": "success", "message": f"项目「{novel_id}」已移入本机作品回收目录",
            "trash_entry": pipeline.state_manager.last_trash_path.name}


@app.get("/api/trash")
def list_project_trash():
    entries, warnings = list_trashed_projects(get_output_dir())
    return {"entries": entries, "warnings": warnings}


@app.post("/api/trash/{entry_id}/restore")
def restore_project_trash(entry_id: str):
    _ensure_idle()
    try:
        restored = restore_trashed_project(get_output_dir(), entry_id)
    except TrashEntryNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=safe_user_message(exc, fallback="回收条目不存在"),
        ) from exc
    except InvalidTrashEntryError as exc:
        raise HTTPException(
            status_code=400,
            detail=safe_user_message(exc, fallback="回收条目无效"),
        ) from exc
    return {
        "status": "success",
        "message": f"已恢复作品「{restored['novel_id']}」",
        "project": restored,
    }


@app.post("/api/novel/regenerate")
def regenerate_chapter(req: RegenerateRequest, background_tasks: BackgroundTasks):
    if req.chapter < 1:
        raise HTTPException(status_code=400, detail="章节序号必须 ≥ 1")
    _require_project(req.name)
    pipeline = NovelPipeline(novel_id=req.name, lightweight=True, load_derived=False)
    if not pipeline.state_manager.state.concept:
        raise HTTPException(status_code=404, detail="项目缺少策划案，无法重写")
    if req.chapter not in pipeline.state_manager.state.chapters:
        raise HTTPException(status_code=404, detail=f"章节不存在: 第 {req.chapter} 章")
    _check_revision(pipeline.state_manager, req.expected_revision)
    try:
        task_id = generation_status.reserve(req.name, step=f"第 {req.chapter} 章重写已排队")
    except TaskAlreadyRunning as exc:
        raise HTTPException(
            status_code=400,
            detail=safe_user_message(exc, fallback="当前有任务正在运行"),
        ) from exc
    background_tasks.add_task(
        run_regenerate_job, name=req.name, chapter=req.chapter, task_id=task_id,
        expected_revision=req.expected_revision, preview=req.preview,
    )
    return {"status": "started", "task_id": task_id, "message": f"已启动第 {req.chapter} 章重写"}


@app.get("/api/novel/{novel_id}/rewrite_candidate")
def read_rewrite_candidate(novel_id: str):
    _require_project(novel_id)
    project_dir = (get_output_dir() / novel_id).resolve()
    recover_project(project_dir.parent, novel_id)
    try:
        with project_lock(project_dir, scope="snapshot"):
            rewrite = read_chapter_rewrite(project_dir, project_dir.name)
            previous, _ = (StateManager.read_project_chapter_versioned(
                novel_id, rewrite.draft.content.chapter_number, recover=False,
            ) if rewrite else (None, ""))
    except (OSError, ValueError):
        return {"candidate": None, "warning": "已有重写稿无法读取。确认生成新的重写稿后将替换该文件。"}
    if rewrite is None:
        return {"candidate": None}
    chapter = rewrite.draft.content
    delta = rewrite.draft.state_delta
    hunks = [
        (index, before, after) for index, (kind, before, after) in
        enumerate(chapter_diff(previous.full_text, chapter.full_text)) if kind != "equal"
    ] if previous else []
    return {
        "candidate": {
            "task_id": rewrite.task_id,
            "revision": rewrite.expected_revision,
            "chapter": chapter.chapter_number,
            "title": chapter.title,
            "text": chapter.full_text,
            "audit_status": chapter.audit_status,
            "state_extraction_failed": rewrite.draft.state_extraction_failed,
            "changes": [
                {"index": index, "before": before, "after": after}
                for index, before, after in hunks[:500]
            ],
            "change_count": len(hunks),
            "state_effects": {
                "affected_characters": list(delta.character_updates) + [item.name for item in delta.newly_introduced_characters],
                "new_hooks": len(delta.new_foreshadowings),
                "resolved_hooks": len(delta.resolved_foreshadowing_ids),
                "new_items": delta.newly_introduced_items,
            },
        },
    }


@app.post("/api/novel/{novel_id}/rewrite_candidate")
def apply_rewrite_candidate(novel_id: str, req: ChapterRewriteRequest):
    _ensure_idle()
    _require_project(novel_id)
    with project_lock(get_output_dir() / novel_id, timeout=0):
        pipeline = NovelPipeline(novel_id)
        _check_revision(pipeline.state_manager, req.expected_revision)
        manager = pipeline.state_manager
        try:
            rewrite = read_chapter_rewrite(manager.base_dir, manager.novel_id)
        except (OSError, ValueError) as exc:
            raise ProjectConflictError("重写稿无法读取，请重新生成") from exc
        if rewrite is None or req.task_id != rewrite.task_id:
            raise HTTPException(status_code=404, detail="重写稿已不存在，请重新生成")
        manager.assert_revision(rewrite.expected_revision)
        number = rewrite.draft.content.chapter_number
        merged = None
        if req.selected_hunks is not None:
            try:
                merged = apply_chapter_hunks(
                    manager.state.chapters[number].full_text, rewrite.draft.content.full_text, req.selected_hunks,
                )
            except (KeyError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="选择的段落已变动，请重新打开候选稿") from exc
            if not merged.strip():
                raise HTTPException(status_code=422, detail="选中的修改会留下空白正文")
        with manager.mutation():
            if merged is None:
                pipeline.commit_chapter_draft(rewrite.draft)
            else:
                manager.replace_chapter_text(number, merged, mark_reconciliation=True)
            remove_file(manager.base_dir / REWRITE_FILENAME, missing_ok=False)
    return {"chapter": rewrite.draft.content.chapter_number, "revision": pipeline.state_manager.revision}


@app.delete("/api/novel/{novel_id}/rewrite_candidate/{task_id}")
def discard_rewrite_candidate(novel_id: str, task_id: str):
    _require_project(novel_id)
    project_dir = (get_output_dir() / novel_id).resolve()
    with project_lock(project_dir, timeout=0), project_lock(project_dir, scope="snapshot"):
        try:
            rewrite = read_chapter_rewrite(project_dir, project_dir.name)
        except (OSError, ValueError) as exc:
            raise ProjectConflictError("重写稿无法读取，请重新生成") from exc
        if rewrite is None or rewrite.task_id != task_id:
            raise HTTPException(status_code=404, detail="重写稿已不存在")
        remove_file(project_dir / REWRITE_FILENAME, missing_ok=False)
    return {"status": "discarded"}


@app.patch("/api/novel/{novel_id}/outline/{chapter_num}")
def update_outline(novel_id: str, chapter_num: int, req: OutlineUpdateRequest):
    """手动微调章节细纲：改完可对 本章 触发重写让新细纲生效"""
    _ensure_idle()
    _require_project(novel_id)
    pipeline = NovelPipeline(novel_id=novel_id)
    _check_revision(pipeline.state_manager, req.expected_revision)
    found = pipeline.state_manager.get_chapter_outline(chapter_num)
    if not found:
        raise HTTPException(status_code=404, detail=f"细纲不存在: 第 {chapter_num} 章")
    outline = pipeline.state_manager.state.global_outline.model_copy(deep=True)
    found = next(
        ((volume, chapter) for volume in outline.volumes for chapter in volume.chapters
         if chapter.chapter_number == chapter_num),
        None,
    )
    if not found:
        raise HTTPException(status_code=404, detail=f"细纲不存在: 第 {chapter_num} 章")
    _, chap = found
    if req.title is not None and req.title.strip():
        chap.title = req.title.strip()
    if req.summary is not None and req.summary.strip():
        chap.summary = req.summary.strip()
    if req.main_conflict is not None and req.main_conflict.strip():
        chap.main_conflict = req.main_conflict.strip()
    if req.expectation_hook is not None and req.expectation_hook.strip():
        chap.expectation_hook = req.expectation_hook.strip()
    if req.involved_character_names is not None:
        chap.involved_character_names = [n.strip() for n in req.involved_character_names if n.strip()]
    pipeline.state_manager.set_global_outline(outline)
    return {"status": "success", "message": f"第 {chapter_num} 章细纲已更新"}


@app.patch("/api/novel/{novel_id}/rename")
def rename_novel(novel_id: str, req: RenameRequest):
    _ensure_idle()
    _require_project(novel_id)
    new_name = req.new_name.strip()
    if not _valid_project_id(new_name):
        raise HTTPException(status_code=400, detail="新项目名非法：1~64位中文/字母/数字/下划线/连字符")
    pipeline = NovelPipeline(novel_id=novel_id)
    if StateManager.project_exists(new_name):
        raise HTTPException(status_code=409, detail=f"已存在同名项目: {new_name}")
    try:
        _check_revision(pipeline.state_manager, req.expected_revision)
        pipeline.state_manager.rename_project(new_name)
    except ValueError as exc:
        error_id = report_internal_error(
            logger,
            exc,
            scope="project_rename",
            context={"novel_id": novel_id},
        )
        raise HTTPException(
            status_code=409,
            detail=f"项目重命名未完成（诊断编号 {error_id}）",
        ) from exc
    return {"status": "success", "message": f"已重命名为 {pipeline.state_manager.novel_id}", "new_id": pipeline.state_manager.novel_id}


@app.post("/api/novel/{novel_id}/para_rewrite")
def para_rewrite(novel_id: str, req: ParaRewriteRequest):
    """段落定向重写：仅根据选中段落 + 前后文 + 修改指令重写该段，返回候选（不落库）"""
    _require_project(novel_id)
    if not req.instruction.strip():
        raise HTTPException(status_code=400, detail="请填写修改指令（如：更狠厉一点 / 增加环境描写）")
    if len(req.instruction) > 500:
        raise HTTPException(status_code=400, detail="指令过长（≤500字）")
    pipeline = NovelPipeline(novel_id=novel_id)
    chap = pipeline.state_manager.state.chapters.get(req.chapter)
    if not chap:
        raise HTTPException(status_code=404, detail=f"章节不存在: 第 {req.chapter} 章")
    paragraphs = [p for p in chap.full_text.split("\n\n") if p.strip()]
    if req.paragraph_index < 0 or req.paragraph_index >= len(paragraphs):
        raise HTTPException(status_code=400, detail=f"段落序号越界: {req.paragraph_index}")

    target = paragraphs[req.paragraph_index]
    _check_revision(pipeline.state_manager, req.expected_revision)
    prev_ctx = "\n\n".join(paragraphs[max(0, req.paragraph_index - 2):req.paragraph_index])[-600:]
    next_ctx = "\n\n".join(paragraphs[req.paragraph_index + 1:req.paragraph_index + 3])[:400]
    # Paragraph-only editing needs local text, not a pre-chapter reconstruction.
    # Keep this available for legacy projects without an initial snapshot.
    concept = pipeline.state_manager.state.concept

    prompt = f"""【任务】只重写下面被选中的一个段落，其余段落仅供参考上下文，严禁输出任何别的内容。

【作品】《{concept.title if concept else ""}》第 {req.chapter} 章 《{chap.title}》
【前文】
{prev_ctx or "（本章开头）"}

【需要重写的段落】
{target}

【后文】
{next_ctx or "（本章结尾）"}

【作者修改指令】
{req.instruction.strip()}

要求：保持人称与时态一致；与前后文自然衔接；直接输出重写后的段落正文，不要任何解释或引号。"""

    with collect_model_usage() as usage:
        try:
            rewritten = get_llm_client().generate_text(
                messages=[{"role": "user", "content": prompt}],
                model=pipeline.settings.WRITER_MODEL,
                temperature=0.7,
            )
        finally:
            generation_status.record_usage(uuid4().hex, novel_id, f"第 {req.chapter} 章段落改写", usage)
    return {
        "status": "success", "original": target, "rewritten": rewritten.strip(),
        "revision": pipeline.state_manager.revision,
    }


@app.post("/api/novel/{novel_id}/para_rewrite_apply")
def para_rewrite_apply(novel_id: str, req: ParaRewriteApplyRequest):
    """采纳重写结果：按段落序号替换（等价于整章编辑的单段路径，含空值防护）"""
    _ensure_idle()
    _require_project(novel_id)
    pipeline = NovelPipeline(novel_id=novel_id)
    chap = pipeline.state_manager.state.chapters.get(req.chapter)
    if not chap:
        raise HTTPException(status_code=404, detail=f"章节不存在: 第 {req.chapter} 章")
    if not req.new_text.strip():
        raise HTTPException(status_code=400, detail="重写内容不能为空")
    paragraphs = [p for p in chap.full_text.split("\n\n") if p.strip()]
    if req.paragraph_index < 0 or req.paragraph_index >= len(paragraphs):
        raise HTTPException(status_code=400, detail=f"段落序号越界: {req.paragraph_index}")
    _check_revision(pipeline.state_manager, req.expected_revision)
    paragraphs[req.paragraph_index] = req.new_text.strip()
    check_chapter_length(sum(map(len, paragraphs)) + 2 * (len(paragraphs) - 1))
    pipeline.state_manager.replace_chapter_text(
        req.chapter, "\n\n".join(paragraphs), mark_reconciliation=True
    )
    return {"status": "success", "message": "段落已重写并入库"}


@app.get("/api/compliance/custom_words")
def get_custom_words_api():
    return {"custom_words": get_custom_words(), "builtin_categories": list(COMPLIANCE_CATEGORIES.keys())}


@app.post("/api/compliance/custom_words")
def save_custom_words_api(req: CustomWordsRequest):
    """保存用户自定义合规词库（全量覆盖，前端负责合并内置展示）"""
    clean = req.words
    save_custom_words(clean)
    return {"status": "success", "message": f"已保存 {sum(len(v) for v in clean.values())} 条自定义词"}


@app.get("/api/model_preview")
def model_preview(
    operation: Literal["create", "continue", "audit", "facts"],
    novel_id: str = "", chapter: int = Query(1, ge=1, le=1_000_000), count: int = Query(1, ge=1, le=50),
    include_adaptations: bool = False, idea_length: int = Query(0, ge=0, le=20_000),
):
    """Describe possible outbound context and estimate tokens without a model call."""
    settings = get_settings()
    models = {
        "策划": settings.PLANNER_MODEL, "正文": settings.WRITER_MODEL,
        "审稿": settings.CRITIC_MODEL, "状态": settings.STATE_MODEL,
    }
    data_scope = "本次输入的创意、题材与偏好，以及任务内生成的策划和正文"
    source_chapters = []
    next_chapters = []
    revision = ""
    audit_preview = {}
    if operation == "create":
        low, high = idea_length + 3_000, idea_length * 4 + 20_000
    elif operation == "continue":
        _require_project(novel_id)
        manager = StateManager(novel_id, load_chapter_content=False, load_memory=False, load_derived=False)
        revision = manager.revision
        current = manager.state.current_chapter_idx
        source_chapters = sorted(n for n in manager.state.chapters if current - 2 <= n <= current)
        next_chapters = list(range(current + 1, current + count + 1))
        data_scope = "策划、细纲、人物与伏笔状态、最近章节摘要和运行时检索的前文片段"
        low, high = 0, 20_000 if manager.state.pending_initial_planning or manager.state.pending_outline_plan else 0
    else:
        _require_project(novel_id)
        manager = StateManager(novel_id, load_memory=False, load_derived=False)
        revision = manager.revision
        if operation == "facts":
            if count > MAX_SOURCE_CHAPTERS or any(number not in manager.state.chapters for number in range(chapter, chapter + count)):
                raise HTTPException(status_code=422, detail="所选范围包含不存在的章节或超过 12 章")
            models = {"状态": settings.STATE_MODEL}
            samples = manuscript_samples(manager.state.chapters, chapter, chapter + count - 1)
            source_chapters = list(samples)
            data_scope = "所选每章至多 1,500 字，长章节取章首与章末"
            size = sum(len(part) for parts in samples.values() for part in parts)
        else:
            target = manager.state.chapters.get(chapter)
            if target is None:
                raise HTTPException(status_code=404, detail="该章不存在")
            models = {"审稿": settings.CRITIC_MODEL}
            evidence = retrieve_audit_evidence(
                manager.state.chapters, chapter, target.full_text, continuity=manager.state.manuscript_continuity,
            )
            source_chapters = [chapter, *sorted({passage["chapter"] for passage in evidence.passages})]
            data_scope = "本章正文、列出的前文章节摘录，以及可用的人物、设定与大纲；本地检索范围不等于送审范围"
            size = len(target.full_text) + sum(len(passage["text"]) for passage in evidence.passages)
            audit_preview["retrieval_coverage"] = evidence.coverage.model_dump()
        low, high = size // 2 + 500, size * 2 + 8_000
    if operation in {"create", "continue"}:
        words = settings.WORDS_PER_BEAT * settings.BEATS_PER_CHAPTER
        low += count * (words + 2_000)
        high += count * ((words * 4 + 15_000) * (1 + settings.MAX_AUDIT_RETRIES))
        if include_adaptations:
            high += 10_000 + count * 7_000
    try:
        hostname = urlparse(settings.OPENAI_BASE_URL).hostname or ""
    except ValueError:
        hostname = ""
    return {
        "revision": revision, "host": "" if OFFLINE_DEMO else hostname, "offline": OFFLINE_DEMO,
        "configured": bool(settings.OPENAI_API_KEY), "models": models,
        "scope": data_scope, "source_chapters": source_chapters, "next_chapters": next_chapters,
        "estimated_tokens": [0, 0] if OFFLINE_DEMO else [low, high],
        **audit_preview,
    }


@app.get("/api/llm_usage")
def get_llm_usage():
    """本进程累计的真实 token 用量（API usage 优先，缺失时为估算值）"""
    from core.llm import LLMClient
    return {**LLMClient.usage_snapshot(), "recent_tasks": generation_status.recent_usage()}


@app.post("/api/novel/{novel_id}/adopt_branch")
def adopt_branch(novel_id: str, req: AdoptBranchRequest):
    """采纳推演分支：将该分支策略写入对应章节细纲，重写本章即按新走向执行"""
    _ensure_idle()
    _require_project(novel_id)
    pipeline = NovelPipeline(novel_id=novel_id)
    sm = pipeline.state_manager
    _check_revision(sm, req.expected_revision)
    sim = sm.branch_simulations.get(req.chapter) or sm.branch_simulations.get(str(req.chapter))
    if not sim:
        raise HTTPException(status_code=404, detail=f"第 {req.chapter} 章暂无分支推演结果")
    candidate = next((c for c in sim.candidates if c.branch_id == req.branch_id), None)
    if not candidate:
        raise HTTPException(status_code=404, detail=f"分支不存在: {req.branch_id}")

    found = sm.get_chapter_outline(req.chapter)
    if not found:
        raise HTTPException(status_code=404, detail=f"第 {req.chapter} 章细纲不存在")
    outline = sm.state.global_outline.model_copy(deep=True)
    found = next(
        ((volume, chapter) for volume in outline.volumes for chapter in volume.chapters
         if chapter.chapter_number == req.chapter),
        None,
    )
    if not found:
        raise HTTPException(status_code=404, detail=f"第 {req.chapter} 章细纲不存在")
    _, chap = found
    chap.summary = f"[已采纳分支 {req.branch_id}·{candidate.branch_title}] {candidate.narrative_strategy[:400]}"
    chap.main_conflict = candidate.immediate_conflict[:200]
    if candidate.recommendation_reason:
        chap.expectation_hook = candidate.recommendation_reason[:150]
    sm.set_global_outline(outline)
    return {"status": "success", "message": f"分支 {req.branch_id} 已写入第 {req.chapter} 章细纲，重写本章即可生效"}


@app.post("/api/novel/{novel_id}/compliance_replace")
def compliance_replace(novel_id: str, req: ComplianceReplaceRequest):
    """合规一键替换：将本章所有命中词替换为指定内容"""
    _ensure_idle()
    _require_project(novel_id)
    pipeline = NovelPipeline(novel_id=novel_id)
    sm = pipeline.state_manager
    chap = sm.state.chapters.get(req.chapter)
    if not chap:
        raise HTTPException(status_code=404, detail=f"章节不存在: 第 {req.chapter} 章")
    # 空词会被 str.replace 在每个位置命中，导致全文被污染
    if not req.word.strip() or len(req.word) > 50:
        raise HTTPException(status_code=400, detail="命中词非法（非空且不超过50字）")
    if len(req.replacement) > 200:
        raise HTTPException(status_code=400, detail="替换内容过长（≤200字）")
    _check_revision(sm, req.expected_revision)
    count = chap.full_text.count(req.word)
    if count == 0:
        return {"status": "success", "message": "无命中，未做修改", "replaced": 0}
    check_replacement_length(chap.full_text, req.word, req.replacement, count=count)
    sm.replace_chapter_text(req.chapter, chap.full_text.replace(req.word, req.replacement), mark_reconciliation=True)
    return {"status": "success", "message": f"已替换 {count} 处", "replaced": count}


@app.get("/api/novel/{novel_id}/export_epub")
def export_novel_epub(novel_id: str):
    return _publication_download(novel_id, "epub")


@app.get("/api/novel/{novel_id}/export_docx")
def export_novel_docx(novel_id: str):
    return _publication_download(novel_id, "docx")


@app.get("/api/novel/{novel_id}/export_chapter_bundle")
def export_novel_chapter_bundle(novel_id: str):
    return _publication_download(novel_id, "chapter_bundle")


@app.get("/api/novel/{novel_id}/publication_preview")
def publication_preview(novel_id: str, chapter: int = Query(ge=1, le=1_000_000)):
    _require_project(novel_id)
    content, revision = StateManager.read_project_chapter_versioned(novel_id, chapter)
    if content is None:
        raise HTTPException(status_code=404, detail="该章不存在")
    return {
        "revision": revision,
        "chapter": chapter,
        "title": content.title,
        "text": f"第{chapter}章 {content.title}\n\n{content.full_text}\n",
        "has_unreviewed_changes": content.human_review_status != "approved",
        "official_rules": [
            {"title": "番茄小说长篇发文规则", "url": "https://fanqienovel.com/writer/zone/article/7639950766869839897",
             "date": "2026-10-31"},
            {"title": "番茄小说签约评估标准", "url": "https://fanqienovel.com/writer/zone/help/article?rank1=10189&rank2=10191&rank3=0",
             "date": "2026-09-07"},
        ],
    }


def _publication_download(novel_id: str, format: str):
    """Export a consistent prose snapshot without changing the editorial revision."""
    _ensure_idle()
    _require_project(novel_id)
    base_dir = get_output_dir() / novel_id
    with project_lock(base_dir, timeout=0), project_lock(base_dir, scope="snapshot"):
        _require_project(novel_id)
        manager = StateManager(novel_id, load_memory=False, load_derived=False)
        if manager.load_warnings:
            raise ProjectReadOnlyError("项目存在无法读取的数据，已暂停正文导出以免遗漏内容；请先检查加载警告")
    if format in {"epub", "docx", "chapter_bundle"} and not manager.state.chapters:
        raise HTTPException(status_code=400, detail="暂无章节，无法导出")

    def render(directory: Path) -> Path:
        if format == "txt":
            export_platform_text(directory, manager.novel_id, manager.state, sync_chapters=False)
            return directory / f"{manager.novel_id}_全本发布稿.txt"
        if format == "chapter_bundle":
            bundle = directory / f"{manager.novel_id}_分章稿.zip"
            with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
                for number in sorted(manager.state.chapters):
                    chapter_file = export_platform_chapter(directory, manager.state.chapters[number])
                    archive.write(chapter_file, chapter_file.name)
            return bundle
        exporter = {"epub": export_epub, "docx": export_docx, "markdown": export_markdown}[format]
        return exporter(directory, manager.novel_id, manager.state)

    media_type = {
        "epub": "application/epub+zip", "txt": "text/plain", "markdown": "text/markdown",
        "docx": DOCX_MEDIA_TYPE, "chapter_bundle": "application/zip",
    }[format]
    response = build_download(render, media_type=media_type)
    response.headers["X-Project-Revision"] = manager.revision
    return response


@app.post("/api/novel/{novel_id}/generate_delivery")
def generate_delivery(
    novel_id: str, req: ProjectRevisionRequest, background_tasks: BackgroundTasks,
):
    _require_project(novel_id)
    pipeline = NovelPipeline(novel_id=novel_id, lightweight=True, load_derived=False)
    _check_revision(pipeline.state_manager, req.expected_revision)
    if not pipeline.state_manager.state.concept:
        raise HTTPException(status_code=400, detail="生成商业衍生包失败，请确保至少有策划案！")
    try:
        task_id = generation_status.reserve(novel_id, step="商业交付包生成已排队")
    except TaskAlreadyRunning as exc:
        raise HTTPException(
            status_code=400,
            detail=safe_user_message(exc, fallback="当前有任务正在运行"),
        ) from exc
    background_tasks.add_task(
        run_delivery_job,
        name=novel_id,
        task_id=task_id,
        expected_revision=req.expected_revision,
    )
    return {"status": "started", "task_id": task_id, "message": "商业宣发包生成已启动"}


@app.post("/api/novel/{novel_id}/short_drama/{chapter_num}/generate")
def generate_short_drama(
    novel_id: str,
    chapter_num: int,
    req: ProjectRevisionRequest,
    background_tasks: BackgroundTasks,
):
    _require_project(novel_id)
    if chapter_num < 1:
        raise HTTPException(status_code=400, detail="章节序号必须 ≥ 1")
    pipeline = NovelPipeline(novel_id=novel_id, lightweight=True, load_derived=False)
    _check_revision(pipeline.state_manager, req.expected_revision)
    if not pipeline.state_manager.state.concept:
        raise HTTPException(status_code=400, detail="项目缺少策划案，无法生成短剧分镜")
    if chapter_num not in pipeline.state_manager.state.chapters:
        raise HTTPException(status_code=404, detail=f"章节不存在: 第 {chapter_num} 章")
    try:
        task_id = generation_status.reserve(novel_id, step=f"第 {chapter_num} 章短剧分镜生成已排队")
    except TaskAlreadyRunning as exc:
        raise HTTPException(
            status_code=400,
            detail=safe_user_message(exc, fallback="当前有任务正在运行"),
        ) from exc
    background_tasks.add_task(
        run_short_drama_job,
        name=novel_id,
        chapter=chapter_num,
        task_id=task_id,
        expected_revision=req.expected_revision,
    )
    return {"status": "started", "task_id": task_id, "message": f"第 {chapter_num} 章短剧分镜生成已启动"}


@app.post("/api/novel/{novel_id}/audio_package/generate")
def generate_audio_package(
    novel_id: str, req: ProjectRevisionRequest, background_tasks: BackgroundTasks,
):
    _require_project(novel_id)
    pipeline = NovelPipeline(novel_id=novel_id, lightweight=True, load_derived=False)
    _check_revision(pipeline.state_manager, req.expected_revision)
    if not pipeline.state_manager.state.concept:
        raise HTTPException(status_code=400, detail="项目缺少策划案，无法生成有声演播配置")
    if not pipeline.state_manager.state.characters:
        raise HTTPException(status_code=400, detail="项目缺少人物卡，无法生成有声演播配置")
    try:
        task_id = generation_status.reserve(novel_id, step="有声演播配置生成已排队")
    except TaskAlreadyRunning as exc:
        raise HTTPException(
            status_code=400,
            detail=safe_user_message(exc, fallback="当前有任务正在运行"),
        ) from exc
    background_tasks.add_task(
        run_audio_package_job,
        name=novel_id,
        task_id=task_id,
        expected_revision=req.expected_revision,
    )
    return {"status": "started", "task_id": task_id, "message": "有声演播配置生成已启动"}


@app.post("/api/status/{task_id}/stop")
def stop_generation(task_id: str):
    if not generation_status.request_stop(task_id):
        raise HTTPException(status_code=409, detail="任务已结束或当前步骤不能停止")
    return {"status": "stopping", "message": "完成当前策划或章节后停止，已保存内容会保留。"}


@app.get("/api/status")
def get_task_status():
    return generation_status.snapshot()


@app.get("/api/novel/{novel_id}/export")
def export_novel(novel_id: str):
    return _publication_download(novel_id, "markdown")


@app.get("/api/novel/{novel_id}/export_txt")
def export_novel_txt(novel_id: str):
    return _publication_download(novel_id, "txt")


HTML_FILE = BASE_DIR / "web" / "index.html"


@app.get("/", response_class=HTMLResponse)
def index_page():
    if not HTML_FILE.is_file():
        raise HTTPException(status_code=500, detail="Web UI 资源缺失")
    return HTMLResponse(content=HTML_FILE.read_text(encoding="utf-8"))


def start_server(host: str = "127.0.0.1", port: int = 8000):
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Web 创作台包含项目写入与密钥配置接口，只允许绑定本机回环地址")
    print(f"🚀 Moirai 正在运行: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Moirai 中文长篇小说创作台")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1–65535 之间")
    start_server(port=args.port)
