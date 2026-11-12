"""Span finders added to the normaliser for the question side (SPEC 7.3, 7.4)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from kk.query.normalise import find_amount_constraints, find_model_years

D = Decimal


def _one(text: str):  # type: ignore[no-untyped-def]
    found = find_amount_constraints(text)
    assert len(found) == 1, found
    return found[0]


@pytest.mark.parametrize(
    ("text", "span", "lower", "lower_inc", "upper", "upper_inc"),
    [
        ("loans of $100,000 or less", "$100,000 or less", None, False, D(100000), True),
        ("up to $50,000", "up to $50,000", None, False, D(50000), True),
        ("at most $5k", "at most $5k", None, False, D(5000), True),
        ("no more than $2.50", "no more than $2.50", None, False, D("2.50"), True),
        ("under $100,000", "under $100,000", None, False, D(100000), False),
        ("below $100,000", "below $100,000", None, False, D(100000), False),
        ("less than $100,000", "less than $100,000", None, False, D(100000), False),
        ("over $250,000", "over $250,000", D(250000), False, None, False),
        ("above $250,000", "above $250,000", D(250000), False, None, False),
        ("more than $250,000", "more than $250,000", D(250000), False, None, False),
        ("greater than $250,000", "greater than $250,000", D(250000), False, None, False),
        ("at least $5,000", "at least $5,000", D(5000), True, None, False),
        ("$5,000 or more", "$5,000 or more", D(5000), True, None, False),
        ("$100,000+", "$100,000+", D(100000), True, None, False),
        ("between $5,000 and $10,000", "between $5,000 and $10,000", D(5000), True, D(10000), True),
        ("$5,000 to $10,000", "$5,000 to $10,000", D(5000), True, D(10000), True),
        ("$5,000 - $10,000", "$5,000 - $10,000", D(5000), True, D(10000), True),
        ("$5,000 – $10,000", "$5,000 – $10,000", D(5000), True, D(10000), True),
        ("a $100,000 loan", "$100,000", D(100000), True, D(100000), True),
        ("100,000 dollars", "100,000 dollars", D(100000), True, D(100000), True),
        ("$1.5k", "$1.5k", D(1500), True, D(1500), True),
    ],
)
def test_recognised_amount_phrases(  # noqa: PLR0913, PLR0917
    text: str, span: str, lower: D | None, lower_inc: bool, upper: D | None, upper_inc: bool
) -> None:
    match = _one(text)
    assert text[match.start : match.end] == span
    c = match.constraint
    assert (c.lower, c.upper) == (lower, upper)
    if lower is not None:
        assert c.lower_inclusive is lower_inc
    if upper is not None:
        assert c.upper_inclusive is upper_inc


def test_an_unknown_modifier_stays_outside_the_span() -> None:
    match = _one("roughly $50,000")
    assert "roughly $50,000"[match.start : match.end] == "$50,000"


def test_two_separate_amounts_give_two_matches_in_order() -> None:
    found = find_amount_constraints("$5,000 and $10,000")
    assert [m.constraint.lower for m in found] == [D(5000), D(10000)]
    assert found[0].end <= found[1].start


def test_the_longer_phrase_wins_over_its_own_point() -> None:
    # "up to $5,000" must not also yield a separate point $5,000
    assert len(find_amount_constraints("up to $5,000")) == 1


@pytest.mark.parametrize("text", ["60 months", "what is the rate", "", "5.39%", "2025 model"])
def test_text_without_an_amount_gives_nothing(text: str) -> None:
    assert find_amount_constraints(text) == []


@pytest.mark.parametrize(
    ("text", "span", "first", "last"),
    [
        ("a 2025 model", "2025 model", 2025, 2025),
        ("the 2025 model year", "2025 model year", 2025, 2025),
        ("the 2025 model-year car", "2025 model-year", 2025, 2025),
        ("a '25 model", "'25 model", 2025, 2025),
        ("2024-27 model years", "2024-27 model years", 2024, 2027),
        ("2024-2027 model", "2024-2027 model", 2024, 2027),
        ("2024 - 2027 model year", "2024 - 2027 model year", 2024, 2027),
        ("2024 to 2027 model", "2024 to 2027 model", 2024, 2027),
        ("a 2025 vehicle", "2025 vehicle", 2025, 2025),
        ("the 2024 to 2027 models", "2024 to 2027 models", 2024, 2027),
    ],
)
def test_model_year_next_to_model_or_vehicle(text: str, span: str, first: int, last: int) -> None:
    found = find_model_years(text)
    assert len(found) == 1
    assert text[found[0].start : found[0].end] == span
    assert (found[0].years.first, found[0].years.last) == (first, last)


@pytest.mark.parametrize(
    "text",
    [
        "in 2022",
        "2022",
        "the rate in 2025",
        "2027-24 model",
        "1899 model",
        "3000 model",
        "60 model",
    ],
)
def test_a_year_elsewhere_or_an_invalid_range_is_not_a_model_year(text: str) -> None:
    assert find_model_years(text) == []
