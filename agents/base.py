from typing import TypeVar

from pydantic import BaseModel
from rich.console import Console

from config.settings import get_settings
from core.llm import LLMClient, get_llm_client

T = TypeVar("T", bound=BaseModel)
console = Console()


class BaseAgent:
    """Agent 抽象基类"""

    def __init__(
        self,
        name: str,
        system_prompt: str,
        model_name: str | None = None,
        llm_client: LLMClient | None = None,
    ):
        self.name = name
        self.system_prompt = system_prompt
        self.settings = get_settings()
        self.model_name = model_name or self.settings.DEFAULT_MODEL
        self.llm_client = llm_client or get_llm_client()

    def call_llm(
        self,
        prompt: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """调用 LLM 生成自然语言文本"""
        console.print(f"[cyan]🤖 [{self.name}] 正在思考生成文本...[/cyan]")
        messages = [{"role": "user", "content": prompt}]
        return self.llm_client.generate_text(
            messages=messages,
            model=self.model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            system_prompt=self.system_prompt,
        )

    def call_structured(
        self,
        prompt: str,
        response_model: type[T],
        temperature: float | None = None,
    ) -> T:
        """调用 LLM 生成结构化 Pydantic 模型"""
        console.print(f"[cyan]🤖 [{self.name}] 正在生成结构化数据 ({response_model.__name__})...[/cyan]")
        messages = [{"role": "user", "content": prompt}]
        return self.llm_client.generate_structured(
            messages=messages,
            response_model=response_model,
            model=self.model_name,
            temperature=temperature,
            system_prompt=self.system_prompt,
        )
