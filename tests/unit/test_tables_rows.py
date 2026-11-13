"""Table decisions list how each row label was read (input to TBL-12)."""

from __future__ import annotations

import json

from contract import Page
from kk.ingest.aliases import build_aliases
from kk.ingest.tables import extract_page
from tests.fixtures import tables as fx

PAGE = (
    f"# Auto Loans\n\n## Auto Loans\n\n{fx.CHECKING_TABLE}\nRates are effective as of 06/02/2026.\n"
)


def test_each_emitted_row_reports_its_label_parse_and_fact_ids() -> None:
    page = Page("https://example.test/p", "Business Checking", PAGE, "2026-06-02T00:00:00Z")
    extraction = extract_page(page, build_aliases(["Business 1st Rate Checking"]))
    decision = next(d for d in extraction.decisions if d.facts)
    assert decision.rows, "an accepted table lists its parsed rows"
    ids = {(f.chunk_id, f.fact_key) for f in decision.facts}
    assert {pair for row in decision.rows for pair in row.fact_ids} == ids
    first = decision.rows[0].parse
    assert first.label == "Business 1st Rate Checking"
    assert first.entity_label == "Business 1st Rate Checking"
    assert first.qualifier_cells == ("$25,000+",)
    assert "balance_band" in json.loads(first.qualifiers)


def test_a_carried_down_label_is_reported_with_its_full_text() -> None:
    page = Page("https://example.test/p", "Business Checking", PAGE, "2026-06-02T00:00:00Z")
    extraction = extract_page(page, build_aliases(["Business 1st Rate Checking"]))
    decision = next(d for d in extraction.decisions if d.facts)
    assert {r.parse.label for r in decision.rows} == {"Business 1st Rate Checking"}


def test_rejected_and_quarantined_tables_report_no_rows() -> None:
    page = Page(
        "https://example.test/p",
        "Loans",
        "# Loans\n\n| A | B |\n| --- | --- |\n| x | y |\n",
        "2026-06-02T00:00:00Z",
    )
    extraction = extract_page(page, build_aliases([]))
    assert all(not d.rows for d in extraction.decisions)
