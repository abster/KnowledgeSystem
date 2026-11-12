"""Confidence score, hard caps and routing (SPEC 12.1, 12.2). Thresholds come from config.

Everything here is pure arithmetic over signals the caller has already computed. Retrieval
supplies the similarity and margin inputs (Step 7); the BM25 upper bound is a parameter until
then (SPEC 22).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from kk.config import Confidence
from kk.query.candidates import remove_contradicted
from kk.query.render import RATE_ATTRIBUTES
from kk.types import Fact, ParsedQuestion

ResolutionKind = Literal["unique", "strategy_1_2", "strategy_3", "unresolved"]
RouteName = Literal["refuse", "slow", "fast"]
CAP_MARGIN = 0.01  # SPEC 12.1: a capped score is at most ``tau_accept - 0.01``

_STRATEGY_KIND: dict[str, ResolutionKind] = {
    "effective_date": "strategy_1_2",
    "source_authority": "strategy_1_2",
    "snapshot_recency": "strategy_3",
}
_KNOWN_ATTRIBUTES = frozenset(
    {*RATE_ATTRIBUTES, "fee", "count", "address", "hours", "status", "eligibility", "prose"}
)
_FIXED_TEMPLATE = {  # shape -> (the only attribute, template)
    "count": ("count", "count"),
    "yes_no": ("status", "status"),
    "date": ("status", "status"),
}
_VALUE_TEMPLATES = {
    **dict.fromkeys(RATE_ATTRIBUTES, "rate"),
    "fee": "fee",
    "address": "address",
    "hours": "hours",
    "eligibility": "quoted_prose",
    "prose": "quoted_prose",
}


@dataclass(frozen=True)
class Signals:
    """The five signals of SPEC 12.1, each in [0, 1] (``resolution`` is a state name)."""

    similarity: float
    margin: float
    qualifier: float
    entity: float
    resolution: ResolutionKind


@dataclass(frozen=True)
class CapInputs:
    """Facts about the question and its candidates that trigger the nine hard caps."""

    parse_ok: bool
    has_template: bool
    conflict_unresolved: bool
    aggregate: bool
    multi_source_list: bool
    selection: Literal["one", "several", "none"] | None
    inferred_without_label_match: bool
    degraded: bool
    change_cue_not_history: bool
    history_untrusted: bool


@dataclass(frozen=True)
class Routing:
    """Where a question goes, and why."""

    route: RouteName
    reason: str


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def template_for(shape: str, attribute: str | None) -> str | None:
    """The template id for a (shape, attribute) pair, or ``None`` (hard cap 2)."""
    if shape == "list":
        return "list" if attribute is None or attribute in _KNOWN_ATTRIBUTES else None
    if attribute is None:
        return None
    if shape == "history":
        return "history" if attribute in _KNOWN_ATTRIBUTES else None
    if shape in _FIXED_TEMPLATE:
        wanted, template = _FIXED_TEMPLATE[shape]
        return template if attribute == wanted else None
    return _VALUE_TEMPLATES.get(attribute) if shape == "what" else None


def resolution_kind(strategy: str | None) -> ResolutionKind:
    """Map a conflict strategy (``None`` for no conflict) to a resolution state.

    A strategy name this code does not know is treated as unresolved (fail closed).
    """
    if strategy is None:
        return "unique"
    return _STRATEGY_KIND.get(strategy, "unresolved")


def similarity_signal(cosine: float, bm25: float, bm25_bound: float) -> float:
    """Mean of the clamped cosine and the BM25 score over its bound (a bad bound gives 0)."""
    keyword = _clamp(bm25 / bm25_bound) if bm25_bound > 0 else 0.0
    return (_clamp(cosine) + keyword) / 2


def margin_signal(best: float, runner_up: float | None) -> float:
    """``(s1 - s2) / s1``, or 1.0 with no second candidate; no signal at all is no margin."""
    if runner_up is None:
        return 1.0
    if best <= 0:
        return 0.0
    return _clamp((best - runner_up) / best)


def _fact_qualifiers(fact: Fact) -> dict[str, str | int]:
    try:
        data = json.loads(fact.qualifiers)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def qualifier_signal(
    asked: Mapping[str, str | int],
    fact: Fact,
    *,
    measure: str | None,
    amount_ok: bool | None,
    has_siblings: bool,
) -> float:
    """Qualifiers the question supplied that agree with the fact, over those it supplied.

    The measure (rate, APY, APR) and the amount count as supplied qualifiers (DESIGN worked
    example dup-016: three of three). ``amount_ok`` is the tier selection result, or ``None``
    when the question named no amount. A key the fact lacks does not agree.
    """
    supplied = len(asked) + (measure is not None) + (amount_ok is not None)
    if supplied == 0:
        return 0.0 if has_siblings else 1.0
    have = _fact_qualifiers(fact)
    agree = sum(
        1
        for key, value in asked.items()
        if key in have and remove_contradicted([fact], qualifiers={key: value})
    )
    agree += int(measure is not None and fact.attribute == measure)
    agree += int(amount_ok is True)
    return agree / supplied


def cap_inputs_from_parse(parsed: ParsedQuestion) -> dict[str, bool]:
    """The cap inputs that come from the parse alone."""
    return {
        "parse_ok": parsed.fast_eligible,
        "aggregate": parsed.aggregate,
        "change_cue_not_history": parsed.change_cue and parsed.shape != "history",
    }


def hard_caps(inputs: CapInputs) -> tuple[str, ...]:
    """Names of the hard caps that apply (empty when none), in the order of SPEC 12.1.

    A tier selection of ``none`` is not a cap: a fully covered question is refused on the
    fast path (SPEC 12.2).
    """
    checks = (
        ("not_covered", not inputs.parse_ok),
        ("no_template", not inputs.has_template),
        ("unresolved_conflict", inputs.conflict_unresolved),
        ("aggregate", inputs.aggregate),
        ("multi_source_list", inputs.multi_source_list),
        ("several_tiers", inputs.selection == "several"),
        ("inferred_entity", inputs.inferred_without_label_match),
        ("degraded", inputs.degraded),
        ("change_cue", inputs.change_cue_not_history),
        ("history_untrusted", inputs.history_untrusted),
    )
    return tuple(name for name, applies in checks if applies)


def confidence(signals: Signals, caps: tuple[str, ...], cfg: Confidence) -> float:
    """``c = (w1*sim + w2*mar + w3*q + w4*e) * rs``, limited when a hard cap applies."""
    w = cfg.weights
    weighted = (
        w.similarity * _clamp(signals.similarity)
        + w.margin * _clamp(signals.margin)
        + w.qualifier * _clamp(signals.qualifier)
        + w.entity * _clamp(signals.entity)
    )
    c = _clamp(weighted * getattr(cfg.resolution_state, signals.resolution))
    return min(c, cfg.tau_accept - CAP_MARGIN) if caps else c


def route(c: float, *, target_ok: bool, selection_none_covered: bool, cfg: Confidence) -> Routing:
    """SPEC 12.2 routing table, in order."""
    if not target_ok:
        return Routing("refuse", "no snapshot at as_of, or as_of is invalid")
    if selection_none_covered:
        return Routing("refuse", "no published tier or sibling matches a fully understood question")
    if c < cfg.tau_floor:
        return Routing("refuse", "below the relevance floor")
    if c < cfg.tau_accept:
        return Routing("slow", "between the relevance floor and the acceptance threshold")
    return Routing("fast", "at or above the acceptance threshold")
