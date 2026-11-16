"""A scripted stand-in for the slow model and a fake clock (Step 10). No network, no key."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kk.ingest.prompts.extract import EXTRACT_TOOL
from kk.query.rerank import RERANK_TOOL
from kk.query.rewrite import REWRITE_TOOL


class FakeClock:
    """Monotonic time that only moves when the code under test sleeps or a call takes time."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """The current time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance time instead of waiting."""
        self.now += seconds


class FakeStructured:
    """Records every request; each call pops the next outcome (a dict, or an exception)."""

    def __init__(
        self,
        *outcomes: dict[str, Any] | Exception,
        clock: FakeClock | None = None,
        call_seconds: float = 0.0,
        rewrites: list[str] | Exception | None = None,
        reranks: Callable[[str], list[int]] | list[int] | Exception | None = None,
    ) -> None:
        """Script the outcomes; the last one repeats. ``rewrites`` answers query rewriting."""
        self.rewrites = rewrites
        self.rewrite_calls = 0
        self.reranks = reranks
        self.rerank_requests: list[str] = []
        self.outcomes = list(outcomes)
        self.clock = clock
        self.call_seconds = call_seconds
        self.calls: list[dict[str, Any]] = []

    def call_structured(self, **request: Any) -> dict[str, Any]:  # noqa: ANN401
        """Record the request and return or raise the next outcome."""
        self.calls.append(request)
        if self.clock is not None:
            self.clock.now += self.call_seconds
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def call_json(self, **request: Any) -> dict[str, Any]:  # noqa: ANN401
        """Rewrite calls get ``rewrites``; ingest calls get an empty, harmless answer."""
        if request.get("tool_name") == REWRITE_TOOL:
            self.rewrite_calls += 1
            if isinstance(self.rewrites, Exception):
                raise self.rewrites
            return {"queries": list(self.rewrites or [])}
        if request.get("tool_name") == RERANK_TOOL:
            user = str(request["user"])
            self.rerank_requests.append(user)
            if isinstance(self.reranks, Exception):
                raise self.reranks
            if callable(self.reranks):
                return {"ids": self.reranks(user)}
            return {"ids": list(self.reranks or [])}
        return {"facts": []} if request.get("tool_name") == EXTRACT_TOOL else {"correct": True}


def accepted(answer: str, *evidence_ids: str) -> dict[str, Any]:
    """A well-formed, answerable model response."""
    return {
        "answerable": True,
        "answer": answer,
        "evidence_ids": list(evidence_ids),
        "loser_values_mentioned": [],
    }
