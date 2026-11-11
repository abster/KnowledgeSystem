"""Real-data checks for Step 3. Skipped when ``snapshots/`` is absent (not run in CI).

Reads the local snapshots, never copies their text anywhere. The measurement report written to
``results/`` holds counts and URL paths only, and ``results/`` is git-ignored.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

from contract import Page
from kk.ingest.chunk import ChunkedPage, chunk_page
from kk.ingest.dedupe import IntervalStore
from kk.ingest.meta import classify_source
from kk.ingest.parse import ParseError

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOTS = ROOT / "snapshots"
pytestmark = pytest.mark.real_data


@dataclass
class _Snap:
    snapshot_id: str
    taken_on: str
    pages: list[Page]


def _load() -> list[_Snap]:
    snaps = []
    for folder in sorted(p for p in SNAPSHOTS.glob("*") if (p / "manifest.json").exists()):
        manifest = json.loads((folder / "manifest.json").read_text())
        pages = [
            Page(
                url=entry["url"],
                title=entry.get("title", ""),
                markdown=(folder / entry["file"]).read_text(encoding="utf-8"),
                fetched_at=entry.get("fetched_at", manifest["taken_on"]),
            )
            for entry in manifest["pages"]
        ]
        snaps.append(_Snap(manifest["snapshot_id"], manifest["taken_on"], pages))
    return snaps


@pytest.fixture(scope="module")
def corpus() -> list[_Snap]:
    snaps = _load()
    if not snaps:
        pytest.skip("snapshots/ is not present")
    return snaps


def _chunk_all(snap: _Snap) -> tuple[list[ChunkedPage], list[str]]:
    results: list[ChunkedPage] = []
    skipped: list[str] = []
    for page in snap.pages:
        try:
            results.append(chunk_page(page))
        except ParseError:
            skipped.append(page.url)
    return results, skipped


@pytest.fixture(scope="module")
def chunked(corpus: list[_Snap]) -> list[tuple[list[ChunkedPage], list[str]]]:
    return [_chunk_all(snap) for snap in corpus]


def test_every_page_parses_and_chunking_is_deterministic(
    corpus: list[_Snap], chunked: list[tuple[list[ChunkedPage], list[str]]]
) -> None:
    for snap, (results, skipped) in zip(corpus, chunked, strict=True):
        assert _chunk_all(snap) == (results, skipped)
        non_empty = {p.url for p in snap.pages if p.markdown.strip()}
        assert not (set(skipped) & non_empty), f"{snap.snapshot_id}: non-empty pages failed"
        assert sum(len(r.chunks) for r in results) > 0


def test_chunk_structure_invariants(chunked: list[tuple[list[ChunkedPage], list[str]]]) -> None:
    for results, _ in chunked:
        for result in results:
            ids = [c.chunk_id for c in result.chunks]
            assert len(ids) == len(set(ids))
            known = set(ids)
            for chunk in result.chunks:
                assert len(chunk.text) >= 20
                assert chunk.parent_headers
                if chunk.chunk_type == "table_row":
                    assert chunk.parent_id in known


def test_measure_corpus_and_write_report(
    corpus: list[_Snap], chunked: list[tuple[list[ChunkedPage], list[str]]]
) -> None:
    store = IntervalStore()
    per_snapshot = []
    footer_counts: Counter[int] = Counter()
    for ordinal, (snap, (results, skipped)) in enumerate(zip(corpus, chunked, strict=True), 1):
        chunks = [c for r in results for c in r.chunks]
        delta = store.add_snapshot(ordinal, snap.taken_on, chunks)
        footer_counts.update(
            c.footer_count for c in chunks if c.chunk_type == "table" and c.footer_count is not None
        )
        per_snapshot.append(
            {
                "snapshot": snap.snapshot_id,
                "pages": len(snap.pages),
                "skipped_pages": len(skipped),
                "chunks": len(chunks),
                "by_type": dict(Counter(c.chunk_type for c in chunks)),
                "tables": sum(r.tables for r in results),
                "layout_tables": sum(r.layout_tables for r in results),
                "dropped_short": sum(r.dropped_short for r in results),
                "dropped_duplicate": sum(r.dropped_duplicate for r in results),
                "with_effective_date": sum(1 for c in chunks if c.effective_date),
                "with_expiry_date": sum(1 for c in chunks if c.expiry_date),
                "intervals_new": len(delta.new),
                "intervals_extended": len(delta.extended),
            }
        )
    latest = corpus[-1]
    sources = [classify_source(p.url, p.title) for p in latest.pages]
    report = {
        "snapshots": per_snapshot,
        "intervals_total": len(store.intervals),
        "source_class_pages_latest_snapshot": dict(Counter(s.name for s in sources)),
        "other_class_urls_latest_snapshot": sorted(
            p.url for p, s in zip(latest.pages, sources, strict=True) if s.name == "other"
        ),
        "table_footer_count_histogram": {str(k): v for k, v in sorted(footer_counts.items())},
    }
    out = ROOT / "results" / "real_data_step3.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    assert report["intervals_total"] > 0
