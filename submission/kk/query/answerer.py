"""The fast-path answer pipeline (SPEC 8 to 12, 14, 16).

Stages, each in its own error boundary: ``as_of``, parse, cache lookup, retrieval, candidate
facts, sibling selection, template, confidence and routing, validators. Before Step 10 there
is no slow path, so a question routed to it is refused (decision D10). The same refusal covers
every failure: a raising stage, a validator failure and a score below the floor.

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
from kk.query.retrieve import CandidateGroup, RankedChunk, candidate_facts, retrieve
from kk.query.validators import (
    ValidationContext,
    check_refusal,
    numbers_of,
    validate,
)
from kk.store import Store
from kk.types import Fact, FactVersion, HistoryContext, ParsedQuestion, SnapshotRef

Route = Literal["cache", "fast", "slow", "refusal"]

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

    def __init__(  # noqa: PLR0913
        self,
        store: Store,
        embedder: EmbedderLike,
        config: Config,
        state: IngestState,
        cache: AnswerCache,
    ) -> None:
        """Keep the collaborators; ``refresh`` builds the entity linker after each ingest."""
        self.store = store
        self.embedder = embedder
        self.config = config
        self.state = state
        self.cache = cache
        self._linker = build_linker((), ())

    def refresh(self) -> None:
        """After an ingest: rebuild the entity linker and clear the cache (CACHE-2)."""
        self.cache.clear()
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

    def evaluate(self, question: str, as_of: str | None, *, use_cache: bool = True) -> Evaluation:
        """Run every stage and report the route, answer, confidence and candidate.

        ``use_cache=False`` neither reads nor writes the cache (calibration needs the scores of
        every paraphrase, not the stored answer of the first).
        """
        try:
            return self._evaluate(question, as_of, use_cache=use_cache)
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

    def _evaluate(  # noqa: PLR0911, PLR0912, PLR0915
        self, question: str, as_of: str | None, *, use_cache: bool
    ) -> Evaluation:
        cfg = self.config
        snapshots = self.store.snapshots
        with _guard("as_of"):
            target = target_ordinal(as_of, snapshots)
        if target.ordinal is None:
            return self._refuse(question, f"no snapshot at as_of ({target.reason})")
        ordinal = target.ordinal
        with _guard("parse"):
            parsed = parse(question, self._linker, target_ord=ordinal)
        key = parsed.canonical_key if use_cache else None  # CACHE-1: no key, no cache
        cached = self._cache_get(key)
        if cached is not None:
            return Evaluation("cache", cached, "cache hit", None, (), None, True, None)
        if not question.strip() or len(question) > MAX_QUESTION_CHARS:
            return self._refuse(question, "not a member question", caps=("not_covered",))
        with _guard("retrieval"):
            ranked = retrieve(self.store, self.embedder, question, ordinal, cfg.retrieval)
        top = ranked[0] if ranked else None
        bm25 = (top.bm25 or 0.0) if top is not None else 0.0
        cosine = (top.cosine or 0.0) if top is not None else 0.0
        groups, pick = self._candidates(parsed, ranked, ordinal, snapshots)
        label = self._label(parsed, groups)
        if not groups:
            evaluation = self._refuse(
                question,
                "no candidate fact",
                label=label,
                c=0.0,
                caps=hard_caps_for_parse(parsed),
                bm25=bm25,
                cosine=cosine,
                raw=0.0,
            )
            self._cache_put(key, evaluation.answer)  # deterministic (CACHE-3)
            return evaluation
        with _guard("render"):
            built = self._build(parsed, pick, ordinal, snapshots, question)
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

        def refuse(reason: str) -> Evaluation:
            return self._refuse(
                question,
                reason,
                label=label,
                c=c,
                caps=caps,
                candidate=candidate,
                validated=validated,
                bm25=bm25,
                cosine=cosine,
                raw=raw,
                signals=signals,
            )

        if routing.route == "refuse":
            evaluation = refuse(routing.reason)
            self._cache_put(key, evaluation.answer)  # CACHE-3: deterministic
            return evaluation
        if routing.route == "slow":
            return refuse(f"slow path unavailable before Step 10: {routing.reason}")
        if built is None or built.answer is None or not validated:
            names = ", ".join(failed) if failed else "no answer"
            return refuse(f"fast answer failed validators ({names}); slow path unavailable")
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
