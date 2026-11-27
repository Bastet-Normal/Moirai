"""Public model and storage API, imported on first use."""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .llm import LLMClient, get_llm_client
    from .schemas import (
        AuditReport,
        ChapterBeat,
        ChapterBeatsPlan,
        ChapterContent,
        ChapterOutline,
        CharacterCard,
        CharacterState,
        ForeshadowingRecord,
        GlobalOutline,
        HumanChapterReview,
        NovelConcept,
        NovelProjectState,
        StateUpdateDelta,
        VolumeOutline,
        WorldSetting,
    )
    from .state_manager import StateManager

__all__ = [
    "NovelConcept",
    "WorldSetting",
    "CharacterCard",
    "CharacterState",
    "ForeshadowingRecord",
    "ChapterBeat",
    "ChapterBeatsPlan",
    "ChapterOutline",
    "VolumeOutline",
    "GlobalOutline",
    "AuditReport",
    "HumanChapterReview",
    "ChapterContent",
    "StateUpdateDelta",
    "NovelProjectState",
    "LLMClient",
    "get_llm_client",
    "StateManager",
]


def __getattr__(name: str):
    if name in {"schemas", "llm", "state_manager"}:
        return import_module(f".{name}", __name__)
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if name == "StateManager":
        module = ".state_manager"
    elif name in {"LLMClient", "get_llm_client"}:
        module = ".llm"
    else:
        module = ".schemas"
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()).union(__all__))
