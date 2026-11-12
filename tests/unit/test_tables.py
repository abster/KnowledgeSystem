"""Golden, negative, gate and diff tests for table fact extraction (SPEC 6.7.2, TBL-1 to TBL-11)."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest

from contract import Page
from kk.ingest.aliases import build_aliases
from kk.ingest.chunk import chunk_page
from kk.ingest.parse import TableBlock, parse_markdown
from kk.ingest.tables import (
    RowRef,
    TableDecision,
    diff_fact_keys,
    entity_labels,
    extract_page,
    extract_table,
    family_index,
    make_fact_key,
    normalise_header,
)
from kk.types import Fact
from tests.fixtures import tables as fx

_LABELS = [
    "Home Loans",
    "Auto Loans",
    "Share Certificates",
    "Business Checking",
    "Example Share Savings",
]
REF = RowRef("chunk-1", "2026-06-02", None, 6)


def _table(markdown: str) -> TableBlock:
    return next(b for b in parse_markdown(markdown) if isinstance(b, TableBlock))


def _make(header: list[str], rows: list[list[str]]) -> TableBlock:
    width = len(header)
    fixed = tuple(tuple((row + [""] * width)[:width]) for row in rows)
    return TableBlock(
        header=tuple(header),
        align=(None,) * width,
        rows=fixed,
        raw_header=tuple(header),
        raw_rows=fixed,
    )


def _decide(
    source: str | TableBlock,
    heading: str = "Home Loans",
    refs: object = REF,
) -> TableDecision:
    table = _table(source) if isinstance(source, str) else source
    return extract_table(table, heading=heading, aliases=build_aliases(_LABELS), refs=refs)  # type: ignore[arg-type]


def _quals(fact: Fact) -> dict[str, object]:
    loaded: dict[str, object] = json.loads(fact.qualifiers)
    return loaded


def _of(decision: TableDecision, attribute: str) -> list[Fact]:
    return [f for f in decision.facts if f.attribute == attribute]


# --- TBL-2 header normalisation ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Annual Percentage Yield (APY)1", "annual percentage yield (apy)"),
        ("APY1", "apy"),
        ("\\*Payment Example", "payment example"),
        ("*Payment Example", "payment example"),
        ("  Min.  to   Open ", "min. to open"),
        ("APY* After Promotional Period", "apy after promotional period"),
        ("Loan Term 1", "loan term 1"),
        ("Annual Percentage Rate (As Low As) 1", "annual percentage rate (as low as)"),
        ("APY (Rate) 12", "apy (rate)"),
        ("Annual Percentage Rate (As Low As)", "annual percentage rate (as low as)"),
        ("", ""),
    ],
)
def test_normalise_header(raw: str, expected: str) -> None:
    assert normalise_header(raw) == expected


# --- golden per header shape ------------------------------------------------------------------


def test_business_checking_golden() -> None:
    decision = _decide(fx.CHECKING_TABLE, heading="Unknown Heading")
    assert (decision.outcome, decision.gate) == ("ACCEPT", None)
    assert len(decision.facts) == 12  # three rows by four measures
    row = [f for f in decision.facts if _quals(f) == {"balance_band": "5000-24999.99"}]
    by_attr = {f.attribute: f for f in row}
    assert set(by_attr) == {"dividend_rate", "apy", "min_to_open", "min_to_earn"}
    assert {f.entity for f in row} == {"business 1st rate checking"}
    assert {f.entity_label for f in row} == {"Business 1st Rate Checking"}
    assert by_attr["dividend_rate"].value_num == Decimal("2.47")
    assert by_attr["dividend_rate"].unit == "percent"
    assert (by_attr["apy"].value_text, by_attr["apy"].value_num) == ("2.50%", Decimal("2.50"))
    assert by_attr["min_to_open"].none_value is True
    assert by_attr["min_to_open"].value_num is None
    assert by_attr["min_to_earn"].value_num == Decimal("5000.00")
    assert by_attr["min_to_earn"].unit == "usd"
    assert by_attr["min_to_earn"].value_text == "$5,000.00"
    assert all(f.origin == "table_rule" and not f.entity_inferred for f in row)


def test_blank_row_label_is_carried_down() -> None:
    decision = _decide(fx.CHECKING_TABLE, heading="Unknown Heading")
    assert {f.entity for f in decision.facts} == {"business 1st rate checking"}
    bands = {_quals(f)["balance_band"] for f in decision.facts}
    assert bands == {">=25000", "5000-24999.99", "0.01-4999.99"}


def test_quote_is_the_verbatim_row() -> None:
    decision = _decide(fx.CHECKING_TABLE, heading="Unknown Heading")
    quotes = {f.quote for f in decision.facts}
    assert (
        "Business 1st Rate Checking | $5,000 – $24,999.99 | 2.47% | 2.50%1 | — | $5,000.00"
        in quotes
    )


def test_tiered_mortgage_with_inferred_entity() -> None:
    decision = _decide(fx.MORTGAGE_TABLE, heading="Home Loans")
    assert decision.outcome == "ACCEPT_INFERRED"
    assert len(decision.facts) == 6
    assert {f.entity for f in decision.facts} == {"home loans"}
    assert all(f.entity_inferred for f in decision.facts)
    first = [f for f in _of(decision, "rate") if f.value_text == "7.13%"][0]
    assert _quals(first) == {
        "product": "purchase",
        "term": 360,
        "rate_type": "fixed",
        "loan_type": "conventional",
        "amount_tier": "<=100000",
    }
    jumbo = [f for f in _of(decision, "apr") if f.value_text == "6.60%"][0]
    assert _quals(jumbo) == {
        "product": "refinance",
        "term": 180,
        "rate_type": "fixed",
        "loan_type": "jumbo",
    }
    rates = _of(decision, "rate")
    assert len({f.fact_key for f in rates}) == 3  # tiers differ, so they are different facts
    assert decision.unmapped_headers == ("payment example", "points")


def test_certificate_term_from_row_label() -> None:
    decision = _decide(fx.CERTIFICATE_TABLE, heading="Share Certificates")
    assert decision.outcome == "ACCEPT_INFERRED"
    seventeen = [f for f in decision.facts if _quals(f) == {"term": 17}]
    assert {f.attribute for f in seventeen} == {"dividend_rate", "apy", "min_to_earn"}
    assert {f.entity for f in seventeen} == {"share certificates"}


def test_ira_label_keeps_its_name_after_qualifiers_are_stripped() -> None:
    decision = _decide(fx.IRA_TABLE, heading="Unknown Heading")
    assert decision.outcome == "ACCEPT"
    assert {f.entity for f in decision.facts} == {"roth special", "traditional special"}
    assert all(
        _quals(f) == {"term": 17, "rate_type": "fixed", "loan_type": "jumbo"}
        for f in decision.facts
    )
    assert not any(f.entity_inferred for f in decision.facts)


def test_auto_loan_model_years_terms_and_bound() -> None:
    decision = _decide(fx.AUTO_TABLE, heading="Auto Loans")
    assert decision.outcome == "ACCEPT_INFERRED"
    assert [f.attribute for f in decision.facts] == ["apr"] * 3
    sixty = decision.facts[0]
    assert _quals(sixty) == {"model_year": "2024-2027", "term": 60, "bound": "as_low_as"}
    assert sixty.value_text == "5.39%"
    assert sixty.value_num == Decimal("5.39")
    assert {f.entity for f in decision.facts} == {"auto loans"}


def test_share_savings_bands() -> None:
    decision = _decide(fx.SAVINGS_TABLE, heading="Unknown Heading")
    assert decision.outcome == "ACCEPT"
    assert len(decision.facts) == 6
    bands = [_quals(f)["balance_band"] for f in _of(decision, "apy")]
    assert bands == [">=25000", "10000-24999.99", "0.01-9999.99"]


# --- negative goldens -------------------------------------------------------------------------


def test_amortization_table_gives_no_facts() -> None:
    unresolved = _decide(fx.AMORTIZATION_TABLE, heading="Loan Calculator")
    assert (unresolved.outcome, unresolved.gate, unresolved.facts) == ("REJECT", "P2", ())
    resolved = _decide(fx.AMORTIZATION_TABLE, heading="Home Loans")
    assert (resolved.outcome, resolved.gate, resolved.facts) == ("REJECT", "P3", ())


def test_creditor_form_with_interest_rate_column_is_rejected() -> None:
    decision = _decide(fx.CREDITOR_FORM_TABLE, heading="Loan Application")
    assert (decision.outcome, decision.gate, decision.facts) == ("REJECT", "P2", ())
    assert "interest rate" in decision.unmapped_headers


def test_blank_header_layout_table_is_rejected() -> None:
    decision = _decide(fx.BLANK_HEADER_TABLE)
    assert (decision.outcome, decision.gate, decision.facts) == ("REJECT", "TBL-1", ())


def test_balance_only_landing_table_needs_a_resolvable_heading() -> None:
    unresolved = _decide(fx.LANDING_TABLE, heading="Business Rates")
    assert (unresolved.outcome, unresolved.gate, unresolved.facts) == ("REJECT", "P2", ())
    resolved = _decide(fx.LANDING_TABLE, heading="Business Checking")
    assert resolved.outcome == "ACCEPT_INFERRED"
    assert {f.entity for f in resolved.facts} == {"business checking"}
    assert {f.entity_inferred for f in resolved.facts} == {True}


def test_abbreviated_as_low_as_header_is_not_in_the_lexicon() -> None:
    decision = _decide(fx.AS_LOW_AS_ABBREVIATED_TABLE, heading="Home Loans")
    assert (decision.outcome, decision.facts) == ("REJECT", ())
    assert decision.unmapped_headers == ("apr (as low as)", "product")


def test_full_as_low_as_header_keeps_its_bound() -> None:
    table = _make(
        ["Loan Program", "Term", "Annual Percentage Rate (As Low As)"],
        [["Example Loan", "36 Months", "7.00%"]],
    )
    (fact,) = _decide(table).facts
    assert _quals(fact) == {"term": 36, "bound": "as_low_as"}


def test_annual_percentage_yield_without_apy_is_not_mapped() -> None:
    table = _make(["Account Type", "Annual Percentage Yield"], [["Example", "1.00%"]])
    decision = _decide(table)
    assert (decision.outcome, decision.gate) == ("REJECT", "P3")


# --- gates ------------------------------------------------------------------------------------


def test_p1_table_without_data_rows() -> None:
    decision = _decide(_make(["Account Type", "Dividend Rate"], []))
    assert (decision.outcome, decision.gate, decision.facts) == ("REJECT", "P1", ())
    blank_rows = _decide(_make(["Account Type", "Dividend Rate"], [["", ""]]))
    assert (blank_rows.outcome, blank_rows.gate) == ("REJECT", "P1")


def test_p3_no_measure_column() -> None:
    decision = _decide(_make(["Certificate", "Notes"], [["6 Month", "see branch"]]))
    assert (decision.outcome, decision.gate, decision.facts) == ("REJECT", "P3", ())


def test_p4_dollar_amounts_under_a_rate_header() -> None:
    table = _make(["Program", "Rate"], [["Alpha Loan", "$5.00"], ["Beta Loan", "$6.00"]])
    decision = _decide(table)
    assert (decision.outcome, decision.gate, decision.facts) == ("REJECT", "P4", ())


def test_p4_exactly_eighty_percent_passes_and_drops_the_bad_row() -> None:
    names = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]
    rows = [[n, "1.00%"] for n in names[:4]] + [[names[4], "call us"]]
    decision = _decide(_make(["Account Type", "Dividend Rate"], rows))
    assert decision.outcome == "ACCEPT"
    assert [f.entity for f in decision.facts] == ["alpha", "beta", "gamma", "delta"]


def test_p4_below_eighty_percent_rejects_the_table() -> None:
    rows = [["Alpha", "1.00%"], ["Beta", "1.00%"], ["Gamma", "x"], ["Delta", "y"], ["Eps", "z"]]
    decision = _decide(_make(["Account Type", "Dividend Rate"], rows))
    assert (decision.outcome, decision.gate, decision.facts) == ("REJECT", "P4", ())


@pytest.mark.parametrize(
    "bands",
    [
        ["$100 - $199.99", "12 Months"],  # two kinds of value in one qualifier column
        ["$100 - $500.00", "$400 - $900.00"],  # overlap
        ["$100 - $199.99", "$500 - $599.99", "$200 - $299.99"],  # not ordered
    ],
)
def test_p5_qualifier_coherence_quarantines(bands: list[str]) -> None:
    rows = [["Alpha Savings", band, f"{i + 1}.00%"] for i, band in enumerate(bands)]
    decision = _decide(_make(["Account Type", "Balance Tier", "Dividend Rate"], rows))
    assert (decision.outcome, decision.gate, decision.facts) == ("QUARANTINE", "P5", ())


def test_p5_same_bands_for_different_entities_are_fine() -> None:
    rows = [
        ["Alpha Savings", "$1,000 - $1,999.99", "1.00%"],
        ["Alpha Savings", "$0.01 - $999.99", "0.50%"],
        ["Beta Savings", "$1,000 - $1,999.99", "1.10%"],
        ["Beta Savings", "$0.01 - $999.99", "0.60%"],
    ]
    decision = _decide(_make(["Account Type", "Balance Tier", "Dividend Rate"], rows))
    assert decision.outcome == "ACCEPT"
    assert len(decision.facts) == 4


def test_a_dash_in_a_balance_column_means_no_tier() -> None:
    rows = [
        ["Free Checking", "—", "0.00%"],
        ["Tiered Checking", "$1,000+", "1.00%"],
        ["Tiered Checking", "$0.01 - $999.99", "0.10%"],
    ]
    decision = _decide(_make(["Account Type", "Balance Tier", "Dividend Rate"], rows))
    assert decision.outcome == "ACCEPT"
    free = next(f for f in decision.facts if f.entity == "free checking")
    assert _quals(free) == {}


def test_p6_same_key_with_different_values() -> None:
    rows = [["Alpha", "1.00%"], ["Alpha", "1.20%"]]
    decision = _decide(_make(["Account Type", "Dividend Rate"], rows))
    assert (decision.outcome, decision.gate, decision.facts) == ("QUARANTINE", "P6", ())


def test_p6_identical_duplicate_rows_collapse_to_one_fact() -> None:
    rows = [["Alpha", "1.00%"], ["Alpha", "1.00%"]]
    decision = _decide(_make(["Account Type", "Dividend Rate"], rows))
    assert decision.outcome == "ACCEPT"
    assert len(decision.facts) == 1


def test_p7_apy_below_dividend_rate() -> None:
    table = _make(
        ["Account Type", "Dividend Rate", "Annual Percentage Yield (APY)"],
        [["Alpha", "2.00%", "1.50%"]],
    )
    decision = _decide(table)
    assert (decision.outcome, decision.gate, decision.facts) == ("QUARANTINE", "P7", ())


def test_p7_apr_below_rate() -> None:
    table = _make(["Program", "Rate", "APR"], [["Purchase: 30-Year Fixed", "6.00%", "5.00%"]])
    decision = _decide(table, heading="Home Loans")
    assert (decision.outcome, decision.gate, decision.facts) == ("QUARANTINE", "P7", ())


# --- values, rows and links to chunks ---------------------------------------------------------


def test_values_footnotes_none_and_money() -> None:
    table = _make(
        ["Account Type", "Dividend Rate", "Min. to Open", "Min. to Earn"],
        [["Alpha", "0.10%1", "None", "$25.00"], ["Beta", "0.20%", "—", "$1,000"]],
    )
    facts = {(f.entity, f.attribute): f for f in _decide(table).facts}
    assert facts["alpha", "dividend_rate"].value_text == "0.10%"
    assert facts["alpha", "min_to_open"].none_value is True
    assert facts["alpha", "min_to_open"].value_text == "None"
    assert facts["beta", "min_to_open"].value_text == "—"
    assert facts["alpha", "min_to_earn"].value_num == Decimal("25.00")
    assert facts["beta", "min_to_earn"].value_num == Decimal("1000")


def test_row_label_that_cannot_be_understood_drops_only_that_row() -> None:
    rows = [["Alpha", "1.00%"], ["Special 2024 offer", "1.10%"], ["Gamma", "1.20%"]]
    decision = _decide(_make(["Account Type", "Dividend Rate"], rows))
    assert decision.outcome == "ACCEPT"
    assert [f.entity for f in decision.facts] == ["alpha", "gamma"]
    assert decision.dropped_rows == 1


def test_no_row_resolving_gives_no_facts() -> None:
    table = _make(["Program", "Rate"], [["Purchase: 30-Year Fixed", "6.00%"]])
    decision = _decide(table, heading="Unknown Heading")
    assert (decision.outcome, decision.gate, decision.facts) == ("REJECT", "TBL-10", ())


def test_conflicting_product_words_are_not_guessed() -> None:
    table = _make(["Program", "Rate"], [["Fixed ARM 30-Year", "6.00%"]])
    assert _decide(table, heading="Home Loans").facts == ()


def test_two_terms_in_one_label_are_not_guessed() -> None:
    table = _make(["Program", "Rate"], [["30-Year or 15-Year Fixed", "6.00%"]])
    assert _decide(table, heading="Home Loans").facts == ()


def test_refs_supply_chunk_dates_and_authority() -> None:
    refs = [RowRef("c0", "2026-09-25", "2026-12-31", 5), None, RowRef("c2", None, None, 2)]
    table = _make(
        ["Account Type", "Dividend Rate"],
        [["Alpha", "1.00%"], ["Beta", "1.10%"], ["Gamma", "1.20%"]],
    )
    decision = _decide(table, refs=refs)
    assert [(f.entity, f.chunk_id) for f in decision.facts] == [("alpha", "c0"), ("gamma", "c2")]
    first, second = decision.facts
    assert (first.effective_date, first.expiry_date, first.authority_rank) == (
        "2026-09-25",
        "2026-12-31",
        5,
    )
    assert (second.effective_date, second.expiry_date, second.authority_rank) == (None, None, 2)


def test_fact_key_is_entity_attribute_and_sorted_qualifiers() -> None:
    qualifiers = '{"balance_band":"5000-24999.99"}'
    expected = hashlib.sha256(f"alpha|apy|{qualifiers}".encode()).hexdigest()
    assert make_fact_key("alpha", "apy", qualifiers) == expected
    decision = _decide(fx.CHECKING_TABLE, heading="Unknown Heading")
    fact = next(f for f in decision.facts if _quals(f) == {"balance_band": "5000-24999.99"})
    assert fact.fact_key == make_fact_key(fact.entity, fact.attribute, fact.qualifiers)
    assert fact.qualifiers == json.dumps(_quals(fact), sort_keys=True, separators=(",", ":"))


# --- page level -------------------------------------------------------------------------------


def _page() -> Page:
    return Page(
        url="https://www.example-cu.test/rates/checking",
        title="Example Checking Rates",
        markdown=fx.PAGE_WITH_FOOTER,
        fetched_at="2026-06-02T00:00:00Z",
    )


def test_entity_labels_hold_the_title_and_entity_column_labels() -> None:
    assert entity_labels(_page()) == {"Example Checking Rates", "Example Checking"}


def test_extract_page_links_facts_to_row_chunks_and_footer_date() -> None:
    page = _page()
    aliases = build_aliases(entity_labels(page))
    extraction = extract_page(page, aliases)
    (decision,) = extraction.decisions
    assert decision.outcome == "ACCEPT"
    row_chunks = {c.chunk_id: c for c in chunk_page(page).chunks if c.chunk_type == "table_row"}
    assert len(decision.facts) == 4
    for fact in extraction.facts:
        chunk = row_chunks[fact.chunk_id]
        assert fact.effective_date == "2026-06-02"
        assert fact.authority_rank == chunk.authority_rank
        assert fact.quote.split(" | ")[0] in chunk.text


# --- TBL-11 snapshot diff ---------------------------------------------------------------------


def _families(count: int, url: str = "u1") -> dict[tuple[str, str], dict[str, str]]:
    return {("alpha", "apy"): {f"k{i}": url for i in range(count)}}


def test_diff_marks_a_family_that_lost_more_than_three_keys() -> None:
    result = diff_fact_keys(_families(5), _families(1), frozenset({"u1"}))
    assert result.degraded == frozenset({("alpha", "apy")})
    assert result.anomalies == (("alpha", "apy", 4),)


def test_diff_allows_up_to_three_vanished_keys() -> None:
    result = diff_fact_keys(_families(5), _families(2), frozenset({"u1"}))
    assert result.degraded == frozenset()
    assert result.anomalies == ()


def test_diff_ignores_keys_whose_page_disappeared() -> None:
    result = diff_fact_keys(_families(8), {}, frozenset())
    assert result.degraded == frozenset()


def test_family_index_groups_keys_by_entity_and_attribute() -> None:
    decision = _decide(fx.SAVINGS_TABLE, heading="Unknown Heading")
    urls = {f.chunk_id: "https://x.test/p" for f in decision.facts}
    index = family_index(decision.facts, urls)
    assert set(index) == {
        ("example share savings", "dividend_rate"),
        ("example share savings", "apy"),
    }
    assert all(len(keys) == 3 for keys in index.values())
    assert set(next(iter(index.values())).values()) == {"https://x.test/p"}
