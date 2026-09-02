"""Planning publication contracts, independent of model transport and UI."""

from dataclasses import dataclass

from core.error_reporting import SafeUserMessage, UserInputError
from core.schemas import (
    ChapterOutline,
    CharacterCard,
    EmotionalPacingBeat,
    GlobalOutline,
    InitialOutlinePlan,
    PacingSchedule,
)

MAX_CHAPTER_NUMBER = 1_000_000


class PlanningIncompleteError(SafeUserMessage, RuntimeError):
    """The complete initial package is durable; only its outline needs retrying."""


def validate_planning_prompt(idea: str, genre: str) -> None:
    for value, maximum, label in ((idea, 20_000, "核心创意"), (genre, 200, "题材")):
        if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
            raise UserInputError(f"{label}必须为有效文本，且不能超过 {maximum} 字")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise UserInputError(f"{label}包含无效 Unicode 字符") from exc
    if not idea.strip():
        raise UserInputError("核心创意不能为空")


def validate_initial_cast(characters: list[CharacterCard]) -> None:
    names = [character.name.strip() for character in characters]
    ids = [character.id.strip() for character in characters]
    if not names or not all(names) or not all(ids):
        raise UserInputError("初始人物不能为空，且每个人物必须有名称与 ID")
    if len(set(names)) != len(names) or len(set(ids)) != len(ids):
        raise UserInputError("初始人物的名称或 ID 重复，拒绝静默覆盖人物卡")


def validate_initial_outline(outline: GlobalOutline, request: InitialOutlinePlan) -> None:
    """Model-schema validity alone does not prove complete ordered coverage."""
    expected_volumes = list(range(1, request.total_volumes + 1))
    if outline.total_volumes != request.total_volumes or [v.volume_number for v in outline.volumes] != expected_volumes:
        raise UserInputError("初始总纲的分卷数量或顺序不符合请求")
    if any(volume.chapter_range is None for volume in outline.volumes):
        raise UserInputError("新总纲必须明确每一卷的 chapter_range，不能靠初始细纲长度猜测转卷位置")
    first_numbers = [chapter.chapter_number for chapter in outline.volumes[0].chapters]
    if len(first_numbers) < request.chapters_per_volume or first_numbers != list(range(1, len(first_numbers) + 1)):
        raise UserInputError("第一卷必须从第 1 章开始连续覆盖请求的初始细纲")
    validate_outline_integrity(outline)


def validate_outline_integrity(outline: GlobalOutline) -> None:
    """Validate edited/model instances too, not only their original JSON parse."""
    GlobalOutline.model_validate(outline.model_dump())
    if not outline.volumes or outline.total_volumes != len(outline.volumes) or any(
        volume.volume_number != index for index, volume in enumerate(outline.volumes, 1)
    ):
        raise UserInputError("总纲必须包含连续且完整的分卷")
    previous_number = 0
    for volume in outline.volumes:
        for chapter in volume.chapters:
            if not previous_number < chapter.chapter_number <= MAX_CHAPTER_NUMBER:
                raise UserInputError("细纲章节重复、次序错误或超出章节范围")
            previous_number = chapter.chapter_number
            if chapter.volume_number != volume.volume_number:
                raise UserInputError("章节所属分卷与总纲分卷不一致")
            if chapter.pacing_beat and chapter.pacing_beat.chapter_number != chapter.chapter_number:
                raise UserInputError("章节情绪节拍的章节身份不一致")
    if outline.pacing_schedule:
        schedule = outline.pacing_schedule
        maximum = schedule.total_chapters_planned
        numbers = [beat.chapter_number for beat in schedule.pacing_beats]
        last_range = outline.volumes[-1].chapter_range
        required_maximum = last_range.end if last_range else previous_number
        if (
            not required_maximum <= maximum <= MAX_CHAPTER_NUMBER or len(set(numbers)) != len(numbers)
            or any(number < 1 or number > maximum for number in numbers)
            or any(number < 1 or number > maximum for number in schedule.climax_milestone_chapters)
        ):
            raise UserInputError("全书情绪排期的数量或章节范围与总纲不一致")


@dataclass(frozen=True)
class OutlineWindow:
    volume_number: int
    start: int
    count: int


def next_outline_window(outline: GlobalOutline, chapter_number: int, block_size: int) -> OutlineWindow | None:
    """Return one unplanned block, bounded by ownership and existing chapters."""
    if type(chapter_number) is not int or not 1 <= chapter_number <= MAX_CHAPTER_NUMBER:
        raise UserInputError("章节序号必须为 1 到 1000000 的整数")
    if type(block_size) is not int or not 1 <= block_size <= 50:
        raise UserInputError("滚动细纲区块大小必须为 1 到 50 的整数")
    validate_outline_integrity(outline)
    chapters = [chapter for volume in outline.volumes for chapter in volume.chapters]
    if any(chapter.chapter_number == chapter_number for chapter in chapters):
        return None

    if outline.volumes[0].chapter_range is not None:
        volume = next((volume for volume in outline.volumes
                       if volume.chapter_range.start <= chapter_number <= volume.chapter_range.end), None)
        if volume is None:
            raise UserInputError("已到达总纲规划终章；请先调整分卷章节范围，不能自动续写到书外")
        end = volume.chapter_range.end
    else:
        # Legacy volumes with actual numbered anchors have unambiguous starts.
        # The last such volume remains open-ended, preserving prior behavior.
        # Empty legacy volumes contain no information about where to transition.
        if any(not volume.chapters for volume in outline.volumes):
            raise UserInputError("旧总纲包含无章节锚点的分卷，无法确定转卷位置；请先明确所有分卷的 chapter_range")
        index = 0
        for candidate in range(1, len(outline.volumes)):
            if outline.volumes[candidate].chapters[0].chapter_number > chapter_number:
                break
            index = candidate
        volume = outline.volumes[index]
        end = (outline.volumes[index + 1].chapters[0].chapter_number - 1
               if index + 1 < len(outline.volumes) else MAX_CHAPTER_NUMBER)

    next_existing = next((chapter.chapter_number for chapter in chapters if chapter.chapter_number > chapter_number), None)
    if next_existing is not None:
        end = min(end, next_existing - 1)
    return OutlineWindow(volume.volume_number, chapter_number, min(block_size, end - chapter_number + 1))


def merge_outline_extension(
    outline: GlobalOutline, window: OutlineWindow,
    chapters: list[ChapterOutline], pacing_beats: list[EmotionalPacingBeat],
) -> GlobalOutline:
    """Validate complete model output before preparing an isolated publication."""
    expected = list(range(window.start, window.start + window.count))
    if [chapter.chapter_number for chapter in chapters] != expected:
        raise UserInputError("滚动细纲必须按顺序恰好覆盖请求章节，不能缺章、重复或越界")
    if [beat.chapter_number for beat in pacing_beats] != expected:
        raise UserInputError("滚动情绪节拍必须与请求章节按顺序一一对应")
    for chapter, beat in zip(chapters, pacing_beats, strict=True):
        if chapter.volume_number != window.volume_number:
            raise UserInputError("滚动细纲所属卷号与请求不符，拒绝自动纠正模型的故事归属")
        if any(not getattr(chapter, field).strip() for field in ("title", "summary", "main_conflict", "expectation_hook")):
            raise UserInputError("滚动细纲的标题、剧情、冲突和章末钩子不能为空")
        if chapter.pacing_beat is not None and chapter.pacing_beat != beat:
            raise UserInputError("细纲内嵌情绪节拍与对应的排期节拍不一致")

    candidate = outline.model_copy(deep=True)
    existing = {chapter.chapter_number for volume in candidate.volumes for chapter in volume.chapters}
    if existing.intersection(expected):
        raise UserInputError("滚动细纲不能覆盖任何已规划章节")
    volume = candidate.volumes[window.volume_number - 1]
    for chapter, beat in zip(chapters, pacing_beats, strict=True):
        copied = chapter.model_copy(deep=True)
        copied.pacing_beat = beat.model_copy(deep=True)
        volume.chapters.append(copied)
    volume.chapters.sort(key=lambda chapter: chapter.chapter_number)
    last_range = candidate.volumes[-1].chapter_range
    total = last_range.end if last_range else max(existing.union(expected))
    if candidate.pacing_schedule is None:
        candidate.pacing_schedule = PacingSchedule(total_chapters_planned=total)
    pacing = candidate.pacing_schedule
    by_number = {beat.chapter_number: beat for beat in pacing.pacing_beats}
    by_number.update({beat.chapter_number: beat.model_copy(deep=True) for beat in pacing_beats})
    pacing.pacing_beats = [by_number[number] for number in sorted(by_number)]
    pacing.total_chapters_planned = max(pacing.total_chapters_planned, total)
    validate_outline_integrity(candidate)
    return candidate
