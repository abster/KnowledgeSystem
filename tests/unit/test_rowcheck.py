"""Ingest-time verification of row-label parses (TBL-12)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kk.ingest.extract_cache import ExtractionCache
from kk.ingest.prompts import extract as prompts
from kk.ingest.rowcheck import RowLabelVerifier
from kk.ingest.tables import RowParse
from kk.llm.client import LlmTransientError
from tests.fixtures.llm import FakeLlm, failing

GOOD = RowParse("Purchase: 30-Year Fixed", (), "mortgage", '{"term":360}')
BAD = RowParse("Share Savings Plus", ("$25,000+",), "share", '{"term":12}')


def verifier(client: FakeLlm | None, tmp_path: Path) -> RowLabelVerifier:
    return RowLabelVerifier(
        client=client,
        cache=ExtractionCache(tmp_path / "cache"),
        model="haiku",
        concurrency=2,
        sleep=lambda _s: None,
    )


def verdicts(bad: RowParse) -> Any:  # noqa: ANN401
    return lambda request: {"correct": bad.label not in request["user"]}


def test_a_no_rejects_the_parse_and_a_yes_keeps_it(tmp_path: Path) -> None:
    result = verifier(FakeLlm(verdicts(BAD)), tmp_path).verify([GOOD, BAD])
    assert result.rejected == frozenset({BAD})
    assert result.checked == 2 and result.calls == 2


def test_each_distinct_parse_is_asked_once_and_the_request_carries_the_reading(
    tmp_path: Path,
) -> None:
    client = FakeLlm()
    verifier(client, tmp_path).verify([GOOD, GOOD, GOOD])
    (request,) = client.calls
    assert request["tool_name"] == prompts.VERIFY_TOOL
    assert request["model"] == "haiku"
    for part in ("Purchase: 30-Year Fixed", "mortgage", '{"term":360}'):
        assert part in request["user"]


def test_verdicts_are_cached_so_a_second_run_makes_no_calls(tmp_path: Path) -> None:
    client = FakeLlm(verdicts(BAD))
    verifier(client, tmp_path).verify([GOOD, BAD])
    again = verifier(client, tmp_path).verify([GOOD, BAD])
    assert len(client.calls) == 2
    assert again.rejected == frozenset({BAD}) and again.cache_hits == 2 and again.calls == 0


def test_a_failed_call_keeps_the_parse_and_is_not_cached(tmp_path: Path) -> None:
    result = verifier(FakeLlm(failing(LlmTransientError("rate limit"))), tmp_path).verify([BAD])
    assert result.rejected == frozenset() and result.failures == 1
    assert len(ExtractionCache(tmp_path / "cache")) == 0


def test_a_malformed_verdict_keeps_the_parse_and_is_not_cached(tmp_path: Path) -> None:
    result = verifier(FakeLlm(lambda _r: {"correct": "maybe"}), tmp_path).verify([BAD])
    assert result.rejected == frozenset() and result.failures == 1
    assert len(ExtractionCache(tmp_path / "cache")) == 0


def test_without_a_key_or_a_cached_answer_the_step_is_skipped(tmp_path: Path) -> None:
    result = verifier(None, tmp_path).verify([GOOD, BAD])
    assert result.rejected == frozenset() and result.skipped_no_key == 2 and result.calls == 0


def test_without_a_key_a_cached_no_still_quarantines(tmp_path: Path) -> None:
    verifier(FakeLlm(verdicts(BAD)), tmp_path).verify([BAD])
    assert verifier(None, tmp_path).verify([BAD]).rejected == frozenset({BAD})
