"""Prose fact extraction: candidates, validation, caching and failure handling (PRO-1 to PRO-8)."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from kk.ingest.extract_cache import ExtractionCache, cache_key
from kk.ingest.prompts import extract as prompts
from kk.ingest.prose import ProseExtractor, is_candidate, validate_facts
from kk.ingest.tables import make_fact_key
from kk.llm.client import LlmError, LlmOutputError, LlmTransientError
from kk.types import Chunk
from tests.fixtures.llm import FakeLlm, failing

RATE_TEXT = "The 30-year fixed mortgage rate is 7.13% for loans of $100,000 or less."
RATE_QUOTE = "30-year fixed mortgage rate is 7.13%"


def make_chunk(
    text: str = RATE_TEXT,
    *,
    chunk_type: str = "text_paragraph",
    headers: tuple[str, ...] = ("Home Loans", "Rates"),
    chunk_id: str = "c1",
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        url="https://example.test/loans",
        file_name="loans",
        title="Home Loans",
        parent_headers=headers,
        chunk_type=chunk_type,  # type: ignore[arg-type]
        parent_id=None,
        text=text,
        effective_date="2026-06-02",
        expiry_date=None,
        source_class="product_page",
        authority_rank=6,
        fetched_at="2026-06-02T00:00:00Z",
        position=0,
    )


def fact(**changes: Any) -> dict[str, Any]:  # noqa: ANN401
    base: dict[str, Any] = {
        "entity": "Mortgage",
        "attribute": "rate",
        "qualifiers": {"term": "30-year", "rate_type": "fixed"},
        "value": "7.13%",
        "unit": "percent",
        "quote": RATE_QUOTE,
    }
    return {**base, **changes}


def answer(*facts: dict[str, Any]) -> Any:  # noqa: ANN401
    return lambda _request: {"facts": list(facts)}


def extractor(
    client: FakeLlm | None,
    tmp_path: Path,
    *,
    concurrency: int = 4,
    sleeps: list[float] | None = None,
) -> ProseExtractor:
    return ProseExtractor(
        client=client,
        cache=ExtractionCache(tmp_path / "cache"),
        model="haiku",
        concurrency=concurrency,
        sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
    )


# --- PRO-1 candidates ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Rates start at 4.5% today.",
        "The annual fee is $25.",
        "Visit us at 123 Main Street.",
        "Our lobby is open Monday to Friday.",
        "Members are eligible after joining.",
        "Call the branch about the fee schedule.",
        "Rates may be 3 percent",
    ],
)
def test_prose_with_a_digit_a_currency_sign_or_a_cue_word_is_a_candidate(text: str) -> None:
    assert is_candidate(make_chunk(text))
    assert is_candidate(make_chunk(text, chunk_type="list_item"))


@pytest.mark.parametrize("text", ["Welcome to our credit union.", "Banking made simple for you."])
def test_plain_prose_is_not_a_candidate(text: str) -> None:
    assert not is_candidate(make_chunk(text))


@pytest.mark.parametrize("chunk_type", ["table", "table_row"])
def test_table_chunks_are_never_candidates(chunk_type: str) -> None:
    assert not is_candidate(make_chunk(RATE_TEXT, chunk_type=chunk_type))


# --- PRO-3, PRO-4 validation -----------------------------------------------------------------


def test_a_valid_fact_is_normalised_into_a_prose_llm_fact() -> None:
    facts, dropped = validate_facts({"facts": [fact()]}, make_chunk())
    assert dropped == 0
    (f,) = facts
    assert f.origin == "prose_llm"
    assert (f.entity, f.entity_label) == ("mortgage", "Mortgage")
    assert f.attribute == "rate"
    assert json.loads(f.qualifiers) == {"rate_type": "fixed", "term": 360}
    assert list(json.loads(f.qualifiers)) == sorted(json.loads(f.qualifiers))
    assert f.fact_key == make_fact_key("mortgage", "rate", f.qualifiers)
    assert (f.value_text, f.value_num, f.unit, f.none_value) == (
        "7.13%",
        Decimal("7.13"),
        "percent",
        False,
    )
    assert f.quote == RATE_QUOTE
    assert (f.chunk_id, f.effective_date, f.expiry_date, f.authority_rank) == (
        "c1",
        "2026-06-02",
        None,
        6,
    )
    assert f.entity_inferred is False


def test_amount_and_model_year_qualifiers_use_the_shared_normaliser() -> None:
    text = "Auto loans for 2024-2027 model vehicles cost 5.5% on balances of $100,000 or less."
    quote = "cost 5.5% on balances of $100,000 or less"
    item = fact(
        entity="Auto Loan",
        qualifiers={"model_year": "2024-2027 model", "amount_tier": "$100,000 or less"},
        value="5.5%",
        quote=quote,
    )
    (f,), dropped = validate_facts({"facts": [item]}, make_chunk(text))
    assert dropped == 0
    assert json.loads(f.qualifiers) == {"amount_tier": "<=100000", "model_year": "2024-2027"}


def test_text_attributes_keep_the_value_as_written() -> None:
    text = "Our lobby is open Mon-Fri 9:00 a.m. to 5:00 p.m."
    item = fact(
        entity="Lobby", attribute="hours", qualifiers={}, unit=None,
        value="Mon-Fri 9:00 a.m. to 5:00 p.m.", quote="open Mon-Fri 9:00 a.m. to 5:00 p.m.",
    )  # fmt: skip
    (f,), _ = validate_facts({"facts": [item]}, make_chunk(text))
    assert (f.attribute, f.value_num, f.unit) == ("hours", None, None)
    assert f.value_text == "Mon-Fri 9:00 a.m. to 5:00 p.m."


@pytest.mark.parametrize(
    ("label", "item"),
    [
        ("attribute outside the vocabulary", fact(attribute="colour")),
        ("quote not verbatim", fact(quote="30 year fixed mortgage rate is 7.13 percent")),
        ("number not in the quote", fact(value="7.31%")),
        ("percent attribute with a money value", fact(attribute="rate", value="$100,000")),
        ("unknown qualifier key", fact(qualifiers={"colour": "red"})),
        ("unparseable qualifier", fact(qualifiers={"term": "a while"})),
        ("empty entity", fact(entity="  ")),
        ("empty value", fact(value="")),
        ("not an object", "just text"),
        ("missing field", {"entity": "Mortgage"}),
    ],
)
def test_a_fact_failing_any_rule_is_dropped_and_counted(label: str, item: Any) -> None:  # noqa: ANN401
    facts, dropped = validate_facts({"facts": [item]}, make_chunk())
    assert (facts, dropped) == ([], 1), label


def test_one_bad_fact_does_not_hide_a_good_one_and_duplicates_collapse() -> None:
    facts, dropped = validate_facts(
        {"facts": [fact(), fact(), fact(attribute="colour")]}, make_chunk()
    )
    assert len(facts) == 1
    assert dropped == 1


@pytest.mark.parametrize("raw", [None, [], "x", {"facts": "no"}, {"other": []}])
def test_a_response_that_is_not_the_schema_is_rejected(raw: Any) -> None:  # noqa: ANN401
    with pytest.raises(LlmOutputError):
        validate_facts(raw, make_chunk())


# --- PRO-5, PRO-6 calls and the cache --------------------------------------------------------


def test_extract_returns_facts_and_counts(tmp_path: Path) -> None:
    client = FakeLlm(answer(fact()))
    result = extractor(client, tmp_path).extract([make_chunk(), make_chunk("Plain words.")])
    assert [f.value_text for f in result.facts] == ["7.13%"]
    stats = result.stats
    assert (stats.candidates, stats.calls, stats.cache_hits, stats.failures) == (1, 1, 0, 0)
    assert (stats.facts, stats.dropped_facts) == (1, 0)


def test_the_request_carries_text_header_path_vocabulary_and_temperature_zero_inputs(
    tmp_path: Path,
) -> None:
    client = FakeLlm(answer())
    extractor(client, tmp_path).extract([make_chunk()])
    (request,) = client.calls
    assert request["model"] == "haiku"
    assert request["tool_name"] == prompts.EXTRACT_TOOL
    assert request["schema"] == prompts.EXTRACT_SCHEMA
    assert RATE_TEXT in request["user"]
    assert "Home Loans > Rates" in request["user"]
    assert request["system"] == prompts.EXTRACT_SYSTEM


def test_a_second_run_makes_no_calls_and_gives_the_same_facts(tmp_path: Path) -> None:
    client = FakeLlm(answer(fact()))
    first = extractor(client, tmp_path).extract([make_chunk()])
    second = extractor(client, tmp_path).extract([make_chunk()])
    assert len(client.calls) == 1
    assert second.facts == first.facts
    assert second.stats.cache_hits == 1 and second.stats.calls == 0


def test_a_result_with_no_facts_is_cached(tmp_path: Path) -> None:
    client = FakeLlm(answer())
    extractor(client, tmp_path).extract([make_chunk()])
    extractor(client, tmp_path).extract([make_chunk()])
    assert len(client.calls) == 1


def test_identical_text_in_one_run_costs_one_call(tmp_path: Path) -> None:
    client = FakeLlm(answer(fact()))
    chunks = [make_chunk(chunk_id="a"), make_chunk(chunk_id="b")]
    result = extractor(client, tmp_path).extract(chunks)
    assert len(client.calls) == 1
    assert {f.chunk_id for f in result.facts} == {"a", "b"}


def test_the_cache_key_follows_the_prompt_version_model_text_and_headers(tmp_path: Path) -> None:
    cache = ExtractionCache(tmp_path / "cache")
    cache.put(
        cache_key(prompts.EXTRACT_VERSION, "haiku", RATE_TEXT, "Home Loans > Rates"),
        json.dumps({"facts": [fact()]}),
    )
    client = FakeLlm(answer())
    hit = ProseExtractor(client=client, cache=cache, model="haiku", concurrency=1)
    assert len(hit.extract([make_chunk()]).facts) == 1 and not client.calls
    other_model = ProseExtractor(client=client, cache=cache, model="other", concurrency=1)
    assert other_model.extract([make_chunk()]).facts == () and len(client.calls) == 1
    other_headers = make_chunk(headers=("Other",))
    ProseExtractor(client=client, cache=cache, model="haiku", concurrency=1).extract(
        [other_headers]
    )
    assert len(client.calls) == 2


def test_the_version_changes_when_the_prompt_or_schema_changes() -> None:
    base = prompts.prompt_version("x", "system", {"a": 1})
    assert base != prompts.prompt_version("x", "system 2", {"a": 1})
    assert base != prompts.prompt_version("x", "system", {"a": 2})
    assert prompts.EXTRACT_VERSION != prompts.VERIFY_VERSION
    assert prompts.EXTRACT_VERSION.startswith("prose-")


@pytest.mark.parametrize(
    "error",
    [
        LlmTransientError("timeout"),
        LlmTransientError("rate limit"),
        LlmOutputError("malformed"),
        LlmError("bad request"),
    ],
)
def test_a_failed_call_gives_no_facts_and_no_cache_entry(tmp_path: Path, error: LlmError) -> None:
    ext = extractor(FakeLlm(failing(error)), tmp_path)
    result = ext.extract([make_chunk()])
    assert result.facts == ()
    assert result.stats.failures == 1
    assert len(ExtractionCache(tmp_path / "cache")) == 0
    client = FakeLlm(answer(fact()))  # the next run retries and succeeds
    assert len(extractor(client, tmp_path).extract([make_chunk()]).facts) == 1


def test_a_response_that_breaks_the_schema_is_a_failure_and_is_not_cached(tmp_path: Path) -> None:
    client = FakeLlm(lambda _r: {"facts": "not a list"})
    result = extractor(client, tmp_path).extract([make_chunk()])
    assert (result.facts, result.stats.failures) == ((), 1)
    assert len(ExtractionCache(tmp_path / "cache")) == 0


def test_transient_errors_are_retried_after_one_and_four_seconds(tmp_path: Path) -> None:
    attempts = {"n": 0}

    def flaky(_request: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise LlmTransientError("rate limit")
        return {"facts": [fact()]}

    sleeps: list[float] = []
    result = extractor(FakeLlm(flaky), tmp_path, sleeps=sleeps).extract([make_chunk()])
    assert len(result.facts) == 1 and sleeps == [1.0, 4.0]
    assert result.stats.failures == 0 and result.stats.calls == 1


def test_calls_run_concurrently_but_never_above_the_limit(tmp_path: Path) -> None:
    client = FakeLlm(answer(), delay=0.05)
    chunks = [make_chunk(f"Fee number {i} is $25.", chunk_id=f"c{i}") for i in range(9)]
    extractor(client, tmp_path, concurrency=3).extract(chunks)
    assert len(client.calls) == 9
    assert 1 < client.max_in_flight <= 3


# --- PRO-7 no key ----------------------------------------------------------------------------


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def warnings_seen() -> Any:  # noqa: ANN401
    handler = _Capture()
    handler.setLevel(logging.WARNING)
    logger = logging.getLogger("kk")
    logger.addHandler(handler)
    yield handler.messages
    logger.removeHandler(handler)


def test_no_key_and_no_cache_gives_no_facts_and_one_warning_per_run(
    tmp_path: Path, warnings_seen: list[str]
) -> None:
    ext = extractor(None, tmp_path)
    chunks = [make_chunk(f"Fee {i} is $25.", chunk_id=f"c{i}") for i in range(5)]
    first = ext.extract(chunks)
    second = ext.extract(chunks)
    assert first.facts == () and first.stats.skipped_no_key == 5 and first.stats.calls == 0
    assert second.stats.skipped_no_key == 5
    assert len([m for m in warnings_seen if "API key" in m]) == 1


def test_no_key_still_serves_cached_results(tmp_path: Path, warnings_seen: list[str]) -> None:
    extractor(FakeLlm(answer(fact())), tmp_path).extract([make_chunk()])
    result = extractor(None, tmp_path).extract([make_chunk()])
    assert len(result.facts) == 1 and result.stats.skipped_no_key == 0
    assert not [m for m in warnings_seen if "API key" in m]


def test_a_rate_limit_with_a_stated_wait_is_waited_out_and_then_succeeds(tmp_path: Path) -> None:
    attempts = {"n": 0}

    def limited(_request: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        if attempts["n"] <= 3:
            raise LlmTransientError("HTTP 429", 3.0)
        return {"facts": [fact()]}

    sleeps: list[float] = []
    result = extractor(FakeLlm(limited), tmp_path, sleeps=sleeps).extract([make_chunk()])
    assert len(result.facts) == 1 and sleeps == [3.0, 3.0, 3.0]
    assert result.stats.failures == 0
