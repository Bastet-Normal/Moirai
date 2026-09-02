import logging
from functools import wraps
from inspect import signature

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from config.settings import get_output_dir, get_settings
from core.error_reporting import (
    SafeUserMessage,
    UserInputError,
    referenced_failure,
    report_internal_error,
    safe_user_message,
)
from core.planning import validate_planning_prompt
from core.project_transactions import recover_all_projects
from core.state_manager import StateManager
from core.storage import project_lock
from pipeline.novel_pipeline import NovelPipeline

app = typer.Typer(
    help="🚀 Moirai - 全自动网文小说生成 Agent 系统 (OpenAI/Anthropic/DeepSeek 兼容协议)",
    pretty_exceptions_show_locals=False,
)
console = Console()
logger = logging.getLogger("novelengine.cli")


def require_api_key() -> None:
    if not get_settings().OPENAI_API_KEY:
        console.print("[bold red]❌ 未检测到 API Key！请先在 .env 或 Web 创作台中配置 OPENAI_API_KEY。[/bold red]")
        raise typer.Exit(1)


def validate_project_name(name: str) -> None:
    if not StateManager.is_valid_project_id(name):
        console.print("[bold red]❌ 项目名非法：请使用 1~64 位中文、字母、数字、下划线或连字符，并避开系统保留名。[/bold red]")
        raise typer.Exit(1)


def require_existing_project(name: str) -> None:
    validate_project_name(name)
    if not StateManager.project_exists(name):
        console.print(f"[bold red]❌ 未找到小说项目「{name}」，请检查名称。[/bold red]")
        raise typer.Exit(1)


def cli_errors(command):
    """Give scripts reliable exit codes without dumping model/credential locals."""
    @wraps(command)
    def wrapped(*args, **kwargs):
        try:
            return command(*args, **kwargs)
        except typer.Exit:
            raise
        except SafeUserMessage as exc:
            message = safe_user_message(exc, fallback="操作未完成") or "操作未完成"
            console.print(f"❌ {message}", style="bold red", markup=False)
            raise typer.Exit(2) from exc
        except Exception as exc:
            error_id = report_internal_error(
                logger,
                exc,
                scope=f"cli.{command.__name__}",
            )
            console.print(
                "❌ " + referenced_failure(
                    "命令未完成；已保存的结果仍保留，请检查服务连接和项目状态后重试",
                    error_id,
                ),
                style="bold red", markup=False,
            )
            raise typer.Exit(2) from exc

    return wrapped


def project_command(command):
    """Reserve the project before loading any potentially mutating CLI workflow."""
    @cli_errors
    @wraps(command)
    def wrapped(*args, **kwargs):
        name = signature(command).bind(*args, **kwargs).arguments["name"]
        validate_project_name(name)
        with project_lock(get_output_dir() / name, timeout=0):
            return command(*args, **kwargs)

    return wrapped


def require_new_project(name: str) -> None:
    if StateManager.project_exists(name):
        console.print("❌ 已存在同名作品；新创意不会覆盖或自动续写。请使用 continue-gen 续写，或 resume-plan 恢复未完成策划。", style="bold red", markup=False)
        raise typer.Exit(1)


@app.command()
@cli_errors
def config():
    """查看当前 API 配置与模型路由"""
    settings = get_settings()
    from core.llm import LLMClient
    masked = (settings.OPENAI_API_KEY[:6] + "..." + settings.OPENAI_API_KEY[-4:]) if len(settings.OPENAI_API_KEY) > 10 else ("***" if settings.OPENAI_API_KEY else "未配置")
    console.print(Panel(
        "[bold cyan]当前配置[/bold cyan]\n"
        f"API 端点: {settings.OPENAI_BASE_URL}\n"
        f"API Key: {masked}\n"
        f"主笔模型: {settings.WRITER_MODEL} | 策划模型: {settings.PLANNER_MODEL}\n"
        f"质检模型: {settings.CRITIC_MODEL} | 状态模型: {settings.STATE_MODEL}\n"
        f"质检严格度: {settings.AUDIT_STRICTNESS} | 重试次数: {settings.MAX_RETRIES}"
    ))
    usage = LLMClient.usage_snapshot()
    console.print(f"本进程累计用量: 输入 {usage['prompt_tokens']} / 输出 {usage['completion_tokens']} tokens（{usage['calls']} 次调用）")


@app.command()
@project_command
def auto(
    name: str = typer.Option(..., "--name", "-n", help="小说项目ID/唯一名称 (如: xianxia_01)"),
    idea: str = typer.Option(..., "--idea", "-i", help="一句话创意/灵感或详细策划要求"),
    genre: str = typer.Option("", "--genre", "-g", help="题材分类 (如: 诡异修仙, 都市高武, 系统爆兵, 无限流)"),
    chapters: int = typer.Option(3, "--chapters", "-c", min=1, max=50, help="本次连续生成章节数"),
    volumes: int = typer.Option(3, "--volumes", "-v", min=1, max=20, help="全书规划分卷总数"),
):
    """一键全自动生产：从一句话灵感 -> 策划/世界观/大纲 -> 连续生成指定章节 -> 自动质检润色与状态入库"""
    validate_planning_prompt(idea, genre)
    require_new_project(name)
    require_api_key()

    console.print(Panel(f"[bold cyan]🎯 启动一键全自动网文流水线[/bold cyan]\n项目名: {name}\n创意: {idea}\n目标生成章节: {chapters} 章"))

    pipeline = NovelPipeline(novel_id=name)

    pipeline.initialize_new_novel(
        user_idea=idea,
        genre_preference=genre,
        total_volumes=volumes,
        chapters_per_volume=max(10, chapters),
    )

    # 2. 批量连续生成章节
    # Existing projects must use the explicit continuation command.
    pipeline.generate_batch(count=chapters)

    # 3. 输出汇总统计
    summary = pipeline.get_status_summary()
    console.print(Panel(
        f"[bold green]✨ 全自动流程执行完毕！[/bold green]\n"
        f"书名: 《{summary['title']}》\n"
        f"题材: {summary['genre']}\n"
        f"已生成章节: {summary['total_generated_chapters']} 章\n"
        f"总中文字数: {summary['total_words']} 字\n"
        f"人物数量: {summary['character_count']} 位\n"
        f"剧情伏笔: {summary['foreshadowing_count']} 条\n"
        f"文件输出目录: {pipeline.state_manager.base_dir.resolve()}"
    ))


@app.command()
@project_command
def plan(
    name: str = typer.Option(..., "--name", "-n", help="小说项目名称"),
    idea: str = typer.Option(..., "--idea", "-i", help="一句话创意/灵感"),
    genre: str = typer.Option("", "--genre", "-g", help="题材分类"),
    volumes: int = typer.Option(3, "--volumes", "-v", min=1, max=20, help="规划卷数"),
    chapters_per_vol: int = typer.Option(
        10, "--chap-per-vol", min=1, max=100, help="第一卷初始连续细纲的最少章节数",
    ),
):
    """仅执行小说架构策划（生成核心卖点、金手指机制、世界观、战力境界、初始人设卡与分卷总纲）"""
    validate_planning_prompt(idea, genre)
    require_new_project(name)
    require_api_key()
    pipeline = NovelPipeline(novel_id=name)
    pipeline.initialize_new_novel(
        user_idea=idea,
        genre_preference=genre,
        total_volumes=volumes,
        chapters_per_volume=chapters_per_vol,
    )


@app.command(name="resume-plan")
@project_command
def resume_plan(
    name: str = typer.Option(..., "--name", "-n", help="需要恢复策划的项目名称"),
    volumes: int | None = typer.Option(None, "--volumes", min=1, max=20, help="仅旧版缺少记录时指定原规划卷数"),
    chapters_per_vol: int | None = typer.Option(
        None, "--chap-per-vol", min=1, max=100, help="仅旧版缺少记录时指定原第一卷初始细纲数",
    ),
):
    """从检查点恢复未完成的概念、世界观、人物或初始总纲，不生成正文"""
    require_existing_project(name)
    require_api_key()
    pipeline = NovelPipeline(novel_id=name)
    if pipeline.state_manager.state.pending_initial_planning is not None:
        if volumes is not None or chapters_per_vol is not None:
            raise UserInputError("分阶段主策划已保存原始参数；请省略恢复参数，拒绝替换原请求")
        pipeline.resume_initialization()
    else:
        pipeline.resume_initial_planning(total_volumes=volumes, chapters_per_volume=chapters_per_vol)


@app.command()
@project_command
def chapter(
    name: str = typer.Option(..., "--name", "-n", help="小说项目名称"),
    num: int = typer.Option(..., "--num", min=1, max=1_000_000, help="要生成的章节序号"),
    rewrite: bool = typer.Option(False, "--rewrite", help="明确允许重新生成已存在的指定章节"),
):
    """生成或重写指定序号的单个章节"""
    require_existing_project(name)
    pipeline = NovelPipeline(novel_id=name)
    if not pipeline.state_manager.state.concept:
        console.print("[bold red]❌ 尚未进行小说策划，请先使用 plan 或 auto 命令！[/bold red]")
        raise typer.Exit(1)

    exists = num in pipeline.state_manager.state.chapters
    if exists and not rewrite:
        console.print("❌ 该章已有正文；如需重写，请显式添加 --rewrite。未调用模型，也未修改正文。", style="bold red", markup=False)
        raise typer.Exit(1)
    if rewrite and not exists:
        raise UserInputError("指定章节不存在，不能重写；新写章节请省略 --rewrite")
    if not exists and num != pipeline.state_manager.state.current_chapter_idx + 1:
        raise UserInputError("不能跳章生成，请从当前进度的下一章开始")
    require_api_key()
    pipeline.generate_single_chapter(chapter_number=num)


@app.command()
@project_command
def continue_gen(
    name: str = typer.Option(..., "--name", "-n", help="小说项目名称"),
    count: int = typer.Option(3, "--count", "-c", min=1, max=50, help="从当前进度继续向下生成的章节数"),
):
    """断点续写：从当前已有进度继续批量生成后续章节"""
    require_existing_project(name)
    require_api_key()
    pipeline = NovelPipeline(novel_id=name)
    if not pipeline.state_manager.state.concept and pipeline.state_manager.state.pending_initial_planning is None:
        console.print("[bold red]❌ 未找到已有小说项目，请检查名称是否正确！[/bold red]")
        raise typer.Exit(1)

    if pipeline.state_manager.state.pending_initial_planning is not None:
        pipeline.resume_initialization()
    elif pipeline.state_manager.state.pending_outline_plan is not None:
        pipeline.resume_initial_planning()
    pipeline.generate_batch(count=count)


@app.command()
@cli_errors
def status(
    name: str = typer.Option(..., "--name", "-n", help="小说项目名称"),
):
    """查看小说的实时状态看板（书名、当前章节、人物境界与背包、活跃伏笔账本）"""
    require_existing_project(name)
    pipeline = NovelPipeline(novel_id=name, lightweight=True, load_derived=False)
    state = pipeline.state_manager.state

    if not state.concept and state.pending_initial_planning is None:
        console.print("[bold red]❌ 未找到小说项目状态，请确认名称！[/bold red]")
        raise typer.Exit(1)

    checkpoint_concept = state.pending_initial_planning.concept if state.pending_initial_planning else None
    display_concept = state.concept or checkpoint_concept
    console.print(Panel(
        f"[bold cyan]📚 小说项目看板: 《{display_concept.title if display_concept else '主策划未完成'}》[/bold cyan]\n"
        f"题材: {display_concept.genre if display_concept else ''} | 当前已生成: 第 {state.current_chapter_idx} 章 | 累计字数: {sum(c.word_count for c in state.chapters.values())} 字\n"
        f"核心卖点: {display_concept.logline if display_concept else '尚待概念阶段生成'}\n"
        f"金手指: {display_concept.golden_finger if display_concept else '尚待概念阶段生成'}"
    ))
    if state.pending_initial_planning is not None:
        console.print(
            f"⚠ 主策划尚未完成，下一恢复阶段: {state.pending_initial_planning.next_stage}。"
            "可用 resume-plan 或 continue-gen 从检查点恢复。",
            style="yellow", markup=False,
        )
    elif state.pending_outline_plan is not None:
        console.print("⚠ 初始总纲尚未完成。可用 resume-plan 恢复策划，或 continue-gen 补全总纲后续写。", style="yellow", markup=False)
    for warning in pipeline.state_manager.load_warnings:
        console.print(warning, style="yellow", markup=False)

    # 人物状态表
    if state.characters:
        char_table = Table(title="🎭 核心人物动态状态表", title_style="bold blue")
        char_table.add_column("姓名", style="cyan")
        char_table.add_column("角色定位", style="magenta")
        char_table.add_column("当前境界", style="green")
        char_table.add_column("所在地点", style="yellow")
        char_table.add_column("装备与底牌", style="white")
        char_table.add_column("健康状态", style="red")

        for c in state.characters.values():
            char_table.add_row(
                c.name,
                c.role,
                c.current_state.level,
                c.current_state.location,
                ", ".join(c.current_state.equipment_and_items) or "无",
                c.current_state.hp_status,
            )
        console.print(char_table)

    # 伏笔表
    if state.foreshadowings:
        hook_table = Table(title="🔮 剧情伏笔与悬念账本", title_style="bold magenta")
        hook_table.add_column("ID", style="dim")
        hook_table.add_column("伏笔名称", style="cyan")
        hook_table.add_column("埋下章节", style="green")
        hook_table.add_column("状态", style="yellow")
        hook_table.add_column("详情说明", style="white")

        for h in state.foreshadowings.values():
            status_style = "[green]已揭晓[/green]" if h.status == "RESOLVED" else "[yellow]发酵中/未解[/yellow]"
            hook_table.add_row(
                h.hook_id,
                h.title,
                f"第{h.planted_chapter}章",
                status_style,
                h.description,
            )
        console.print(hook_table)


@app.command()
@project_command
def export(
    name: str = typer.Option(..., "--name", "-n", help="小说项目名称"),
):
    """导出排版整合版全书 Markdown"""
    require_existing_project(name)
    pipeline = NovelPipeline(novel_id=name)
    export_path = pipeline.state_manager.export_markdown()
    console.print(f"[bold green]✔ 导出成功！文件位置: {export_path.resolve()}[/bold green]")


@app.command(name="list")
@cli_errors
def list_novels():
    """列出当前本地所有的网文小说项目"""
    from config.settings import get_output_dir
    base_dir = get_output_dir()
    if not base_dir.exists():
        console.print("[yellow]当前尚无小说项目生成。[/yellow]")
        return
    for warning in recover_all_projects(base_dir):
        console.print(warning, style="yellow", markup=False)

    table = Table(title="📁 本地小说项目列表", title_style="bold green")
    table.add_column("项目目录", style="cyan")
    table.add_column("书名", style="magenta")
    table.add_column("题材", style="blue")
    table.add_column("已产出章节", style="green")
    table.add_column("总字数", style="yellow")

    for p in base_dir.iterdir():
        if p.is_dir() and (p / "project_state.json").exists():
            try:
                pipeline = NovelPipeline(novel_id=p.name, lightweight=True, load_derived=False)
                summary = pipeline.get_status_summary()
                table.add_row(
                    p.name,
                    f"《{summary['title']}》",
                    summary['genre'],
                    f"{summary['total_generated_chapters']} 章",
                    f"{summary['total_words']} 字",
                )
            except Exception as exc:
                console.print(f"[yellow]⚠ 跳过无法读取的项目 {p.name}: {type(exc).__name__}[/yellow]")

    console.print(table)


if __name__ == "__main__":
    app()
