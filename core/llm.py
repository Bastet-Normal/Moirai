"""Bounded OpenAI-compatible chat transport used by every model-facing agent."""

import json
import logging
import random
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any, TypeVar

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI, RateLimitError
from pydantic import BaseModel, ValidationError
from rich.console import Console

from config.settings import get_settings
from core.error_reporting import SafeUserMessage, referenced_failure, report_internal_error

T = TypeVar("T", bound=BaseModel)
console = Console()
logger = logging.getLogger("novelengine.llm")

_TRANSIENT_STATUS = {408, 409, 425, 429}
_UNSUPPORTED_STATUS = {400, 422}
_MAX_RESPONSE_CHARS = 8 * 1024 * 1024
_MAX_REPAIR_OUTPUT_CHARS = 8_000


class LLMResponseError(RuntimeError):
    """The provider replied, but not with a valid complete text response."""


class EmptyLLMResponseError(LLMResponseError):
    """The transport completed successfully but produced no assistant text."""


class IncompleteLLMResponseError(LLMResponseError):
    """A stream ended without the protocol's terminal finish marker."""


class TruncatedLLMResponseError(LLMResponseError):
    """The provider explicitly stopped because the output limit was reached."""


class LLMRefusalError(LLMResponseError):
    """The provider refused or filtered the requested output."""


class LLMResponseTooLargeError(LLMResponseError):
    """A provider exceeded the process-safe output boundary."""


class LLMCallError(SafeUserMessage, RuntimeError):
    """A safe terminal model-call failure carrying only a diagnostic ID."""


def _remove_structural_trailing_commas(text: str) -> str:
    """Remove commas before `]`/`}` only while outside JSON strings."""
    result: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "]}":
                index += 1
                continue
        result.append(char)
        index += 1
    return "".join(result)


def _balanced_json_end(text: str, start: int) -> int | None:
    stack: list[str] = []
    in_string = False
    escaped = False
    matching = {"{": "}", "[": "]"}
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in matching:
            stack.append(matching[char])
        elif char in "]}":
            if not stack or char != stack.pop():
                return None
            if not stack:
                return index + 1
    return None


def clean_json_string(text: str) -> str:
    """Extract the first balanced JSON object/array without corrupting strings."""
    candidate_space = (text or "").strip()
    lower = candidate_space.casefold()
    if lower.startswith("```json") or candidate_space.startswith("```"):
        first_newline = candidate_space.find("\n")
        closing = candidate_space.rfind("```")
        if first_newline >= 0 and closing > first_newline:
            candidate_space = candidate_space[first_newline + 1 : closing].strip()

    start = 0
    while start < len(candidate_space):
        starts = [position for position in (candidate_space.find("{", start), candidate_space.find("[", start)) if position >= 0]
        if not starts:
            break
        start = min(starts)
        end = _balanced_json_end(candidate_space, start)
        if end is None:
            start += 1
            continue
        candidate = _remove_structural_trailing_commas(candidate_space[start:end])
        try:
            json.loads(candidate)
        except json.JSONDecodeError:
            # Never salvage a valid-looking nested object from malformed outer
            # JSON; continue only after this balanced top-level candidate.
            start = end
            continue
        return candidate
    return _remove_structural_trailing_commas(candidate_space)


def _error_detail(error: Exception) -> tuple[str, str]:
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        detail = body.get("error", body)
        if isinstance(detail, dict):
            return str(detail.get("param") or ""), str(detail.get("message") or error)
    return "", str(error)


def is_unsupported_parameter_error(error: Exception, parameter: str) -> bool:
    """Downgrade a feature only when the provider names that exact parameter."""
    if not isinstance(error, APIError) or getattr(error, "status_code", None) not in _UNSUPPORTED_STATUS:
        return False
    param, message = _error_detail(error)
    parameter = parameter.casefold()
    explicitly_named = param.casefold() == parameter or parameter in message.casefold()
    unsupported = any(word in message.casefold() for word in (
        "unsupported", "not support", "unknown", "unrecognized", "not allowed", "extra field",
    ))
    return explicitly_named and unsupported


def is_retryable_error(error: Exception) -> bool:
    """Retry transient transport failures and an EOF without a finish marker."""
    if isinstance(error, (APITimeoutError, APIConnectionError, RateLimitError, IncompleteLLMResponseError)):
        return True
    if isinstance(error, EmptyLLMResponseError):
        return True
    if isinstance(error, (TruncatedLLMResponseError, LLMRefusalError, LLMResponseTooLargeError)):
        return False
    if isinstance(error, APIError):
        status = getattr(error, "status_code", None)
        return status is None or status in _TRANSIENT_STATUS or (isinstance(status, int) and status >= 500)
    return False


def _safe_error_label(error: Exception | None) -> str:
    """Classify an error without copying provider or model-controlled text."""
    if error is None:
        return "未知错误"
    if isinstance(error, ValidationError):
        return "结构校验失败"
    if isinstance(error, json.JSONDecodeError):
        return "JSON 语法错误"
    if isinstance(error, APITimeoutError):
        return "模型服务超时"
    if isinstance(error, APIConnectionError):
        return "模型服务连接失败"
    if isinstance(error, RateLimitError):
        return "模型服务限流"
    if isinstance(error, LLMResponseError):
        return type(error).__name__
    if isinstance(error, APIError):
        status = getattr(error, "status_code", None)
        return f"模型服务 HTTP {status}" if isinstance(status, int) else "模型服务错误"
    return type(error).__name__


def _schema_repair_hint(error: Exception) -> str:
    """Give the model field/type guidance without echoing rejected values."""
    if isinstance(error, json.JSONDecodeError):
        return f"JSON 语法错误，字符位置 {max(0, error.pos)}"
    if isinstance(error, ValidationError):
        hints: list[str] = []
        for item in error.errors(include_url=False, include_context=False, include_input=False)[:20]:
            location = ".".join(str(part)[:80] for part in item.get("loc", ())) or "root"
            hints.append(f"{location}: {str(item.get('type', 'invalid'))[:80]}")
        return "；".join(hints) or "结构不符合 Schema"
    return "结构不符合 Schema"


class LLMClient:
    """OpenAI-compatible streaming client with one application-owned retry budget."""

    USAGE_TOTALS: dict[str, Any] = {
        "prompt_tokens": 0, "completion_tokens": 0, "calls": 0, "estimated": False,
    }
    _USAGE_LOCK = threading.Lock()

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.settings = get_settings()
        self.api_key = api_key or self.settings.OPENAI_API_KEY
        self.base_url = base_url or self.settings.OPENAI_BASE_URL
        self._json_mode_supported = True
        self._stream_options_supported = True
        self._client_lock = threading.RLock()
        self._active_calls = 0
        self._retired_clients: list[Any] = []
        self._closed = False
        self.client = self._new_client(self.api_key, self.base_url, self.settings)

    @staticmethod
    def _new_client(api_key: str, base_url: str, settings: Any) -> OpenAI:
        return OpenAI(
            api_key=api_key or "sk-dummy-key",
            base_url=base_url,
            timeout=settings.REQUEST_TIMEOUT,
            max_retries=0,
            default_headers={"Accept-Encoding": "identity"},
        )

    @staticmethod
    def _close_clients(clients: list[Any]) -> None:
        for client in clients:
            with suppress(Exception):
                client.close()

    @contextmanager
    def _client_lease(self) -> Iterator[Any]:
        with self._client_lock:
            if self._closed or self.client is None:
                raise RuntimeError("LLM 客户端已关闭")
            client = self.client
            self._active_calls += 1
        try:
            yield client
        finally:
            retired: list[Any] = []
            with self._client_lock:
                self._active_calls -= 1
                if self._active_calls == 0 and self._retired_clients:
                    retired, self._retired_clients = self._retired_clients, []
            self._close_clients(retired)

    def update_credentials(self, api_key: str | None = None, base_url: str | None = None) -> None:
        """Atomically swap clients; active streams keep their old client alive."""
        new_settings = get_settings()
        new_key = api_key if api_key is not None else self.api_key
        new_url = base_url if base_url is not None else self.base_url
        replacement = self._new_client(new_key, new_url, new_settings)
        close_now: list[Any] = []
        with self._client_lock:
            if self._closed:
                close_now.append(replacement)
                error = RuntimeError("LLM 客户端已关闭，不能更新凭据")
            else:
                old = self.client
                self.client = replacement
                self.settings = new_settings
                self.api_key = new_key
                self.base_url = new_url
                self._json_mode_supported = True
                self._stream_options_supported = True
                if self._active_calls:
                    self._retired_clients.append(old)
                else:
                    close_now.append(old)
                error = None
        self._close_clients(close_now)
        if error:
            raise error

    def close(self) -> None:
        close_now: list[Any] = []
        with self._client_lock:
            if self._closed:
                return
            self._closed = True
            current, self.client = self.client, None
            if current is not None:
                if self._active_calls:
                    self._retired_clients.append(current)
                else:
                    close_now.append(current)
            if not self._active_calls:
                close_now.extend(self._retired_clients)
                self._retired_clients = []
        self._close_clients(close_now)

    @classmethod
    def usage_snapshot(cls) -> dict[str, Any]:
        with cls._USAGE_LOCK:
            return dict(cls.USAGE_TOTALS)

    @staticmethod
    def _retry_after(error: Exception | None) -> float | None:
        response = getattr(error, "response", None)
        value = response.headers.get("Retry-After") if response is not None else None
        if value is None:
            return None
        try:
            return max(0.0, min(float(value), 30.0))
        except (TypeError, ValueError):
            return None

    def _sleep_backoff(self, attempt: int, error: Exception | None = None) -> None:
        wait_time = self._retry_after(error)
        if wait_time is None:
            wait_time = min(2**attempt + random.uniform(0, 1), 30)
        console.print(f"[yellow]⏳ 等待 {wait_time:.1f}s 后重试...[/yellow]")
        getattr(self, "_sleep", time.sleep)(wait_time)

    def _disable_optional_for(self, client: Any, parameter: str) -> None:
        """An old endpoint response cannot change a newly swapped client's cache."""
        with self._client_lock:
            if client is not self.client:
                return
            if parameter == "stream_options":
                self._stream_options_supported = False
            elif parameter == "response_format":
                self._json_mode_supported = False

    def _create_stream(self, client: Any, kwargs: dict[str, Any]) -> Any:
        compatible = dict(kwargs)
        while True:
            try:
                return client.chat.completions.create(**compatible)
            except APIError as error:
                if "stream_options" in compatible and is_unsupported_parameter_error(error, "stream_options"):
                    self._disable_optional_for(client, "stream_options")
                    compatible.pop("stream_options")
                    console.print("[yellow]ℹ️ 当前端点不支持流式 usage 汇总，已仅关闭该可选参数[/yellow]")
                    continue
                if "response_format" in compatible and is_unsupported_parameter_error(error, "response_format"):
                    self._disable_optional_for(client, "response_format")
                    compatible.pop("response_format")
                    console.print("[yellow]ℹ️ 当前端点明确不支持 json_object，已仅关闭该可选参数[/yellow]")
                    continue
                raise

    def _chat(
        self, model: str, messages: list[dict[str, str]], temperature: float,
        max_tokens: int | None, json_mode: bool,
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": model, "messages": messages, "temperature": temperature, "stream": True,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        stream = None
        parts: list[str] = []
        refusal_parts: list[str] = []
        usage = None
        finish_reason = None
        completed_iteration = False
        response_chars = 0
        with self._client_lease() as client:
            with self._client_lock:
                if self._stream_options_supported:
                    kwargs["stream_options"] = {"include_usage": True}
                if json_mode and self._json_mode_supported:
                    kwargs["response_format"] = {"type": "json_object"}
            try:
                stream = self._create_stream(client, kwargs)
                try:
                    for response_chunk in stream:
                        if getattr(response_chunk, "usage", None) is not None:
                            usage = response_chunk.usage
                        choices = getattr(response_chunk, "choices", None)
                        if not choices:
                            continue
                        choice = choices[0]
                        if getattr(choice, "finish_reason", None) is not None:
                            finish_reason = choice.finish_reason
                        delta = getattr(choice, "delta", None)
                        content = getattr(delta, "content", None) if delta is not None else None
                        refusal = getattr(delta, "refusal", None) if delta is not None else None
                        if content:
                            if not isinstance(content, str):
                                raise LLMResponseError("LLM 返回了非文本 content 增量")
                            parts.append(content)
                            response_chars += len(content)
                        if refusal:
                            refusal_parts.append(str(refusal))
                        if response_chars > _MAX_RESPONSE_CHARS:
                            raise LLMResponseTooLargeError("LLM 输出超过 8 MiB 安全上限")
                    completed_iteration = True
                except Exception:
                    # The finish marker is the protocol completion boundary. A
                    # disconnect while waiting only for the optional usage tail
                    # must not repeat a completed, potentially billable request.
                    if finish_reason is None:
                        self._record_usage(messages, "".join(parts), usage)
                        raise
                    completed_iteration = True
            finally:
                if stream is not None:
                    with suppress(Exception):
                        stream.close()

        text = "".join(parts)
        if completed_iteration:
            self._record_usage(messages, text, usage)
        if refusal_parts or finish_reason in {"content_filter", "safety"}:
            raise LLMRefusalError("LLM 拒绝或过滤了本次输出")
        if finish_reason == "length":
            raise TruncatedLLMResponseError("LLM 输出因 token 上限被截断，未保存为完整结果")
        if finish_reason is None:
            raise IncompleteLLMResponseError("LLM 流未返回结束标记，可能发生静默截断")
        if finish_reason != "stop":
            raise LLMResponseError(f"LLM 以非文本结束原因终止: {finish_reason}")
        if not text.strip():
            raise EmptyLLMResponseError("LLM 返回了空内容")
        return text

    def _record_usage(self, messages: list[dict[str, str]], output_text: str, usage: Any | None) -> None:
        try:
            with self._USAGE_LOCK:
                if usage is not None and getattr(usage, "total_tokens", 0):
                    self.USAGE_TOTALS["prompt_tokens"] += int(getattr(usage, "prompt_tokens", 0) or 0)
                    self.USAGE_TOTALS["completion_tokens"] += int(getattr(usage, "completion_tokens", 0) or 0)
                else:
                    self.USAGE_TOTALS["prompt_tokens"] += sum(len(m.get("content") or "") for m in messages)
                    self.USAGE_TOTALS["completion_tokens"] += len(output_text)
                    self.USAGE_TOTALS["estimated"] = True
                self.USAGE_TOTALS["calls"] += 1
        except Exception:
            pass

    def generate_text(
        self, messages: list[dict[str, str]], model: str | None = None,
        temperature: float | None = None, max_tokens: int | None = None,
        system_prompt: str | None = None,
    ) -> str:
        target_model = model or self.settings.DEFAULT_MODEL
        temp = temperature if temperature is not None else self.settings.TEMPERATURE
        call_messages = ([{"role": "system", "content": system_prompt}] if system_prompt else []) + list(messages)
        last_error: Exception | None = None
        for attempt in range(1, self.settings.MAX_RETRIES + 1):
            try:
                return self._chat(target_model, call_messages, temp, max_tokens, False).strip()
            except SafeUserMessage:
                raise
            except Exception as error:
                last_error = error
                if not is_retryable_error(error) or attempt == self.settings.MAX_RETRIES:
                    break
                console.print(
                    f"[yellow]⚠️ LLM 调用异常 (尝试 {attempt}/{self.settings.MAX_RETRIES}): "
                    f"{_safe_error_label(error)}[/yellow]"
                )
                self._sleep_backoff(attempt, error)
        terminal = last_error or RuntimeError("model call ended without an exception")
        error_id = report_internal_error(
            logger,
            terminal,
            scope="generate_text",
            context={"model": target_model, "attempts": self.settings.MAX_RETRIES},
        )
        raise LLMCallError(referenced_failure("模型生成未完成，请检查服务连接后重试", error_id)) from terminal

    def generate_structured(
        self, messages: list[dict[str, str]], response_model: type[T],
        model: str | None = None, temperature: float | None = None,
        system_prompt: str | None = None,
    ) -> T:
        target_model = model or self.settings.DEFAULT_MODEL
        temp = temperature if temperature is not None else 0.5
        schema_json = json.dumps(response_model.model_json_schema(), ensure_ascii=False, indent=2)
        schema_instruction = (
            "\n\n【输出格式严格要求】\n只输出符合下列 JSON Schema 的 JSON，不得输出解释或 Markdown：\n"
            f"{schema_json}\n输出必须可直接由 json.loads 解析。"
        )
        call_messages = [{"role": "system", "content": (system_prompt or "") + schema_instruction}, *messages]
        last_error: Exception | None = None
        raw_output = ""

        for attempt in range(1, self.settings.MAX_RETRIES + 1):
            try:
                raw_output = self._chat(target_model, call_messages, temp, None, True)
                return response_model.model_validate(json.loads(clean_json_string(raw_output)))
            except SafeUserMessage:
                raise
            except (json.JSONDecodeError, ValidationError) as parse_error:
                last_error = parse_error
                if attempt == self.settings.MAX_RETRIES:
                    break
                console.print(
                    f"[yellow]⚠️ {response_model.__name__} 解析失败，正在请求一次受限自纠正: "
                    f"{_safe_error_label(parse_error)}[/yellow]"
                )
                repair_hint = _schema_repair_hint(parse_error)
                call_messages.extend([
                    {"role": "assistant", "content": raw_output[:_MAX_REPAIR_OUTPUT_CHARS]},
                    {"role": "user", "content": (
                        "上述 JSON 无法通过 Schema。错误："
                        f"{repair_hint}。只输出修正后的完整 JSON。"
                    )},
                ])
                getattr(self, "_sleep", time.sleep)(min(float(attempt), 5.0))
            except Exception as api_error:
                last_error = api_error
                if not is_retryable_error(api_error) or attempt == self.settings.MAX_RETRIES:
                    break
                console.print(
                    f"[yellow]⚠️ LLM API 错误 (尝试 {attempt}/{self.settings.MAX_RETRIES}): "
                    f"{_safe_error_label(api_error)}[/yellow]"
                )
                self._sleep_backoff(attempt, api_error)

        terminal = last_error or RuntimeError("structured model call ended without an exception")
        error_id = report_internal_error(
            logger,
            terminal,
            scope="generate_structured",
            context={
                "model": target_model,
                "response_model": response_model.__name__,
                "attempts": self.settings.MAX_RETRIES,
                "raw_output_chars": len(raw_output),
            },
        )
        raise LLMCallError(
            referenced_failure(f"无法生成结构化数据 {response_model.__name__}，请重试", error_id)
        ) from terminal


_global_client: LLMClient | None = None
_global_client_lock = threading.Lock()


def get_llm_client() -> LLMClient:
    global _global_client
    with _global_client_lock:
        if _global_client is None:
            _global_client = LLMClient()
        return _global_client


def reset_llm_client() -> None:
    """Close and discard the global client without interrupting an active stream."""
    global _global_client
    with _global_client_lock:
        old, _global_client = _global_client, None
    if old is not None:
        old.close()
