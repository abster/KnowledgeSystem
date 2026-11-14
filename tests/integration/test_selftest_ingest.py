"""Ingest of the provided selftest snapshots through the real wiring (Step 7).

The selftest conflict is plain text, so it needs Step 8 prose facts; here we check that both
snapshots ingest and that a direct store query honours the target ordinal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contract import Page, Snapshot
from kk.query.asof import target_ordinal
from kk.system import KnowledgeSystem
from tests.fixtures.embedder import FakeEmbedder

SELFTEST = Path(__file__).resolve().parents[2] / "harness" / "selftest" / "snapshots"
pytestmark = pytest.mark.skipif(not SELFTEST.exists(), reason="harness/selftest not present")


def _load() -> list[Snapshot]:
    snaps = []
    for folder in sorted(p for p in SELFTEST.iterdir() if (p / "manifest.json").exists()):
        manifest = json.loads((folder / "manifest.json").read_text())
        pages = [
            Page(
                url=e["url"],
                title=e["title"],
                markdown=(folder / e["file"]).read_text(),
                fetched_at=e.get("fetched_at", manifest["taken_on"]),
            )
            for e in manifest["pages"]
        ]
        snaps.append(Snapshot(manifest["snapshot_id"], manifest["taken_on"], pages))
    return snaps


def test_both_selftest_snapshots_ingest_and_as_of_filters_a_direct_store_query(
    work_dir: Path,
) -> None:
    embedder = FakeEmbedder()
    ks = KnowledgeSystem(embedder=embedder)
    for snap in _load():
        ks.ingest(snap)
    assert [s.ordinal for s in ks.store.snapshots] == [1, 2]
    query = "6-month certificate APY"
    old = ks.store.search_text(query, 1, 10)
    new = ks.store.search_text(query, 2, 10)
    assert any("2.95%" in h.chunk.text for h in old)
    assert not any("3.50%" in h.chunk.text for h in old)
    assert any("3.50%" in h.chunk.text for h in new)
    assert not any(
        "2.95%" in h.chunk.text and "ended" not in h.chunk.text
        for h in new
        if h.first_seen_ord == 1
    )
    ordinal = target_ordinal("2026-02-01", ks.store.snapshots).ordinal
    assert ordinal == 1


def test_every_selftest_question_gets_an_answer_and_the_refusal_item_is_refused(
    work_dir: Path,
) -> None:
    """Routing end to end on the provided selftest data (SPEC 19.2, Step 9).

    The selftest facts are plain text, so without prose extraction (no key here) the fast path
    has nothing to answer from; what must hold is that nothing raises, no refusal states a new
    number, and the item that must be refused is refused.
    """
    ks = KnowledgeSystem(embedder=FakeEmbedder())
    for snap in _load():
        ks.ingest(snap)
    rows = [
        json.loads(line)
        for line in (SELFTEST.parent / "eval.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert rows
    for row in rows:
        answer = ks.answer(row["question"], row.get("as_of"))
        assert isinstance(answer.text, str)
        if answer.confident is False:
            assert answer.sources == []
            digits = set("0123456789")
            assert (set(answer.text) & digits) <= (set(row["question"]) & digits)
        if row["category"] == "should_refuse":
            assert answer.confident is False
    before_first = ks.answer("What is the 6-month certificate APY?", "2026-01-01")
    assert before_first.confident is False
    assert before_first.sources == []
