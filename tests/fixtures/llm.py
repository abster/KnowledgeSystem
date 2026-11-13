"""A scripted stand-in for the Anthropic client (no network, no key)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from kk.ingest.prompts.extract import EXTRACT_TOOL
from kk.llm.client import LlmError


class FakeLlm:
    """Records every request and answers through ``responder`` (which may raise)."""

    def __init__(
        self,
        responder: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        *,
        delay: float = 0.0,
    ) -> None:
        """Answer with ``{"facts": []}`` for extraction and ``{"correct": True}`` otherwise."""
        self.responder = responder or _default
        self.delay = delay
        self.calls: list[dict[str, Any]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def call_json(self, **request: Any) -> dict[str, Any]:  # noqa: ANN401
        """Record the request and return the scripted answer."""
        with self._lock:
            self.calls.append(request)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                time.sleep(self.delay)
            return self.responder(request)
        finally:
            with self._lock:
                self.in_flight -= 1


def _default(request: Mapping[str, Any]) -> dict[str, Any]:
    if request["tool_name"] == EXTRACT_TOOL:
        return {"facts": []}
    return {"correct": True}


def failing(error: LlmError) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """A responder that always raises ``error``."""

    def respond(_request: dict[str, Any]) -> dict[str, Any]:
        raise error

    return respond
