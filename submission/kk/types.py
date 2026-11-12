"""Internal dataclasses shared by the ingest and query packages (SPEC 2.1, 5.1)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from contract import Source

if TYPE_CHECKING:
    from kk.ingest.dedupe import Interval
    from kk.query.normalise import AmountConstraint

ChunkType = Literal["text_paragraph", "table_row", "table", "list_item"]


@dataclass(frozen=True)
class Chunk:
    """One chunk of a page, before storage (the SPEC 5.1 row minus vector and interval)."""

    chunk_id: str
    url: str
    file_name: str
    title: str
    parent_headers: tuple[str, ...]
    chunk_type: ChunkType
    parent_id: str | None
    text: str
    effective_date: str | None
    expiry_date: str | None
    source_class: str
    authority_rank: int
    fetched_at: str
    position: int
    row_index: int | None = None
    row_count: int | None = None
    footer_count: int | None = None


@dataclass(frozen=True)
class Fact:
    """One atomic claim before storage (the SPEC 5.2 row minus the interval ordinals).

    ``fact_id``, ``first_seen_ord`` and ``last_seen_ord`` are set when facts are stored
    (Step 7). ``qualifiers`` is JSON with sorted keys.
    """

    entity: str
    entity_label: str
    attribute: str
    qualifiers: str
    fact_key: str
    value_text: str
    value_num: Decimal | None
    unit: str | None
    none_value: bool
    quote: str
    chunk_id: str
    effective_date: str | None
    expiry_date: str | None
    authority_rank: int
    entity_inferred: bool
    origin: Literal["table_rule", "prose_llm"]


@dataclass(frozen=True)
class FactVersion:
    """A fact joined to the interval of its chunk (SPEC 5.1, 5.2).

    ``Fact`` holds the stored copies of ``effective_date``, ``expiry_date`` and
    ``authority_rank``; those are always read from the fact. The interval supplies the
    ordinals, the page (URL, title, source class) and ``fetched_at``.
    """

    fact: Fact
    interval: Interval

    def __post_init__(self) -> None:
        """Reject a fact paired with the interval of another chunk or another rank."""
        chunk = self.interval.chunk
        if self.fact.chunk_id != chunk.chunk_id:
            raise ValueError("fact and interval belong to different chunks")
        if self.fact.authority_rank != chunk.authority_rank:
            raise ValueError("fact and chunk disagree on authority_rank")

    @property
    def first_seen_ord(self) -> int:
        """First snapshot ordinal in which the chunk (and so the fact) was visible."""
        return self.interval.first_seen_ord

    @property
    def last_seen_ord(self) -> int:
        """Last snapshot ordinal in which the chunk (and so the fact) was visible."""
        return self.interval.last_seen_ord

    @property
    def url(self) -> str:
        """URL of the page that carries the fact."""
        return self.interval.chunk.url

    @property
    def title(self) -> str:
        """Title of the page that carries the fact."""
        return self.interval.chunk.title

    @property
    def source_class(self) -> str:
        """Source class of the page (SPEC 6.6)."""
        return self.interval.chunk.source_class

    @property
    def fetched_at(self) -> str:
        """``fetched_at`` of the most recent page that contained the chunk."""
        return self.interval.fetched_at

    def visible_at(self, ordinal: int) -> bool:
        """True if the fact's interval contains ``ordinal`` (the SPEC 5.4 validity filter)."""
        return self.first_seen_ord <= ordinal <= self.last_seen_ord


@dataclass(frozen=True, kw_only=True)
class SnapshotRef:
    """One entry of the snapshots list ``(ord, snapshot_id, taken_on)`` (ING-1)."""

    ordinal: int
    snapshot_id: str
    taken_on: str


@dataclass(frozen=True, kw_only=True)
class HistoryContext:
    """What history building needs besides the facts: the snapshots and degraded pairs."""

    snapshots: tuple[SnapshotRef, ...]
    degraded: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True)
class ConflictRecord:
    """One row of the ``conflicts`` table (SPEC 5.3, 10.4)."""

    conflict_id: str
    topic: str
    fact_key: str
    values: tuple[str, ...]
    sources: tuple[Source, ...]
    resolution: str | None
    strategy: str
    reason: str
    ords: tuple[int, ...]


Shape = Literal["history", "count", "yes_no", "date", "list", "what", "other"]


@dataclass(frozen=True)
class Span:
    """A character span of the normalised question consumed by one rule (COV-1)."""

    start: int
    end: int
    kind: str


@dataclass(frozen=True)
class QualifierSpan:
    """A qualifier the question stated (``term`` in months, ``model_year``, ``rate_type``...)."""

    key: str
    value: str | int
    start: int
    end: int


@dataclass(frozen=True)
class ParsedQuestion:
    """The result of ``parse`` (SPEC 8.2).

    ``covered`` is the COV-2 result and ``uncertain`` is set by ambiguity (ENT-2 and similar).
    ``reasons`` lists both kinds (``residual:<token>``, ``entity_ambiguous``...). The question
    may use the fast path only if ``fast_eligible`` (COV-5, GR-1).
    """

    text: str
    shape: Shape
    entity_candidates: tuple[str, ...]
    entity: str | None
    entity_score: float
    measure: str | None
    qualifiers: tuple[QualifierSpan, ...]
    amount: AmountConstraint | None
    change_cue: bool
    aggregate: bool
    consumed: tuple[Span, ...]
    residual: str
    covered: bool
    uncertain: bool
    reasons: tuple[str, ...]
    pattern: str | None
    canonical_key: str | None

    @property
    def qualifier_map(self) -> dict[str, str | int]:
        """The stated qualifiers as ``key -> value`` (the amount is separate)."""
        return {q.key: q.value for q in self.qualifiers}

    @property
    def fast_eligible(self) -> bool:
        """COV-5 and GR-1: fully covered, certain, not ``other`` and a grammar pattern matched."""
        return (
            self.covered
            and not self.uncertain
            and self.shape != "other"
            and self.pattern is not None
            and self.entity is not None
        )
