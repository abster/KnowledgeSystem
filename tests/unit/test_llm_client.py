"""The client wrapper: structured output, error mapping and the retry schedule (Step 8)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest

from kk.llm.client import (
    AnthropicClient,
    LlmError,
    LlmOutputError,
    LlmTransientError,
    PauseGate,
    call_with_retries,
    make_client,
    retry_after_seconds,
)
from kk.llm.key import ApiKey

REQUEST: dict[str, Any] = {
    "model": "m",
    "system": "s",
    "user": "u",
    "tool_name": "t",
    "schema": {"type": "object"},
    "max_tokens": 100,
    "timeout_s": 5.0,
}


class _Scripted:
    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.attempts = 0

    def call_json(self, **_request: Any) -> dict[str, Any]:  # noqa: ANN401
        self.attempts += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]


def test_transient_errors_are_retried_on_the_fixed_schedule() -> None:
    client = _Scripted(LlmTransientError("a"), LlmTransientError("b"), {"ok": True})
    sleeps: list[float] = []
    assert call_with_retries(client, sleep=sleeps.append, **REQUEST) == {"ok": True}
    assert client.attempts == 3
    assert sleeps == [1.0, 4.0]


def test_a_transient_error_that_persists_is_raised_after_the_last_retry() -> None:
    client = _Scripted(*(LlmTransientError("x") for _ in range(3)))
    with pytest.raises(LlmTransientError):
        call_with_retries(client, sleep=lambda _s: None, **REQUEST)
    assert client.attempts == 3


@pytest.mark.parametrize("error", [LlmError("bad request"), LlmOutputError("no tool")])
def test_other_errors_are_not_retried(error: LlmError) -> None:
    client = _Scripted(error)
    with pytest.raises(type(error)):
        call_with_retries(client, sleep=lambda _s: None, **REQUEST)
    assert client.attempts == 1


class _FakeSdk:
    def __init__(self, *, content: list[Any] | None = None, error: Exception | None = None):
        self.seen: dict[str, Any] = {}
        self._content = content or []
        self._error = error
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self.seen = kwargs
        if self._error is not None:
            raise self._error
        return SimpleNamespace(content=self._content)


def _tool_block(data: Any) -> SimpleNamespace:  # noqa: ANN401
    return SimpleNamespace(type="tool_use", name="t", input=data)


def _client(sdk: _FakeSdk) -> AnthropicClient:
    return AnthropicClient(ApiKey("sk-ant-SECRETSECRET"), sdk=sdk)


def test_call_json_forces_the_tool_sends_no_sampling_parameters_and_returns_its_input() -> None:
    sdk = _FakeSdk(content=[SimpleNamespace(type="text", text="hi"), _tool_block({"a": 1})])
    assert _client(sdk).call_json(**REQUEST) == {"a": 1}
    assert not {"temperature", "top_p", "top_k"} & set(sdk.seen)  # rejected by current models
    assert sdk.seen["model"] == "m"
    assert sdk.seen["tool_choice"] == {"type": "tool", "name": "t"}
    assert sdk.seen["tools"][0]["input_schema"] == {"type": "object"}
    assert sdk.seen["max_tokens"] == 100


@pytest.mark.parametrize(
    "content",
    [[], [SimpleNamespace(type="text", text="no tool")], [_tool_block(["not", "a", "dict"])]],
)
def test_a_response_without_a_dict_tool_input_is_an_output_error(content: list[Any]) -> None:
    with pytest.raises(LlmOutputError):
        _client(_FakeSdk(content=content)).call_json(**REQUEST)


def _http_error(cls: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.test/v1/messages")
    response = httpx.Response(status, request=request)
    return cls("boom", response=response, body=None)


@pytest.mark.parametrize(
    "error",
    [
        _http_error(anthropic.RateLimitError, 429),
        _http_error(anthropic.InternalServerError, 500),
        anthropic.APITimeoutError(httpx.Request("POST", "https://api.test")),
        anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.test")),
    ],
)
def test_timeouts_rate_limits_and_server_errors_are_transient(error: Exception) -> None:
    with pytest.raises(LlmTransientError):
        _client(_FakeSdk(error=error)).call_json(**REQUEST)


def test_other_http_errors_are_plain_errors_and_never_leak_the_key() -> None:
    error = _http_error(anthropic.AuthenticationError, 401)
    with pytest.raises(LlmError) as info:
        _client(_FakeSdk(error=error)).call_json(**REQUEST)
    assert not isinstance(info.value, LlmTransientError)
    assert "SECRETSECRET" not in str(info.value) + repr(info.value)


def test_make_client_is_none_without_a_key(work_dir: Path) -> None:
    assert make_client() is None


def test_make_client_builds_a_client_when_a_key_exists(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRETSECRET")
    client = make_client()
    assert isinstance(client, AnthropicClient)
    assert "SECRETSECRET" not in repr(client)


# --- rate-limit-aware retries (PRO-6) -----------------------------------------------------------


def _limited(wait: float | None) -> LlmTransientError:
    return LlmTransientError("HTTP 429", wait)


def test_a_server_stated_wait_is_honoured_for_up_to_five_retries() -> None:
    client = _Scripted(*(_limited(7.0) for _ in range(5)), {"ok": True})
    sleeps: list[float] = []
    assert call_with_retries(client, sleep=sleeps.append, **REQUEST) == {"ok": True}
    assert sleeps == [7.0] * 5


def test_a_server_stated_wait_that_keeps_coming_is_raised_after_five_retries() -> None:
    client = _Scripted(*(_limited(2.0) for _ in range(6)))
    with pytest.raises(LlmTransientError):
        call_with_retries(client, sleep=lambda _s: None, **REQUEST)
    assert client.attempts == 6


@pytest.mark.parametrize(("asked", "waited"), [(0.0, 1.0), (0.2, 1.0), (30.0, 30.0), (600.0, 60.0)])
def test_the_wait_is_kept_between_one_and_sixty_seconds(asked: float, waited: float) -> None:
    client = _Scripted(_limited(asked), {"ok": True})
    sleeps: list[float] = []
    call_with_retries(client, sleep=sleeps.append, **REQUEST)
    assert sleeps == [waited]


def test_an_error_without_a_stated_wait_keeps_the_fixed_schedule() -> None:
    client = _Scripted(_limited(None), _limited(None), _limited(None))
    sleeps: list[float] = []
    with pytest.raises(LlmTransientError):
        call_with_retries(client, sleep=sleeps.append, **REQUEST)
    assert sleeps == [1.0, 4.0]


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_a_gate_makes_later_callers_wait_out_the_pause() -> None:
    clock = _Clock()
    gate = PauseGate(clock=clock.time, sleep=clock.sleep)
    gate.wait()
    assert clock.slept == []
    gate.pause(10.0)
    gate.pause(3.0)  # never shortens an existing pause
    gate.wait()
    assert clock.slept == [10.0]
    gate.wait()
    assert clock.slept == [10.0]


def test_with_a_gate_the_pause_replaces_the_per_call_sleep() -> None:
    clock = _Clock()
    gate = PauseGate(clock=clock.time, sleep=clock.sleep)
    client = _Scripted(_limited(5.0), {"ok": True})
    sleeps: list[float] = []
    call_with_retries(client, sleep=sleeps.append, gate=gate, **REQUEST)
    assert sleeps == [] and clock.slept == [5.0]


def _status_error(status: int, headers: dict[str, str]) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.test/v1/messages")
    cls = anthropic.RateLimitError if status == 429 else anthropic.InternalServerError
    return cls("boom", response=httpx.Response(status, request=request, headers=headers), body=None)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [({"retry-after": "12"}, 12.0), ({"retry-after": "2.5"}, 2.5), ({}, None),
     ({"retry-after": "soon"}, None), ({"retry-after": "-3"}, None)],
)  # fmt: skip
def test_the_retry_after_header_becomes_the_requested_wait(
    headers: dict[str, str], expected: float | None
) -> None:
    error = _status_error(429, headers)
    assert retry_after_seconds(error) == expected
    with pytest.raises(LlmTransientError) as info:
        _client(_FakeSdk(error=error)).call_json(**REQUEST)
    assert info.value.retry_after == expected
