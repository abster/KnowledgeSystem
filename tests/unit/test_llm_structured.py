"""Structured output for the slow model (Step 10): no forced tool call, JSON from a text block."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kk.llm.client import AnthropicClient, LlmError, LlmOutputError
from kk.llm.key import ApiKey

SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}}


class _Sdk:
    def __init__(self, response: object) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **request: Any) -> object:  # noqa: ANN401
        self.requests.append(request)
        return self.response


def _reply(text: str, stop_reason: str = "end_turn") -> object:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason
    )


def _client(sdk: _Sdk) -> AnthropicClient:
    return AnthropicClient(ApiKey("k"), sdk=sdk)


def _call(client: AnthropicClient) -> dict[str, Any]:
    return client.call_structured(
        model="m", system="s", user="u", schema=SCHEMA, max_tokens=50, timeout_s=3.0
    )


def test_the_request_uses_output_config_and_no_forced_tool_choice() -> None:
    sdk = _Sdk(_reply('{"x": "1"}'))
    assert _call(_client(sdk)) == {"x": "1"}
    request = sdk.requests[0]
    assert request["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
    assert "tool_choice" not in request
    assert "tools" not in request
    assert "temperature" not in request
    assert request["timeout"] == 3.0


def test_effort_goes_into_output_config_when_given() -> None:
    sdk = _Sdk(_reply('{"x": "1"}'))
    _client(sdk).call_structured(
        model="m", system="s", user="u", schema=SCHEMA, max_tokens=50, timeout_s=3.0, effort="low"
    )
    assert sdk.requests[0]["output_config"]["effort"] == "low"
    assert sdk.requests[0]["output_config"]["format"]["type"] == "json_schema"


def test_a_refusal_stop_reason_is_an_output_error() -> None:
    with pytest.raises(LlmOutputError):
        _call(_client(_Sdk(_reply("", stop_reason="refusal"))))


def test_text_that_is_not_json_is_an_output_error() -> None:
    with pytest.raises(LlmOutputError):
        _call(_client(_Sdk(_reply("not json"))))


def test_json_that_is_not_an_object_is_an_output_error() -> None:
    with pytest.raises(LlmOutputError):
        _call(_client(_Sdk(_reply("[1]"))))


def test_a_response_without_text_is_an_output_error() -> None:
    response = SimpleNamespace(content=[], stop_reason="end_turn")
    with pytest.raises(LlmOutputError):
        _call(_client(_Sdk(response)))


def test_a_max_tokens_stop_is_an_output_error() -> None:
    with pytest.raises(LlmOutputError):
        _call(_client(_Sdk(_reply('{"x": "1"', stop_reason="max_tokens"))))


def test_sdk_errors_map_like_call_json() -> None:
    import anthropic  # noqa: PLC0415
    import httpx  # noqa: PLC0415

    class _Boom:
        messages = SimpleNamespace(
            create=lambda **_r: (_ for _ in ()).throw(
                anthropic.APIStatusError(
                    "x",
                    response=httpx.Response(400, request=httpx.Request("POST", "http://x")),
                    body=None,
                )
            )
        )

    with pytest.raises(LlmError) as info:
        _call(AnthropicClient(ApiKey("k"), sdk=_Boom()))
    assert "HTTP 400" in str(info.value)
