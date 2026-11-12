"""Sibling selection, tier selection and history segments (SPEC 9.3, 9.4).

Everything here is pure over in-memory facts. The question side is passed in as an optional
``AmountConstraint`` and a plain mapping of the other qualifiers it stated; question parsing
itself is Step 6.

Interpretations the SPEC leaves open (all listed in the Step 5 report):

* Only a certain match with exactly one tier is *one*: the constraint equals a tier's own
  constraint, or exactly one tier is touched and the whole range lies inside it. Several tiers,
  a range that runs past the published tiers (a capped top tier cannot cover "over $250,000")
  and boundary cases are *several* (slow path). A range that touches no tier is *none*, a fast
  path refusal.
* The 0.5% boundary band of SIB-4 applies to cap tiers only, not to bands, and not to an
  amount exactly on a cap (that case is decided by the inclusive or strict modifier).
* A key that is absent for one ordinal after it first appeared is a gap; a key with no visible
  fact at ``target_ordinal`` has no history.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from kk.conflicts import NormValue, normalised_value, resolve_key_at
from kk.query.normalise import AmountConstraint, parse_tier_text
from kk.types import Fact, FactVersion, HistoryContext

BOUNDARY_FRACTION = Decimal("0.005")  # SIB-4
MAX_SEGMENTS = 5  # HSEG-3
AMOUNT_KEYS = ("amount_tier", "balance_band")
_YEARS = re.compile(r"^(\d{4})(?:-(\d{2}|\d{4}))?$")
_CENTURY = 100

QualifierInput = Mapping[str, str | int]


@dataclass(frozen=True)
class SiblingGroup:
    """Facts of one entity and attribute that differ only in ``varying_key`` (SIB-1)."""

    entity: str
    attribute: str
    varying_key: str
    members: tuple[Fact, ...]


@dataclass(frozen=True)
class Selection:
    """The result of sibling selection (SIB-2): one fact, none, or several."""

    status: Literal["one", "none", "several"]
    facts: tuple[Fact, ...]
    varying_key: str | None
    detail: str


@dataclass(frozen=True)
class Segment:
    """A run of consecutive snapshots with the same winning value (HSEG-2)."""

    value_text: str
    first_date: str
    last_date: str
    first_ordinal: int
    last_ordinal: int


@dataclass(frozen=True)
class History:
    """History of one fact key. ``reasons`` lists why it is untrusted (HSEG-3)."""

    segments: tuple[Segment, ...]
    trusted: bool
    reasons: tuple[str, ...]


# --- qualifiers ------------------------------------------------------------------------------


def _qualifiers(fact: Fact) -> dict[str, str | int]:
    try:
        data = json.loads(fact.qualifiers)
    except ValueError:
        return {}
    return (
        {k: v for k, v in data.items() if isinstance(v, (str, int))}
        if isinstance(data, dict)
        else {}
    )


def _unique(facts: Sequence[Fact]) -> list[Fact]:
    seen: set[str] = set()
    out = []
    for fact in facts:
        if fact.fact_key not in seen:
            seen.add(fact.fact_key)
            out.append(fact)
    return out


def find_siblings(facts: Sequence[Fact]) -> SiblingGroup | None:
    """Group facts that differ in exactly one qualifier key, or ``None``."""
    members = _unique(facts)
    if len(members) < 2:  # noqa: PLR2004
        return None
    if len({(f.entity, f.attribute) for f in members}) != 1:
        return None
    dicts = [_qualifiers(f) for f in members]
    keys = set().union(*dicts)
    varying = sorted(k for k in keys if len({repr(d.get(k)) for d in dicts}) > 1)
    if len(varying) != 1:
        return None
    return SiblingGroup(members[0].entity, members[0].attribute, varying[0], tuple(members))


def _year_range(value: str | int) -> tuple[int, int] | None:
    match = _YEARS.match(str(value))
    if match is None:
        return None
    first = int(match.group(1))
    tail = match.group(2)
    if tail is None:
        return first, first
    last = int(tail) if len(tail) == 4 else first // _CENTURY * _CENTURY + int(tail)  # noqa: PLR2004
    return first, last


def _agrees(key: str, fact_value: str | int, asked: str | int) -> bool:
    if key == "model_year":
        have, want = _year_range(fact_value), _year_range(asked)
        if have is not None and want is not None:
            return have[0] <= want[0] and want[1] <= have[1]
    return str(fact_value) == str(asked)


def remove_contradicted(facts: Sequence[Fact], *, qualifiers: QualifierInput) -> tuple[Fact, ...]:
    """SIB-6: drop facts whose qualifier contradicts one the question stated.

    A fact that lacks the key is not contradicted. Amount tiers are handled by tier selection.
    """
    kept = []
    for fact in facts:
        have = _qualifiers(fact)
        if all(
            _agrees(key, have[key], asked)
            for key, asked in qualifiers.items()
            if key in have and key not in AMOUNT_KEYS
        ):
            kept.append(fact)
    return tuple(kept)


# --- tiers -----------------------------------------------------------------------------------

Bound = tuple[Decimal | None, bool]  # (value, inclusive); ``None`` value means unbounded


@dataclass(frozen=True)
class _Tier:
    fact: Fact
    constraint: AmountConstraint
    lower: Decimal | None
    lower_inclusive: bool

    @property
    def upper(self) -> Decimal | None:
        return self.constraint.upper

    @property
    def upper_inclusive(self) -> bool:
        return self.constraint.upper_inclusive


def _build_tiers(facts: Sequence[Fact], key: str) -> list[_Tier] | None:
    """Tier intervals. A cap tier starts where the next-lower tier ends (SPEC 7.3)."""
    parsed = []
    for fact in facts:
        text = _qualifiers(fact).get(key)
        constraint = parse_tier_text(text) if isinstance(text, str) else None
        if constraint is None:
            return None
        parsed.append((fact, constraint))
    tiers = []
    for fact, constraint in parsed:
        lower, inclusive = constraint.lower, constraint.lower_inclusive
        if constraint.kind == "cap" and constraint.upper is not None:
            below = [c for _, c in parsed if c.upper is not None and c.upper < constraint.upper]
            if below:
                nearest = max(below, key=lambda c: (c.upper, c.upper_inclusive))
                lower, inclusive = nearest.upper, not nearest.upper_inclusive
        tiers.append(_Tier(fact, constraint, lower, inclusive))
    return tiers


def _lower_ok(tier: _Tier, value: Decimal | None, inclusive: bool) -> bool:
    """True if a region starting at ``value`` does not begin before the tier does."""
    if tier.lower is None:
        return True
    if value is None:
        return False
    return value > tier.lower or (value == tier.lower and (tier.lower_inclusive or not inclusive))


def _upper_ok(tier: _Tier, value: Decimal | None, inclusive: bool) -> bool:
    """True if a region ending at ``value`` does not end after the tier does."""
    if tier.upper is None:
        return True
    if value is None:
        return False
    return value < tier.upper or (value == tier.upper and (tier.upper_inclusive or not inclusive))


def _within(tier: _Tier, c: AmountConstraint) -> bool:
    return _lower_ok(tier, c.lower, c.lower_inclusive) and _upper_ok(
        tier, c.upper, c.upper_inclusive
    )


def _overlaps(tier: _Tier, c: AmountConstraint) -> bool:
    lows = [
        (v, i)
        for v, i in ((tier.lower, tier.lower_inclusive), (c.lower, c.lower_inclusive))
        if v is not None
    ]
    highs = [
        (v, i)
        for v, i in ((tier.upper, tier.upper_inclusive), (c.upper, c.upper_inclusive))
        if v is not None
    ]
    if not lows or not highs:
        return True
    low = max(v for v, _ in lows)
    high = min(v for v, _ in highs)
    if low != high:
        return low < high
    return all(i for v, i in lows if v == low) and all(i for v, i in highs if v == high)


def _same_constraint(a: AmountConstraint, b: AmountConstraint) -> bool:
    return (a.lower, a.lower_inclusive, a.upper, a.upper_inclusive) == (
        b.lower,
        b.lower_inclusive,
        b.upper,
        b.upper_inclusive,
    )


def _boundary_tiers(tiers: Sequence[_Tier], c: AmountConstraint) -> list[_Tier]:
    """SIB-4: cap tiers whose cap the constraint sits next to, or strictly under."""
    ends = [v for v in (c.lower, c.upper) if v is not None]
    near = []
    for tier in tiers:
        cap = tier.upper
        if tier.constraint.kind != "cap" or cap is None:
            continue
        close = any(end != cap and abs(end - cap) <= cap * BOUNDARY_FRACTION for end in ends)
        strict_on_cap = c.kind == "cap" and not c.upper_inclusive and c.upper == cap
        if close or strict_on_cap:
            near.append(tier)
    return near


def _with_successors(tiers: Sequence[_Tier], chosen: Sequence[_Tier]) -> list[_Tier]:
    """The chosen tiers plus the tier just above each, in the original order."""
    by_cap = sorted(tiers, key=lambda t: (t.upper is None, t.upper))
    keep = {id(t) for t in chosen}
    for i, tier in enumerate(by_cap[:-1]):
        if id(tier) in keep:
            keep.add(id(by_cap[i + 1]))
    return [t for t in tiers if id(t) in keep]


def _tier_selection(tiers: Sequence[_Tier], c: AmountConstraint, key: str) -> Selection:
    """SIB-3 and SIB-4: ``one`` only for a certain match with exactly one tier."""

    def result(
        status: Literal["one", "none", "several"], chosen: Sequence[_Tier], why: str
    ) -> Selection:
        return Selection(
            status, tuple(t.fact for t in chosen), key if status == "several" else None, why
        )

    exact = [t for t in tiers if _same_constraint(t.constraint, c)]
    if len(exact) == 1:
        return result("one", exact, "equals the tier's own constraint")
    near = _boundary_tiers(tiers, c)
    if near:
        return result("several", _with_successors(tiers, near), "on a tier boundary")
    touched = [t for t in tiers if _overlaps(t, c)]
    if not touched:
        return result("none", [], "no tier holds an amount in the range")
    if len(touched) == 1 and _within(touched[0], c):
        return result("one", touched, "the whole range is inside one tier")
    why = "the range spans tiers" if len(touched) > 1 else "the range extends beyond the tier"
    return result("several", touched, why)


def select_siblings(  # noqa: PLR0911
    facts: Sequence[Fact], *, amount: AmountConstraint | None, qualifiers: QualifierInput
) -> Selection:
    """SIB-2 to SIB-4: which sibling, if any, the question selects."""
    kept = remove_contradicted(_unique(facts), qualifiers=qualifiers)
    if not kept:
        return Selection("none", (), None, "no fact survives the question's qualifiers")
    group = find_siblings(kept) if len(kept) > 1 else None
    if len(kept) > 1 and group is None:
        return Selection("several", kept, None, "facts are not siblings")
    amount_key = next((k for k in AMOUNT_KEYS if k in _qualifiers(kept[0])), None)
    if amount is not None and amount_key is not None:
        if group is not None and group.varying_key == amount_key:
            tiers = _build_tiers(kept, amount_key)
            if tiers is None:
                return Selection("several", kept, amount_key, "a tier cannot be read")
            return _tier_selection(tiers, amount, amount_key)
        tiers = _build_tiers(kept[:1], amount_key)
        if tiers is None:
            return Selection("several", kept, None, "a tier cannot be read")
        single = _tier_selection(tiers, amount, amount_key)
        if single.status != "one":
            return single
    if len(kept) == 1:
        return Selection("one", kept, None, "a single fact")
    return Selection(
        "several", kept, group.varying_key if group else None, "question does not choose"
    )


# --- history ---------------------------------------------------------------------------------


def _valid_date(text: str) -> bool:
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def build_history(  # noqa: PLR0912
    versions: Sequence[FactVersion], *, fact_key: str, target_ordinal: int, ctx: HistoryContext
) -> History:
    """HSEG-1 to HSEG-4: the value history of ``fact_key`` up to ``target_ordinal``."""
    mine = [v for v in versions if v.fact.fact_key == fact_key]
    reasons: list[str] = []
    if not mine:
        return History((), False, ("absent",))
    dates = {s.ordinal: s.taken_on for s in ctx.snapshots}
    runs: list[tuple[NormValue, str, int, int]] = []  # (norm, value_text, first, last)
    seen = False
    previous: int | None = None
    for ordinal in range(1, target_ordinal + 1):
        res = resolve_key_at(mine, ordinal=ordinal, snapshots=ctx.snapshots)
        if res.outcome == "absent":
            if seen:
                reasons.append("gap")
            continue
        seen = True
        if res.winner is None:
            reasons.append("unresolved_conflict")
            previous = None
            continue
        if res.winner.fact.none_value:
            reasons.append("none_value")
        if not _valid_date(dates.get(ordinal, "")):
            reasons.append("bad_date")
        norm = normalised_value(res.winner)
        if runs and previous == ordinal - 1 and runs[-1][0] == norm:
            runs[-1] = (norm, runs[-1][1], runs[-1][2], ordinal)
        else:
            runs.append((norm, res.winner.fact.value_text, ordinal, ordinal))
        previous = ordinal
    if not runs:
        reasons.append("absent")
    first = mine[0].fact
    if (first.entity, first.attribute) in ctx.degraded:
        reasons.append("degraded")
    if len(runs) > MAX_SEGMENTS:
        reasons.append("too_many_segments")
    segments = tuple(
        Segment(text, dates.get(a, ""), dates.get(b, ""), a, b) for _, text, a, b in runs
    )
    unique_reasons = tuple(dict.fromkeys(reasons))
    return History(segments, not unique_reasons, unique_reasons)
