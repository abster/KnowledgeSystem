"""Hybrid retrieval and candidate facts (SPEC 9.1, 9.2).

Retrieval embeds the question, runs a vector and a BM25 search (both with the validity
pre-filter), and fuses them with reciprocal rank fusion. Candidate facts are the facts of the
retrieved chunks (and of the rows under a retrieved parent), narrowed by the parsed question,
grouped by fact key and resolved with the conflict rules of SPEC 10.3.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from kk.config import Retrieval
from kk.conflicts import Resolution, resolve_key_at
from kk.ingest.embed import EmbedderLike
from kk.query.candidates import find_siblings, remove_contradicted
from kk.query.confidence import qualifier_signal
from kk.query.normalise import clean_text
from kk.store import Store, StoredHit
from kk.types import Chunk, FactVersion, ParsedQuestion, SnapshotRef

RATE_FAMILY = frozenset({"rate", "apy", "apr"})  # CAND-2


@dataclass(frozen=True)
class RankedChunk:
    """A fused hit: the chunk, its RRF score and the raw scores of each search (RET-4)."""

    chunk: Chunk
    first_seen_ord: int
    rrf: float
    cosine: float | None
    bm25: float | None

    @property
    def chunk_id(self) -> str:
        """The chunk's id."""
        return self.chunk.chunk_id


@dataclass(frozen=True)
class CandidateGroup:
    """The candidates of one fact key, resolved to a winner (CAND-3, CAND-4)."""

    fact_key: str
    versions: tuple[FactVersion, ...]
    resolution: Resolution
    score: float
    qualifier: float
    entity: float
    rrf: float

    @property
    def winner(self) -> FactVersion | None:
        """The winning fact version, or ``None`` for an unresolved conflict."""
        return self.resolution.winner


def max_rrf(cfg: Retrieval) -> float:
    """The highest possible RRF score: a chunk ranked first by both searches."""
    return 2.0 / (cfg.rrf_k + 1)


def fuse(
    vector_hits: Sequence[StoredHit], text_hits: Sequence[StoredHit], cfg: Retrieval
) -> list[RankedChunk]:
    """RET-3: ``1 / (rrf_k + rank)`` summed per chunk, top ``top_k``, ties by lower chunk id."""
    scores: dict[str, float] = defaultdict(float)
    cosine: dict[str, float] = {}
    bm25: dict[str, float] = {}
    seen: dict[str, StoredHit] = {}
    for rank, hit in enumerate(vector_hits, start=1):
        scores[hit.chunk.chunk_id] += 1.0 / (cfg.rrf_k + rank)
        cosine[hit.chunk.chunk_id] = hit.score
        seen.setdefault(hit.chunk.chunk_id, hit)
    for rank, hit in enumerate(text_hits, start=1):
        scores[hit.chunk.chunk_id] += 1.0 / (cfg.rrf_k + rank)
        bm25[hit.chunk.chunk_id] = hit.score
        seen.setdefault(hit.chunk.chunk_id, hit)
    order = sorted(scores, key=lambda cid: (-scores[cid], cid))[: cfg.top_k]
    return [
        RankedChunk(
            chunk=seen[cid].chunk,
            first_seen_ord=seen[cid].first_seen_ord,
            rrf=scores[cid],
            cosine=cosine.get(cid),
            bm25=bm25.get(cid),
        )
        for cid in order
    ]


def retrieve(
    store: Store, embedder: EmbedderLike, question: str, target_ord: int, cfg: Retrieval
) -> list[RankedChunk]:
    """RET-1 to RET-4: embed the question, search both indices, fuse."""
    text = clean_text(question)
    vector_hits = store.search_vector(embedder.embed_query(text), target_ord, cfg.per_index_k)
    text_hits = store.search_text(text, target_ord, cfg.per_index_k)
    return fuse(vector_hits, text_hits, cfg)


def _measure_ok(attribute: str, measure: str | None) -> bool:
    if measure is None:
        return True  # no measure stated: rate, APY and APR candidates stay together (CAND-2)
    if measure == "rate":
        return attribute in RATE_FAMILY
    return attribute == measure


def candidate_facts(  # noqa: PLR0913
    store: Store,
    ranked: Sequence[RankedChunk],
    parsed: ParsedQuestion,
    *,
    target_ord: int,
    rrf_k: int,
    snapshots: Sequence[SnapshotRef],
    amount_ok: bool | None = None,
) -> list[CandidateGroup]:
    """CAND-1 to CAND-4: narrow, score, group and resolve the facts of the retrieved chunks.

    ``amount_ok`` is the tier-selection result for the qualifier signal (SPEC 12.1); the
    caller supplies it once it has run tier selection. Without a unique entity link the entity
    score is 0, so the groups are ordered by retrieval score and then fact key.
    """
    if not ranked:
        return []
    rrf_of = {r.chunk_id: r.rrf for r in ranked}
    for child, parent_id in store.child_parents(list(rrf_of), target_ord).items():
        rrf_of.setdefault(child, rrf_of[parent_id])
    versions = store.facts_for_chunks(list(rrf_of), target_ord)
    asked = parsed.qualifier_map
    kept = [
        v
        for v in versions
        if (parsed.entity is None or v.fact.entity == parsed.entity)
        and _measure_ok(v.fact.attribute, parsed.measure)
        and remove_contradicted([v.fact], qualifiers=asked)
    ]
    has_siblings = find_siblings([v.fact for v in kept]) is not None
    scale = 2.0 / (rrf_k + 1)
    best: dict[str, tuple[float, float, float, float]] = {}
    for version in kept:
        r = min(1.0, rrf_of.get(version.fact.chunk_id, 0.0) / scale)
        q = qualifier_signal(
            asked,
            version.fact,
            measure=parsed.measure,
            amount_ok=amount_ok,
            has_siblings=has_siblings,
        )
        e = parsed.entity_score if parsed.entity == version.fact.entity else 0.0
        score = r * (0.5 + 0.5 * q) * e
        key = version.fact.fact_key
        current = best.get(key)
        if current is None or (score, rrf_of.get(version.fact.chunk_id, 0.0)) > (
            current[0],
            current[3],
        ):
            best[key] = (score, q, e, rrf_of.get(version.fact.chunk_id, 0.0))
    groups = []
    for fact_key, (score, q, e, rrf) in best.items():
        everything = store.facts_by_key([fact_key], target_ord)
        resolution = resolve_key_at(everything, ordinal=target_ord, snapshots=snapshots)
        group_versions = tuple(v for v in everything if v.visible_at(target_ord))
        groups.append(CandidateGroup(fact_key, group_versions, resolution, score, q, e, rrf))
    return sorted(groups, key=lambda g: (-g.score, -g.rrf, g.fact_key))
