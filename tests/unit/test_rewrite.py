"""Query rewriting for the slow path: alternative search queries, never required (Step 10)."""

from __future__ import annotations

from typing import Any

from kk.llm.client import LlmError, LlmTransientError
from kk.query.retrieve import fuse_rankings
from kk.query.rewrite import MAX_QUERIES, QueryRewriter
from tests.fixtures.llm import FakeLlm, failing


def _llm(queries: Any) -> FakeLlm:  # noqa: ANN401
    return FakeLlm(lambda _r: {"queries": queries})


def test_alternative_queries_are_returned_without_the_original_or_duplicates() -> None:
    llm = _llm(["home loan rates", "Home Loan Rates", "mortgage rates?", "  ", "mortgage rates?"])
    rewriter = QueryRewriter(llm, "m")
    assert rewriter.rewrite("mortgage rates?") == ["home loan rates"]


def test_at_most_three_queries_of_bounded_length_are_kept() -> None:
    rewriter = QueryRewriter(_llm([f"query {i}" for i in range(10)] + ["x" * 500]), "m")
    got = rewriter.rewrite("original")
    assert len(got) == MAX_QUERIES == 3 and all(len(q) <= 200 for q in got)


def test_a_failure_or_no_client_gives_no_queries() -> None:
    assert QueryRewriter(None, "m").rewrite("q") == []
    assert QueryRewriter(FakeLlm(failing(LlmError("HTTP 400"))), "m").rewrite("q") == []
    assert QueryRewriter(FakeLlm(failing(LlmTransientError("t"))), "m").rewrite("q") == []
    assert QueryRewriter(_llm("not a list"), "m").rewrite("q") == []
    assert QueryRewriter(_llm([1, None]), "m").rewrite("q") == []


def test_the_same_question_is_rewritten_once() -> None:
    llm = _llm(["a b"])
    rewriter = QueryRewriter(llm, "m")
    rewriter.rewrite("same question")
    rewriter.rewrite("same question")
    assert len(llm.calls) == 1


def test_the_call_has_a_short_timeout_and_no_retry() -> None:
    llm = _llm(["a b"])
    QueryRewriter(llm, "m", timeout_s=3.0).rewrite("q")
    assert llm.calls[0]["timeout_s"] == 3.0 and len(llm.calls) == 1


def test_rankings_are_fused_by_reciprocal_rank() -> None:
    # chunk "b" is second in both lists and beats "a" (first in one) and "c" (first in the other)
    fused = fuse_rankings([["a", "b"], ["c", "b"]], rrf_k=60, limit=3)
    assert fused[0] == "b"
    assert set(fused) == {"a", "b", "c"}
    assert fuse_rankings([["a", "b", "c"]], rrf_k=60, limit=2) == ["a", "b"]
