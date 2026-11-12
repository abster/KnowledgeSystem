"""Validators V1 to V5 (SPEC 14)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from scoring import _numbers  # the harness: dev-only equivalence check

from contract import Answer, Source
from kk.query.render import safe_refusal
from kk.query.validators import (
    ValidationContext,
    amount_numbers,
    check_citations,
    check_grounding,
    check_quotes,
    check_refusal,
    check_stale,
    extract_numbers,
    numbers_of,
    validate,
)

# --- number extraction: the scorer's own ------------------------------------------------------


def test_extract_numbers_value_and_unit() -> None:
    assert extract_numbers("5.39% APR for 60 months and $2,500.00") == {
        ("%", 5.39),
        ("", 60.0),
        ("$", 2500.0),
    }
    assert extract_numbers("1,234") == {("", 1234.0)}


@pytest.mark.parametrize("text", ["3.5%", "3.50 %", "3.5 percent", "3.50%"])
def test_percent_forms_agree(text: str) -> None:
    assert extract_numbers(text) == {("%", 3.5)}


def test_extract_numbers_of_nothing() -> None:
    assert extract_numbers("") == set()
    assert extract_numbers("no digits here") == set()


@given(st.text())
def test_extraction_matches_the_scorer_exactly(text: str) -> None:
    assert extract_numbers(text) == _numbers(text)


@pytest.mark.parametrize(
    "text",
    [
        "The auto loan APR for a 60-month term is 5.39%.",
        "Yes - the APY was 3.50% as of 2026-06-09 and had changed to 3.90% by 2026-08-27.",
        "$100k and 1.5k and 3.5x and 12abc",
        "(5.39 %) -> $ 7 / 1 ARM",
        "0.10%1 and 1,23,4 and 1,234,567.89",
    ],
)
def test_extraction_matches_the_scorer_on_samples(text: str) -> None:
    assert extract_numbers(text) == _numbers(text)


def test_numbers_of_unions_and_amount_numbers() -> None:
    assert numbers_of(["5.39%", "60 months"]) == {("%", 5.39), ("", 60.0)}
    assert numbers_of([]) == frozenset()
    assert amount_numbers(Decimal("100000")) == {("$", 100000.0), ("", 100000.0)}
    assert amount_numbers(Decimal("2.50")) == {("$", 2.5), ("", 2.5)}


# --- V1 ---------------------------------------------------------------------------------------

ALLOWED = frozenset({("%", 5.39), ("", 60.0), ("", 2024.0), ("", 27.0), ("", 2025.0)})  # dup-016


def test_v1_accepts_grounded_numbers() -> None:
    assert check_grounding("The APR is 5.39% for a 60-month term.", ALLOWED)


def test_v1_rejects_the_stale_value() -> None:
    assert not check_grounding("The APR is 5.34% for a 60-month term.", ALLOWED)


def test_v1_rejects_an_ungrounded_number() -> None:
    assert not check_grounding("The APR is 5.39% and the fee is $25.", ALLOWED)


def test_v1_text_without_numbers_passes() -> None:
    assert check_grounding("Please contact Dupaco.", frozenset())


def test_v1_unit_must_match_unless_the_number_is_bare() -> None:
    assert not check_grounding("It costs $5.", numbers_of(["5%"]))
    assert check_grounding("It is 5.", numbers_of(["5%"]))  # a bare number matches by value
    assert not check_grounding("It is 6.", numbers_of(["5%"]))


def test_v1_amounts_from_qualifiers() -> None:
    allowed = amount_numbers(Decimal("100000")) | numbers_of(["7.13%"])
    assert check_grounding("For loans of $100,000 or less the rate is 7.13%.", allowed)


# --- V2 ---------------------------------------------------------------------------------------

LOSERS = frozenset({("%", 5.34)})
WINNERS = frozenset({("%", 5.39)})


def test_v2_rejects_a_loser() -> None:
    assert not check_stale("The APR is 5.34%.", LOSERS, WINNERS)


def test_v2_accepts_the_winner() -> None:
    assert check_stale("The APR is 5.39%.", LOSERS, WINNERS)


def test_v2_a_loser_that_is_also_a_winner_for_another_key_passes() -> None:
    assert check_stale("The APR is 5.34%.", LOSERS, numbers_of(["5.34%"]))


def test_v2_nothing_known_passes() -> None:
    assert check_stale("The APR is 5.34%.", frozenset(), frozenset())


# --- V3 ---------------------------------------------------------------------------------------


def test_v3_accepts_the_safe_refusal() -> None:
    q = "What is the rate for a 7/1 ARM?"
    assert check_refusal(safe_refusal(q, "ARM 7"), q)


def test_v3_rejects_a_confident_refusal() -> None:
    q = "What is the rate?"
    answer = safe_refusal(q)
    answer.confident = True
    assert not check_refusal(answer, q)


def test_v3_rejects_sources() -> None:
    q = "What is the rate?"
    answer = safe_refusal(q)
    answer.sources = [Source(url="https://x.test/a")]
    assert not check_refusal(answer, q)


def test_v3_follows_the_scorer_on_units() -> None:
    # the scorer compares (unit, value): a bare 5 is not the question's 5 percent
    q = "What is the 5% rate?"
    assert not check_refusal(Answer(text="I couldn't find 5.", sources=[], confident=False), q)
    assert check_refusal(Answer(text="I couldn't find 5%.", sources=[], confident=False), q)


def test_v3_rejects_a_digit_that_is_not_in_the_question() -> None:
    q = "What is the rate for a 7/1 ARM?"
    answer = Answer(text="I couldn't find 3 on Dupaco's website.", sources=[], confident=False)
    assert not check_refusal(answer, q)
    ok = Answer(text="I couldn't find 7 on Dupaco's website.", sources=[], confident=False)
    assert check_refusal(ok, q)


@given(st.text(), st.one_of(st.none(), st.text()))
def test_v3_the_safe_refusal_always_passes(question: str, label: str | None) -> None:
    assert check_refusal(safe_refusal(question, label), question)


# --- V4 and V5 --------------------------------------------------------------------------------


def _answer(*urls: str) -> Answer:
    return Answer(text="x", sources=[Source(url=u) for u in urls], confident=True)


def test_v4_cited_urls_must_be_evidence() -> None:
    evidence = frozenset({"https://x.test/a", "https://x.test/b"})
    assert check_citations(_answer("https://x.test/a"), evidence)
    assert not check_citations(_answer("https://x.test/c"), evidence)
    assert not check_citations(_answer("https://x.test/a", "https://x.test/c"), evidence)
    assert check_citations(_answer("https://x.test/a/"), evidence)  # trailing slash only
    assert check_citations(_answer(), evidence)


def test_v5_quote_must_be_verbatim() -> None:
    chunk = "Today the rate is  5.39%\nfor all members."
    assert check_quotes([("the rate is 5.39% for all", chunk)])  # whitespace runs collapse
    assert check_quotes([("the rate is 5.39%", chunk), ("for all members.", chunk)])
    assert not check_quotes([("the rate is 5.34%", chunk)])
    assert not check_quotes([("the rate is 5.39% for all", chunk), ("not here", chunk)])
    assert not check_quotes([("", chunk)])
    assert not check_quotes([("   ", chunk)])
    assert check_quotes([])


# --- validate() -------------------------------------------------------------------------------


def test_validate_passes_a_grounded_answer() -> None:
    ctx = ValidationContext(
        question="What is the APR for a 60-month auto loan?",
        allowed=ALLOWED,
        losers=LOSERS,
        winners=WINNERS,
        evidence_urls=frozenset({"https://x.test/a"}),
        quotes=(("APR 5.39%", "the APR 5.39% applies"),),
    )
    answer = Answer(
        text="The auto loan APR for a 60-month term is 5.39%.",
        sources=[Source(url="https://x.test/a", quote="APR 5.39%")],
        confident=True,
    )
    result = validate(answer, ctx)
    assert result.ok
    assert result.failed == ()


def test_validate_reports_every_failure_in_order() -> None:
    ctx = ValidationContext(
        question="q",
        allowed=ALLOWED,
        losers=LOSERS,
        winners=WINNERS,
        evidence_urls=frozenset({"https://x.test/a"}),
        quotes=(("nope", "chunk"),),
    )
    answer = Answer(text="The APR is 5.34%.", sources=[Source(url="https://x.test/z")])
    result = validate(answer, ctx)
    assert not result.ok
    assert result.failed == ("V1", "V2", "V4", "V5")


def test_validate_relaxes_v2_for_history_answers() -> None:
    allowed = numbers_of(["5.34%", "5.39%"])
    base = {"question": "q", "allowed": allowed, "losers": LOSERS, "winners": WINNERS}
    answer = Answer(text="It was 5.34% and is now 5.39%.", sources=[], confident=True)
    assert validate(answer, ValidationContext(**base)).failed == ("V2",)  # type: ignore[arg-type]
    assert validate(answer, ValidationContext(**base, relax_stale=True)).ok  # type: ignore[arg-type]


def test_validate_a_refusal_is_checked_by_v3_only() -> None:
    q = "What is the rate for a 7/1 ARM?"
    ctx = ValidationContext(question=q, refusal=True)
    assert validate(safe_refusal(q), ctx).ok
    bad = Answer(text="I couldn't find 9.", sources=[], confident=False)
    assert validate(bad, ctx).failed == ("V3",)
    confident = Answer(text="Sure.", sources=[], confident=True)
    assert validate(confident, ctx).failed == ("V3",)
