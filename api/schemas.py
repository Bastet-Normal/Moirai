"""Validated request payloads shared by the Web API routes."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from core.text_limits import MAX_CHAPTER_TEXT_CHARS


def valid_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("文本不能包含空字符")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("文本包含无效 Unicode 字符") from exc
    return value


Text = Annotated[str, AfterValidator(valid_text)]
ChapterNumber = Annotated[int, Field(strict=True, ge=1, le=1_000_000)]
Tag = Annotated[Text, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Word = Annotated[Text, StringConstraints(strip_whitespace=True, min_length=1, max_length=30)]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def bounded_structure(cls, value):
        # Bound error collection before Pydantic walks attacker-sized arrays or
        # generates a separate extra-field error for every arbitrary input key.
        if isinstance(value, dict):
            if len(value) > len(cls.model_fields):
                raise ValueError("字段数量超过接口约定")
            for item in value.values():
                if isinstance(item, list) and len(item) > 500:
                    raise ValueError("输入列表超过 500 项")
                if isinstance(item, dict):
                    if len(item) > 50:
                        raise ValueError("输入分类超过 50 项")
                    if any(isinstance(words, list) and len(words) > 500 for words in item.values()):
                        raise ValueError("输入词条列表超过 500 项")
        return value


class ConfigRequest(RequestModel):
    api_key: Text = Field(default="", max_length=4096)
    base_url: Text = Field(min_length=1, max_length=2048)
    model: Text = Field(min_length=1, max_length=200)
    audit_strictness: Literal["", "strict", "standard", "lenient"] = ""
    writer_model: Text = Field(default="", max_length=200)
    planner_model: Text = Field(default="", max_length=200)
    critic_model: Text = Field(default="", max_length=200)
    state_model: Text = Field(default="", max_length=200)


class ProjectRevisionRequest(RequestModel):
    # The API returns 428 for an absent precondition and 409 for a stale one.
    expected_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class CreateNovelRequest(RequestModel):
    name: Text = Field(min_length=1, max_length=64)
    idea: Text = Field(min_length=1, max_length=20_000)
    genre: Text = Field(default="", max_length=200)
    target_platform: Literal["FANQIE", "QIDIAN", "OVERSEAS_LITRPG", "GENERAL"] = "FANQIE"
    author_style: Literal[
        "FANQIE_DOPAMINE",
        "FANREN_TACTICAL",
        "CUTTLEFISH_MYSTERY",
        "CHENDONG_EPIC",
        "DAO_GUI_MADNESS",
    ] = "FANQIE_DOPAMINE"
    protagonist_persona: Literal[
        "RUTHLESS_DECISIVE", "CUNNING_SCHEMER", "INVINCIBLE_CHILL", "HEROIC_DEVOTION"
    ] = "RUTHLESS_DECISIVE"
    female_lead_setting: Literal["NO_FEMALE_LEAD", "SINGLE_LEAD", "HAREM", "ANY"] = "NO_FEMALE_LEAD"
    avoided_tropes: list[Tag] = Field(default_factory=list, max_length=50)
    chapters: int = Field(default=3, strict=True, ge=1, le=50)
    volumes: int = Field(default=3, strict=True, ge=1, le=20)


class GenerateChaptersRequest(ProjectRevisionRequest):
    name: Text = Field(min_length=1, max_length=64)
    count: int = Field(default=3, strict=True, ge=1, le=50)


class RollbackRequest(ProjectRevisionRequest):
    name: Text = Field(min_length=1, max_length=64)
    target_chapter: ChapterNumber


class BranchSimRequest(ProjectRevisionRequest):
    name: Text = Field(min_length=1, max_length=64)
    chapter: ChapterNumber


class ReaderSimRequest(ProjectRevisionRequest):
    name: Text = Field(min_length=1, max_length=64)
    chapter: ChapterNumber


class PatchParagraphRequest(ProjectRevisionRequest):
    name: Text = Field(min_length=1, max_length=64)
    chapter: ChapterNumber
    old_text: Text = Field(max_length=MAX_CHAPTER_TEXT_CHARS)
    new_text: Text = Field(max_length=MAX_CHAPTER_TEXT_CHARS)


class ConceptUpdateRequest(ProjectRevisionRequest):
    title: Text | None = Field(default=None, max_length=500)
    genre: Text | None = Field(default=None, max_length=200)
    logline: Text | None = Field(default=None, max_length=10_000)
    golden_finger: Text | None = Field(default=None, max_length=20_000)


class CharacterUpdateRequest(ProjectRevisionRequest):
    name: Text = Field(min_length=1, max_length=200)
    role: Text | None = Field(default=None, max_length=500)
    background: Text | None = Field(default=None, max_length=20_000)
    personality_tags: list[Tag] | None = Field(default=None, max_length=50)
    level: Text | None = Field(default=None, max_length=500)
    location: Text | None = Field(default=None, max_length=500)
    hp_status: Text | None = Field(default=None, max_length=1000)
    current_mood: Text | None = Field(default=None, max_length=1000)


class HistoricalCharacterSeed(RequestModel):
    name: Text = Field(min_length=1, max_length=200)
    level: Text = Field(min_length=1, max_length=500)
    location: Text = Field(min_length=1, max_length=500)
    hp_status: Text = Field(min_length=1, max_length=1000)
    equipment_and_items: list[Tag] = Field(default_factory=list, max_length=100)
    skills_and_abilities: list[Tag] = Field(default_factory=list, max_length=100)
    current_mood: Text = Field(min_length=1, max_length=1000)
    relations: dict[Tag, Text] = Field(default_factory=dict, max_length=50)
    is_alive: bool = Field(strict=True)


class HistoryRepairRequest(ProjectRevisionRequest):
    confirmation: Literal["我已核对第1章开始前状态"]
    author_note: Text = Field(min_length=1, max_length=4000)
    characters: list[HistoricalCharacterSeed] = Field(default_factory=list, max_length=500)

    @field_validator("characters")
    @classmethod
    def unique_character_names(cls, value: list[HistoricalCharacterSeed]) -> list[HistoricalCharacterSeed]:
        names = [item.name for item in value]
        if len(names) != len(set(names)):
            raise ValueError("章前人物名称不能重复")
        return value


class ChapterTextRequest(ProjectRevisionRequest):
    chapter: ChapterNumber
    text: Text = Field(min_length=1, max_length=MAX_CHAPTER_TEXT_CHARS)

    @field_validator("text")
    @classmethod
    def nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("正文不能为空白")
        return value


class HumanReviewRequest(ProjectRevisionRequest):
    decision: Literal["approved", "changes_requested"]
    note: Text = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def require_change_notes(self):
        self.note = self.note.strip()
        if self.decision == "changes_requested" and not self.note:
            raise ValueError("要求修改时请填写具体意见")
        return self


class RegenerateRequest(ProjectRevisionRequest):
    name: Text = Field(min_length=1, max_length=64)
    chapter: ChapterNumber


class OutlineUpdateRequest(ProjectRevisionRequest):
    title: Text | None = Field(default=None, max_length=500)
    summary: Text | None = Field(default=None, max_length=20_000)
    main_conflict: Text | None = Field(default=None, max_length=10_000)
    expectation_hook: Text | None = Field(default=None, max_length=10_000)
    involved_character_names: list[Tag] | None = Field(default=None, max_length=100)


class ComplianceReplaceRequest(ProjectRevisionRequest):
    chapter: ChapterNumber
    word: Text = Field(max_length=50)
    replacement: Text = Field(default="＊＊＊", max_length=200)


class RenameRequest(ProjectRevisionRequest):
    new_name: Text = Field(min_length=1, max_length=64)


class AdoptBranchRequest(ProjectRevisionRequest):
    chapter: ChapterNumber
    branch_id: Text = Field(min_length=1, max_length=200)


class CustomWordsRequest(RequestModel):
    words: dict[Text, Annotated[list[Word], Field(max_length=500)]] = Field(max_length=50)

    @field_validator("words")
    @classmethod
    def bounded_categories(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        if sum(map(len, value.values())) > 500:
            raise ValueError("自定义词条总数上限 500")
        clean: dict[str, list[str]] = {}
        for category, words in value.items():
            category = category.strip()
            if not category or len(category) > 12:
                raise ValueError("分类名需要 1~12 个字符，不会自动截断")
            if category in clean:
                raise ValueError("去除首尾空白后出现重复分类名")
            clean[category] = list(dict.fromkeys(words))
        return clean


class ParaRewriteRequest(ProjectRevisionRequest):
    chapter: ChapterNumber
    paragraph_index: int = Field(strict=True, ge=0)
    instruction: Text = Field(default="", max_length=500)


class ParaRewriteApplyRequest(ProjectRevisionRequest):
    chapter: ChapterNumber
    paragraph_index: int = Field(strict=True, ge=0)
    new_text: Text = Field(max_length=MAX_CHAPTER_TEXT_CHARS)
