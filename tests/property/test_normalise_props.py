"""Property tests for the normaliser and the chunk pipeline (SPEC 7.7, CH-6)."""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from contract import Page
from kk.ingest.chunk import chunk_page
from kk.query.normalise import (
    Quantity,
    clean_text,
    find_dates,
    find_terms,
    format_quantity,
    parse_amount_constraint,
    parse_date,
    parse_model_years,
    parse_quantity,
    parse_term,
)

_NO_DEADLINE = settings(deadline=None, max_examples=200)


@_NO_DEADLINE
@given(st.text())
def test_parsers_never_raise(text: str) -> None:
    parse_quantity(text)
    parse_term(text)
    parse_amount_constraint(text)
    parse_model_years(text)
    parse_model_years(text, bare_ok=True)
    parse_date(text)
    find_terms(text)
    find_dates(text)
    clean_text(text)


@_NO_DEADLINE
@given(st.decimals(min_value=0, max_value=10**9, places=2))
def test_percent_and_usd_round_trip(value: Decimal) -> None:
    for kind in ("percent", "usd"):
        quantity = Quantity(kind, value)  # type: ignore[arg-type]
        assert parse_quantity(format_quantity(quantity)) == quantity


@_NO_DEADLINE
@given(st.integers(min_value=0, max_value=10**12))
def test_count_round_trip(value: int) -> None:
    quantity = Quantity("count", Decimal(value))
    assert parse_quantity(format_quantity(quantity)) == quantity


@_NO_DEADLINE
@given(st.text(alphabet=st.characters(blacklist_categories=("Nd", "Cs")), max_size=30))
def test_text_without_digits_is_never_a_number(text: str) -> None:
    assert parse_quantity(text) is None


@_NO_DEADLINE
@given(st.text(max_size=400))
def test_chunking_never_raises_and_is_deterministic(markdown: str) -> None:
    page = Page(url="https://example.test/p", title="T", markdown=markdown, fetched_at="x")
    try:
        first = chunk_page(page)
    except Exception as exc:  # noqa: BLE001 - only ParseError is allowed
        assert type(exc).__name__ == "ParseError"
        return
    assert chunk_page(page) == first
