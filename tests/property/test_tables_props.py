"""Property tests for table extraction and the alias builder (Step 4)."""

from __future__ import annotations

import re

from hypothesis import given, settings
from hypothesis import strategies as st

from kk.ingest.aliases import build_aliases, canonical_id
from kk.ingest.parse import TableBlock
from kk.ingest.tables import extract_table, make_fact_key, normalise_header
from tests.unit.test_tables import REF

_NO_DEADLINE = settings(deadline=None, max_examples=200)
_VOCABULARY = {
    "rate",
    "dividend_rate",
    "apy",
    "apr",
    "min_to_open",
    "min_to_earn",
}
_HEADERS = st.one_of(
    st.sampled_from(
        [
            "Account Type",
            "Program",
            "Certificate",
            "Balance Tier",
            "Term",
            "Dividend Rate",
            "Annual Percentage Yield (APY)1",
            "APR",
            "Rate",
            "Min. to Open",
            "Min. to Earn",
            "Annual Percentage Rate (As Low As)",
        ]
    ),
    st.text(max_size=12),
)
_CELLS = st.one_of(
    st.sampled_from(
        [
            "1.00%",
            "0.10%1",
            "$5.00",
            "$1,000 - $1,999.99",
            "$100,000+",
            "30-Year",
            "6 Months",
            "None",
        ]
    ),
    st.sampled_from(
        ["—", "", "Alpha Savings", "Fixed Conventional $100,000 or less", "2024-27 Model Year"]
    ),
    st.text(max_size=15),
)


@st.composite
def _tables(draw: st.DrawFn) -> TableBlock:
    header = tuple(draw(st.lists(_HEADERS, min_size=1, max_size=6)))
    width = len(header)
    rows = tuple(
        tuple(draw(st.lists(_CELLS, min_size=width, max_size=width)))
        for _ in range(draw(st.integers(0, 6)))
    )
    return TableBlock(header, (None,) * width, rows, header, rows)


@_NO_DEADLINE
@given(_tables(), st.text(max_size=20))
def test_extraction_never_raises_and_keeps_its_invariants(table: TableBlock, heading: str) -> None:
    aliases = build_aliases(["Home Loans", "Alpha Savings", heading])
    decision = extract_table(table, heading=heading, aliases=aliases, refs=REF)
    if decision.outcome in ("QUARANTINE", "REJECT"):
        assert decision.facts == ()
    for fact in decision.facts:
        assert fact.attribute in _VOCABULARY
        assert fact.fact_key == make_fact_key(fact.entity, fact.attribute, fact.qualifiers)
        assert set(re.findall(r"\d+", fact.value_text)) <= set(re.findall(r"\d+", fact.quote))
        assert fact.none_value == (fact.value_num is None)
    keys = [(f.fact_key) for f in decision.facts]
    assert len(keys) == len(set(keys))


@_NO_DEADLINE
@given(st.text())
def test_header_and_alias_functions_never_raise(text: str) -> None:
    assert normalise_header(text) == normalise_header(normalise_header(text))
    assert canonical_id(text) == canonical_id(canonical_id(text))
    build_aliases([text]).resolve(text)


@_NO_DEADLINE
@given(st.lists(st.text(max_size=20), max_size=6))
def test_an_entity_label_resolves_to_itself_unless_blank(labels: list[str]) -> None:
    index = build_aliases(labels)
    for label in labels:
        entity = canonical_id(label)
        if entity:
            assert index.resolve(label) == entity
