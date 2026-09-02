"""No-network transport contract tests with controlled OpenAI-compatible streams."""

import json
import re
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import pytest
from openai import APIConnectionError, BadRequestError, InternalServerError, OpenAI
from pydantic import BaseModel

from core.llm import (
    IncompleteLLMResponseError,
    LLMCallError,
    LLMClient,
    LLMRefusalError,
    TruncatedLLMResponseError,
    clean_json_string,
    is_retryable_error,
    is_unsupported_parameter_error,
)


class Result(BaseModel):
    text: str
    count: int = 1


def chunk(text=None, *, finish=None, refusal=None, usage=None):
    choices = [] if text is None and finish is None and refusal is None else [
        SimpleNamespace(delta=SimpleNamespace(content=text, refusal=refusal), finish_reason=finish)
    ]
    return SimpleNamespace(choices=choices, usage=usage)


class FakeStream:
    def __init__(self, chunks, error=None):
        self.chunks = chunks
        self.error = error
        self.closed = False

    def __iter__(self):
        yield from self.chunks
        if self.error:
            raise self.error

    def close(self):
        self.closed = True


class FakeCompletions:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeSDKClient:
    def __init__(self, results):
        self.chat = SimpleNamespace(completions=FakeCompletions(results))
        self.closed = False

    def close(self):
        self.closed = True


class TrackedSSE(httpx.SyncByteStream):
    def __init__(self, payload):
        self.payload = payload
        self.closed = False

    def __iter__(self):
        yield self.payload

    def close(self):
        self.closed = True


def settings(attempts=3):
    return SimpleNamespace(
        OPENAI_API_KEY="", OPENAI_BASE_URL="https://example.invalid/v1",
        REQUEST_TIMEOUT=30, MAX_RETRIES=attempts, DEFAULT_MODEL="fake-model", TEMPERATURE=0.5,
    )


def client_with(results, attempts=3):
    client = LLMClient.__new__(LLMClient)
    client.settings = settings(attempts)
    client.api_key = "fake"
    client.base_url = "https://example.invalid/v1"
    client._json_mode_supported = True
    client._stream_options_supported = True
    client._client_lock = threading.RLock()
    client._active_calls = 0
    client._retired_clients = []
    client._closed = False
    client.client = FakeSDKClient(results)
    return client


def api_error(kind, status, message, body=None, headers=None):
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(status, request=request, headers=headers or {})
    return kind(message, response=response, body=body)


@pytest.mark.parametrize("raw,expected", [
    ('{"text":"comma,} and bracket,] stay", "count":1,}', {"text": "comma,} and bracket,] stay", "count": 1}),
    ('prefix {"text":"escaped quote \\\" and }", "count":2,} suffix [not this]', {"text": 'escaped quote " and }', "count": 2}),
    ('```json\n{"text":"fence },]", "count":3,}\n``` trailing', {"text": "fence },]", "count": 3}),
])
def test_json_cleanup_repairs_only_structural_trailing_commas(raw, expected):
    assert json.loads(clean_json_string(raw)) == expected


def test_json_cleanup_chooses_first_balanced_value_and_rejects_unbalanced():
    assert json.loads(clean_json_string('notes [1,{"x":"}"},] ignored {"later":2}')) == [1, {"x": "}"}]
    with pytest.raises(json.JSONDecodeError):
        json.loads(clean_json_string('notes {"x": 1'))


def test_json_cleanup_never_salvages_nested_object_from_malformed_outer_value():
    malformed = '{"text":BROKEN,"nested":{"text":"plausible", "count":1}}'
    with pytest.raises(json.JSONDecodeError):
        json.loads(clean_json_string(malformed))


def test_sdk_retries_are_disabled_because_application_owns_budget():
    with patch("core.llm.get_settings", return_value=settings()), patch("core.llm.OpenAI") as openai:
        openai.return_value = FakeSDKClient([])
        client = LLMClient()
    assert openai.call_args.kwargs["max_retries"] == 0
    client.close()
    assert openai.return_value.closed


def test_installed_sdk_parses_local_sse_and_transport_is_closed():
    events = [
        {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "fake", "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": "真实"}, "finish_reason": None}
        ]},
        {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "fake", "choices": [
            {"index": 0, "delta": {"content": "流"}, "finish_reason": "stop"}
        ]},
        {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "fake", "choices": [],
         "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}},
    ]
    payload = b"".join(f"data: {json.dumps(event)}\n\n".encode() for event in events) + b"data: [DONE]\n\n"
    tracked = TrackedSSE(payload)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=tracked)

    sdk = OpenAI(
        api_key="fake", base_url="https://local.invalid/v1", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client = client_with([])
    client.client = sdk
    try:
        assert client._chat("fake", [{"role": "user", "content": "x"}], 0.5, None, False) == "真实流"
        assert len(requests) == 1
        request_body = json.loads(requests[0].content)
        assert request_body["stream"] is True
        assert request_body["stream_options"] == {"include_usage": True}
        assert tracked.closed
    finally:
        client.close()


def test_installed_sdk_does_not_hide_automatic_retry():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=request, json={"error": {"message": "offline", "type": "server_error"}})

    sdk = OpenAI(
        api_key="fake", base_url="https://local.invalid/v1", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client = client_with([])
    client.client = sdk
    try:
        with pytest.raises(InternalServerError):
            client._chat("fake", [], 0.5, None, False)
        assert calls == 1
    finally:
        client.close()


def test_stream_is_closed_and_complete_usage_recorded():
    usage = SimpleNamespace(total_tokens=3, prompt_tokens=1, completion_tokens=2)
    stream = FakeStream([chunk("正文"), chunk(finish="stop"), chunk(usage=usage)])
    client = client_with([stream])
    before = client.usage_snapshot()
    assert client._chat("m", [{"role": "user", "content": "x"}], 0.5, None, False) == "正文"
    after = client.usage_snapshot()
    assert stream.closed
    assert after["calls"] == before["calls"] + 1
    assert after["completion_tokens"] == before["completion_tokens"] + 2


def test_stream_is_closed_and_partial_attempt_is_counted_on_iteration_failure():
    stream = FakeStream([chunk("partial")], APIConnectionError(request=httpx.Request("POST", "https://x")))
    client = client_with([stream])
    before = client.usage_snapshot()
    with pytest.raises(APIConnectionError):
        client._chat("m", [], 0.5, None, False)
    assert stream.closed
    after = client.usage_snapshot()
    assert after["calls"] == before["calls"] + 1
    assert after["completion_tokens"] == before["completion_tokens"] + len("partial")


def test_failure_after_finish_marker_keeps_completed_result_without_retry():
    stream = FakeStream(
        [chunk("complete"), chunk(finish="stop")],
        APIConnectionError(request=httpx.Request("POST", "https://x")),
    )
    client = client_with([stream])
    client._sleep_backoff = Mock()
    assert client.generate_text([]) == "complete"
    assert stream.closed
    client._sleep_backoff.assert_not_called()


@pytest.mark.parametrize("finish,error_type", [
    ("length", TruncatedLLMResponseError),
    ("content_filter", LLMRefusalError),
])
def test_non_success_finish_reason_is_never_returned_as_complete(finish, error_type):
    client = client_with([FakeStream([chunk("partial"), chunk(finish=finish)])])
    with pytest.raises(error_type):
        client._chat("m", [], 0.5, 50, False)


def test_missing_finish_marker_is_retryable_and_then_succeeds():
    client = client_with([FakeStream([chunk("partial")]), FakeStream([chunk("complete"), chunk(finish="stop")])])
    client._sleep_backoff = Mock()
    assert client.generate_text([{"role": "user", "content": "x"}]) == "complete"
    assert client._sleep_backoff.call_count == 1
    assert is_retryable_error(IncompleteLLMResponseError("x"))
    assert not is_retryable_error(TruncatedLLMResponseError("x"))


def test_refusal_delta_is_not_treated_as_empty_retryable_response():
    client = client_with([FakeStream([chunk(refusal="cannot comply"), chunk(finish="stop")])])
    with pytest.raises(LLMRefusalError, match="拒绝"):
        client._chat("m", [], 0.5, None, False)


def test_stream_options_downgrade_requires_explicit_parameter_error():
    unsupported = api_error(
        BadRequestError, 400, "unsupported parameter stream_options",
        {"error": {"param": "stream_options", "message": "unsupported parameter"}},
    )
    stream = FakeStream([chunk("ok"), chunk(finish="stop")])
    client = client_with([unsupported, stream])
    assert client._chat("m", [], 0.5, None, False) == "ok"
    calls = client.client.chat.completions.calls
    assert "stream_options" in calls[0] and "stream_options" not in calls[1]
    assert not client._stream_options_supported


def test_generic_400_does_not_disable_stream_or_json_modes():
    generic = api_error(BadRequestError, 400, "invalid model", {"error": {"param": "model"}})
    client = client_with([generic])
    with pytest.raises(BadRequestError):
        client._chat("m", [], 0.5, None, True)
    assert client._stream_options_supported and client._json_mode_supported
    assert not is_unsupported_parameter_error(generic, "response_format")


def test_json_mode_downgrade_is_targeted_and_validation_succeeds():
    unsupported = api_error(
        BadRequestError, 400, "response_format unsupported",
        {"error": {"param": "response_format", "message": "unsupported"}},
    )
    stream = FakeStream([chunk('{"text":"literal ,} preserved", "count":1,}'), chunk(finish="stop")])
    client = client_with([unsupported, stream])
    result = client.generate_structured([{"role": "user", "content": "x"}], Result)
    assert result.text == "literal ,} preserved"
    calls = client.client.chat.completions.calls
    assert "response_format" in calls[0] and "response_format" not in calls[1]
    assert not client._json_mode_supported


def test_generic_bad_request_does_not_trigger_plain_mode_or_retry():
    generic = api_error(BadRequestError, 400, "invalid model", {"error": {"param": "model"}})
    client = client_with([])
    client._chat = Mock(side_effect=generic)
    with pytest.raises(RuntimeError, match="结构化"):
        client.generate_structured([], Result)
    assert client._chat.call_count == 1
    assert client._json_mode_supported


def test_own_retry_budget_is_exact_and_retry_after_is_bounded():
    server_error = api_error(InternalServerError, 503, "unavailable", headers={"Retry-After": "999"})
    client = client_with([], attempts=3)
    client._chat = Mock(side_effect=[server_error, server_error, "ok"])
    waits = []
    client._sleep = waits.append
    assert client.generate_text([]) == "ok"
    assert client._chat.call_count == 3
    assert waits == [30, 30]


def test_parse_retry_context_and_terminal_error_are_bounded():
    very_large = "x" * 100_000
    client = client_with([], attempts=2)
    client._chat = Mock(side_effect=[very_large, very_large])
    client._sleep = Mock()
    with pytest.raises(RuntimeError) as caught:
        client.generate_structured([], Result)
    assert len(str(caught.value)) < 10_000
    second_messages = client._chat.call_args_list[1].args[1]
    assert max(len(m["content"]) for m in second_messages) < 20_000


def test_terminal_provider_error_never_exposes_response_or_credentials(caplog, capsys):
    secret = "sk-private-token-and-unpublished-prose"
    client = client_with([], attempts=1)
    client._chat = Mock(side_effect=RuntimeError(secret))
    with caplog.at_level("ERROR", logger="novelengine.llm"), pytest.raises(LLMCallError) as caught:
        client.generate_text([])
    combined = str(caught.value) + caplog.text + capsys.readouterr().out
    assert secret not in combined
    assert re.search(r"诊断编号 [0-9a-f]{12}", combined)


def test_terminal_schema_error_never_exposes_model_output(caplog, capsys):
    secret = "unpublished-private-prose"
    client = client_with([], attempts=1)
    client._chat = Mock(return_value=json.dumps({"text": secret, "count": "not-an-integer"}))
    with caplog.at_level("ERROR", logger="novelengine.llm"), pytest.raises(LLMCallError) as caught:
        client.generate_structured([], Result)
    combined = str(caught.value) + caplog.text + capsys.readouterr().out
    assert secret not in combined
    assert "原始输出预览" not in combined
    assert re.search(r"诊断编号 [0-9a-f]{12}", combined)


def test_credential_swap_does_not_close_client_used_by_active_stream():
    client = client_with([])
    old = client.client
    new = FakeSDKClient([])
    entered = threading.Event()
    release = threading.Event()

    def active_call():
        with client._client_lease() as leased:
            assert leased is old
            entered.set()
            release.wait(5)
            assert not old.closed

    thread = threading.Thread(target=active_call)
    thread.start()
    assert entered.wait(2)
    client._new_client = Mock(return_value=new)
    client.update_credentials(api_key="new-key", base_url="https://new.invalid/v1")
    assert client.client is new and not old.closed
    release.set()
    thread.join(2)
    assert not thread.is_alive() and old.closed
    client.close()
    assert new.closed


def test_retired_endpoint_cannot_disable_new_endpoint_capabilities():
    client = client_with([])
    old = client.client
    new = FakeSDKClient([])
    client._new_client = Mock(return_value=new)
    client.update_credentials(api_key="new")
    client._disable_optional_for(old, "stream_options")
    client._disable_optional_for(old, "response_format")
    assert client._stream_options_supported and client._json_mode_supported


def test_close_is_idempotent_and_future_calls_fail():
    client = client_with([])
    sdk = client.client
    client.close()
    client.close()
    assert sdk.closed
    with pytest.raises(RuntimeError, match="关闭"), client._client_lease():
        pass
