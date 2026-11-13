"""Tests for ingest wiring: chunks, table facts, conflicts and the summary (Step 7)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from contract import Page, Snapshot
from kk.ingest.embed import EmbeddingError
from kk.system import KnowledgeSystem
from tests.fixtures.embedder import FakeEmbedder

SAVINGS_URL = "https://example.test/personal/savings"
PROMO_URL = "https://example.test/promotions/summer"


def _savings_page(apy: str = "3.50%", rows: int = 2) -> Page:
    tiers = [
        ("Example Share Savings", "$25,000+", apy),
        ("Example Share Savings", "$10,000 - $24,999.99", "0.15%"),
        ("Example Share Savings", "$5,000 - $9,999.99", "0.12%"),
        ("Example Share Savings", "$1,000 - $4,999.99", "0.11%"),
        ("Example Share Savings", "$0.01 - $999.99", "0.10%"),
    ][:rows]
    body = "\n".join(f"| {a} | {b} | {c} | {c} |" for a, b, c in tiers)
    markdown = (
        "# Savings\n\n## Savings Rates\n\n"
        "| Account Type | Balance Tier | Dividend Rate | Annual Percentage Yield (APY) |\n"
        "| --- | --- | --- | --- |\n"
        f"{body}\n\nRates are effective as of 06/02/2026.\n"
    )
    return Page(SAVINGS_URL, "Savings", markdown, "2026-06-02T00:00:00Z")


def _promo_page() -> Page:
    markdown = (
        "# Summer Promo\n\n## Savings Rates\n\n"
        "| Account Type | Balance Tier | Dividend Rate | Annual Percentage Yield (APY) |\n"
        "| --- | --- | --- | --- |\n"
        "| Example Share Savings | $25,000+ | 2.90% | 2.95% |\n\n"
        "Summer special. Offer ended 2025-09-30.\n"
    )
    return Page(PROMO_URL, "Summer Promo", markdown, "2026-06-02T00:00:00Z")


def _snapshot(n: int, taken_on: str, pages: Sequence[Page]) -> Snapshot:
    return Snapshot(snapshot_id=f"{n:02d}_{taken_on}", taken_on=taken_on, pages=list(pages))


@pytest.fixture
def ks(work_dir: Path) -> KnowledgeSystem:
    return KnowledgeSystem(embedder=FakeEmbedder())


def test_ingest_stores_chunks_and_table_facts(ks: KnowledgeSystem) -> None:
    ks.ingest(_snapshot(1, "2026-06-02", [_savings_page()]))
    assert ks.store.row_count("chunks") > 0
    versions = ks.store.visible_fact_versions(1)
    assert {v.fact.attribute for v in versions} == {"apy", "dividend_rate"}
    assert {v.fact.origin for v in versions} == {"table_rule"}
    assert {v.url for v in versions} == {SAVINGS_URL}


def test_a_broken_page_is_skipped_counted_and_never_raises(ks: KnowledgeSystem) -> None:
    broken = Page("https://example.test/empty", "Empty", "", "2026-06-02T00:00:00Z")
    ks.ingest(_snapshot(1, "2026-06-02", [broken, _savings_page()]))
    assert ks.last_summary is not None
    assert ks.last_summary.pages == 2
    assert ks.last_summary.skipped_pages == 1
    assert ks.store.row_count("facts") > 0


def test_conflict_between_a_live_and_an_expired_page_is_found_and_resolved(
    ks: KnowledgeSystem,
) -> None:
    ks.ingest(_snapshot(1, "2026-06-02", [_savings_page(), _promo_page()]))
    found = [c for c in ks.conflicts() if "APY" in c.topic]
    assert len(found) == 1
    conflict = found[0]
    assert conflict.values == ["2.95%", "3.50%"]
    assert conflict.resolution == "3.50%"
    assert conflict.reason.startswith("strategy=effective_date:")
    assert "savings" in conflict.topic.lower()
    assert {s.url for s in conflict.sources} == {SAVINGS_URL, PROMO_URL}


def test_conflicts_are_the_last_ingest_results_in_a_stable_order(ks: KnowledgeSystem) -> None:
    ks.ingest(_snapshot(1, "2026-06-02", [_savings_page(), _promo_page()]))
    first = ks.conflicts()
    assert first == ks.conflicts()
    topics = [(c.topic, c.values[0]) for c in first]
    assert topics == sorted(topics)
    ks.ingest(_snapshot(2, "2026-06-09", [_savings_page()]))  # the promo page is gone
    records = [r for r in ks.store.conflict_records() if "APY" in r.topic]
    assert [r.ords for r in records] == [(1,)]  # it coexisted only at ordinal 1, and is kept


def test_conflicts_before_any_ingest_is_empty(ks: KnowledgeSystem) -> None:
    assert ks.conflicts() == []


def test_a_second_identical_snapshot_extends_everything_and_embeds_nothing(
    work_dir: Path,
) -> None:
    embedder = FakeEmbedder()
    ks = KnowledgeSystem(embedder=embedder)
    pages = [_savings_page(), _promo_page()]
    ks.ingest(_snapshot(1, "2026-06-02", pages))
    chunks, facts, embedded = (
        ks.store.row_count("chunks"),
        ks.store.row_count("facts"),
        len(embedder.document_texts),
    )
    ks.ingest(_snapshot(2, "2026-06-09", pages))
    assert (ks.store.row_count("chunks"), ks.store.row_count("facts")) == (chunks, facts)
    assert len(embedder.document_texts) == embedded
    assert ks.last_summary is not None
    assert ks.last_summary.chunks_new == 0
    assert ks.last_summary.chunks_extended == chunks
    records = ks.store.conflict_records()
    assert [r.ords for r in records if "APY" in r.topic] == [(1, 2)]


def test_changed_value_keeps_the_older_value_visible_at_the_older_ordinal(
    ks: KnowledgeSystem,
) -> None:
    ks.ingest(_snapshot(1, "2026-06-02", [_savings_page("3.50%")]))
    ks.ingest(_snapshot(2, "2026-06-09", [_savings_page("3.60%")]))

    def apy(ordinal: int) -> set[str]:
        return {
            v.fact.value_text
            for v in ks.store.visible_fact_versions(ordinal)
            if v.fact.attribute == "apy" and "25,000" in v.fact.quote
        }

    assert apy(1) == {"3.50%"}
    assert apy(2) == {"3.60%"}


def test_a_family_that_loses_more_than_three_keys_is_marked_degraded(ks: KnowledgeSystem) -> None:
    ks.ingest(_snapshot(1, "2026-06-02", [_savings_page(rows=5)]))
    assert ks.degraded == frozenset()
    ks.ingest(_snapshot(2, "2026-06-09", [_savings_page(rows=1)]))
    assert ("example share savings", "apy") in ks.degraded


def test_ingest_summary_has_the_spec_fields_and_is_logged_once(
    ks: KnowledgeSystem, work_dir: Path
) -> None:
    ks.ingest(_snapshot(1, "2026-06-02", [_savings_page()]))
    summary = ks.last_summary
    assert summary is not None
    assert summary.pages == 1 and summary.skipped_pages == 0
    assert summary.chunks_new > 0 and summary.chunks_extended == 0
    assert summary.tables_accepted == 1
    assert summary.facts_by_origin == {"table_rule": ks.store.row_count("facts")}
    assert summary.embed_seconds >= 0 and summary.total_seconds >= summary.embed_seconds
    lines = [
        json.loads(line)
        for line in (work_dir / "kk.log").read_text().splitlines()
        if '"ingest summary"' in line
    ]
    assert len(lines) == 1
    assert lines[0]["pages"] == 1
    assert "Rates are effective" not in json.dumps(lines[0])  # LOG-4: no page text


def test_answer_is_still_the_refusal_stub(ks: KnowledgeSystem) -> None:
    ks.ingest(_snapshot(1, "2026-06-02", [_savings_page()]))
    ans = ks.answer("What is the Example Share Savings APY?")
    assert ans.confident is False and ans.sources == []


class _FlakyEmbedder(FakeEmbedder):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("model hiccup")
        return super().embed_documents(texts)


def test_one_embedding_failure_is_retried(work_dir: Path) -> None:
    ks = KnowledgeSystem(embedder=_FlakyEmbedder(failures=1))
    ks.ingest(_snapshot(1, "2026-06-02", [_savings_page()]))
    assert ks.store.row_count("chunks") > 0


def test_two_embedding_failures_raise_because_the_index_would_be_incomplete(
    work_dir: Path,
) -> None:
    ks = KnowledgeSystem(embedder=_FlakyEmbedder(failures=2))
    with pytest.raises(EmbeddingError):
        ks.ingest(_snapshot(1, "2026-06-02", [_savings_page()]))


def test_ingest_writes_only_under_the_work_dir(
    ks: KnowledgeSystem, work_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ks.ingest(_snapshot(1, "2026-06-02", [_savings_page()]))
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    assert (work_dir / "lance").exists()


def test_the_store_uses_the_batch_budget_from_the_config(work_dir: Path) -> None:
    ks = KnowledgeSystem(embedder=FakeEmbedder())
    assert ks.store.max_batch_chars == ks.config.embed.max_batch_chars == 8000
