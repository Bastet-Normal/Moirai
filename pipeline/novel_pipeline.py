import hashlib
import logging
from collections.abc import Callable
from functools import cached_property, wraps
from typing import Any, Literal, ParamSpec, TypeVar

from rich.console import Console
from rich.panel import Panel

from agents.beat_director import BeatDirectorAgent
from agents.commercial_delivery import CommercialDeliveryAgent
from agents.concept_planner import ConceptPlannerAgent, InitialNovelPackage
from agents.consistency_auditor import ConsistencyAuditorAgent
from agents.outline_architect import OutlineArchitectAgent
from agents.scene_writer import SceneWriterAgent
from agents.state_updater import StateUpdaterAgent
from agents.stylist_polisher import StylistPolisherAgent
from config.author_styles import AUTHOR_STYLE_PRESETS
from config.settings import get_settings
from core.error_reporting import (
    SafeUserMessage,
    UserInputError,
    referenced_failure,
    report_internal_error,
)
from core.multimodal_generator import AudioDramaPackage, MultimodalGenerator, ShortDramaScript
from core.narrative_brancher import BranchingSimulationResult, NarrativeBrancher
from core.planning import (
    PlanningIncompleteError,
    merge_outline_extension,
    next_outline_window,
    validate_initial_cast,
    validate_initial_outline,
    validate_planning_prompt,
)
from core.reader_simulator import ReaderSimulationReport, ReaderSimulator
from core.schemas import (
    AuditReport,
    ChapterContent,
    CommercialDeliveryPackage,
    CommercialTarget,
    InitialOutlinePlan,
    InitialPlanningCheckpoint,
    InitialPlanningRequest,
    NovelConcept,
    StateUpdateDelta,
)
from core.state_manager import StateManager
from core.storage import append_text_durable, project_lock

console = Console()
logger = logging.getLogger("novelengine.pipeline")
P = ParamSpec("P")
R = TypeVar("R")


class BatchGenerationError(SafeUserMessage, RuntimeError):
    """A failed batch retains its completed prefix, with machine-readable identity."""

    def __init__(self, failed_chapter: int, completed_count: int):
        self.failed_chapter = failed_chapter
        self.completed_count = completed_count
        super().__init__(f"批量生成在第 {failed_chapter} 章停止；已完成 {completed_count} 章。请检查该章后再续写。")


def project_operation(method: Callable[P, R]) -> Callable[P, R]:
    """Reserve a complete workflow before consuming model calls or stale state."""
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        with args[0].state_manager.operation():
            return method(*args, **kwargs)

    return wrapped


class NovelPipeline:
    """全自动网文小说多 Agent 生产流水线调度引擎（白金全能终极版）"""

    def __init__(
        self,
        novel_id: str,
        base_dir: str | None = None,
        *,
        lightweight: bool = False,
        load_derived: bool = True,
    ):
        self.settings = get_settings()
        self.state_manager = StateManager(
            novel_id=novel_id,
            base_dir=base_dir,
            load_chapter_content=not lightweight,
            load_memory=not lightweight,
            load_derived=load_derived,
        )

    def _diagnose_failure(
        self,
        error: BaseException,
        *,
        scope: str,
        message: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        details = {"novel_id": self.state_manager.novel_id, **(context or {})}
        error_id = report_internal_error(logger, error, scope=scope, context=details)
        return referenced_failure(message, error_id)

    # Status, export and editing operations should not construct ten agents and
    # an HTTP client.  Components are initialized only when their stage runs.
    @cached_property
    def concept_planner(self) -> ConceptPlannerAgent:
        return ConceptPlannerAgent()

    @cached_property
    def outline_architect(self) -> OutlineArchitectAgent:
        return OutlineArchitectAgent()

    @cached_property
    def beat_director(self) -> BeatDirectorAgent:
        return BeatDirectorAgent()

    @cached_property
    def scene_writer(self) -> SceneWriterAgent:
        return SceneWriterAgent()

    @cached_property
    def consistency_auditor(self) -> ConsistencyAuditorAgent:
        return ConsistencyAuditorAgent()

    @cached_property
    def stylist_polisher(self) -> StylistPolisherAgent:
        return StylistPolisherAgent()

    @cached_property
    def state_updater(self) -> StateUpdaterAgent:
        return StateUpdaterAgent()

    @cached_property
    def delivery_agent(self) -> CommercialDeliveryAgent:
        return CommercialDeliveryAgent()

    @cached_property
    def narrative_brancher(self) -> NarrativeBrancher:
        return NarrativeBrancher()

    @cached_property
    def multimodal_generator(self) -> MultimodalGenerator:
        return MultimodalGenerator()

    @cached_property
    def reader_simulator(self) -> ReaderSimulator:
        return ReaderSimulator()

    @project_operation
    def initialize_new_novel(
        self,
        user_idea: str,
        genre_preference: str = "",
        commercial_target: CommercialTarget | None = None,
        total_volumes: int = 3,
        chapters_per_volume: int = 10,
    ) -> NovelConcept:
        """从一句话灵感与商业化配置全自动策划、构建世界观、初始化知识图谱并生成全书情绪心电图大纲"""
        validate_planning_prompt(user_idea, genre_preference)
        outline_request = InitialOutlinePlan(total_volumes=total_volumes, chapters_per_volume=chapters_per_volume)
        state = self.state_manager.state
        if state.pending_initial_planning is not None:
            raise UserInputError("项目已有未完成的主策划检查点；请使用 resume-plan 恢复，拒绝替换原始创意")
        if (
            state.concept or state.world_setting or state.characters or state.chapters
            or state.current_chapter_idx or state.global_outline or state.pending_outline_plan
        ):
            raise UserInputError("项目已有设定或正文，拒绝覆盖；请使用新项目名或恢复未完成策划")
        target = (commercial_target or CommercialTarget()).model_copy(deep=True)
        checkpoint = InitialPlanningCheckpoint(request=InitialPlanningRequest(
            user_idea=user_idea,
            genre_preference=genre_preference,
            commercial_target=target,
            outline_plan=outline_request,
        ))
        console.print(Panel(
            f"[bold cyan]✦ 启动白金级小说架构流水线: {self.state_manager.novel_id}[/bold cyan]\n"
            f"目标平台: {target.target_platform} | 人设: {target.protagonist_persona} | 避坑项: {len(target.avoided_tropes)} 条\n"
            f"灵感: {user_idea}"
        ))
        with self.state_manager.mutation():
            state.pending_initial_planning = checkpoint
            self.state_manager.save_state()

        return self.resume_initialization()

    @project_operation
    def resume_initialization(self) -> NovelConcept:
        """Resume concept/world/cast from the last durable paid-stage checkpoint."""
        state = self.state_manager.state
        checkpoint = state.pending_initial_planning
        if checkpoint is None:
            raise UserInputError("项目没有未完成的主策划检查点；若仅缺少总纲，请恢复总纲阶段")
        request = checkpoint.request.model_copy(deep=True)

        def save_stage(stage: Literal["concept", "world", "cast"], value: Any) -> None:
            current = state.pending_initial_planning
            if current is None or current.next_stage != stage:
                raise UserInputError("主策划阶段返回次序与持久化检查点不一致，已拒绝覆盖")
            candidate = current.model_copy(deep=True)
            if stage == "concept":
                concept = NovelConcept.model_validate(value.model_dump())
                concept.commercial_target = request.commercial_target.model_copy(deep=True)
                candidate.concept = concept
            elif stage == "world":
                from core.schemas import WorldSetting
                candidate.world_setting = WorldSetting.model_validate(value.model_dump())
            else:
                characters = [item.model_copy(deep=True) for item in value]
                validate_initial_cast(characters)
                for character in characters:
                    character.current_state = character.initial_state.model_copy(deep=True)
                candidate.initial_characters = characters
            candidate = InitialPlanningCheckpoint.model_validate(candidate.model_dump())
            try:
                with self.state_manager.mutation():
                    state.pending_initial_planning = candidate
                    self.state_manager.save_state()
            except BaseException:
                state.pending_initial_planning = current
                raise

        try:
            with console.status("[bold green]主策划 Agent 正在恢复缺失的概念、世界观或人物阶段..."):
                current = state.pending_initial_planning
                initial_pkg = self.concept_planner.plan_novel(
                    user_idea=request.user_idea,
                    genre_preference=request.genre_preference,
                    commercial_target=request.commercial_target.model_copy(deep=True),
                    concept=current.concept.model_copy(deep=True) if current.concept else None,
                    world_setting=current.world_setting.model_copy(deep=True) if current.world_setting else None,
                    initial_characters=(
                        [item.model_copy(deep=True) for item in current.initial_characters]
                        if current.initial_characters else None
                    ),
                    stage_callback=save_stage,
                )
                initial_pkg = InitialNovelPackage.model_validate(initial_pkg.model_dump())
                # Test doubles and third-party compatible planners may return a
                # complete package without invoking callbacks. Publish each
                # missing result through exactly the same ordered checkpoints.
                for stage, value in (
                    ("concept", initial_pkg.concept),
                    ("world", initial_pkg.world_setting),
                    ("cast", initial_pkg.initial_characters),
                ):
                    current = state.pending_initial_planning
                    if current is not None and current.next_stage == stage:
                        save_stage(stage, value)
        except PlanningIncompleteError:
            raise
        except Exception as exc:
            current = state.pending_initial_planning
            next_stage = current.next_stage if current is not None else "promotion"
            message = self._diagnose_failure(
                exc,
                scope="initial_planning",
                message=(
                    f"主策划未完成（待恢复阶段: {next_stage}）；"
                    "已提交的前序模型结果不受影响"
                ),
                context={"stage": next_stage},
            )
            raise PlanningIncompleteError(
                message + "请重新加载后使用 resume-plan，或在网页选择继续生成。"
            ) from exc

        checkpoint = state.pending_initial_planning
        if checkpoint is None or checkpoint.next_stage != "promotion":
            raise PlanningIncompleteError("主策划没有形成完整的可晋升检查点，请重新加载后恢复。")
        characters = [item.model_copy(deep=True) for item in checkpoint.initial_characters]
        original_state = state.model_copy(deep=True)
        original_graph = self.state_manager.knowledge_graph.model_copy(deep=True)
        try:
            with self.state_manager.mutation():
                state.pending_initial_planning = None
                state.pending_outline_plan = request.outline_plan.model_copy(deep=True)
                state.concept = checkpoint.concept.model_copy(deep=True)
                state.world_setting = checkpoint.world_setting.model_copy(deep=True)
                state.characters = {character.name: character.model_copy(deep=True) for character in characters}
                self.state_manager.set_concept(state.concept)
                self.state_manager.set_world_setting(state.world_setting)
                self.state_manager.upsert_characters_bulk(characters)
        except BaseException:
            self.state_manager.state = original_state
            self.state_manager.knowledge_graph = original_graph
            raise

        console.print(f"[bold green]✔ 策划案已完成！书名: 《{state.concept.title}》 | 题材: {state.concept.genre}[/bold green]")

        return self.resume_initial_planning()

    @project_operation
    def resume_initial_planning(
        self, *, total_volumes: int | None = None, chapters_per_volume: int | None = None,
    ) -> NovelConcept:
        """Resume only the missing outline, never regenerate saved world/cast."""
        state = self.state_manager.state
        if state.pending_initial_planning is not None:
            raise UserInputError("主策划仍有未完成阶段，请先恢复概念、世界观或人物检查点")
        if not state.concept or not state.world_setting or not state.characters:
            raise UserInputError("缺少完整策划案、世界观或人物卡，不能恢复总纲；请先核对项目数据")
        if state.global_outline is not None or state.chapters or state.current_chapter_idx:
            raise UserInputError("项目已有总纲或正文，拒绝重新初始化；请使用正常续写命令")
        request = state.pending_outline_plan
        if request is None:
            if total_volumes is None or chapters_per_volume is None:
                raise UserInputError("旧版项目未保存策划参数，请显式指定卷数和第一卷初始细纲数后恢复总纲")
            request = InitialOutlinePlan(total_volumes=total_volumes, chapters_per_volume=chapters_per_volume)
            with self.state_manager.mutation():
                state.pending_outline_plan = request
                self.state_manager.save_state()
        elif total_volumes is not None or chapters_per_volume is not None:
            if total_volumes != request.total_volumes or chapters_per_volume != request.chapters_per_volume:
                raise UserInputError("恢复参数与已保存请求不同；请省略参数，沿用原始规划")

        try:
            with console.status(f"[bold green]架构师 Agent 正在恢复 {request.total_volumes} 卷初始总纲..."):
                global_outline = self.outline_architect.generate_global_outline(
                    concept=state.concept.model_copy(deep=True),
                    world_setting=state.world_setting.model_copy(deep=True),
                    characters=[character.model_copy(deep=True) for character in state.characters.values()],
                    total_volumes=request.total_volumes,
                    chapters_per_volume=request.chapters_per_volume,
                )
                validate_initial_outline(global_outline, request)
                with self.state_manager.mutation():
                    state.pending_outline_plan = None
                    self.state_manager.set_global_outline(global_outline)
        except Exception as exc:
            message = self._diagnose_failure(
                exc,
                scope="initial_outline",
                message="初始总纲未完成；已保存的策划、世界观和人物不受影响",
            )
            raise PlanningIncompleteError(
                message + "请重新加载后使用 resume-plan，或在网页选择继续生成以重试未完成阶段。"
            ) from exc

        # Step 3: 初始化有声书角色音色分配表
        try:
            with console.status("[bold cyan]正在为全书角色分配专属有声书演播音色与音效指导..."):
                self._generate_audio_drama_package()
        except Exception as exc:
            # 衍生内容不能让已经完成的策划案和总纲整体失败。
            message = self._diagnose_failure(
                exc,
                scope="audio_drama_package",
                message="有声演播配置生成失败，不影响主线初始化",
            )
            console.print(f"[yellow]⚠️ {message}[/yellow]")

        console.print(f"[bold green]✔ 全书总纲规划完成！已规划 {len(global_outline.volumes)} 卷大纲[/bold green]")
        return state.concept

    @project_operation
    def simulate_chapter_branching(self, chapter_number: int) -> BranchingSimulationResult:
        """在关键章节触发 Tree-of-Thoughts 3 条剧情分支沙盒推演"""
        state = self.state_manager.state
        context = self.state_manager.build_chapter_context(chapter_number)
        current_situation = f"本章目标冲突: {context.get('main_conflict')}。当前主角处于: {context.get('chapter_summary')}"

        with console.status(f"[bold yellow]Tree-of-Thoughts 引擎正在推演第 {chapter_number} 章的 3 种不同命运走向..."):
            branch_result = self.narrative_brancher.simulate_branches(
                chapter_number=chapter_number,
                novel_concept=state.concept,
                world_setting=state.world_setting,
                characters=list(state.characters.values()),
                current_situation=current_situation,
            )
            self.state_manager.save_branch_simulation(chapter_number, branch_result)

        console.print(f"[bold green]✔ 第 {chapter_number} 章分支推演完成！推荐采纳分支: {branch_result.selected_branch_id}[/bold green]")
        return branch_result

    @project_operation
    def extend_outline_to(self, chapter_number: int, *, block_size: int = 10) -> None:
        """Extend the rolling outline when generation reaches an unplanned chapter."""
        state = self.state_manager.state
        if not state.concept or not state.world_setting or not state.global_outline:
            raise UserInputError("项目缺少策划案、世界观或总纲，无法扩展章节细纲")
        window = next_outline_window(state.global_outline, chapter_number, block_size)
        if window is None:
            return
        planning_outline = state.global_outline.model_copy(deep=True)
        volume = planning_outline.volumes[window.volume_number - 1]
        all_outlines = [chapter for volume in planning_outline.volumes for chapter in volume.chapters]
        prior = [chapter for chapter in all_outlines if chapter.chapter_number < chapter_number]
        following = next((chapter for chapter in all_outlines if chapter.chapter_number > chapter_number), None)
        planning_state, _ = self.state_manager.state_before_chapter(
            min(chapter_number, state.current_chapter_idx + 1)
        )
        extension = self.outline_architect.extend_outline(
            concept=state.concept.model_copy(deep=True),
            world_setting=state.world_setting.model_copy(deep=True),
            characters=list(planning_state.characters.values()),
            volume=volume,
            start_chapter=chapter_number,
            count=window.count,
            recent_summaries=planning_state.recent_chapter_summaries,
            preceding_chapters=prior,
            global_outline=planning_outline,
            following_chapter=following,
        )
        candidate = merge_outline_extension(
            state.global_outline, window, extension.chapters, extension.pacing_beats,
        )
        original = state.global_outline
        try:
            with self.state_manager.mutation():
                self.state_manager.set_global_outline(candidate)
        except BaseException:
            state.global_outline = original
            raise

    @project_operation
    def simulate_chapter_readers(self, chapter_number: int) -> ReaderSimulationReport:
        """为指定章节生成读者心智弹幕与留存率预警"""
        state = self.state_manager.state
        if chapter_number not in state.chapters:
            raise UserInputError(f"章节 {chapter_number} 尚未生成")
        chap = state.chapters[chapter_number]
        
        with console.status(f"[bold cyan]虚拟读者群正在对第 {chapter_number} 章进行沉浸阅读与弹幕留存推演..."):
            report = self.reader_simulator.simulate_chapter_readers(
                novel_concept=state.concept,
                chapter=chap,
            )
            self.state_manager.save_reader_simulation(chapter_number, report)
        return report

    def _generate_short_drama_script(self, chapter_number: int) -> ShortDramaScript:
        state = self.state_manager.state
        if not state.concept:
            raise UserInputError("项目缺少策划案，无法生成短剧分镜")
        chapter = state.chapters.get(chapter_number)
        if chapter is None or not chapter.full_text.strip():
            raise UserInputError(f"第 {chapter_number} 章正文不存在，无法生成短剧分镜")
        script = self.multimodal_generator.generate_short_drama_script(
            novel_concept=state.concept,
            chapter=chapter,
        )
        self.state_manager.save_short_drama_script(chapter_number, script)
        return script

    @project_operation
    def generate_short_drama_script(self, chapter_number: int) -> ShortDramaScript:
        """Rebuild one prose-bound short-drama projection without rewriting prose."""
        return self._generate_short_drama_script(chapter_number)

    def _generate_audio_drama_package(self) -> AudioDramaPackage:
        state = self.state_manager.state
        if not state.concept:
            raise UserInputError("项目缺少策划案，无法生成有声演播配置")
        if not state.characters:
            raise UserInputError("项目缺少人物卡，无法生成有声演播配置")
        package = self.multimodal_generator.generate_audio_drama_package(
            novel_concept=state.concept,
            characters=list(state.characters.values()),
        )
        self.state_manager.save_audio_drama_package(package)
        return package

    @project_operation
    def generate_audio_drama_package(self) -> AudioDramaPackage:
        """Rebuild the concept/cast-bound audio projection independently."""
        return self._generate_audio_drama_package()

    def _audit_text(
        self, text: str, context: dict[str, Any], *,
        stage: Literal["draft", "polished"], attempt: int,
    ) -> AuditReport:
        """Attach local provenance, ignoring identity fields returned by a model."""
        service_error = False
        try:
            report = self.consistency_auditor.audit_chapter(
                chapter_number=context["chapter_number"],
                chapter_title=context["chapter_title"],
                chapter_text=text,
                world_rules=context.get("special_rules", []),
                characters_state=context.get("characters_status", []),
                main_conflict=context.get("main_conflict", ""),
                chapter_context=context,
            )
        except Exception as exc:
            service_error = True
            message = self._diagnose_failure(
                exc,
                scope=f"chapter_audit_{stage}",
                message="自动质检服务不可用，发布前需要人工复核",
                context={"chapter": context["chapter_number"], "attempt": attempt},
            )
            report = AuditReport(
                passed=False, consistency_score=0, pacing_score=0,
                logic_issues=[message], cliffhanger_detected=False,
                critique="自动质检未完成，发布前需要人工复核。",
            )
            console.print(f"[yellow]⚠️ {message}[/yellow]")
        return report.model_copy(update={
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "stage": stage, "attempt": attempt, "service_error": service_error,
        })

    @project_operation
    def generate_single_chapter(
        self,
        chapter_number: int,
        max_audit_retries: int | None = None,
    ) -> ChapterContent:
        """全自动生成单个章节（图谱子图注入、长程记忆召回、按情绪节拍扩写、质检重审、润色去AI味、状态与图谱入库、短剧分镜转化、读者心智仿真）"""
        if type(chapter_number) is not int or not 1 <= chapter_number <= 1_000_000:
            raise UserInputError("章节序号必须为 1 到 1000000 的整数")
        if chapter_number > self.state_manager.state.current_chapter_idx + 1:
            raise UserInputError("不能跳章生成，请从当前进度的下一章开始")
        max_retries = max_audit_retries if max_audit_retries is not None else self.settings.MAX_AUDIT_RETRIES
        if type(max_retries) is not int or not 0 <= max_retries <= 10:
            raise UserInputError("质检重写次数必须在 0 到 10 之间")
        console.print(f"\n[bold magenta]══════════════ 正在生产第 {chapter_number} 章 ══════════════[/bold magenta]")

        # Check the historical boundary before any paid planner/writer call.
        self.state_manager.state_before_chapter(chapter_number)
        if not self.state_manager.get_chapter_outline(chapter_number):
            console.print(f"[cyan]🧭 第 {chapter_number} 章尚无细纲，正在滚动扩展后续 10 章规划...[/cyan]")
            self.extend_outline_to(chapter_number)

        # 1. 精准上下文构建（含知识图谱与长程语义记忆召回）
        context = self.state_manager.build_chapter_context(chapter_number)
        chap_title = context.get("chapter_title", f"第{chapter_number}章")

        console.print(f"[cyan]📖 章节目标: 《{chap_title}》 | 矛盾: {context.get('main_conflict')}[/cyan]")

        # 2. 导演 Agent 拆解微细纲（LLM 返回空节拍时使用本地确定性兜底方案）
        try:
            with console.status("[bold blue]导演 Agent 正在拆解本章情绪微节拍 (Beats) 与设计章末钩子..."):
                beats_plan = self.beat_director.plan_chapter_beats(context)
        except Exception as exc:
            message = self._diagnose_failure(
                exc,
                scope="beat_director",
                message="导演 Agent 调用失败，已启用本地兜底节拍方案",
                context={"chapter": chapter_number},
            )
            console.print(f"[yellow]⚠️ {message}[/yellow]")
            beats_plan = self.beat_director.build_fallback_plan(context)
        if not beats_plan.beats:
            console.print("[yellow]⚠️ 导演 Agent 未产出有效节拍，启用本地兜底节拍方案...[/yellow]")
            beats_plan = self.beat_director.build_fallback_plan(context)

        # 3. 主笔 Agent 逐个节拍扩写
        characters_desc = "\n".join([
            f"- {c['name']} ({c['role']}) | 境界: {c.get('level')} | 装备: {','.join(c.get('items', []))}"
            for c in context.get("characters_status", [])
        ])
        target_data = context.get("commercial_target") or {}
        style_id = target_data.get("author_style", "FANQIE_DOPAMINE")
        style_info = AUTHOR_STYLE_PRESETS.get(style_id, AUTHOR_STYLE_PRESETS["FANQIE_DOPAMINE"])
        author_style_instruction = style_info["system_instruction"]

        audit_history: list[AuditReport] = []
        revision_directives = []
        best_text, best_beats = "", []
        best_rank = (-1, -1, -1.0)

        for attempt in range(max_retries + 1):
            raw_beats_text = []
            previous_tail = ""
            console.print(f"[bold yellow]✍️ 主笔正在扩写正文 (轮次 {attempt + 1}/{max_retries + 1})...[/bold yellow]")
            for beat in beats_plan.beats:
                beat_text = self.scene_writer.write_beat(
                    beat=beat,
                    chapter_title=chap_title,
                    chapter_number=chapter_number,
                    novel_title=context.get("novel_title", ""),
                    golden_finger=context.get("golden_finger", ""),
                    characters_desc=characters_desc,
                    previous_text_tail=previous_tail,
                    revision_directives=revision_directives if attempt > 0 else None,
                    author_style_instruction=author_style_instruction,
                    chapter_context=context,
                )
                if not isinstance(beat_text, str) or not beat_text.strip():
                    raise UserInputError(f"第 {beat.beat_index} 节拍正文为空，已停止保存本章")
                raw_beats_text.append(beat_text)
                previous_tail = beat_text[-1200:]

            assembled_raw_text = "\n\n".join(raw_beats_text)
            if not assembled_raw_text.strip():
                raise UserInputError("本章没有有效节拍正文，已停止保存")
            with console.status("[bold red]质检 Agent 正在审查正文..."):
                audit_report = self._audit_text(assembled_raw_text, context, stage="draft", attempt=attempt + 1)
            audit_history.append(audit_report)
            # A genuine pass outranks any failed draft. An outage must not
            # replace a reviewed candidate just because it was generated later.
            rank = (
                int(not audit_report.service_error), int(audit_report.passed),
                audit_report.consistency_score + audit_report.pacing_score,
            )
            if rank > best_rank:
                best_rank, best_text, best_beats = rank, assembled_raw_text, list(raw_beats_text)
            if audit_report.passed or audit_report.service_error or attempt == max_retries:
                if audit_report.passed:
                    console.print("[bold green]✔ 正文质检通过。[/bold green]")
                else:
                    console.print("[yellow]⚠️ 质检尚未通过，保留最佳可用版本，发布前需复核。[/yellow]")
                break
            revision_directives = audit_report.revision_directives
            console.print("[red]✘ 质检未通过，正在根据指导意见重写...[/red]")

        assembled_raw_text, raw_beats_text = best_text, best_beats
        # Polishing is another draft, not a privileged way to bypass auditing.
        polished_text = assembled_raw_text
        try:
            with console.status("[bold magenta]润色 Agent 正在优化语言与节奏..."):
                candidate = self.stylist_polisher.polish_chapter(
                    raw_chapter_text=assembled_raw_text,
                    chapter_title=chap_title,
                    chapter_number=chapter_number,
                    novel_title=context.get("novel_title", ""),
                    author_style_instruction=author_style_instruction,
                )
            if not isinstance(candidate, str) or not candidate.strip():
                raise UserInputError("润色正文为空")
            if candidate != assembled_raw_text:
                with console.status("[bold red]正在复检润色后的最终正文..."):
                    polished_audit = self._audit_text(candidate, context, stage="polished", attempt=1)
                audit_history.append(polished_audit)
                if polished_audit.passed and not polished_audit.service_error:
                    polished_text = candidate
                else:
                    console.print("[yellow]⚠️ 润色稿未通过复检，保留原稿及其对应质检记录。[/yellow]")
        except Exception as exc:
            message = self._diagnose_failure(
                exc,
                scope="stylist_polish",
                message="润色失败，已保留质检后的主笔正文",
                context={"chapter": chapter_number},
            )
            console.print(f"[yellow]⚠️ {message}[/yellow]")

        chinese_char_count = sum('\u4e00' <= c <= '\u9fff' for c in polished_text)

        # 6. 状态管家 Agent 提取增量状态
        state_extraction_failed = False
        try:
            with console.status("[bold green]设定管家 Agent 正在分析本章事实变动并同步知识图谱..."):
                delta = self.state_updater.extract_state_delta(
                    chapter_number=chapter_number,
                    chapter_title=chap_title,
                    chapter_text=polished_text,
                    current_characters={c["name"]: c for c in context.get("characters_status", [])},
                    active_foreshadowings=context.get("active_foreshadowings", []),
                )
        except Exception as exc:
            state_extraction_failed = True
            delta = StateUpdateDelta(
                summary_of_events=f"《{chap_title}》已生成；状态抽取服务不可用，待人工补录。"
            )
            message = self._diagnose_failure(
                exc,
                scope="state_extraction",
                message="状态抽取失败，正文正常入库并已标记待补录",
                context={"chapter": chapter_number},
            )
            console.print(f"[yellow]⚠️ {message}[/yellow]")

        # 7. 保存章节与长程语义记忆
        chapter_content = ChapterContent(
            chapter_number=chapter_number,
            volume_number=context.get("volume_number") or 1,
            title=chap_title,
            word_count=chinese_char_count,
            beats_raw_text=raw_beats_text,
            full_text=polished_text,
            cliffhanger_ending=beats_plan.cliffhanger_design,
            audit_history=audit_history,
        )

        # Body and incomplete-state marker must commit together; a crash between
        # separate saves must not make an unextracted chapter look reconciled.
        with self.state_manager.mutation():
            self.state_manager.save_chapter(chapter_content, delta)
            if state_extraction_failed:
                self.state_manager.mark_state_reconciliation_required(
                    chapter_number,
                    "state extraction failed and requires regeneration or manual reconciliation",
                )

        # 8. 自动转换为竖屏短剧分镜头剧本（衍生功能，失败不阻断主线但必须留痕）
        try:
            self._generate_short_drama_script(chapter_number)
        except Exception as exc:
            message = self._diagnose_failure(
                exc,
                scope="short_drama",
                message="短剧分镜转化失败，不影响正文入库",
                context={"chapter": chapter_number},
            )
            console.print(f"[yellow]⚠️ {message}[/yellow]")

        console.print(f"[bold green]🎉 第 {chapter_number} 章 《{chap_title}》 生产完成！字数: {chinese_char_count} 字[/bold green]")
        return chapter_content

    @project_operation
    def reconcile_existing_chapter_state(self, chapter_number: int) -> StateUpdateDelta:
        """Extract one pending event from accepted prose without rewriting that prose."""
        status = self.state_manager.get_state_reconciliation_status()
        pending = status["chapters"]
        if not pending:
            raise UserInputError("当前没有待核对的章节状态")
        if chapter_number != pending[0]:
            raise UserInputError(f"历史状态必须按顺序核对；下一章是第 {pending[0]} 章")
        timeline = self.state_manager.get_timeline_status()
        if timeline.get("baseline_chapter") != 0 or not timeline.get("integrity_valid"):
            raise UserInputError("缺少可信的第 0 章基线，不能提取历史章节状态")
        chapter = self.state_manager.state.chapters.get(chapter_number)
        if chapter is None or not chapter.full_text.strip():
            raise UserInputError(f"第 {chapter_number} 章正文不存在或为空")

        # This check happens before constructing/calling the state agent, so an
        # unresolved earlier chapter can never incur a model call.
        historical_state, _ = self.state_manager.state_before_chapter(chapter_number)
        context = self.state_manager.build_chapter_context(chapter_number)
        known_characters = {
            card.name: {
                "name": card.name,
                "role": card.role,
                "level": card.current_state.level,
                "location": card.current_state.location,
                "items": card.current_state.equipment_and_items,
                "skills": card.current_state.skills_and_abilities,
                "hp_status": card.current_state.hp_status,
                "is_alive": card.current_state.is_alive,
            }
            for card in historical_state.characters.values()
        }
        delta = self.state_updater.extract_state_delta(
            chapter_number=chapter_number,
            chapter_title=chapter.title,
            chapter_text=chapter.full_text,
            current_characters=known_characters,
            active_foreshadowings=context.get("active_foreshadowings", []),
        )
        # Preserve the accepted prose exactly and retain its audit/review history;
        # only the derived state event, graph, snapshots and exports are refreshed.
        self.state_manager.save_chapter(chapter.model_copy(deep=True), delta)
        return delta

    @staticmethod
    def plan_chapter_range(current_chapter_idx: int, count: int) -> tuple:
        """根据当前进度与本次要新生成的章数，计算本批次的目标章节区间 [start, end]

        语义约定：count 是"本次新写几章"，而非"写到第几章"。
        """
        if type(current_chapter_idx) is not int or not 0 <= current_chapter_idx < 1_000_000:
            raise UserInputError("当前章节进度必须为 0 到 999999 的整数")
        if type(count) is not int or not 1 <= count <= 50:
            raise UserInputError("批量生成章节数必须为 1 到 50 的整数")
        start = current_chapter_idx + 1
        end = start + count - 1
        if end > 1_000_000:
            raise UserInputError("生成范围超过最大章节序号 1000000")
        return start, end

    def _append_generation_log(
        self,
        chap_num: int,
        ok: bool,
        detail: str = "",
        *,
        error: BaseException | None = None,
    ):
        """批量生成留痕：每章成败追加到项目目录的生成报告，失败可追溯"""
        import datetime
        import json as _json
        report_file = self.state_manager.base_dir / "generation_report.jsonl"
        if not ok:
            if error is None:
                detail = "章节生成未完成；没有可用的异常对象。"
            else:
                detail = self._diagnose_failure(
                    error,
                    scope="chapter_generation",
                    message="章节生成未完成，可按诊断编号排查",
                    context={"chapter": chap_num},
                )
        entry = {
            "time": datetime.datetime.now().isoformat(timespec="seconds"),
            "chapter": chap_num,
            "ok": ok,
            "detail": detail[:500],
        }
        try:
            with project_lock(self.state_manager.base_dir):
                append_text_durable(report_file, _json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            message = self._diagnose_failure(
                exc,
                scope="generation_report_write",
                message="生成报告写入失败",
                context={"chapter": chap_num},
            )
            console.print(f"[yellow]⚠️ {message}[/yellow]")

    @project_operation
    def generate_batch(self, start_chapter: int | None = None, count: int = 3) -> list[ChapterContent]:
        """批量连续生成；首个失败即停止，避免产生断章时间线。"""
        if start_chapter is not None and (type(start_chapter) is not int or not 1 <= start_chapter <= 1_000_000):
            raise UserInputError("起始章节必须为 1 到 1000000 的整数")
        current = start_chapter - 1 if start_chapter is not None else self.state_manager.state.current_chapter_idx
        start, end = self.plan_chapter_range(current, count)
        generated_chapters = []
        batch_failure: tuple[int, Exception] | None = None

        console.print(Panel(f"[bold green]🚀 开启批量全自动生产模式：第 {start} 章 至 第 {end} 章 (共 {count} 章)[/bold green]"))

        with self.state_manager.defer_full_exports():
            for chap_num in range(start, end + 1):
                try:
                    chap = self.generate_single_chapter(chap_num)
                    generated_chapters.append(chap)
                    self._append_generation_log(chap_num, True, f"{chap.word_count} 字《{chap.title}》")
                except Exception as exc:
                    error_id = report_internal_error(
                        logger,
                        exc,
                        scope="batch_chapter_generation",
                        context={"novel_id": self.state_manager.novel_id, "chapter": chap_num},
                    )
                    console.print(
                        f"❌ 第 {chap_num} 章生成失败（诊断编号 {error_id}），后续章节未执行。",
                        style="bold red",
                        markup=False,
                    )
                    self._append_generation_log(chap_num, False, error=exc)
                    batch_failure = (chap_num, exc)
                    break

        # 当生成满 3 章后，自动调用 CommercialDeliveryAgent 生成全套商业衍生包
        if (
            batch_failure is None
            and len(self.state_manager.state.chapters) >= 3
            and not self.state_manager.state.delivery_package
        ):
            try:
                self.generate_commercial_package()
            except Exception as exc:
                message = self._diagnose_failure(
                    exc,
                    scope="commercial_package",
                    message="商业交付包生成失败，不影响已完成章节",
                )
                console.print(f"[yellow]⚠️ {message}[/yellow]")

        if batch_failure is not None:
            failed_chapter, error = batch_failure
            raise BatchGenerationError(failed_chapter, len(generated_chapters)) from error
        console.print(f"[bold green]✨ 批量生产完毕！全本与分卷发布包已更新至: {self.state_manager.delivery_dir}[/bold green]")
        return generated_chapters

    @project_operation
    def generate_commercial_package(self) -> CommercialDeliveryPackage | None:
        """生成全套商业衍生交付包（短视频推文脚本、AI封面Prompt、推荐文案）"""
        state = self.state_manager.state
        if not state.concept or not state.world_setting:
            return None

        protagonist = None
        for c in state.characters.values():
            if "主角" in c.role or c.role.lower() == "protagonist":
                protagonist = c
                break
        if not protagonist:
            protagonist = list(state.characters.values())[0] if state.characters else None

        first_three = [state.chapters[k] for k in sorted(state.chapters.keys())[:3]]

        with console.status("[bold cyan]商业宣发 Agent 正在生成短视频推文脚本与 AI 电影级封面 Prompt..."):
            pkg = self.delivery_agent.generate_delivery_package(
                concept=state.concept,
                world_setting=state.world_setting,
                protagonist=protagonist,
                first_three_chapters=first_three,
            )
            total_words = sum(c.word_count for c in state.chapters.values())
            pkg.cost_and_token_estimate = {
                "total_words_generated": total_words,
                "estimated_input_tokens": total_words * 4,
                "estimated_output_tokens": total_words * 2,
                "cost_usd_estimate": f"${round(total_words * 0.000008, 4)}",
            }
            self.state_manager.set_delivery_package(pkg)

        console.print("[bold green]✔ 商业衍生交付包已成功生成！包含抖音推文脚本与 AI 封面 Prompt。[/bold green]")
        return pkg

    def get_status_summary(self) -> dict[str, Any]:
        """获取当前小说生成进度与状态概览"""
        state = self.state_manager.state
        total_words = sum(c.word_count for c in state.chapters.values())
        pending_concept = (
            state.pending_initial_planning.concept
            if state.pending_initial_planning else None
        )
        return {
            "novel_id": state.novel_id,
            "title": (
                state.concept.title if state.concept
                else pending_concept.title if pending_concept
                else f"未策划 · {state.novel_id}"
            ),
            "genre": state.concept.genre if state.concept else (
                pending_concept.genre if pending_concept else ""
            ),
            "current_chapter": state.current_chapter_idx,
            "total_generated_chapters": len(state.chapters),
            "total_words": total_words,
            "character_count": len(state.characters),
            "foreshadowing_count": len(state.foreshadowings),
            "has_delivery_package": state.delivery_package is not None,
            "planning_pending": (
                state.pending_initial_planning is not None
                or (state.pending_outline_plan is not None and state.global_outline is None)
            ),
            "planning_stage": (
                state.pending_initial_planning.next_stage if state.pending_initial_planning
                else ("outline" if state.pending_outline_plan is not None and state.global_outline is None else None)
            ),
            "knowledge_graph_node_count": len(self.state_manager.knowledge_graph.nodes),
            "knowledge_graph_edge_count": len(self.state_manager.knowledge_graph.edges),
            "semantic_memory_chunks_count": self.state_manager.semantic_memory_chunk_count,
        }
