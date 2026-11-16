"""The fast-path answer pipeline (SPEC 8 to 12, 14, 16).

Stages, each in its own error boundary: ``as_of``, parse, cache lookup, retrieval, candidate
facts, sibling selection, template, confidence and routing, validators. A question routed to
the slow path, a fast answer that fails the validators and a stage that raises after retrieval
all go to the slow path (SPEC 12.2, 16); without a client, or if the slow answer is not
accepted, the result is the safe refusal.

``Answerer.evaluate`` reports the whole story (route, confidence, caps, the answer the fast
path would give); calibration reads it. ``answer`` returns only the ``Answer`` and never raises.
"""

from __future__ import annotations

import contextlib
import hashlib
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

from contract import Answer, Source
from kk.config import Config
from kk.conflicts import normalised_value, resolve_key_at
from kk.ingest.aliases import build_aliases
from kk.ingest.embed import EmbedderLike
from kk.ingest.pipeline import IngestState
from kk.log import get_logger
from kk.query.asof import target_ordinal
from kk.query.cache import AnswerCache
from kk.query.candidates import (
    History,
    Segment,
    Selection,
    build_history,
    select_siblings,
)
from kk.query.confidence import (
    CapInputs,
    Signals,
    cap_inputs_from_parse,
    confidence,
    hard_caps,
    margin_signal,
    resolution_kind,
    route,
    similarity_signal,
    template_for,
)
from kk.query.question import LABEL_SCORE, MAX_QUESTION_CHARS, EntityLinker, parse
from kk.query.render import (
    qualifier_phrase,
    render_address,
    render_count,
    render_fee,
    render_history,
    render_hours,
    render_quoted_prose,
    render_rate,
    safe_refusal,
)
from kk.query.rerank import Reranker
from kk.query.retrieve import (
    CandidateGroup,
    RankedChunk,
    candidate_facts,
    fuse_rankings,
    retrieve,
    retrieve_deep,
)
from kk.query.rewrite import QueryRewriter
from kk.query.slow import (
    STALE_STRATEGIES,
    ParentChunk,
    SlowInputs,
    SlowPath,
    SlowRequest,
    build_request,
    history_view,
    loser_numbers,
    loser_scopes,
    published_date,
    stale_tables_detail,
    superseded_detail,
)
from kk.query.validators import (
    Number,
    ValidationContext,
    check_refusal,
    numbers_of,
    validate,
)
from kk.store import Store
from kk.types import Chunk, Fact, FactVersion, HistoryContext, ParsedQuestion, SnapshotRef

Route = Literal["cache", "fast", "slow", "refusal"]

PROSE_TYPES = frozenset({"text_paragraph", "list_item"})
CONTEXT_WINDOW = 3  # prose chunks of context on each side of a hit (same page and heading)
MAX_HISTORY_KEYS = 5  # fact keys whose history goes into a slow-path payload

RATE_ORDER = ("rate", "apy", "apr")  # which attribute a generic "rate" question means
MAX_SOURCES = 3  # SPEC 4.1
MAX_QUOTE_CHARS = 200  # SPEC 4.1
_QUOTE_LEAD = 80  # characters kept before the value when a long quote is cut
_QUESTION_HASH_CHARS = 12

_log = get_logger("answer")


@dataclass(frozen=True)
class Evaluation:
    """What happened to one question: the route, the answer and the numbers behind it.

    ``candidate`` is the answer the fast path would give whatever the route (calibration
    labels it); ``validated`` is the validator result for it. The scoring fields (``confidence``,
    ``raw_confidence``, ``top_bm25``, ``top_cosine``, ``signals``) are ``None`` when the question
    never reached scoring; calibration recomputes the score from ``signals``.
    """

    route: Route
    answer: Answer
    reason: str
    confidence: float | None
    caps: tuple[str, ...]
    candidate: Answer | None
    validated: bool
    top_bm25: float | None
    raw_confidence: float | None = None  # ``confidence`` before the hard caps
    top_cosine: float | None = None
    signals: Signals | None = None


def _question_hash(question: str) -> str:
    digest = hashlib.sha256(question.encode("utf-8", "replace")).hexdigest()
    return digest[:_QUESTION_HASH_CHARS]


def build_linker(labels: Iterable[str], prose_entities: Iterable[str]) -> EntityLinker:
    """Entity linker over the table entity labels and the entity ids that prose facts name."""
    return EntityLinker(build_aliases([*labels, *prose_entities]))


class _StageError(Exception):
    """A pipeline stage raised; the question is refused (SPEC 16)."""

    def __init__(self, stage: str, cause: Exception) -> None:
        super().__init__(f"{stage} error: {type(cause).__name__}")
        self.stage = stage


@contextmanager
def _guard(stage: str) -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        raise _StageError(stage, exc) from exc


@dataclass(frozen=True)
class _Pick:
    """The sibling selection for the question: the chosen group and what came with it."""

    primary: CandidateGroup | None
    companion: CandidateGroup | None
    selection: Selection | None
    attribute: str | None


@dataclass(frozen=True)
class _Built:
    """A rendered fast-path answer (``None`` if no template could be filled) and its context."""

    answer: Answer | None
    ctx: ValidationContext
    history_untrusted: bool


@dataclass(frozen=True)
class _Run:
    """The inputs every stage after retrieval shares."""

    question: str
    parsed: ParsedQuestion | None  # ``None`` if parsing raised (SPEC 16: the raw question goes on)
    ordinal: int
    snapshots: Sequence[SnapshotRef]
    ranked: Sequence[RankedChunk]
    key: str | None
    allow_slow: bool


@dataclass(frozen=True)
class _Scoring:
    """What is known about the fast path when a question is escalated or refused."""

    label: str | None = None
    c: float | None = None
    caps: tuple[str, ...] = ()
    candidate: Answer | None = None
    validated: bool = False
    bm25: float | None = None
    cosine: float | None = None
    raw: float | None = None
    signals: Signals | None = None


def _fact_of(group: CandidateGroup) -> Fact:
    return group.winner.fact if group.winner is not None else group.versions[0].fact


def _attributes(groups: Sequence[CandidateGroup]) -> list[str]:
    seen: list[str] = []
    for group in groups:
        attribute = _fact_of(group).attribute
        if attribute not in seen:
            seen.append(attribute)
    return seen


def _excerpt(text: str, needle: str) -> str:
    """A verbatim excerpt of at most ``MAX_QUOTE_CHARS``, centred on ``needle`` when cut."""
    text = text.strip()
    if len(text) <= MAX_QUOTE_CHARS:
        return text
    at = max(text.find(needle), 0)
    start = max(0, at - _QUOTE_LEAD)
    return text[start : start + MAX_QUOTE_CHARS]


def _has_amount_key(fact: Fact) -> bool:
    return any(k in fact.qualifiers for k in ("amount_tier", "balance_band"))


class Answerer:
    """Runs the staged answer pipeline against a store."""

    def __init__(  # noqa: PLR0913, PLR0917
        self,
        store: Store,
        embedder: EmbedderLike,
        config: Config,
        state: IngestState,
        cache: AnswerCache,
        slow: SlowPath | None = None,
        rewriter: QueryRewriter | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        """Keep the collaborators; ``refresh`` builds the entity linker after each ingest."""
        self.slow = slow
        self.rewriter = rewriter
        self.reranker = reranker
        self._stale_tables: dict[
            int, tuple[frozenset[str], frozenset[Number], dict[Number, frozenset[str]]]
        ] = {}
        self.store = store
        self.embedder = embedder
        self.config = config
        self.state = state
        self.cache = cache
        self._linker = build_linker((), ())

    def refresh(self) -> None:
        """After an ingest: rebuild the entity linker and clear the cache (CACHE-2)."""
        self.cache.clear()
        self._stale_tables.clear()
        self._linker = build_linker(self.state.labels, self.state.prose_labels)

    # --- cache ---------------------------------------------------------------------------------

    def _cache_get(self, key: str | None) -> Answer | None:
        try:
            return self.cache.get(key)
        except Exception:  # noqa: BLE001 - a cache error is a miss (SPEC 16)
            return None

    def _cache_put(self, key: str | None, answer: Answer) -> None:
        with contextlib.suppress(Exception):  # a cache error is ignored (SPEC 16)
            self.cache.put(key, answer)

    # --- refusals ------------------------------------------------------------------------------

    @staticmethod
    def _label(parsed: ParsedQuestion | None, groups: Sequence[CandidateGroup]) -> str | None:
        if parsed is None or parsed.entity is None:
            return None
        for group in groups:
            fact = _fact_of(group)
            if fact.entity == parsed.entity:
                return fact.entity_label
        return None

    def _refuse(  # noqa: PLR0913
        self,
        question: str,
        reason: str,
        *,
        label: str | None = None,
        c: float | None = None,
        caps: tuple[str, ...] = (),
        candidate: Answer | None = None,
        validated: bool = False,
        bm25: float | None = None,
        cosine: float | None = None,
        raw: float | None = None,
        signals: Signals | None = None,
    ) -> Evaluation:
        answer = safe_refusal(question, label)
        if not check_refusal(answer, question):  # V3: fall back to the label-free text
            answer = safe_refusal(question, None)
        return Evaluation(
            "refusal", answer, reason, c, caps, candidate, validated, bm25, raw, cosine, signals
        )

    # --- the pipeline --------------------------------------------------------------------------

    def evaluate(
        self, question: str, as_of: str | None, *, use_cache: bool = True, allow_slow: bool = True
    ) -> Evaluation:
        """Run every stage and report the route, answer, confidence and candidate.

        ``use_cache=False`` neither reads nor writes the cache (calibration needs the scores of
        every paraphrase, not the stored answer of the first). ``allow_slow=False`` never calls
        the model: a question that would go there is refused (calibration measures the fast path).
        """
        try:
            return self._evaluate(question, as_of, use_cache=use_cache, allow_slow=allow_slow)
        except _StageError as err:
            _log.warning("stage failed", extra={"stage": err.stage})
            return self._refuse(question, str(err))
        except Exception as exc:  # noqa: BLE001 - answer() must always return an Answer
            _log.warning("answer failed", extra={"error": type(exc).__name__})
            return self._refuse(question, f"unexpected error: {type(exc).__name__}")

    def answer(self, question: str, as_of: str | None) -> Answer:
        """The answer only; never raises. Logs one record (LOG-1)."""
        started = time.perf_counter()
        try:
            evaluation = self.evaluate(question, as_of)
        except Exception:  # noqa: BLE001
            return safe_refusal(question if isinstance(question, str) else "", None)
        with contextlib.suppress(Exception):  # logging must never break an answer
            _log.info(
                "answer",
                extra={
                    "question_hash": _question_hash(question),
                    "route": evaluation.route,
                    "reason": evaluation.reason,
                    "confidence": evaluation.confidence,
                    "caps": list(evaluation.caps),
                    "validated": evaluation.validated,
                    "ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
        return evaluation.answer

    def _evaluate(
        self, question: str, as_of: str | None, *, use_cache: bool, allow_slow: bool
    ) -> Evaluation:
        snapshots = self.store.snapshots
        with _guard("as_of"):
            target = target_ordinal(as_of, snapshots)
        if target.ordinal is None:
            return self._refuse(question, f"no snapshot at as_of ({target.reason})")
        ordinal = target.ordinal
        parsed: ParsedQuestion | None = None
        try:
            with _guard("parse"):
                parsed = parse(question, self._linker, target_ord=ordinal)
        except _StageError as err:  # a parser bug: log it, and let the slow path read the question
            _log.warning("stage failed", extra={"stage": err.stage})
        key = parsed.canonical_key if (parsed is not None and use_cache) else None  # CACHE-1
        cached = self._cache_get(key)
        if cached is not None:
            return Evaluation("cache", cached, "cache hit", None, (), None, True, None)
        if not question.strip() or len(question) > MAX_QUESTION_CHARS:
            return self._refuse(question, "not a member question", caps=("not_covered",))
        with _guard("retrieval"):
            ranked = self._retrieve(question, ordinal)
        run = _Run(question, parsed, ordinal, snapshots, ranked, key, allow_slow)
        if parsed is None:
            return self._escalate(run, [], "parse error", _Scoring())
        try:
            return self._after_retrieval(run)
        except (
            _StageError
        ) as err:  # SPEC 16: a stage that raises after retrieval goes to the slow path
            _log.warning("stage failed", extra={"stage": err.stage})
            return self._escalate(run, [], str(err), _Scoring())

    def _retrieve(self, question: str, ordinal: int) -> list[RankedChunk]:
        """Hybrid retrieval, tried twice: a store hiccup (IO, a lock) may not repeat."""
        try:
            return retrieve(self.store, self.embedder, question, ordinal, self.config.retrieval)
        except Exception as exc:  # noqa: BLE001
            _log.warning("retrieval failed, retrying", extra={"error": type(exc).__name__})
            return retrieve(self.store, self.embedder, question, ordinal, self.config.retrieval)

    def _after_retrieval(self, run: _Run) -> Evaluation:  # noqa: PLR0911
        cfg = self.config
        question, key = run.question, run.key
        parsed = run.parsed
        if parsed is None:  # pragma: no cover - the caller escalates before this point
            return self._escalate(run, [], "parse error", _Scoring())
        top = run.ranked[0] if run.ranked else None
        bm25 = (top.bm25 or 0.0) if top is not None else 0.0
        cosine = (top.cosine or 0.0) if top is not None else 0.0
        groups, pick = self._candidates(parsed, run.ranked, run.ordinal, run.snapshots)
        label = self._label(parsed, groups)
        if not groups:
            scoring = _Scoring(
                label, 0.0, hard_caps_for_parse(parsed), None, False, bm25, cosine, 0.0
            )
            routing = route(0.0, target_ok=True, selection_none_covered=False, cfg=cfg.confidence)
            if routing.route == "slow":  # a zero floor lets a question with no fact read raw chunks
                return self._escalate(run, [], "no candidate fact", scoring)
            evaluation = self._refuse_with(question, "no candidate fact", scoring)
            self._cache_put(key, evaluation.answer)  # deterministic (CACHE-3)
            return evaluation
        with _guard("render"):
            built = self._build(parsed, pick, run.ordinal, run.snapshots, question)
        caps = self._caps(parsed, pick, built)
        anchor = pick.primary or groups[0]
        signals = self._signals(top, anchor, pick, groups)
        c = confidence(signals, caps, cfg.confidence)
        raw = confidence(signals, (), cfg.confidence)
        selection_status = pick.selection.status if pick.selection is not None else None
        routing = route(
            c,
            target_ok=True,
            selection_none_covered=parsed.fast_eligible and selection_status == "none",
            cfg=cfg.confidence,
        )
        candidate = built.answer if built is not None else None
        validated = False
        failed: tuple[str, ...] = ()
        if built is not None and built.answer is not None:
            with _guard("validate"):
                result = validate(built.answer, built.ctx)
            validated, failed = result.ok, result.failed
        scoring = _Scoring(label, c, caps, candidate, validated, bm25, cosine, raw, signals)
        if routing.route == "refuse":
            evaluation = self._refuse_with(question, routing.reason, scoring)
            self._cache_put(key, evaluation.answer)  # CACHE-3: deterministic
            return evaluation
        if routing.route == "slow":
            return self._escalate(run, groups, routing.reason, scoring, pick=pick)
        if built is None or built.answer is None or not validated:
            names = ", ".join(failed) if failed else "no answer"
            return self._escalate(
                run, groups, f"fast answer failed validators ({names})", scoring, pick=pick
            )
        self._cache_put(key, built.answer)
        return Evaluation(
            "fast",
            built.answer,
            routing.reason,
            c,
            caps,
            candidate,
            True,
            bm25,
            raw,
            cosine,
            signals,
        )

    # --- the slow path -------------------------------------------------------------------------

    def _refuse_with(self, question: str, reason: str, scoring: _Scoring) -> Evaluation:
        return self._refuse(
            question,
            reason,
            label=scoring.label,
            c=scoring.c,
            caps=scoring.caps,
            candidate=scoring.candidate,
            validated=scoring.validated,
            bm25=scoring.bm25,
            cosine=scoring.cosine,
            raw=scoring.raw,
            signals=scoring.signals,
        )

    def _escalate(
        self,
        run: _Run,
        groups: Sequence[CandidateGroup],
        why: str,
        scoring: _Scoring,
        *,
        pick: _Pick | None = None,
    ) -> Evaluation:
        """SPEC 12.2 and 16: ask the slow model; any failure is the safe refusal, never cached."""
        slow = self.slow
        if not run.allow_slow or slow is None:
            return self._refuse_with(run.question, f"{why}; slow path unavailable", scoring)
        if not slow.available:
            outcome = slow.unavailable()
            return self._refuse_with(
                run.question, f"{why}; slow path unavailable: {outcome.reason}", scoring
            )
        try:
            request = self._slow_request(run, groups, pick)
        except Exception as exc:  # noqa: BLE001 - a payload error is a refusal
            _log.warning("slow payload failed", extra={"error": type(exc).__name__})
            return self._refuse_with(
                run.question, f"{why}; slow path refused: payload {type(exc).__name__}", scoring
            )
        outcome = slow.answer(request, run.question)
        if outcome.answer is None:
            return self._refuse_with(
                run.question, f"{why}; slow path refused: {outcome.reason}", scoring
            )
        self._cache_put(run.key, outcome.answer)  # a validated answer (CACHE-3)
        return Evaluation(
            "slow",
            outcome.answer,
            f"{why}; slow path answered",
            scoring.c,
            scoring.caps,
            scoring.candidate,
            True,
            scoring.bm25,
            scoring.raw,
            scoring.cosine,
            scoring.signals,
        )

    @staticmethod
    def _loser_chunk_ids(groups: Sequence[CandidateGroup]) -> frozenset[str]:
        """Chunks that carry a value which is not the current one for an in-scope key."""
        ids: set[str] = set()
        for group in groups:
            winner = group.winner
            if winner is not None and group.resolution.strategy not in STALE_STRATEGIES:
                continue  # losing on recency alone does not make a value stale
            for version in group.versions:
                if winner is None or normalised_value(version) != normalised_value(winner):
                    ids.add(version.fact.chunk_id)
        return frozenset(ids)

    def _window(self, chunk: Chunk, ordinal: int, pages: dict[str, list[Chunk]]) -> list[Chunk]:
        """The chunks around a prose hit: same page, same heading path, up to 3 each side.

        A paragraph or list item is often one line of a card or section (a name, an address, an
        hours line), meaningless without its neighbours.
        """
        page = pages.get(chunk.url)
        if page is None:
            page = [
                c for c in self.store.page_chunks(chunk.url, ordinal) if c.chunk_type in PROSE_TYPES
            ]
            pages[chunk.url] = page
        at = next((i for i, c in enumerate(page) if c.chunk_id == chunk.chunk_id), None)
        if at is None:
            return [chunk]
        low, high = max(0, at - CONTEXT_WINDOW), min(len(page) - 1, at + CONTEXT_WINDOW)
        start, end = at, at
        while start > low and page[start - 1].parent_headers == chunk.parent_headers:
            start -= 1
        while end < high and page[end + 1].parent_headers == chunk.parent_headers:
            end += 1
        return page[start : end + 1]

    def _slow_ranking(self, run: _Run) -> list[RankedChunk]:
        """What the slow path reads (SRET-1 to SRET-4).

        The question and its rewrites are searched deeply with copies of the same text collapsed,
        the rankings are fused, and a cheap model puts the passages most likely to answer first.
        Without a reranker, or when it picks nothing, the fused order stands.
        """
        retrieval = self.config.retrieval
        cfg = retrieval.model_copy(
            update={"top_k": max(retrieval.slow_top_k, retrieval.rerank_candidates)}
        )
        queries = [run.question]
        if self.rewriter is not None:
            queries += self.rewriter.rewrite(run.question)
        rankings: list[list[RankedChunk]] = []
        for query in queries:
            try:
                rankings.append(retrieve_deep(self.store, self.embedder, query, run.ordinal, cfg))
            except Exception as exc:  # noqa: BLE001 - one failed query must not stop the others
                _log.warning("slow retrieval failed", extra={"error": type(exc).__name__})
        if not rankings:
            return list(run.ranked)
        by_id = {hit.chunk_id: hit for ranking in rankings for hit in ranking}
        order = fuse_rankings(
            [[hit.chunk_id for hit in ranking] for ranking in rankings],
            rrf_k=retrieval.rrf_k,
            limit=cfg.top_k,
        )
        fused = [by_id[i] for i in order]
        return self._rerank(run.question, fused, run.ordinal)[: retrieval.slow_top_k]

    def _rerank(self, question: str, fused: list[RankedChunk], ordinal: int) -> list[RankedChunk]:
        """The reranker's picks first, then the rest in fused order (SRET-3)."""
        if self.reranker is None:
            return fused
        retrieval = self.config.retrieval
        shown = fused[: retrieval.rerank_candidates]
        heads = self.store.page_heads(
            [hit.chunk.url for hit in shown], ordinal, limit=PUBLISHED_HEAD_CHUNKS
        )
        snippets = [
            (n, _snippet(hit.chunk, published_date(heads.get(hit.chunk.url, []))))
            for n, hit in enumerate(shown, start=1)
        ]
        picks = self.reranker.rank(question, snippets, keep=retrieval.rerank_keep)
        if not picks:
            return fused
        first = [shown[n - 1] for n in picks]
        taken = {hit.chunk_id for hit in first}
        return first + [hit for hit in fused if hit.chunk_id not in taken]

    def _superseded(
        self, ranking: Sequence[RankedChunk], run: _Run
    ) -> tuple[frozenset[str], frozenset[Number], dict[Number, frozenset[str]]]:
        """Chunks and numbers that hold a value no longer current for the key it belongs to.

        The third item scopes each stale number to the chunks that replaced it (V2).

        The facts of every retrieved chunk (the rows of a retrieved table too) are resolved at
        the target snapshot, whatever the question names; a chunk whose fact lost to a later
        effective date or a stronger source is stale (SLOW-2, V2; ``slow.superseded``).
        """
        parents = [h.chunk.parent_id or h.chunk.chunk_id for h in ranking]
        ids = {h.chunk.chunk_id for h in ranking}
        ids.update(self.store.children_of(list(dict.fromkeys(parents)), run.ordinal))
        versions = self.store.facts_for_chunks(sorted(ids), run.ordinal)
        keys = sorted({v.fact.fact_key for v in versions})
        by_key: dict[str, list[FactVersion]] = {}
        for version in self.store.facts_by_key(keys, run.ordinal):
            by_key.setdefault(version.fact.fact_key, []).append(version)
        chunks, numbers, scopes = superseded_detail(
            by_key, ordinal=run.ordinal, snapshots=run.snapshots
        )
        old_tables, old_numbers, table_scopes = self._stale_table_info(run.ordinal)
        return chunks | old_tables, numbers | old_numbers, _merge_scopes(scopes, table_scopes)

    def _stale_table_info(
        self, ordinal: int
    ) -> tuple[frozenset[str], frozenset[Number], dict[Number, frozenset[str]]]:
        """Tables replaced by a later generation (``slow.stale_tables``), once per snapshot."""
        if ordinal not in self._stale_tables:
            self._stale_tables[ordinal] = stale_tables_detail(self.store.table_chunks(ordinal))
        return self._stale_tables[ordinal]

    def _parent_chunks(
        self, run: _Run, ranking: Sequence[RankedChunk], excluded: frozenset[str]
    ) -> list[ParentChunk]:
        """What the model reads for each retrieved chunk, best first.

        A table row is replaced by its parent table (header, all rows, footer). A paragraph or
        list item comes with its neighbours under the same heading, labelled with the heading
        path. Anything that holds a loser, stale or expired value of an in-scope key is left out,
        so the model never reads a value that must not be stated (SLOW-2, V2). Each chunk is
        offered once.
        """
        found: dict[str, ParentChunk] = {}
        emitted: set[str] = set()
        pages: dict[str, list[Chunk]] = {}
        for place, hit in enumerate(ranking):
            score = 1.0 / (place + 1)  # the slow path's own order, reranking included
            chunk = hit.chunk
            if chunk.chunk_type in PROSE_TYPES:
                members = self._window(chunk, run.ordinal, pages)
                fresh = [c for c in members if c.chunk_id not in emitted]
                if not fresh or {c.chunk_id for c in members} & excluded:
                    continue
                emitted.update(c.chunk_id for c in fresh)
                found[chunk.chunk_id] = ParentChunk(
                    chunk.chunk_id,
                    chunk.url,
                    chunk.title,
                    "\n".join(c.text for c in fresh),
                    score,
                    chunk.authority_rank,
                    chunk.parent_headers,
                    chunk.effective_date,
                    chunk.expiry_date,
                    published_date([c.text for c in pages.get(chunk.url, [])]),
                    tuple(c.chunk_id for c in members),
                )
                continue
            parent_id = chunk.parent_id or chunk.chunk_id
            if parent_id in found:
                continue
            family = {parent_id, chunk.chunk_id, *self.store.children_of([parent_id], run.ordinal)}
            if family & excluded:
                continue
            parent = self.store.get_chunk(parent_id, run.ordinal) or chunk
            found[parent_id] = ParentChunk(
                parent_id,
                parent.url,
                parent.title,
                parent.text,
                score,
                parent.authority_rank,
                parent.parent_headers,
                parent.effective_date,
                parent.expiry_date,
                None,
                tuple(sorted(family)),
            )
        return list(found.values())

    def _slow_request(
        self, run: _Run, groups: Sequence[CandidateGroup], pick: _Pick | None
    ) -> SlowRequest:
        """SPEC 13.1: resolved facts, parent chunks, conflict notes, history view, loser numbers."""
        snapshot = next(s for s in run.snapshots if s.ordinal == run.ordinal)
        keys = [g.fact_key for g in groups]
        everything = self.store.facts_by_key(keys, run.ordinal) if keys else []
        winner_values = [g.winner.fact.value_text for g in groups if g.winner is not None]
        ranking = self._slow_ranking(run)
        stale_chunks, stale_numbers, stale_scopes = self._superseded(ranking, run)
        excluded = self._loser_chunk_ids(groups) | stale_chunks
        losers = loser_numbers(everything, winner_values) | stale_numbers
        scopes = _merge_scopes(loser_scopes(groups, everything), stale_scopes)
        notes = [
            f"{_fact_of(g).entity_label} {_fact_of(g).attribute}: strategy "
            f"{g.resolution.strategy}; the value shown is the current one, others are superseded"
            for g in groups
            if g.resolution.outcome == "resolved"
        ]
        history: list[str] = []
        if run.parsed is not None and (run.parsed.change_cue or run.parsed.shape == "history"):
            wanted = set(keys[:MAX_HISTORY_KEYS])
            history = history_view(
                [v for v in everything if v.fact.fact_key in wanted], run.snapshots, run.ordinal
            )
        primary = pick.primary if pick is not None and pick.primary is not None else None
        anchor = primary or (groups[0] if groups else None)
        attribute = _fact_of(anchor).attribute if anchor is not None else "*"
        tiers = self.config.tier.default_by_attribute
        inputs = SlowInputs(
            question=run.question,
            taken_on=snapshot.taken_on,
            snapshot_id=snapshot.snapshot_id,
            groups=groups,
            chunks=self._parent_chunks(run, ranking, excluded),
            conflict_notes=notes,
            history_lines=history,
            losers=losers,
            loser_scopes=scopes,
            default_tier=tiers.get(attribute, tiers.get("*", "entry")),
            conflict_note=self._conflict_note(anchor) if anchor is not None else None,
        )
        return build_request(inputs)

    # --- candidates and sibling selection ------------------------------------------------------

    def _candidates(
        self,
        parsed: ParsedQuestion,
        ranked: Sequence[RankedChunk],
        ordinal: int,
        snapshots: Sequence[SnapshotRef],
    ) -> tuple[list[CandidateGroup], _Pick]:
        rrf_k = self.config.retrieval.rrf_k
        with _guard("candidate_facts"):
            groups = candidate_facts(
                self.store,
                ranked,
                parsed,
                target_ord=ordinal,
                rrf_k=rrf_k,
                snapshots=snapshots,
            )
        groups = [g for g in groups if g.versions]
        with _guard("siblings"):
            pick = self._pick(parsed, groups)
        if parsed.amount is not None and groups:
            primary = pick.primary
            ok = primary is not None and _has_amount_key(_fact_of(primary))
            with _guard("candidate_facts"):
                groups = candidate_facts(
                    self.store,
                    ranked,
                    parsed,
                    target_ord=ordinal,
                    rrf_k=rrf_k,
                    snapshots=snapshots,
                    amount_ok=ok,
                )
            groups = [g for g in groups if g.versions]
            with _guard("siblings"):
                pick = self._pick(parsed, groups)
        return groups, pick

    @staticmethod
    def _attribute(parsed: ParsedQuestion, groups: Sequence[CandidateGroup]) -> str | None:
        attrs = _attributes(groups)
        if not attrs:
            return None
        measure = parsed.measure
        if measure == "rate":
            if parsed.shape == "history" and len(attrs) > 1:
                return None  # the history of "rate" is ambiguous between attributes (HSEG-3)
            return next((a for a in RATE_ORDER if a in attrs), None)
        if measure is not None:
            return measure if measure in attrs else None
        return attrs[0] if len(attrs) == 1 else None

    def _pick(self, parsed: ParsedQuestion, groups: Sequence[CandidateGroup]) -> _Pick:
        attribute = self._attribute(parsed, groups)
        if attribute is None:
            return _Pick(None, None, None, None)
        siblings = [g for g in groups if _fact_of(g).attribute == attribute]
        selection = select_siblings(
            [_fact_of(g) for g in siblings],
            amount=parsed.amount,
            qualifiers=parsed.qualifier_map,
        )
        if selection.status != "one":
            return _Pick(None, None, selection, attribute)
        key = selection.facts[0].fact_key
        primary = next((g for g in siblings if g.fact_key == key), None)
        companion = None
        if primary is not None and attribute == "rate":
            fact = _fact_of(primary)
            companion = next(
                (
                    g
                    for g in groups
                    if _fact_of(g).attribute == "apr"
                    and _fact_of(g).entity == fact.entity
                    and _fact_of(g).qualifiers == fact.qualifiers
                    and g.winner is not None
                ),
                None,
            )
        return _Pick(primary, companion, selection, attribute)

    # --- rendering -----------------------------------------------------------------------------

    def _build(
        self,
        parsed: ParsedQuestion,
        pick: _Pick,
        ordinal: int,
        snapshots: Sequence[SnapshotRef],
        question: str,
    ) -> _Built | None:
        primary = pick.primary
        if primary is None or primary.winner is None:
            return None
        if parsed.shape == "history":
            return self._build_history(parsed, primary, ordinal, snapshots, question)
        if parsed.shape == "list":
            return None  # list answers are not on the fast path in Step 9 (hard cap 4)
        winner = primary.winner
        fact = winner.fact
        companion = pick.companion.winner if pick.companion is not None else None
        text = self._render(parsed, fact, companion.fact if companion is not None else None)
        if text is None:
            return None
        snapshot_id = {s.ordinal: s.snapshot_id for s in snapshots}.get(ordinal)
        versions = [winner] if companion is None else [winner, companion]
        sources = self._sources(versions, snapshot_id)
        note = self._conflict_note(primary)
        answer = Answer(text=text, sources=sources, confident=True, conflict_note=note)
        keys = [primary.fact_key] + ([pick.companion.fact_key] if pick.companion else [])
        everything = self.store.facts_by_key(keys, ordinal)
        winner_values = [v.fact.value_text for v in versions]
        phrase = qualifier_phrase(fact.qualifiers) or ""
        allowed = numbers_of([*winner_values, phrase, question])
        losers = numbers_of(v.fact.value_text for v in everything) - numbers_of(winner_values)
        quotes = (
            [(fact.quote, winner.interval.chunk.text)]
            if template_for(parsed.shape, fact.attribute) == "quoted_prose"
            else []
        )
        ctx = ValidationContext(
            question=question,
            allowed=allowed,
            losers=losers,
            winners=numbers_of([*winner_values, phrase, question]),
            evidence_urls=frozenset(v.url for v in versions),
            quotes=tuple(quotes),
        )
        return _Built(answer, ctx, history_untrusted=False)

    @staticmethod
    def _render(parsed: ParsedQuestion, fact: Fact, companion: Fact | None) -> str | None:
        template = template_for(parsed.shape, fact.attribute)
        if template == "rate":
            return render_rate(fact, companion)
        renderers = {
            "fee": render_fee,
            "count": render_count,
            "address": render_address,
            "hours": render_hours,
            "quoted_prose": render_quoted_prose,
        }  # status and list have no fast-path wording in Step 9 (hard cap 2)
        renderer = renderers.get(template or "")
        return renderer(fact) if renderer is not None else None

    def _build_history(
        self,
        parsed: ParsedQuestion,
        primary: CandidateGroup,
        ordinal: int,
        snapshots: Sequence[SnapshotRef],
        question: str,
    ) -> _Built | None:
        fact = _fact_of(primary)
        versions = self.store.facts_by_key([primary.fact_key], ordinal)
        history: History = build_history(
            versions,
            fact_key=primary.fact_key,
            target_ordinal=ordinal,
            ctx=HistoryContext(snapshots=tuple(snapshots), degraded=self.state.degraded),
        )
        text = render_history(
            entity_label=fact.entity_label,
            attribute=fact.attribute,
            qualifiers_json=fact.qualifiers,
            history=history,
        )
        if text is None:
            return _Built(None, ValidationContext(question), history_untrusted=not history.trusted)
        ids = {s.ordinal: s.snapshot_id for s in snapshots}
        oldest, newest = history.segments[0], history.segments[-1]
        sources = self._history_sources(
            versions,
            fact_key=primary.fact_key,
            snapshots=snapshots,
            oldest=oldest,
            newest=newest,
            ids=ids,
        )
        values = [s.value_text for s in history.segments]
        dates = [d for s in history.segments for d in (s.first_date, s.last_date)]
        phrase = qualifier_phrase(fact.qualifiers) or ""
        allowed = numbers_of([*values, *dates, phrase, question])
        ctx = ValidationContext(
            question=question,
            allowed=allowed,
            winners=allowed,
            evidence_urls=frozenset(s.url for s in sources),
            relax_stale=True,  # earlier values are the point of a history answer
        )
        answer = Answer(text=text, sources=sources, confident=True)
        return _Built(answer, ctx, history_untrusted=not history.trusted)

    def _history_sources(  # noqa: PLR0913
        self,
        versions: Sequence[FactVersion],
        *,
        fact_key: str,
        snapshots: Sequence[SnapshotRef],
        oldest: Segment,
        newest: Segment,
        ids: dict[int, str],
    ) -> list[Source]:
        sources: list[Source] = []
        for at in (oldest.first_ordinal, newest.last_ordinal):
            res = resolve_key_at(
                [v for v in versions if v.fact.fact_key == fact_key],
                ordinal=at,
                snapshots=snapshots,
            )
            if res.winner is None:
                continue
            fact = res.winner.fact
            source = Source(
                url=res.winner.url,
                snapshot_id=ids.get(at),
                quote=_excerpt(fact.quote or res.winner.interval.chunk.text, fact.value_text),
            )
            if source not in sources:
                sources.append(source)
        return sources[:MAX_SOURCES]

    @staticmethod
    def _sources(versions: Sequence[FactVersion], snapshot_id: str | None) -> list[Source]:
        sources: list[Source] = []
        for version in versions:
            fact = version.fact
            quote = _excerpt(fact.quote or version.interval.chunk.text, fact.value_text)
            source = Source(url=version.url, snapshot_id=snapshot_id, quote=quote)
            if source not in sources:
                sources.append(source)
        return sources[:MAX_SOURCES]

    @staticmethod
    def _conflict_note(group: CandidateGroup) -> str | None:
        """CA-1: the strategy and the loser values, only when the key had a resolved conflict."""
        resolution = group.resolution
        winner = group.winner
        if resolution.outcome != "resolved" or winner is None:
            return None
        won = normalised_value(winner)
        losers = sorted({v.fact.value_text for v in group.versions if normalised_value(v) != won})
        if not losers:
            return None
        return f"strategy={resolution.strategy}: {winner.fact.value_text} used; " + (
            ", ".join(losers) + " superseded"
        )

    # --- confidence ----------------------------------------------------------------------------

    def _caps(self, parsed: ParsedQuestion, pick: _Pick, built: _Built | None) -> tuple[str, ...]:
        primary = pick.primary
        winner = primary.winner if primary is not None else None
        fact = winner.fact if winner is not None else None
        untrusted = parsed.shape == "history" and (
            pick.primary is None or built is None or built.history_untrusted
        )  # a history with no usable fact, wording or trust goes to the slow path (cap 9)
        if parsed.shape == "history" and parsed.measure is None:
            untrusted = True
        inputs = CapInputs(
            has_template=built is not None and built.answer is not None,
            conflict_unresolved=primary is not None and primary.resolution.outcome == "unresolved",
            multi_source_list=parsed.shape == "list",
            selection=pick.selection.status if pick.selection is not None else None,
            inferred_without_label_match=bool(
                fact is not None and fact.entity_inferred and parsed.entity_score < LABEL_SCORE
            ),
            degraded=bool(
                fact is not None and (fact.entity, fact.attribute) in self.state.degraded
            ),
            history_untrusted=untrusted,
            **cap_inputs_from_parse(parsed),
        )
        return hard_caps(inputs)

    def _signals(
        self,
        top: RankedChunk | None,
        anchor: CandidateGroup,
        pick: _Pick,
        groups: Sequence[CandidateGroup],
    ) -> Signals:
        cosine = (top.cosine or 0.0) if top is not None else 0.0
        bm25 = (top.bm25 or 0.0) if top is not None else 0.0
        sim = similarity_signal(cosine, bm25, self.config.confidence.bm25_bound)
        own = {anchor.fact_key}
        if pick.companion is not None:
            own.add(pick.companion.fact_key)
        anchor_value = normalised_value(anchor.winner) if anchor.winner is not None else None
        rivals = [
            g
            for g in groups
            if g.fact_key not in own
            and (
                g.winner is None
                or anchor_value is None
                or normalised_value(g.winner) != anchor_value
            )
        ]
        runner_up = max((g.score for g in rivals), default=None)
        margin = margin_signal(anchor.score, runner_up)
        outcome = anchor.resolution.outcome
        kind = (
            "unresolved"
            if outcome == "unresolved"
            else resolution_kind(anchor.resolution.strategy if outcome == "resolved" else None)
        )
        return Signals(sim, margin, anchor.qualifier, anchor.entity, kind)


def hard_caps_for_parse(parsed: ParsedQuestion) -> tuple[str, ...]:
    """The caps that come from the parse alone (used when nothing was retrieved)."""
    names = []
    if not parsed.fast_eligible:
        names.append("not_covered")
    if parsed.aggregate:
        names.append("aggregate")
    if parsed.change_cue and parsed.shape != "history":
        names.append("change_cue")
    return tuple(names)


SNIPPET_CHARS = 220
PUBLISHED_HEAD_CHUNKS = 6


def _snippet(chunk: Chunk, published: str | None = None) -> str:
    """One line for the reranker: title, publication date, heading path, start of the text."""
    path = " > ".join(chunk.parent_headers) if chunk.parent_headers else ""
    text = " ".join(chunk.text.split())[:SNIPPET_CHARS]
    dated = f"published {published}" if published else ""
    return " | ".join(part for part in (chunk.title, dated, path, text) if part)


def _merge_scopes(
    *scopes: dict[Number, frozenset[str]],
) -> dict[Number, frozenset[str]]:
    """Union of the scopes of every origin of a loser: it is judged against all of them (V2)."""
    merged: dict[Number, frozenset[str]] = {}
    for one in scopes:
        for number, ids in one.items():
            merged[number] = merged.get(number, frozenset()) | ids
    return merged
