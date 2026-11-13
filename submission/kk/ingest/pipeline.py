"""Ingest of one snapshot: pages to chunks and table facts, into the store (SPEC 6.1 to 6.12).

Page-level problems (a page that cannot be parsed, an unexpected error on one page) skip that
page and are counted; they never stop the snapshot (IF-2). Embedding and store failures are
not caught here: the index would be incomplete, so they propagate (SPEC 6.11).
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field

from contract import Page, Snapshot
from kk.conflicts import detect_conflicts
from kk.ingest.aliases import build_aliases
from kk.ingest.chunk import chunk_page
from kk.ingest.embed import EmbedderLike
from kk.ingest.parse import ParseError
from kk.ingest.tables import KeyFamilies, diff_fact_keys, entity_labels, extract_page, family_index
from kk.log import get_logger
from kk.store import Store
from kk.types import Chunk, Fact, SnapshotRef

TOP_UNMAPPED = 20  # SPEC 6.12

_log = get_logger("ingest")


@dataclass(frozen=True)
class IngestSummary:
    """The one record logged after each ingest (SPEC 6.12). Never holds page text."""

    pages: int
    skipped_pages: int
    chunks_new: int
    chunks_extended: int
    chunks_dropped: int
    tables_accepted: int
    tables_inferred: int
    tables_quarantined: int
    tables_rejected: int
    unmapped_headers: tuple[tuple[str, int], ...]
    facts_by_origin: dict[str, int]
    embed_seconds: float
    total_seconds: float


@dataclass
class IngestState:
    """What carries over from one snapshot to the next."""

    labels: set[str] = field(default_factory=set)
    previous_families: KeyFamilies = field(default_factory=dict)
    degraded: frozenset[tuple[str, str]] = frozenset()


@dataclass
class _PageResults:
    chunks: list[Chunk] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    skipped: int = 0
    dropped: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)
    unmapped: Counter[str] = field(default_factory=Counter)


def _read_pages(pages: list[Page], aliases_labels: set[str], results: _PageResults) -> None:
    aliases = build_aliases(aliases_labels)
    for page in pages:
        try:
            chunked = chunk_page(page)
            extraction = extract_page(page, aliases)
        except ParseError:
            results.skipped += 1
            _log.warning("page skipped: cannot be parsed", extra={"url": page.url})
            continue
        except Exception:  # noqa: BLE001 - IF-2: a bad page never stops the snapshot
            results.skipped += 1
            _log.exception("page skipped: unexpected error", extra={"url": page.url})
            continue
        results.chunks.extend(chunked.chunks)
        results.dropped += chunked.dropped_short + chunked.dropped_duplicate
        results.facts.extend(extraction.facts)
        for decision in extraction.decisions:
            results.outcomes[decision.outcome] += 1
            results.unmapped.update(decision.unmapped_headers)


def _collect_labels(pages: list[Page]) -> set[str]:
    labels: set[str] = set()
    for page in pages:
        try:
            labels |= entity_labels(page)
        except Exception:  # noqa: BLE001 - a page that cannot be read gives no labels
            _log.warning("labels skipped for one page", extra={"url": page.url})
    return labels


def ingest_snapshot(
    store: Store, embedder: EmbedderLike, snapshot: Snapshot, state: IngestState
) -> IngestSummary:
    """Ingest one snapshot into ``store`` and rebuild the conflicts table (SPEC 6.10).

    Raises:
        EmbeddingError: A batch failed twice (the index would be incomplete).
    """
    started = time.perf_counter()
    ordinal = len(store.snapshots) + 1
    ref = SnapshotRef(ordinal=ordinal, snapshot_id=snapshot.snapshot_id, taken_on=snapshot.taken_on)
    pages = list(snapshot.pages)
    state.labels |= _collect_labels(pages)
    results = _PageResults()
    _read_pages(pages, state.labels, results)
    delta = store.add_snapshot(ref, results.chunks, results.facts, embedder)
    _track_degraded(state, results, pages)
    _rebuild_conflicts(store)
    summary = IngestSummary(
        pages=len(pages),
        skipped_pages=results.skipped,
        chunks_new=delta.new_chunks,
        chunks_extended=delta.extended_chunks,
        chunks_dropped=results.dropped,
        tables_accepted=results.outcomes["ACCEPT"],
        tables_inferred=results.outcomes["ACCEPT_INFERRED"],
        tables_quarantined=results.outcomes["QUARANTINE"],
        tables_rejected=results.outcomes["REJECT"],
        unmapped_headers=tuple(results.unmapped.most_common(TOP_UNMAPPED)),
        facts_by_origin=dict(Counter(f.origin for f in results.facts)),
        embed_seconds=round(delta.embed_seconds, 3),
        total_seconds=round(time.perf_counter() - started, 3),
    )
    _log.info("ingest summary", extra={"ord": ordinal, **summary.__dict__})
    return summary


def _track_degraded(state: IngestState, results: _PageResults, pages: list[Page]) -> None:
    """TBL-11: a family that lost more than three keys while its page still exists."""
    urls = {c.chunk_id: c.url for c in results.chunks}
    current = family_index(results.facts, urls)
    if state.previous_families:
        diff = diff_fact_keys(state.previous_families, current, frozenset(p.url for p in pages))
        state.degraded = state.degraded | diff.degraded
        for entity, attribute, vanished in diff.anomalies:
            _log.warning(
                "fact keys vanished",
                extra={"entity": entity, "attribute": attribute, "vanished": vanished},
            )
    state.previous_families = current


def _rebuild_conflicts(store: Store) -> None:
    """Rebuild the conflicts table from all facts; a failure here leaves it empty, logged."""
    try:
        records = detect_conflicts(store.all_fact_versions(), snapshots=store.snapshots)
        store.replace_conflicts(records)
    except Exception:  # noqa: BLE001 - conflict analysis must not lose the ingested index
        _log.exception("conflict detection failed")
