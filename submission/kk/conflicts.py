"""Conflict detection and resolution (SPEC 10, CR-1, CR-2, CA-1).

A conflict is a coexisting contradiction: in one snapshot, two or more live pages carry the
same fact key with different normalised values. Resolution applies, in order, the effective
date, the source authority and the snapshot recency; the first strategy that picks a unique
winner decides, and its name and evidence go into ``reason``.

The reference date is the ``taken_on`` of the ordinal being resolved. Steps (SPEC 10.3):

0. Expired values and values not yet in force lose. No current value: unresolved. One: it wins.
1. Among current values that are all dated, the latest effective date wins (ties continue).
2. The highest authority rank wins; at an equal rank a table fact beats a prose fact.
3. Values still sharing the top rank go to snapshot recency; a tie there is unresolved.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from contract import Conflict, Source
from kk.ingest.meta import is_expired, is_not_yet_in_force
from kk.query.render import topic_text
from kk.types import ConflictRecord, FactVersion, SnapshotRef

STRATEGIES = ("effective_date", "source_authority", "snapshot_recency", "unresolved")
MAX_NAMED_LOSERS = 3
MAX_TITLE_CHARS = 60
MAX_QUOTE_CHARS = 200
MIN_CONFLICT_URLS = 2
_ID_CHARS = 16
_DATE_CHARS = 10

NormValue = tuple[int, Decimal, str]


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving one fact key at one ordinal (SPEC 10.3)."""

    outcome: Literal["absent", "single", "resolved", "unresolved"]
    winner: FactVersion | None
    strategy: str | None


def normalised_value(version: FactVersion) -> NormValue:
    """Normalised value: the decimal if there is one, else the lower-cased text."""
    fact = version.fact
    if fact.value_num is not None:
        return (0, fact.value_num, "")
    return (1, Decimal(0), fact.value_text.strip().casefold())


@dataclass
class _Group:
    """All visible facts that carry one normalised value."""

    norm: NormValue
    versions: list[FactVersion]
    current: list[FactVersion] = field(init=False)
    rep: FactVersion = field(init=False)
    expired: bool = field(init=False)
    newest_fetch: str = field(init=False)
    newest_first_seen: int = field(init=False)

    def settle(self, taken_on: str) -> None:
        """Pick the representative page and the group-level facts used by the steps (CR-3)."""
        self.current = [
            v
            for v in self.versions
            if not is_expired(v.fact.expiry_date, taken_on)
            and not is_not_yet_in_force(v.fact.effective_date, taken_on)
        ]
        pool = self.current or self.versions
        ordered = sorted(pool, key=lambda v: (v.url, v.fact.chunk_id))
        ordered.sort(key=lambda v: v.fetched_at, reverse=True)
        ordered.sort(key=lambda v: v.fact.effective_date or "", reverse=True)
        ordered.sort(key=lambda v: v.fact.authority_rank, reverse=True)
        self.rep = ordered[0]
        self.expired = is_expired(self.rep.fact.expiry_date, taken_on)
        self.newest_fetch = max(v.fetched_at for v in pool)
        self.newest_first_seen = max(v.first_seen_ord for v in pool)

    @property
    def is_current(self) -> bool:
        return bool(self.current)

    @property
    def display(self) -> str:
        return self.rep.fact.value_text

    @property
    def rank(self) -> int:
        return self.rep.fact.authority_rank

    @property
    def is_table(self) -> bool:
        return self.rep.fact.origin == "table_rule"


def _groups(visible: Sequence[FactVersion], taken_on: str) -> list[_Group]:
    by_value: dict[NormValue, list[FactVersion]] = defaultdict(list)
    for version in visible:
        by_value[normalised_value(version)].append(version)
    groups = [_Group(norm, vs) for norm, vs in sorted(by_value.items())]
    for group in groups:
        group.settle(taken_on)
    return groups


def _resolve_groups(groups: list[_Group]) -> tuple[str, _Group | None]:  # noqa: PLR0911
    """SPEC 10.3 over the value groups of one fact key at one ordinal."""
    pool = [g for g in groups if g.is_current]  # step 0
    if not pool:
        return "unresolved", None
    if len(pool) == 1 and len(groups) > 1:
        return "effective_date", pool[0]
    dates = [g.rep.fact.effective_date for g in pool]
    if all(dates) and len(set(dates)) > 1:  # step 1
        latest = max(d for d in dates if d)
        pool = [g for g in pool if g.rep.fact.effective_date == latest]
        if len(pool) == 1:
            return "effective_date", pool[0]
    top_rank = max(g.rank for g in pool)  # step 2
    top = [g for g in pool if g.rank == top_rank]
    tables = [g for g in top if g.is_table]
    if tables and len(tables) < len(top):
        top = tables  # PRO-4: a table fact beats a prose fact at an equal rank
    if len(top) == 1 and len(pool) > 1:
        return "source_authority", top[0]
    newest = max((g.newest_fetch, g.newest_first_seen) for g in top)  # step 3
    winners = [g for g in top if (g.newest_fetch, g.newest_first_seen) == newest]
    if len(winners) == 1:
        return "snapshot_recency", winners[0]
    return "unresolved", None


def _taken_on(snapshots: Sequence[SnapshotRef], ordinal: int) -> str:
    return next((s.taken_on for s in snapshots if s.ordinal == ordinal), "")


def resolve_key_at(
    versions: Sequence[FactVersion], *, ordinal: int, snapshots: Sequence[SnapshotRef]
) -> Resolution:
    """Resolve the facts of one fact key visible at ``ordinal``."""
    visible = [v for v in versions if v.visible_at(ordinal)]
    if not visible:
        return Resolution("absent", None, None)
    groups = _groups(visible, _taken_on(snapshots, ordinal))
    if len(groups) == 1:
        return Resolution("single", groups[0].rep, None)
    if len({v.url for v in visible}) < MIN_CONFLICT_URLS:
        return Resolution("unresolved", None, "unresolved")  # two values on one page (P6)
    strategy, winner = _resolve_groups(groups)
    if winner is None:
        return Resolution("unresolved", None, strategy)
    return Resolution("resolved", winner.rep, strategy)


# --- wording ---------------------------------------------------------------------------------


def _title(text: str) -> str:
    if len(text) <= MAX_TITLE_CHARS:
        return text
    return text[: MAX_TITLE_CHARS - 1].rstrip() + "…"


def _describe(group: _Group, strategy: str) -> str:
    rep = group.rep
    effective = rep.fact.effective_date
    if group.is_current:
        standing = "live"
    elif group.expired:
        standing = f"expired {rep.fact.expiry_date}"
    else:
        standing = "not yet in force"
    parts = [f"effective {effective}" if effective else "no effective date", standing]
    if strategy == "source_authority":
        parts.append(f"{rep.source_class} rank {rep.fact.authority_rank}, {rep.fact.origin}")
    elif strategy == "snapshot_recency":
        parts.append(f"fetched {group.newest_fetch[:_DATE_CHARS]}")
    parts.append(f'"{_title(rep.title)}"')
    return f"{group.display} ({', '.join(parts)})"


def _join_named(groups: Sequence[_Group], strategy: str) -> str:
    named = [_describe(g, strategy) for g in groups[:MAX_NAMED_LOSERS]]
    hidden = len(groups) - len(named)
    if hidden > 0:
        named.append(f"and {hidden} more")
    return "; ".join(named)


def _reason(strategy: str, winner: _Group | None, groups: Sequence[_Group]) -> str:
    if winner is None and not any(g.is_current for g in groups):
        why = (
            "every competing value is expired"
            if all(g.expired for g in groups)
            else "no competing value is current"
        )
        return f"strategy=unresolved: {why}: {_join_named(groups, strategy)}"
    if winner is None:
        return (
            f"strategy=unresolved: {_join_named(groups, strategy)} cannot be separated "
            "by effective date, source authority or snapshot recency"
        )
    name = "snapshot_recency (weaker evidence)" if strategy == "snapshot_recency" else strategy
    losers = [g for g in groups if g is not winner]
    return (
        f"strategy={name}: {_describe(winner, strategy)} supersedes {_join_named(losers, strategy)}"
    )


# --- detection -------------------------------------------------------------------------------


def _conflict_id(fact_key: str, norms: Sequence[NormValue]) -> str:
    text = "|".join(f"{k}:{n.normalize()}:{t}" for k, n, t in sorted(norms))
    return hashlib.sha256(f"{fact_key}|{text}".encode()).hexdigest()[:_ID_CHARS]


def detect_conflicts(
    versions: Sequence[FactVersion], *, snapshots: Sequence[SnapshotRef]
) -> list[ConflictRecord]:
    """Find, merge, resolve and order the coexisting contradictions (SPEC 10.2 to 10.6)."""
    eligible = [v for v in versions if not v.fact.entity_inferred and not v.fact.none_value]
    coexisting: dict[tuple[str, frozenset[NormValue]], list[int]] = defaultdict(list)
    for snap in sorted(snapshots, key=lambda s: s.ordinal):
        by_key: dict[str, list[FactVersion]] = defaultdict(list)
        for version in eligible:
            if version.visible_at(snap.ordinal):
                by_key[version.fact.fact_key].append(version)
        for fact_key, visible in by_key.items():
            norms = frozenset(normalised_value(v) for v in visible)
            if len(norms) >= MIN_CONFLICT_URLS and len({v.url for v in visible}) >= 2:  # noqa: PLR2004
                coexisting[(fact_key, norms)].append(snap.ordinal)
    records = [
        _record(fact_key, norms, ords, eligible, snapshots)
        for (fact_key, norms), ords in coexisting.items()
    ]
    return sorted(records, key=lambda r: (r.topic, r.values[0], r.conflict_id))


def _record(
    fact_key: str,
    norms: frozenset[NormValue],
    ords: Sequence[int],
    eligible: Sequence[FactVersion],
    snapshots: Sequence[SnapshotRef],
) -> ConflictRecord:
    latest = max(ords)
    visible = [v for v in eligible if v.fact.fact_key == fact_key and v.visible_at(latest)]
    groups = _groups(visible, _taken_on(snapshots, latest))
    strategy, winner = _resolve_groups(groups)
    snapshot_id = next((s.snapshot_id for s in snapshots if s.ordinal == latest), "")
    lead = sorted(groups, key=lambda g: (-g.rank, g.norm))[0].rep.fact
    ordered_losers = [g for g in groups if g is not winner]
    return ConflictRecord(
        conflict_id=_conflict_id(fact_key, sorted(norms)),
        topic=topic_text(lead.entity_label, lead.qualifiers, lead.attribute),
        fact_key=fact_key,
        values=tuple(g.display for g in groups),
        sources=tuple(
            Source(url=g.rep.url, snapshot_id=snapshot_id, quote=g.rep.fact.quote[:MAX_QUOTE_CHARS])
            for g in groups
        ),
        resolution=winner.display if winner is not None else None,
        strategy=strategy,
        reason=_reason(strategy, winner, ordered_losers if winner is not None else groups),
        ords=tuple(sorted(ords)),
    )


# --- outputs ---------------------------------------------------------------------------------


def conflict_note(record: ConflictRecord) -> str:
    """The short ``Answer.conflict_note`` for a record (CA-1)."""
    if record.resolution is None:
        return f"strategy=unresolved: {', '.join(record.values)} conflict; no value chosen"
    losers = [v for v in record.values if v != record.resolution]
    return f"strategy={record.strategy}: {record.resolution} used; {', '.join(losers)} superseded"


def to_contract(record: ConflictRecord) -> Conflict:
    """Convert a record to the harness ``Conflict``."""
    return Conflict(
        topic=record.topic,
        values=list(record.values),
        sources=list(record.sources),
        resolution=record.resolution,
        reason=record.reason,
    )
