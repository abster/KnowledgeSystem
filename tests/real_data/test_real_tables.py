"""Real-data checks for Step 4. Skipped when ``snapshots/`` is absent (not run in CI).

Reads the local snapshots and writes counts and header text only to ``results/`` (git-ignored).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kk.ingest.aliases import AliasIndex, build_aliases
from kk.ingest.parse import TableBlock, parse_markdown
from kk.ingest.tables import RowRef, entity_labels, extract_page, extract_table
from kk.ingest.tables_report import Snapshot, build_report, load_snapshots

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.real_data
_AMORTIZATION_HEADERS = {"interest", "principal", "remaining"}


@pytest.fixture(scope="module")
def corpus() -> list[Snapshot]:
    snaps = load_snapshots(ROOT / "snapshots")
    if not snaps:
        pytest.skip("snapshots/ is not present")
    return snaps


@pytest.fixture(scope="module")
def report(corpus: list[Snapshot]) -> dict[str, object]:
    return build_report(corpus)


@pytest.fixture(scope="module")
def latest_aliases(corpus: list[Snapshot]) -> AliasIndex:
    return build_aliases(label for page in corpus[-1].pages for label in entity_labels(page))


def test_report_is_deterministic(corpus: list[Snapshot]) -> None:
    assert build_report(corpus[:1]) == build_report(corpus[:1])


def test_no_rejected_or_quarantined_table_produces_a_fact(
    corpus: list[Snapshot], latest_aliases: AliasIndex
) -> None:
    seen = 0
    for page in corpus[-1].pages:
        for decision in extract_page(page, latest_aliases).decisions:
            seen += 1
            if decision.outcome in ("QUARANTINE", "REJECT"):
                assert decision.facts == ()
            for fact in decision.facts:
                assert set(re.findall(r"\d+", fact.value_text)) <= set(
                    re.findall(r"\d+", fact.quote)
                )
    assert seen > 0


def test_amortization_style_tables_never_give_facts(
    corpus: list[Snapshot], latest_aliases: AliasIndex
) -> None:
    checked = 0
    for page in corpus[-1].pages:
        for block in parse_markdown(page.markdown):
            if isinstance(block, TableBlock) and _AMORTIZATION_HEADERS <= {
                h.lower() for h in block.header
            }:
                checked += 1
                decision = extract_table(
                    block,
                    heading="Home Loans",
                    aliases=latest_aliases,
                    refs=RowRef("c", None, None, 0),
                )
                assert decision.facts == ()
    assert checked > 0


def test_write_coverage_report(report: dict[str, object]) -> None:
    out = ROOT / "results" / "real_data_step4.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    assert report["snapshots"]
