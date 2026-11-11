"""Golden tests for the shared normaliser (SPEC 7.1 to 7.6)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from kk.query.normalise import (
    AmountConstraint,
    ModelYears,
    Quantity,
    clean_text,
    find_dates,
    find_terms,
    fold_dashes,
    format_quantity,
    parse_amount_constraint,
    parse_date,
    parse_model_years,
    parse_quantity,
    parse_term,
)


def _q(kind: str, value: str) -> Quantity:
    return Quantity(kind, Decimal(value))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3.5%", _q("percent", "3.5")),
        ("3.50 %", _q("percent", "3.5")),
        ("3.5 percent", _q("percent", "3.5")),
        ("0.10%1", _q("percent", "0.10")),
        ("0.00%", _q("percent", "0")),
        ("$100,000", _q("usd", "100000")),
        ("$100000", _q("usd", "100000")),
        ("100,000 dollars", _q("usd", "100000")),
        ("$100k", _q("usd", "100000")),
        ("100K", _q("usd", "100000")),
        ("$1.5k", _q("usd", "1500")),
        ("$2.50", _q("usd", "2.5")),
        ("$1.2 million", _q("usd", "1200000")),
        ("$1.2M", _q("usd", "1200000")),
        ("1,234", _q("count", "1234")),
        ("  7 ", _q("count", "7")),
    ],
)
def test_quantity_recognised(text: str, expected: Quantity) -> None:
    assert parse_quantity(text) == expected


@pytest.mark.parametrize(
    "text",
    ["3.5x", "12abc", "", "abc", "$", "%", "5M", "1,23", "3.5", "1.2.3", "$1,2", "three percent"],
)
def test_quantity_not_recognised(text: str) -> None:
    assert parse_quantity(text) is None


def test_format_quantity_is_canonical() -> None:
    assert format_quantity(_q("percent", "3.5")) == "3.5%"
    assert format_quantity(_q("usd", "100000")) == "$100,000"
    assert format_quantity(_q("usd", "2.5")) == "$2.50"
    assert format_quantity(_q("count", "1234")) == "1,234"


@pytest.mark.parametrize(
    ("text", "months"),
    [
        ("five year", 60),
        ("5-year", 60),
        ("5 yr", 60),
        ("5 yrs", 60),
        ("60 months", 60),
        ("six-month", 6),
        ("6 months", 6),
        ("6 mo", 6),
        ("17-Month", 17),
        ("30-Year", 360),
        ("one year", 12),
        ("twenty years", 240),
        ("twenty-four months", 24),
        ("thirty-six month", 36),
        ("forty-eight months", 48),
        ("sixty months", 60),
        ("thirty year", 360),
    ],
)
def test_term_recognised(text: str, months: int) -> None:
    assert parse_term(text) == months


@pytest.mark.parametrize(
    "text",
    [
        "half a year",
        "a year and a half",
        "18 months or so",
        "1.5 years",
        "five",
        "year",
        "0 months",
        "twenty-five months",
        "",
    ],
)
def test_term_not_recognised(text: str) -> None:
    assert parse_term(text) is None


def test_find_terms_reports_spans() -> None:
    text = "Purchase 30-Year Fixed or 6-month promo"
    found = find_terms(text)
    assert [t.months for t in found] == [360, 6]
    assert text[found[0].start : found[0].end] == "30-Year"
    assert find_terms("a year and a half") == []


def _ac(lower: str | None, li: bool, upper: str | None, ui: bool) -> AmountConstraint:
    return AmountConstraint(
        Decimal(lower) if lower is not None else None,
        li,
        Decimal(upper) if upper is not None else None,
        ui,
    )


@pytest.mark.parametrize(
    ("text", "expected", "kind"),
    [
        ("$100,000 or less", _ac(None, False, "100000", True), "cap"),
        ("up to $5,000", _ac(None, False, "5000", True), "cap"),
        ("at most $5,000", _ac(None, False, "5000", True), "cap"),
        ("no more than $5,000", _ac(None, False, "5000", True), "cap"),
        ("under $5,000", _ac(None, False, "5000", False), "cap"),
        ("Below $5,000", _ac(None, False, "5000", False), "cap"),
        ("less than $5,000", _ac(None, False, "5000", False), "cap"),
        ("$5,000 or more", _ac("5000", True, None, False), "floor"),
        ("at least $5,000", _ac("5000", True, None, False), "floor"),
        ("$100,000+", _ac("100000", True, None, False), "floor"),
        ("over $5,000", _ac("5000", False, None, False), "floor"),
        ("above $5,000", _ac("5000", False, None, False), "floor"),
        ("more than $5,000", _ac("5000", False, None, False), "floor"),
        ("greater than $5,000", _ac("5000", False, None, False), "floor"),
        ("between $1,000 and $5,000", _ac("1000", True, "5000", True), "band"),
        ("$1,000 to $5,000", _ac("1000", True, "5000", True), "band"),
        ("$1,000 - $5,000", _ac("1000", True, "5000", True), "band"),
        ("$25,000 - $99,999.99", _ac("25000", True, "99999.99", True), "band"),
        ("$1,000 – $5,000", _ac("1000", True, "5000", True), "band"),
        ("$1,000—$5,000", _ac("1000", True, "5000", True), "band"),
        ("$5,000", _ac("5000", True, "5000", True), "point"),
        ("$100k or less", _ac(None, False, "100000", True), "cap"),
    ],
)
def test_amount_constraint_recognised(text: str, expected: AmountConstraint, kind: str) -> None:
    result = parse_amount_constraint(text)
    assert result == expected
    assert result is not None and result.kind == kind


@pytest.mark.parametrize(
    "text",
    [
        "around $5,000",
        "about $5,000",
        "roughly $5,000",
        "nearly $5,000",
        "approximately $5,000",
        "$5,000 or so",
        "between $5,000 and $1,000",
        "over",
        "",
        "$5,000 or less or more",
    ],
)
def test_amount_constraint_not_recognised(text: str) -> None:
    assert parse_amount_constraint(text) is None


@pytest.mark.parametrize(
    ("text", "years"),
    [
        ("2024-27", ModelYears(2024, 2027)),
        ("2024-2027", ModelYears(2024, 2027)),
        ("2024 - 2027", ModelYears(2024, 2027)),
        ("2024 to 2027", ModelYears(2024, 2027)),
        ("2024–2027", ModelYears(2024, 2027)),
        ("2025 model", ModelYears(2025, 2025)),
        ("2025 model year", ModelYears(2025, 2025)),
        ("'25 model", ModelYears(2025, 2025)),
    ],
)
def test_model_years_recognised(text: str, years: ModelYears) -> None:
    assert parse_model_years(text) == years


@pytest.mark.parametrize("text", ["2025", "2024-23", "20245 model", "model", "1850 model", ""])
def test_model_years_not_recognised(text: str) -> None:
    assert parse_model_years(text) is None


def test_bare_year_needs_model_context() -> None:
    assert parse_model_years("2025", bare_ok=True) == ModelYears(2025, 2025)
    assert parse_model_years("1850", bare_ok=True) is None


@pytest.mark.parametrize(
    "text",
    [
        "09/25/2026",
        "9/25/26",
        "2026-09-25",
        "September 25, 2026",
        "Sept. 25, 2026",
        "25 September 2026",
        "sep 25 2026",
    ],
)
def test_date_recognised(text: str) -> None:
    assert parse_date(text) == "2026-09-25"


def test_two_digit_year_maps_to_2000s() -> None:
    assert parse_date("9/5/26") == "2026-09-05"


@pytest.mark.parametrize("text", ["13/45/2026", "2026-02-30", "not a date", "", "2026", "9/25"])
def test_date_not_recognised(text: str) -> None:
    assert parse_date(text) is None


def test_find_dates_reports_spans_without_trailing_punctuation() -> None:
    text = "Rates effective as of 06/02/2026 and valid through Sept. 30, 2026."
    found = find_dates(text)
    assert [d.iso for d in found] == ["2026-06-02", "2026-09-30"]
    assert text[found[0].start : found[0].end] == "06/02/2026"
    assert text[found[1].start : found[1].end] == "Sept. 30, 2026"


def test_clean_text_and_dashes() -> None:
    assert clean_text("  a  b\n c ") == "a b c"
    assert clean_text("ｆｕｌｌ Case") == "full Case"
    assert fold_dashes("5–10—20") == "5-10-20"
