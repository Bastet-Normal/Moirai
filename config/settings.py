from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（config/ 的上一级），所有相对路径都锚定到这里，与启动时的工作目录解耦
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # API 核心配置 (OpenAI 兼容协议)
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"

    # 角色模型路由
    DEFAULT_MODEL: str = "deepseek-chat"
    WRITER_MODEL: str = "deepseek-chat"
    PLANNER_MODEL: str = "deepseek-chat"
    CRITIC_MODEL: str = "deepseek-chat"
    STATE_MODEL: str = "deepseek-chat"

    # 生成参数
    TEMPERATURE: float = Field(default=0.75, ge=0, le=2)
    MAX_RETRIES: int = Field(default=3, ge=1, le=10)
    REQUEST_TIMEOUT: int = Field(default=120, ge=10, le=3600)

    # 存储与输出路径（相对路径自动锚定到项目根目录）
    OUTPUT_DIR: str = str(BASE_DIR / "novel_projects")

    # 网文篇幅超参数
    WORDS_PER_BEAT: int = Field(default=800, ge=100, le=5000)
    BEATS_PER_CHAPTER: int = Field(default=3, ge=1, le=10)
    MAX_AUDIT_RETRIES: int = Field(default=2, ge=0, le=10)
    # 质检严格度：strict(苛刻，分低于8.5即打回) / standard(标准，7.0) / lenient(宽松，5.5)
    AUDIT_STRICTNESS: Literal["strict", "standard", "lenient"] = "standard"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_output_dir() -> Path:
    """解析输出目录：绝对路径直接用；相对路径锚定到项目根目录"""
    p = Path(get_settings().OUTPUT_DIR)
    return p if p.is_absolute() else (BASE_DIR / p)
