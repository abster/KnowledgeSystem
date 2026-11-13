"""Tests for the LanceDB store (SPEC 5, 6.4, 6.8, 6.10; Step 7)."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from contract import Source
from kk.ingest.dedupe import make_chunk_id
from kk.store import Store
from kk.types import Chunk, ConflictRecord, Fact, SnapshotRef
from tests.fixtures.embedder import DIM, FakeEmbedder
from tests.fixtures.facts import make_fact

URL = "https://example.test/rates/certificates"


def _ref(ordinal: int, taken_on: str) -> SnapshotRef:
    return SnapshotRef(ordinal=ordinal, snapshot_id=f"snap{ordinal}", taken_on=taken_on)


def _chunk(  # noqa: PLR0913
    text: str,
    *,
    url: str = URL,
    kind: str = "table_row",
    parent_id: str | None = None,
    effective: str | None = None,
    expiry: str | None = None,
    fetched_at: str = "2026-05-12T10:00:00Z",
) -> Chunk:
    headers = ("Certificates", "Rates")
    return Chunk(
        chunk_id=make_chunk_id(url, headers, text),
        url=url,
        file_name=url.rsplit("/", 1)[-1],
        title="Certificates",
        parent_headers=headers,
        chunk_type=kind,  # type: ignore[arg-type]
        parent_id=parent_id,
        text=text,
        effective_date=effective,
        expiry_date=expiry,
        source_class="rate_sheet",
        authority_rank=6,
        fetched_at=fetched_at,
        position=3,
        row_index=1 if kind == "table_row" else None,
        row_count=2 if kind in {"table_row", "table"} else None,
        footer_count=0 if kind in {"table_row", "table"} else None,
    )


def _fact(chunk: Chunk, value: str = "3.50%", **kwargs: object) -> Fact:
    base = make_fact(value=value, chunk_id=chunk.chunk_id, rank=chunk.authority_rank)
    return replace(base, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "lance", max_batch_chars=8000)


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


def test_opens_with_empty_tables_and_survives_reopening(tmp_path: Path) -> None:
    first = Store(tmp_path / "lance", max_batch_chars=8000)
    assert [first.row_count(t) for t in ("chunks", "facts", "conflicts")] == [0, 0, 0]
    first.add_snapshot(
        _ref(1, "2026-05-12"), [_chunk("6 month certificate 3.50%")], [], FakeEmbedder()
    )
    reopened = Store(tmp_path / "lance", max_batch_chars=8000)
    assert reopened.row_count("chunks") == 1


def test_only_searchable_chunk_types_are_embedded(store: Store, embedder: FakeEmbedder) -> None:
    table = _chunk("Certificates table header and rows", kind="table")
    row = _chunk("6 month certificate APY 3.50%", parent_id=table.chunk_id)
    para = _chunk("Certificates are insured by the NCUA to the legal limit.", kind="text_paragraph")
    item = _chunk("Open a certificate online or in a branch", kind="list_item")
    delta = store.add_snapshot(_ref(1, "2026-05-12"), [table, row, para, item], [], embedder)
    assert delta.embedded == 3
    assert sorted(embedder.document_texts) == sorted([row.text, para.text, item.text])
    assert store.row_count("chunks") == 4
    assert store.get_chunk(table.chunk_id, 1) == table
    assert store.get_chunk(row.chunk_id, 1) == row  # lossless round trip of every field


def test_vectors_are_unit_length_and_dimension_384(store: Store, embedder: FakeEmbedder) -> None:
    store.add_snapshot(
        _ref(1, "2026-05-12"), [_chunk("6 month certificate APY 3.50%")], [], embedder
    )
    (hit,) = store.search_vector(embedder.embed_query("6 month certificate"), 1, 5)
    assert hit.vector is not None
    assert len(hit.vector) == DIM
    assert abs(float(sum(v * v for v in hit.vector)) - 1.0) < 1e-5


def test_unchanged_chunk_extends_its_interval_without_embedding(
    store: Store, embedder: FakeEmbedder
) -> None:
    chunks = [_chunk("6 month certificate APY 3.50%"), _chunk("12 month certificate APY 3.80%")]
    store.add_snapshot(_ref(1, "2026-05-12"), chunks, [], embedder)
    calls_before = len(embedder.document_texts)
    delta = store.add_snapshot(_ref(2, "2026-05-19"), chunks, [], embedder)
    assert (delta.new_chunks, delta.extended_chunks, delta.embedded) == (0, 2, 0)
    assert len(embedder.document_texts) == calls_before
    assert store.row_count("chunks") == 2
    rows = store.intervals_of(chunks[0].chunk_id)
    assert [(r.first_seen_ord, r.last_seen_ord) for r in rows] == [(1, 2)]
    assert rows[0].last_seen_date == "2026-05-19"


def test_extension_updates_fetched_at(store: Store, embedder: FakeEmbedder) -> None:
    chunk = _chunk("6 month certificate APY 3.50%")
    store.add_snapshot(_ref(1, "2026-05-12"), [chunk], [], embedder)
    later = replace(chunk, fetched_at="2026-05-19T09:00:00Z")
    store.add_snapshot(_ref(2, "2026-05-19"), [later], [], embedder)
    assert store.get_chunk(chunk.chunk_id, 2) == later


def test_reappearing_chunk_gets_a_second_interval_and_reuses_its_vector(
    store: Store, embedder: FakeEmbedder
) -> None:
    gone = _chunk("6 month certificate APY 3.50%")
    stays = _chunk("Dividends are compounded monthly on all certificates", kind="text_paragraph")
    store.add_snapshot(_ref(1, "2026-05-12"), [gone, stays], [], embedder)
    store.add_snapshot(_ref(2, "2026-05-19"), [stays], [], embedder)
    calls_before = len(embedder.document_texts)
    delta = store.add_snapshot(_ref(3, "2026-06-09"), [gone, stays], [], embedder)
    assert (delta.new_chunks, delta.extended_chunks, delta.embedded) == (1, 1, 0)
    assert len(embedder.document_texts) == calls_before  # the vector was reused (EMB-3)
    rows = store.intervals_of(gone.chunk_id)
    assert [(r.first_seen_ord, r.last_seen_ord) for r in rows] == [(1, 1), (3, 3)]
    assert store.row_count("chunks") == 3


def test_changed_metadata_opens_a_new_interval_and_reuses_the_vector(
    store: Store, embedder: FakeEmbedder
) -> None:
    old = _chunk("6 month certificate APY 3.50%", effective="2026-05-01")
    new = replace(old, effective_date="2026-05-15")
    store.add_snapshot(_ref(1, "2026-05-12"), [old], [], embedder)
    calls_before = len(embedder.document_texts)
    delta = store.add_snapshot(_ref(2, "2026-05-19"), [new], [], embedder)
    assert (delta.new_chunks, delta.extended_chunks, delta.embedded) == (1, 0, 0)
    assert len(embedder.document_texts) == calls_before
    assert store.get_chunk(old.chunk_id, 1) == old
    assert store.get_chunk(old.chunk_id, 2) == new


def test_validity_prefilter_hides_chunks_outside_the_target_interval(
    store: Store, embedder: FakeEmbedder
) -> None:
    early = _chunk("6 month certificate APY 2.95%")
    late = _chunk("6 month certificate APY 3.50%")
    both = _chunk("6 month certificate minimum is 1000 dollars", kind="text_paragraph")
    store.add_snapshot(_ref(1, "2026-05-12"), [early, both], [], embedder)
    store.add_snapshot(_ref(2, "2026-05-19"), [both], [], embedder)
    store.add_snapshot(_ref(3, "2026-06-09"), [late, both], [], embedder)
    query = "6 month certificate APY"
    for target, expected in ((1, {early, both}), (2, {both}), (3, {late, both}), (4, set())):
        by_vector = store.search_vector(embedder.embed_query(query), target, 50)
        by_text = store.search_text(query, target, 50)
        assert {h.chunk.chunk_id for h in by_vector} <= {c.chunk_id for c in expected}, target
        assert {h.chunk.chunk_id for h in by_text} <= {c.chunk_id for c in expected}, target
        if expected:
            assert {h.chunk.chunk_id for h in by_vector} == {c.chunk_id for c in expected}
            assert {h.chunk.chunk_id for h in by_text} == {c.chunk_id for c in expected}


def test_text_search_ignores_parent_table_rows(store: Store, embedder: FakeEmbedder) -> None:
    table = _chunk("certificate certificate certificate rates table", kind="table")
    row = _chunk("6 month certificate APY 3.50%", parent_id=table.chunk_id)
    store.add_snapshot(_ref(1, "2026-05-12"), [table, row], [], embedder)
    hits = store.search_text("certificate", 1, 10)
    assert [h.chunk.chunk_id for h in hits] == [row.chunk_id]
    assert hits[0].score > 0


def test_vector_search_ranks_the_closest_text_first_and_scores_cosine(
    store: Store, embedder: FakeEmbedder
) -> None:
    near = _chunk("auto loan rate for new vehicles 5.39%")
    far = _chunk("6 month certificate rate 3.50%")
    store.add_snapshot(_ref(1, "2026-05-12"), [near, far], [], embedder)
    hits = store.search_vector(embedder.embed_query("auto loan rate new vehicles"), 1, 2)
    assert [h.chunk.chunk_id for h in hits] == [near.chunk_id, far.chunk_id]
    assert 0.0 < hits[1].score < hits[0].score <= 1.0


def test_reingesting_an_identical_snapshot_adds_no_rows(
    store: Store, embedder: FakeEmbedder
) -> None:
    chunk = _chunk("6 month certificate APY 3.50%")
    fact = _fact(chunk)
    store.add_snapshot(_ref(1, "2026-05-12"), [chunk], [fact], embedder)
    counts = (store.row_count("chunks"), store.row_count("facts"))
    delta = store.add_snapshot(_ref(2, "2026-05-19"), [chunk], [fact], embedder)
    assert (store.row_count("chunks"), store.row_count("facts")) == counts
    assert (delta.new_chunks, delta.new_facts, delta.extended_facts) == (0, 0, 1)


def test_fact_rows_follow_the_chunk_interval_and_have_the_spec_id(
    store: Store, embedder: FakeEmbedder
) -> None:
    chunk = _chunk("6 month certificate APY 3.50%", effective="2026-05-01")
    fact = _fact(chunk, effective_date="2026-05-01")
    store.add_snapshot(_ref(1, "2026-05-12"), [chunk], [fact], embedder)
    store.add_snapshot(_ref(2, "2026-05-19"), [chunk], [fact], embedder)
    (version,) = store.facts_by_key([fact.fact_key])
    assert (version.first_seen_ord, version.last_seen_ord) == (1, 2)
    expected = hashlib.sha256(f"{fact.fact_key}3.50%{chunk.chunk_id}1".encode()).hexdigest()
    assert store.fact_ids() == [expected]
    assert version.fact == fact
    assert version.fact.value_num == Decimal("3.50")
    assert version.url == URL
    assert version.interval.chunk.title == "Certificates"


def test_a_fact_whose_chunk_gets_a_new_interval_is_a_new_fact_row(
    store: Store, embedder: FakeEmbedder
) -> None:
    old = _chunk("6 month certificate APY 3.50%", effective="2026-05-01")
    new = replace(old, effective_date="2026-05-15")
    fact_old = _fact(old, effective_date="2026-05-01")
    fact_new = _fact(new, effective_date="2026-05-15")
    store.add_snapshot(_ref(1, "2026-05-12"), [old], [fact_old], embedder)
    delta = store.add_snapshot(_ref(2, "2026-05-19"), [new], [fact_new], embedder)
    assert (delta.new_facts, delta.extended_facts) == (1, 0)
    versions = sorted(store.facts_by_key([fact_old.fact_key]), key=lambda v: v.first_seen_ord)
    assert [(v.first_seen_ord, v.last_seen_ord, v.fact.effective_date) for v in versions] == [
        (1, 1, "2026-05-01"),
        (2, 2, "2026-05-15"),
    ]


def test_direct_store_query_at_an_older_ordinal_returns_the_older_value(
    store: Store, embedder: FakeEmbedder
) -> None:
    old = _chunk("6 month certificate APY 2.95%")
    new = _chunk("6 month certificate APY 3.50%")
    store.add_snapshot(_ref(1, "2026-05-12"), [old], [_fact(old, "2.95%")], embedder)
    store.add_snapshot(_ref(2, "2026-05-19"), [new], [_fact(new, "3.50%")], embedder)
    at = lambda ordinal: [v.fact.value_text for v in store.visible_fact_versions(ordinal)]  # noqa: E731
    assert at(1) == ["2.95%"]
    assert at(2) == ["3.50%"]


def test_facts_for_chunks_applies_the_validity_filter(store: Store, embedder: FakeEmbedder) -> None:
    old = _chunk("6 month certificate APY 2.95%")
    new = _chunk("6 month certificate APY 3.50%")
    store.add_snapshot(_ref(1, "2026-05-12"), [old], [_fact(old, "2.95%")], embedder)
    store.add_snapshot(_ref(2, "2026-05-19"), [new], [_fact(new, "3.50%")], embedder)
    ids = [old.chunk_id, new.chunk_id]
    assert [v.fact.value_text for v in store.facts_for_chunks(ids, 1)] == ["2.95%"]
    assert [v.fact.value_text for v in store.facts_for_chunks(ids, 2)] == ["3.50%"]
    assert store.facts_for_chunks([], 2) == []


def test_children_of_lists_the_rows_of_a_parent_table(store: Store, embedder: FakeEmbedder) -> None:
    table = _chunk("Certificates table", kind="table")
    rows = [_chunk(f"{n} month certificate", parent_id=table.chunk_id) for n in (6, 12)]
    store.add_snapshot(_ref(1, "2026-05-12"), [table, *rows], [], embedder)
    assert sorted(store.children_of([table.chunk_id], 1)) == sorted(r.chunk_id for r in rows)
    assert store.children_of([table.chunk_id], 2) == []


def test_snapshots_list_keeps_ordinals_and_dates(store: Store, embedder: FakeEmbedder) -> None:
    store.add_snapshot(_ref(1, "2026-05-12"), [], [], embedder)
    store.add_snapshot(_ref(2, "garbage"), [], [], embedder)
    assert [(s.ordinal, s.snapshot_id, s.taken_on) for s in store.snapshots] == [
        (1, "snap1", "2026-05-12"),
        (2, "snap2", "garbage"),
    ]


def test_conflicts_table_is_replaced_in_full_and_round_trips(store: Store) -> None:
    def record(n: int) -> ConflictRecord:
        return ConflictRecord(
            conflict_id=f"id{n}",
            topic=f"6-month certificate APY {n}",
            fact_key=f"key{n}",
            values=("2.95%", "3.50%"),
            sources=(Source(URL, "snap2", "quote a"), Source(URL + "2", "snap2", "quote b")),
            resolution="3.50%",
            strategy="effective_date",
            reason="strategy=effective_date: 3.50% supersedes 2.95%",
            ords=(1, 2),
        )

    store.replace_conflicts([record(1), record(2)])
    assert store.conflict_records() == [record(1), record(2)]
    store.replace_conflicts([record(3)])
    assert store.conflict_records() == [record(3)]
    store.replace_conflicts([replace(record(4), resolution=None, strategy="unresolved")])
    (only,) = store.conflict_records()
    assert only.resolution is None


def test_empty_store_answers_every_query_with_nothing(store: Store, embedder: FakeEmbedder) -> None:
    assert store.search_vector(embedder.embed_query("anything"), 1, 5) == []
    assert store.search_text("anything", 1, 5) == []
    assert store.facts_by_key(["nope"]) == []
    assert store.visible_fact_versions(1) == []
    assert store.conflict_records() == []
