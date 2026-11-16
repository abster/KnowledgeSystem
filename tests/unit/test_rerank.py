"""Second-stage reranking for the slow path: a cheap model orders the candidates (Step 10)."""

from __future__ import annotations

from typing import Any

from kk.llm.client import LlmError, LlmTransientError
from kk.query.rerank import Reranker
from tests.fixtures.llm import FakeLlm, failing

CANDIDATES = [(i, f"snippet {i}") for i in range(1, 9)]


def _llm(ids: Any) -> FakeLlm:  # noqa: ANN401
    return FakeLlm(lambda _r: {"ids": ids})


def test_the_picked_candidates_come_back_in_the_models_order() -> None:
    assert Reranker(_llm([5, 2, 7]), "m").rank("q", CANDIDATES, keep=3) == [5, 2, 7]


def test_unknown_duplicate_and_surplus_ids_are_dropped() -> None:
    got = Reranker(_llm([5, 99, 5, "x", 2, 7, 1]), "m").rank("q", CANDIDATES, keep=3)
    assert got == [5, 2, 7]


def test_a_failure_or_no_client_picks_nothing() -> None:
    assert Reranker(None, "m").rank("q", CANDIDATES, keep=3) == []
    assert Reranker(FakeLlm(failing(LlmError("HTTP 400"))), "m").rank("q", CANDIDATES, keep=3) == []
    assert (
        Reranker(FakeLlm(failing(LlmTransientError("t"))), "m").rank("q", CANDIDATES, keep=3) == []
    )
    assert Reranker(_llm("not a list"), "m").rank("q", CANDIDATES, keep=3) == []
    assert Reranker(_llm([]), "m").rank("q", CANDIDATES, keep=3) == []


def test_no_candidates_means_no_call() -> None:
    llm = _llm([1])
    assert Reranker(llm, "m").rank("q", [], keep=3) == []
    assert llm.calls == []


def test_the_request_lists_every_candidate_with_its_number_and_has_a_timeout() -> None:
    llm = _llm([1])
    Reranker(llm, "m", timeout_s=5.0).rank("what is it", CANDIDATES, keep=3)
    call = llm.calls[0]
    assert call["timeout_s"] == 5.0 and len(llm.calls) == 1
    assert "what is it" in call["user"]
    assert all(f"[{n}] snippet {n}" in call["user"] for n, _ in CANDIDATES)


def test_the_same_question_and_candidates_are_ranked_once() -> None:
    llm = _llm([2])
    ranker = Reranker(llm, "m")
    ranker.rank("q", CANDIDATES, keep=3)
    ranker.rank("q", CANDIDATES, keep=3)
    assert len(llm.calls) == 1
