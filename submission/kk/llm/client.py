"""Anthropic client wrapper: timeouts, retries and structured output (Step 8).

Every failure is mapped to :class:`LlmError`, :class:`LlmTransientError` or
:class:`LlmOutputError`. Messages carry only the exception class or the HTTP status, never the
SDK's text, so the API key cannot reach a log through an error.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

import anthropic

from kk.llm.key import ApiKey, load_key

RETRY_DELAYS: tuple[float, ...] = (1.0, 4.0)  # PRO-6: deterministic, jitter-free
MAX_RETRY_AFTER_S = 60.0  # longest wait honoured for one retry
MIN_RETRY_AFTER_S = 1.0  # shortest wait when the server asks for one
MAX_SERVER_RETRIES = 5  # retries allowed when the server states a wait
_RATE_LIMIT = 429
_SERVER_ERROR = 500


class LlmError(Exception):
    """A call failed in a way that retrying will not fix."""


class LlmTransientError(LlmError):
    """Timeout, rate limit, connection or server error: retrying may help.

    ``retry_after`` is the wait in seconds that the server asked for (``retry-after`` header).
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        """Create the error with an optional server-requested wait."""
        super().__init__(message)
        self.retry_after = retry_after


class LlmOutputError(LlmError):
    """The response was not the structured output that was asked for."""


class PauseGate:
    """A shared pause: when one worker is told to wait, every worker waits (PRO-6)."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create an open gate."""
        self._clock = clock
        self._sleep = sleep
        self._until = 0.0
        self._lock = threading.Lock()

    def pause(self, seconds: float) -> None:
        """Close the gate for ``seconds`` from now (never shortens an existing pause)."""
        with self._lock:
            self._until = max(self._until, self._clock() + seconds)

    def wait(self) -> None:
        """Return when the gate is open."""
        while True:
            with self._lock:
                remaining = self._until - self._clock()
            if remaining <= 0:
                return
            self._sleep(remaining)


class LlmClient(Protocol):
    """One structured-output call (forced tool use).

    No ``temperature`` is sent: current models reject sampling parameters (HTTP 400), so
    repeatability comes from the extraction cache, not from the sampler.
    """

    def call_json(  # noqa: PLR0913
        self,
        *,
        model: str,
        system: str,
        user: str,
        tool_name: str,
        schema: Mapping[str, Any],
        max_tokens: int,
        timeout_s: float,
    ) -> dict[str, Any]:
        """Return the tool input as a dict, or raise an :class:`LlmError`."""
        ...


@runtime_checkable
class StructuredLlmClient(Protocol):
    """One call whose reply is JSON constrained by a schema (the slow model, Step 10).

    The slow model rejects a forced ``tool_choice``, so the schema goes in
    ``output_config.format`` and the JSON comes back as text.
    """

    def call_structured(  # noqa: PLR0913
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: Mapping[str, Any],
        max_tokens: int,
        timeout_s: float,
        effort: str | None = None,
    ) -> dict[str, Any]:
        """Return the JSON object, or raise an :class:`LlmError`."""
        ...


def call_with_retries(
    client: LlmClient,
    *,
    delays: tuple[float, ...] = RETRY_DELAYS,
    sleep: Callable[[float], None] = time.sleep,
    gate: PauseGate | None = None,
    **request: Any,  # noqa: ANN401
) -> dict[str, Any]:
    """Call ``client``; retry only transient errors (PRO-6).

    Without a stated wait the schedule ``delays`` applies (two retries). When the server states
    a wait, that wait is honoured (between 1 s and 60 s) for up to five retries, and a ``gate``
    makes every worker wait with it.
    """
    attempt = 0
    while True:
        if gate is not None:
            gate.wait()
        try:
            return client.call_json(**request)
        except LlmTransientError as exc:
            stated = exc.retry_after
            if attempt >= (MAX_SERVER_RETRIES if stated is not None else len(delays)):
                raise
            wait = (
                min(max(stated, MIN_RETRY_AFTER_S), MAX_RETRY_AFTER_S)
                if stated is not None
                else delays[attempt]
            )
            if gate is not None and stated is not None:
                gate.pause(wait)
            else:
                sleep(wait)
            attempt += 1


def retry_after_seconds(error: anthropic.APIStatusError) -> float | None:
    """The ``retry-after`` header of an error response in seconds, or ``None``."""
    raw = error.response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return seconds if seconds >= 0 and math.isfinite(seconds) else None


class AnthropicClient:
    """The real client, built on the ``anthropic`` SDK with SDK retries off."""

    def __init__(self, api_key: ApiKey, sdk: Any = None) -> None:  # noqa: ANN401
        """Create the SDK client from ``api_key`` unless ``sdk`` is given (tests)."""
        self._sdk = (
            sdk if sdk is not None else anthropic.Anthropic(api_key=api_key.reveal(), max_retries=0)
        )

    def __repr__(self) -> str:
        """Never shows the SDK client, which holds the key."""
        return "AnthropicClient()"

    def _create(self, **request: Any) -> Any:  # noqa: ANN401
        """``messages.create`` with every failure mapped to an :class:`LlmError`."""
        try:
            return self._sdk.messages.create(**request)
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as exc:
            raise LlmTransientError(type(exc).__name__) from exc
        except anthropic.APIStatusError as exc:
            message = f"HTTP {exc.status_code}"
            if exc.status_code == _RATE_LIMIT or exc.status_code >= _SERVER_ERROR:
                raise LlmTransientError(message, retry_after_seconds(exc)) from exc
            raise LlmError(message) from exc
        except anthropic.AnthropicError as exc:
            raise LlmError(type(exc).__name__) from exc

    def call_json(  # noqa: PLR0913
        self,
        *,
        model: str,
        system: str,
        user: str,
        tool_name: str,
        schema: Mapping[str, Any],
        max_tokens: int,
        timeout_s: float,
    ) -> dict[str, Any]:
        """Force one tool call and return its input."""
        response = self._create(
            model=model,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[
                {
                    "name": tool_name,
                    "description": "Record the result.",
                    "input_schema": dict(schema),
                }
            ],
            tool_choice={"type": "tool", "name": tool_name},
            max_tokens=max_tokens,
            timeout=timeout_s,
        )
        for block in response.content:
            if (
                getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == tool_name
            ):
                data = getattr(block, "input", None)
                if not isinstance(data, dict):
                    raise LlmOutputError("tool input is not an object")
                return dict(data)
        raise LlmOutputError("no tool call in the response")

    def call_structured(  # noqa: PLR0913
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: Mapping[str, Any],
        max_tokens: int,
        timeout_s: float,
        effort: str | None = None,
    ) -> dict[str, Any]:
        """One call with ``output_config.format``; the reply text is the JSON object."""
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": dict(schema)}}
        if effort is not None:
            output_config["effort"] = effort
        response = self._create(
            model=model,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
            max_tokens=max_tokens,
            timeout=timeout_s,
        )
        stop = getattr(response, "stop_reason", None)
        if stop in ("refusal", "max_tokens"):
            raise LlmOutputError(f"stop reason {stop}")
        text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), None)
        if text is None:
            raise LlmOutputError("no text in the response")
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise LlmOutputError("reply is not JSON") from exc
        if not isinstance(data, dict):
            raise LlmOutputError("reply is not an object")
        return data


def make_client() -> LlmClient | None:
    """A real client if an API key is configured, else ``None`` (SLOW-5, PRO-7)."""
    key = load_key()
    return AnthropicClient(key) if key is not None else None
