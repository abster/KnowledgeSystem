"""Tests for hybrid retrieval and candidate facts (SPEC 9.1, 9.2; Step 7)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from kk.config import Retrieval, load_config
from kk.ingest.aliases import build_aliases
from kk.ingest.dedupe import make_chunk_id
from kk.query.question import EntityLinker, parse
from kk.query.retrieve import (
    RankedChunk,
    candidate_facts,
    dedupe_hits,
    fuse,
    max_rrf,
    retrieve,
    retrieve_deep,
)
from kk.store import Store, StoredHit
from kk.types import Chunk, Fact, SnapshotRef
from tests.fixtures.embedder import FakeEmbedder
from tests.fixtures.facts import make_fact

CFG = Retrieval(
    top_k=3,
    per_index_k=40,
    rrf_k=60,
    slow_top_k=9,
    slow_per_index_k=90,
    slow_pool_k=400,
    rerank_candidates=50,
    rerank_keep=10,
)


def _hit(chunk_id: str, score: float, first: int = 1) -> StoredHit:
    chunk = Chunk(
        chunk_id=chunk_id,
        url="https://example.test/p",
        file_name="p",
        title="P",
        parent_headers=("P",),
        chunk_type="table_row",
        parent_id=None,
        text=chunk_id,
        effective_date=None,
        expiry_date=None,
        source_class="other",
        authority_rank=3,
        fetched_at="f",
        position=0,
    )
    return StoredHit(
        chunk=chunk, first_seen_ord=first, last_seen_ord=first, score=score, vector=None
    )


def test_fusion_sums_reciprocal_ranks_across_both_lists() -> None:
    vector = [_hit("a", 0.9), _hit("b", 0.8), _hit("c", 0.7)]
    text = [_hit("b", 5.0), _hit("d", 4.0), _hit("a", 3.0)]
    fused = fuse(vector, text, CFG)
    scores = {r.chunk_id: r.rrf for r in fused}
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 63)
    assert [r.chunk_id for r in fused] == ["b", "a", "d"]  # top_k = 3 keeps d (1/62) over c (1/63)


def test_fusion_keeps_raw_cosine_and_bm25_of_each_hit() -> None:
    fused = fuse([_hit("a", 0.9)], [_hit("a", 7.5), _hit("z", 2.0)], CFG)
    first = next(r for r in fused if r.chunk_id == "a")
    assert (first.cosine, first.bm25) == (0.9, 7.5)
    only_text = next(r for r in fused if r.chunk_id == "z")
    assert (only_text.cosine, only_text.bm25) == (None, 2.0)


def test_fusion_ties_break_on_lower_chunk_id() -> None:
    vector = [_hit("m", 0.9), _hit("k", 0.8)]
    text = [_hit("k", 5.0), _hit("m", 4.0)]  # m and k tie at 1/61 + 1/62
    fused = fuse(vector, text, CFG.model_copy(update={"top_k": 2}))
    assert [r.chunk_id for r in fused] == ["k", "m"]


def test_fusion_is_deterministic_and_cuts_at_top_k() -> None:
    vector = [_hit(f"v{i}", 1.0 - i / 100) for i in range(10)]
    text = [_hit(f"t{i}", 10.0 - i) for i in range(10)]
    one = fuse(vector, text, CFG)
    assert one == fuse(vector, text, CFG)
    assert len(one) == 3


def test_fusion_of_nothing_is_empty() -> None:
    assert fuse([], [], CFG) == []


def test_max_rrf_is_a_hit_ranked_first_in_both_lists() -> None:
    assert max_rrf(CFG) == pytest.approx(2 / 61)


def test_one_chunk_ranked_first_in_both_scores_the_maximum() -> None:
    (top,) = fuse([_hit("a", 0.9)], [_hit("a", 4.0)], CFG.model_copy(update={"top_k": 1}))
    assert top.rrf == pytest.approx(max_rrf(CFG))


# --- retrieval over a store -------------------------------------------------------------------


def _chunk(text: str, *, url: str = "https://example.test/rates", kind: str = "table_row") -> Chunk:
    headers = ("Rates",)
    return Chunk(
        chunk_id=make_chunk_id(url, headers, text),
        url=url,
        file_name="rates",
        title="Rates",
        parent_headers=headers,
        chunk_type=kind,  # type: ignore[arg-type]
        parent_id=None,
        text=text,
        effective_date=None,
        expiry_date=None,
        source_class="rate_sheet",
        authority_rank=6,
        fetched_at="2026-05-12T10:00:00Z",
        position=0,
    )


@pytest.fixture
def populated(tmp_path: Path) -> tuple[Store, FakeEmbedder, dict[str, Chunk]]:
    embedder = FakeEmbedder()
    store = Store(tmp_path / "lance", max_batch_chars=8000)
    chunks = {
        "cert6": _chunk("6 month certificate APY 3.50%"),
        "cert12": _chunk("12 month certificate APY 3.80%"),
        "auto": _chunk("auto loan 60 months APR 5.39%"),
        "new": _chunk("6 month certificate APY 4.00% new offer"),
    }
    store.add_snapshot(
        SnapshotRef(ordinal=1, snapshot_id="s1", taken_on="2026-05-12"),
        [chunks["cert6"], chunks["cert12"], chunks["auto"]],
        [],
        embedder,
    )
    store.add_snapshot(
        SnapshotRef(ordinal=2, snapshot_id="s2", taken_on="2026-05-19"),
        [chunks["cert12"], chunks["auto"], chunks["new"]],
        [],
        embedder,
    )
    return store, embedder, chunks


def test_retrieve_puts_the_relevant_chunk_first(
    populated: tuple[Store, FakeEmbedder, dict[str, Chunk]],
) -> None:
    store, embedder, chunks = populated
    ranked = retrieve(store, embedder, "6 month certificate APY", 2, load_config().retrieval)
    assert ranked[0].chunk.chunk_id == chunks["new"].chunk_id
    assert all(isinstance(r, RankedChunk) for r in ranked)


def test_retrieve_never_returns_a_chunk_outside_the_target_ordinal(
    populated: tuple[Store, FakeEmbedder, dict[str, Chunk]],
) -> None:
    store, embedder, chunks = populated
    at_one = retrieve(store, embedder, "6 month certificate APY", 1, load_config().retrieval)
    ids = {r.chunk.chunk_id for r in at_one}
    assert chunks["new"].chunk_id not in ids
    assert chunks["cert6"].chunk_id in ids
    at_two = retrieve(store, embedder, "6 month certificate APY", 2, load_config().retrieval)
    assert chunks["cert6"].chunk_id not in {r.chunk.chunk_id for r in at_two}


def test_retrieve_embeds_the_question_as_a_query_and_is_repeatable(
    populated: tuple[Store, FakeEmbedder, dict[str, Chunk]],
) -> None:
    store, embedder, _ = populated
    cfg = load_config().retrieval
    first = retrieve(store, embedder, "auto loan APR", 2, cfg)
    assert embedder.query_texts == ["auto loan APR"]
    assert first == retrieve(store, embedder, "auto loan APR", 2, cfg)


def test_retrieve_on_an_empty_store_is_empty(tmp_path: Path) -> None:
    store = Store(tmp_path / "empty", max_batch_chars=8000)
    assert retrieve(store, FakeEmbedder(), "anything", 1, load_config().retrieval) == []


# --- candidate facts --------------------------------------------------------------------------

CERT = "certificate"


def _linker() -> EntityLinker:
    return EntityLinker(build_aliases(["Certificate", "Share Savings", "Auto Loan"]))


def _ranked(chunk: Chunk, rrf: float) -> RankedChunk:
    return RankedChunk(chunk=chunk, first_seen_ord=1, rrf=rrf, cosine=0.8, bm25=3.0)


def _store_with_facts(tmp_path: Path, facts: list[tuple[Chunk, Fact]]) -> Store:
    store = Store(tmp_path / "lance", max_batch_chars=8000)
    store.add_snapshot(
        SnapshotRef(ordinal=1, snapshot_id="s1", taken_on="2026-05-12"),
        [c for c, _ in facts],
        [f for _, f in facts],
        FakeEmbedder(),
    )
    return store


def _fact_in(chunk: Chunk, **kwargs: object) -> Fact:
    return make_fact(chunk_id=chunk.chunk_id, rank=chunk.authority_rank, **kwargs)  # type: ignore[arg-type]


def test_candidates_drop_other_entities_when_the_link_is_unique(tmp_path: Path) -> None:
    c1, c2 = _chunk("cert row"), _chunk("savings row")
    cert = _fact_in(c1)
    savings = _fact_in(c2, entity="share savings", label="Share Savings", value="0.20%")
    store = _store_with_facts(tmp_path, [(c1, cert), (c2, savings)])
    parsed = parse("What is the certificate APY?", _linker(), target_ord=1)
    assert parsed.entity == CERT
    groups = candidate_facts(
        store,
        [_ranked(c1, 0.03), _ranked(c2, 0.03)],
        parsed,
        target_ord=1,
        rrf_k=60,
        snapshots=(SnapshotRef(ordinal=1, snapshot_id="s1", taken_on="2026-05-12"),),
    )
    assert [g.fact_key for g in groups] == [cert.fact_key]


def test_candidates_keep_only_the_asked_measure(tmp_path: Path) -> None:
    c1 = _chunk("row")
    apy = _fact_in(c1, attribute="apy", value="3.50%")
    divd = _fact_in(c1, attribute="dividend_rate", value="3.45%")
    store = _store_with_facts(tmp_path, [(c1, apy), (c1, divd)])
    parsed = parse("What is the certificate APY?", _linker(), target_ord=1)
    assert parsed.measure == "apy"
    groups = candidate_facts(
        store, [_ranked(c1, 0.03)], parsed, target_ord=1, rrf_k=60,
        snapshots=(SnapshotRef(ordinal=1, snapshot_id="s1", taken_on="2026-05-12"),),
    )  # fmt: skip
    assert {g.winner.fact.attribute for g in groups if g.winner} == {"apy"}


def test_candidate_score_follows_the_spec_formula(tmp_path: Path) -> None:
    c1 = _chunk("row")
    fact = _fact_in(c1)
    store = _store_with_facts(tmp_path, [(c1, fact)])
    parsed = parse("What is the certificate APY?", _linker(), target_ord=1)
    (group,) = candidate_facts(
        store, [_ranked(c1, 1 / 61)], parsed, target_ord=1, rrf_k=60,
        snapshots=(SnapshotRef(ordinal=1, snapshot_id="s1", taken_on="2026-05-12"),),
    )  # fmt: skip
    r = (1 / 61) / (2 / 61)
    assert group.score == pytest.approx(r * (0.5 + 0.5 * group.qualifier) * group.entity)
    assert 0.0 <= group.score <= 1.0


def test_candidates_group_by_fact_key_and_resolve_a_conflict(tmp_path: Path) -> None:
    a = _chunk("promo row", url="https://example.test/promo")
    b = _chunk("rate row", url="https://example.test/certificates")
    promo = _fact_in(a, value="2.95%", expiry="2025-09-30")
    live = _fact_in(b, value="3.50%")
    assert promo.fact_key == live.fact_key
    store = _store_with_facts(tmp_path, [(a, promo), (b, live)])
    parsed = parse("What is the certificate APY?", _linker(), target_ord=1)
    (group,) = candidate_facts(
        store, [_ranked(a, 0.03), _ranked(b, 0.02)], parsed, target_ord=1,
        rrf_k=60,
        snapshots=(SnapshotRef(ordinal=1, snapshot_id="s1", taken_on="2026-05-12"),),
    )  # fmt: skip
    assert group.resolution.outcome == "resolved"
    assert group.winner is not None
    assert group.winner.fact.value_text == "3.50%"
    assert group.resolution.strategy == "effective_date"


def test_candidates_include_facts_of_child_rows_of_a_hit_parent(tmp_path: Path) -> None:
    parent = replace(_chunk("table parent", kind="table"))
    child = replace(_chunk("child row"), parent_id=parent.chunk_id)
    fact = _fact_in(child)
    store = _store_with_facts(
        tmp_path,
        [(parent, _fact_in(parent, attribute="min_to_open", value="$1.00")), (child, fact)],
    )
    parsed = parse("What is the certificate APY?", _linker(), target_ord=1)
    ranked = [RankedChunk(parent, 1, 0.03, 0.5, 2.0)]
    groups = candidate_facts(
        store, ranked, parsed, target_ord=1, rrf_k=60,
        snapshots=(SnapshotRef(ordinal=1, snapshot_id="s1", taken_on="2026-05-12"),),
    )  # fmt: skip
    values = {g.winner.fact.value_text for g in groups if g.winner}
    assert "3.50%" in values


def test_no_hits_means_no_candidates(tmp_path: Path) -> None:
    store = Store(tmp_path / "lance", max_batch_chars=8000)
    parsed = parse("What is the certificate APY?", _linker(), target_ord=1)
    assert candidate_facts(store, [], parsed, target_ord=1, rrf_k=60, snapshots=()) == []


# --- boilerplate collapse for the slow path (SRET-4) -----------------------------------------


def _text_hit(chunk_id: str, text: str, url: str = "https://example.test/p") -> StoredHit:
    base = _hit(chunk_id, 1.0)
    return replace(base, chunk=replace(base.chunk, text=text, url=url))


def test_identical_text_on_several_pages_is_kept_once_at_its_best_rank() -> None:
    hits = [
        _text_hit("a", "Becoming a  Dupaco member", "https://example.test/1"),
        _text_hit("b", "becoming a dupaco MEMBER", "https://example.test/2"),
        _text_hit("c", "You live or work in the charter area."),
    ]
    assert [h.chunk.chunk_id for h in dedupe_hits(hits, 10)] == ["a", "c"]


def test_dedupe_stops_at_the_limit_and_keeps_the_order() -> None:
    hits = [_text_hit(f"id{i}", f"text {i}") for i in range(6)]
    assert [h.chunk.chunk_id for h in dedupe_hits(hits, 3)] == ["id0", "id1", "id2"]


def test_empty_text_is_never_collapsed_with_another_empty_chunk() -> None:
    hits = [_text_hit("a", ""), _text_hit("b", "")]
    assert len(dedupe_hits(hits, 5)) == 2


def test_deep_retrieval_reaches_a_chunk_that_copies_of_boilerplate_pushed_down(
    tmp_path: Path,
) -> None:
    embedder = FakeEmbedder()
    store = Store(tmp_path / "lance", max_batch_chars=8000)
    boiler = "Becoming a Dupaco member today"
    copies = [_chunk(boiler, url=f"https://e.test/{i}", kind="paragraph") for i in range(30)]
    target = _chunk(
        "Becoming a Dupaco member needs you to live or work in the charter area",
        url="https://e.test/join",
        kind="paragraph",
    )
    store.add_snapshot(
        SnapshotRef(ordinal=1, snapshot_id="s1", taken_on="2026-01-01"),
        [*copies, target],
        [],
        embedder,
    )
    cfg = CFG.model_copy(update={"top_k": 5, "per_index_k": 5, "slow_pool_k": 100})
    shallow = {h.chunk_id for h in retrieve(store, embedder, boiler, 1, cfg)}
    deep = retrieve_deep(store, embedder, boiler, 1, cfg)
    assert target.chunk_id not in shallow
    assert target.chunk_id in {h.chunk_id for h in deep}
    assert len([h for h in deep if h.chunk.text == boiler]) == 1
