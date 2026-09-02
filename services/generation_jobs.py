"""Long-running generation jobs used by the local Web worker."""

from __future__ import annotations

import logging
from collections.abc import Callable

from config.settings import get_output_dir
from core.error_reporting import UserInputError, referenced_failure, report_internal_error, safe_user_message
from core.project_ids import require_project_id
from core.project_transactions import recover_project
from core.schemas import CommercialTarget
from core.storage import project_lock
from core.task_status import generation_status
from pipeline.novel_pipeline import NovelPipeline

logger = logging.getLogger("novelengine.jobs")


def _log(message: str, agent: str = "", *, task_id: str | None = None) -> None:
    generation_status.append_log(message, agent=agent, task_id=task_id)


def _execute_job(
    name: str,
    task_id: str | None,
    action: Callable[[NovelPipeline, str], None],
    *,
    require_existing: bool = True,
) -> None:
    """Hold the OS-backed lease for construction, every chapter and delivery."""
    task_id = generation_status.ensure_reserved(name, task_id=task_id)
    try:
        project_dir = get_output_dir() / require_project_id(name)
        with project_lock(project_dir, timeout=0):
            recover_project(project_dir.parent, name)
            if require_existing and not (project_dir / "project_state.json").is_file():
                raise UserInputError("项目已不存在，任务取消；未创建空项目")
            if not require_existing and (project_dir / "project_state.json").is_file():
                raise UserInputError("已存在同名项目，拒绝把新建任务转换成续写")
            pipeline = NovelPipeline(novel_id=name)
            action(pipeline, task_id)
    except Exception as exc:
        public_message = safe_user_message(exc, fallback="任务未完成")
        if public_message is None:
            error_id = report_internal_error(
                logger,
                exc,
                scope="background_job",
                context={"novel_id": name, "task_id": task_id or ""},
            )
            public_message = referenced_failure("任务未完成；已保存的结果仍保留", error_id)
        _log(f"❌ {public_message}", "Error", task_id=task_id)
        generation_status.finish(step=public_message, error=public_message, task_id=task_id)


def run_pipeline_job(
    name: str,
    idea: str,
    genre: str,
    target: CommercialTarget,
    chapter_count: int,
    volumes: int,
    task_id: str | None = None,
    *,
    expected_revision: str | None = None,
) -> None:
    """Create or continue a project in the reserved local worker slot."""
    def generate(pipeline: NovelPipeline, task_id: str) -> None:
        if expected_revision is not None:
            pipeline.state_manager.assert_revision(expected_revision)
        start, end = NovelPipeline.plan_chapter_range(
            pipeline.state_manager.state.current_chapter_idx, chapter_count,
        )
        _log(f"✦ 启幕：构建白金小说宇宙 [{name}]", "ConceptPlanner", task_id=task_id)

        if pipeline.state_manager.state.pending_initial_planning is not None:
            stage = pipeline.state_manager.state.pending_initial_planning.next_stage
            generation_status.update(task_id=task_id, current_step=f"正在恢复未完成的主策划阶段: {stage}...")
            _log(f"沿用已保存结果，从主策划 {stage} 阶段继续。", "ConceptPlanner", task_id=task_id)
            pipeline.resume_initialization()
        elif not pipeline.state_manager.state.concept:
            generation_status.update(task_id=task_id, current_step="总策划 Agent 正在构建世界观与白金设定集...")
            _log("正在雕琢核心金手指机制与世界铁律...", "ConceptPlanner", task_id=task_id)
            pipeline.initialize_new_novel(
                user_idea=idea,
                genre_preference=genre,
                commercial_target=target,
                total_volumes=volumes,
                chapters_per_volume=max(10, chapter_count),
            )
            _log(f"✔ 架构定稿: 《{pipeline.state_manager.state.concept.title}》", "OutlineArchitect", task_id=task_id)

        if pipeline.state_manager.state.pending_outline_plan is not None:
            generation_status.update(task_id=task_id, current_step="正在恢复未完成的初始总纲...")
            _log("沿用已保存的策划、世界观与人物，仅重试未完成总纲。", "OutlineArchitect", task_id=task_id)
            pipeline.resume_initial_planning()

        _log(f"📅 本批次生产排期: 第 {start} 章 至 第 {end} 章 (共 {chapter_count} 章)", task_id=task_id)

        with pipeline.state_manager.defer_full_exports():
            for done, chapter_number in enumerate(range(start, end + 1), start=1):
                generation_status.update(
                    task_id=task_id,
                    current_step=f"正在创作第 {chapter_number} 章...",
                    progress=round((done - 1) * 100 / chapter_count),
                )
                _log(f"导演 Agent 正在拆解第 {chapter_number} 章微细纲与情绪张力...", "BeatDirector", task_id=task_id)
                try:
                    chapter = pipeline.generate_single_chapter(chapter_number)
                except Exception as exc:
                    pipeline._append_generation_log(chapter_number, False, error=exc)
                    raise
                pipeline._append_generation_log(chapter_number, True, f"{chapter.word_count} 字《{chapter.title}》")
                generation_status.update(task_id=task_id, progress=round(done * 100 / chapter_count))
                _log(
                    f"✔ 第 {chapter_number} 章 《{chapter.title}》 生产完成 ({chapter.word_count} 字)",
                    "StateUpdater",
                    task_id=task_id,
                )

        if len(pipeline.state_manager.state.chapters) >= 3 and not pipeline.state_manager.state.delivery_package:
            generation_status.update(task_id=task_id, current_step="正在生成全套商业衍生包与 AI 电影级封面...")
            _log("宣发 Agent 正在生成短视频推文黄金脚本与 AI 封面提示词...", "CommercialDelivery", task_id=task_id)
            try:
                pipeline.generate_commercial_package()
            except Exception as exc:
                message = pipeline._diagnose_failure(
                    exc,
                    scope="commercial_package",
                    message="商业交付包生成失败，已完成章节不受影响",
                )
                _log(f"⚠ {message}", "CommercialDelivery", task_id=task_id)

        _log("✦ 全流程篇章生产完毕！", task_id=task_id)
        generation_status.finish(step="全部生成完成", progress=100, task_id=task_id)

    _execute_job(name, task_id, generate, require_existing=not bool(idea.strip()))


def run_regenerate_job(
    name: str, chapter: int, task_id: str | None = None, *, expected_revision: str | None = None,
) -> None:
    def regenerate(pipeline: NovelPipeline, task_id: str) -> None:
        if expected_revision is not None:
            pipeline.state_manager.assert_revision(expected_revision)
        _log(f"↻ 启动第 {chapter} 章重写", "BeatDirector", task_id=task_id)
        generation_status.update(task_id=task_id, current_step=f"正在重写第 {chapter} 章...")
        if chapter not in pipeline.state_manager.state.chapters:
            raise UserInputError("目标章节已不存在，重写任务取消")
        result = pipeline.generate_single_chapter(chapter)
        _log(f"✔ 第 {chapter} 章 《{result.title}》 重写完成 ({result.word_count} 字)", "StateUpdater", task_id=task_id)
        generation_status.finish(step="重写完成", progress=100, task_id=task_id)

    _execute_job(name, task_id, regenerate)


def run_state_reconciliation_job(
    name: str, chapter: int, task_id: str | None = None, *, expected_revision: str | None = None,
) -> None:
    """Extract a pending chapter event from existing prose without regenerating it."""
    def reconcile(pipeline: NovelPipeline, task_id: str) -> None:
        if expected_revision is not None:
            pipeline.state_manager.assert_revision(expected_revision)
        _log(f"⌁ 开始核对第 {chapter} 章现有正文的状态变化", "StateUpdater", task_id=task_id)
        generation_status.update(
            task_id=task_id, current_step=f"正在提取第 {chapter} 章人物、关系与伏笔状态...",
        )
        pipeline.reconcile_existing_chapter_state(chapter)
        remaining = pipeline.state_manager.get_state_reconciliation_status()["chapters"]
        if remaining:
            message = f"第 {chapter} 章状态已核对；下一章为第 {remaining[0]} 章"
        else:
            message = "全部历史章节状态已按顺序核对完成"
        _log(f"✔ {message}", "StateUpdater", task_id=task_id)
        generation_status.finish(step=message, progress=100, task_id=task_id)

    _execute_job(name, task_id, reconcile)


def run_delivery_job(
    name: str, task_id: str | None = None, *, expected_revision: str | None = None,
) -> None:
    def deliver(pipeline: NovelPipeline, task_id: str) -> None:
        if expected_revision is not None:
            pipeline.state_manager.assert_revision(expected_revision)
        generation_status.update(task_id=task_id, current_step="正在生成商业宣发包...")
        _log("宣发 Agent 正在生成商业交付物...", "CommercialDelivery", task_id=task_id)
        package = pipeline.generate_commercial_package()
        if not package:
            raise UserInputError("项目缺少策划案，无法生成商业交付包")
        _log("✔ 商业宣发包生成完成", "CommercialDelivery", task_id=task_id)
        generation_status.finish(step="商业交付包生成完成", progress=100, task_id=task_id)

    _execute_job(name, task_id, deliver)


def run_short_drama_job(
    name: str,
    chapter: int,
    task_id: str | None = None,
    *,
    expected_revision: str | None = None,
) -> None:
    def generate(pipeline: NovelPipeline, task_id: str) -> None:
        if expected_revision is not None:
            pipeline.state_manager.assert_revision(expected_revision)
        generation_status.update(task_id=task_id, current_step=f"正在生成第 {chapter} 章短剧分镜...")
        _log(f"正在重建第 {chapter} 章竖屏短剧分镜...", "Multimodal", task_id=task_id)
        script = pipeline.generate_short_drama_script(chapter)
        _log(f"✔ 第 {chapter} 章《{script.title}》短剧分镜生成完成", "Multimodal", task_id=task_id)
        generation_status.finish(step="短剧分镜生成完成", progress=100, task_id=task_id)

    _execute_job(name, task_id, generate)


def run_audio_package_job(
    name: str, task_id: str | None = None, *, expected_revision: str | None = None,
) -> None:
    def generate(pipeline: NovelPipeline, task_id: str) -> None:
        if expected_revision is not None:
            pipeline.state_manager.assert_revision(expected_revision)
        generation_status.update(task_id=task_id, current_step="正在生成有声演播配置...")
        _log("正在按当前策划案与人物卡重建有声演播配置...", "Multimodal", task_id=task_id)
        pipeline.generate_audio_drama_package()
        _log("✔ 有声演播配置生成完成", "Multimodal", task_id=task_id)
        generation_status.finish(step="有声演播配置生成完成", progress=100, task_id=task_id)

    _execute_job(name, task_id, generate)


def run_reader_simulation_job(
    name: str,
    chapter: int,
    task_id: str | None = None,
    *,
    expected_revision: str | None = None,
) -> None:
    def simulate(pipeline: NovelPipeline, task_id: str) -> None:
        if expected_revision is not None:
            pipeline.state_manager.assert_revision(expected_revision)
        generation_status.update(task_id=task_id, current_step=f"正在推演第 {chapter} 章读者留存...")
        _log(f"正在模拟第 {chapter} 章读者弹幕、留存与弃读风险...", "ReaderSimulator", task_id=task_id)
        pipeline.simulate_chapter_readers(chapter)
        _log(f"✔ 第 {chapter} 章读者推演完成", "ReaderSimulator", task_id=task_id)
        generation_status.finish(step="读者推演完成", progress=100, task_id=task_id)

    _execute_job(name, task_id, simulate)


def run_branch_simulation_job(
    name: str,
    chapter: int,
    task_id: str | None = None,
    *,
    expected_revision: str | None = None,
) -> None:
    def simulate(pipeline: NovelPipeline, task_id: str) -> None:
        if expected_revision is not None:
            pipeline.state_manager.assert_revision(expected_revision)
        generation_status.update(task_id=task_id, current_step=f"正在推演第 {chapter} 章剧情分支...")
        _log(f"正在推演第 {chapter} 章三条剧情分支...", "NarrativeBrancher", task_id=task_id)
        pipeline.simulate_chapter_branching(chapter)
        _log(f"✔ 第 {chapter} 章剧情分支推演完成", "NarrativeBrancher", task_id=task_id)
        generation_status.finish(step="剧情分支推演完成", progress=100, task_id=task_id)

    _execute_job(name, task_id, simulate)
