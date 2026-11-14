"""Confidence signals, hard caps and routing (SPEC 12.1, 12.2)."""

from __future__ import annotations

import dataclasses

import pytest

from kk.config import load_config
from kk.query.confidence import (
    CAP_MARGIN,
    CapInputs,
    Signals,
    cap_inputs_from_parse,
    confidence,
    hard_caps,
    margin_signal,
    qualifier_signal,
    resolution_kind,
    route,
    similarity_signal,
    template_for,
)
from kk.query.question import parse
from tests.fixtures.facts import make_fact
from tests.fixtures.questions import LINKER

# The DESIGN worked examples and the routing table are stated for the SPEC 3 placeholder
# thresholds, so the tests pin them; the shipped values come from `make calibrate`.
CFG = load_config().confidence.model_copy(update={"tau_floor": 0.30, "tau_accept": 0.80})
CLEAR = CapInputs(
    parse_ok=True,
    has_template=True,
    conflict_unresolved=False,
    aggregate=False,
    multi_source_list=False,
    selection="one",
    inferred_without_label_match=False,
    degraded=False,
    change_cue_not_history=False,
    history_untrusted=False,
)


# --- templates --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shape", "attribute", "template"),
    [
        ("what", "rate", "rate"),
        ("what", "apr", "rate"),
        ("what", "apy", "rate"),
        ("what", "dividend_rate", "rate"),
        ("what", "min_to_open", "rate"),
        ("what", "fee", "fee"),
        ("count", "count", "count"),
        ("what", "address", "address"),
        ("what", "hours", "hours"),
        ("yes_no", "status", "status"),
        ("date", "status", "status"),
        ("list", "apr", "list"),
        ("list", None, "list"),
        ("what", "eligibility", "quoted_prose"),
        ("what", "prose", "quoted_prose"),
        ("history", "apy", "history"),
        ("history", "fee", "history"),
    ],
)
def test_template_lookup(shape: str, attribute: str | None, template: str) -> None:
    assert template_for(shape, attribute) == template


@pytest.mark.parametrize(
    ("shape", "attribute"),
    [
        ("what", "phone"),
        ("what", "status"),
        ("what", None),
        ("other", "apr"),
        ("count", "apr"),
        ("yes_no", "apr"),
        ("what", "nonsense"),
        ("history", None),
    ],
)
def test_no_template_for_unknown_pairs(shape: str, attribute: str | None) -> None:
    assert template_for(shape, attribute) is None


# --- signals ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("strategy", "kind"),
    [
        (None, "unique"),
        ("effective_date", "strategy_1_2"),
        ("source_authority", "strategy_1_2"),
        ("snapshot_recency", "strategy_3"),
        ("unresolved", "unresolved"),
        ("something_new", "unresolved"),  # unknown fails closed
    ],
)
def test_resolution_kind(strategy: str | None, kind: str) -> None:
    assert resolution_kind(strategy) == kind


def test_similarity_is_the_mean_of_cosine_and_bounded_bm25() -> None:
    assert similarity_signal(0.9, 5.0, 10.0) == pytest.approx(0.7)
    assert similarity_signal(0.0, 0.0, 10.0) == 0.0
    assert similarity_signal(1.0, 10.0, 10.0) == 1.0


def test_similarity_is_clamped_and_fails_closed_on_a_bad_bound() -> None:
    assert similarity_signal(-0.5, 99.0, 10.0) == pytest.approx(0.5)  # cosine 0, bm25 1
    assert similarity_signal(2.0, 5.0, 10.0) == pytest.approx(0.75)  # cosine clamped to 1
    assert similarity_signal(0.8, 5.0, 0.0) == pytest.approx(0.4)  # no bound: bm25 part is 0
    assert similarity_signal(0.8, -3.0, 10.0) == pytest.approx(0.4)


def test_margin() -> None:
    assert margin_signal(0.8, 0.4) == pytest.approx(0.5)
    assert margin_signal(0.8, None) == 1.0
    assert margin_signal(0.8, 0.8) == 0.0
    assert margin_signal(0.8, 0.9) == 0.0  # clamped
    assert margin_signal(0.0, 0.0) == 0.0  # no signal is no margin
    assert margin_signal(0.5, 0.0) == 1.0


def _fact(**qualifiers: str | int):  # type: ignore[no-untyped-def]
    return make_fact(entity="auto loan", attribute="apr", qualifiers=dict(qualifiers))


def test_qualifier_match_dup016_shape() -> None:
    # 60 months, a 2025 model inside the 2024-27 row, and APR: three of three
    fact = _fact(term=60, model_year="2024-27")
    assert qualifier_signal(
        {"term": 60, "model_year": "2025"},
        fact,
        measure="apr",
        amount_ok=None,
        has_siblings=False,
    ) == pytest.approx(1.0)


def test_qualifier_match_counts_agreeing_over_supplied() -> None:
    fact = _fact(term=60, model_year="2024-27")
    got = qualifier_signal(
        {"term": 36, "model_year": "2025"},
        fact,
        measure="apr",
        amount_ok=None,
        has_siblings=False,
    )
    assert got == pytest.approx(2 / 3)


def test_a_supplied_qualifier_the_fact_lacks_does_not_agree() -> None:
    fact = _fact()
    got = qualifier_signal({"term": 60}, fact, measure=None, amount_ok=None, has_siblings=False)
    assert got == 0.0


def test_amount_counts_as_a_supplied_qualifier() -> None:
    fact = _fact(term=60)
    args = {"measure": None, "has_siblings": False}
    assert qualifier_signal({"term": 60}, fact, amount_ok=True, **args) == 1.0  # type: ignore[arg-type]
    assert qualifier_signal({"term": 60}, fact, amount_ok=False, **args) == 0.5  # type: ignore[arg-type]


def test_measure_counts_and_must_equal_the_attribute() -> None:
    fact = _fact()
    assert qualifier_signal({}, fact, measure="apr", amount_ok=None, has_siblings=False) == 1.0
    assert qualifier_signal({}, fact, measure="apy", amount_ok=None, has_siblings=False) == 0.0


def test_nothing_supplied() -> None:
    fact = _fact()
    assert qualifier_signal({}, fact, measure=None, amount_ok=None, has_siblings=False) == 1.0
    # DESIGN dup-011: tiers exist and the question named none, so 0 of the required
    assert qualifier_signal({}, fact, measure=None, amount_ok=None, has_siblings=True) == 0.0


# --- the score (DESIGN worked examples) -------------------------------------------------------


def test_worked_example_dup016_resolved_by_effective_date() -> None:
    s = Signals(0.9, 0.8, 1.0, 1.0, "strategy_1_2")
    assert confidence(s, (), CFG) == pytest.approx(0.837)  # DESIGN: 0.84, fast path
    assert route(0.837, target_ok=True, selection_none_covered=False, cfg=CFG).route == "fast"


def test_worked_example_dup011_goes_slow() -> None:
    s = Signals(0.85, 0.2, 0.0, 1.0, "unique")
    assert confidence(s, (), CFG) == pytest.approx(0.495)  # DESIGN: 0.50
    assert route(0.495, target_ok=True, selection_none_covered=False, cfg=CFG).route == "slow"


def test_worked_example_dup145_is_refused() -> None:
    s = Signals(0.55, 0.1, 0.0, 0.2, "unique")
    assert confidence(s, (), CFG) == pytest.approx(0.225)  # DESIGN: 0.23
    assert route(0.225, target_ok=True, selection_none_covered=False, cfg=CFG).route == "refuse"


def test_unresolved_conflict_zeroes_the_score() -> None:
    assert confidence(Signals(1, 1, 1, 1, "unresolved"), (), CFG) == 0.0


def test_signals_out_of_range_are_clamped() -> None:
    c = confidence(Signals(5, 5, 5, 5, "unique"), (), CFG)
    assert c == pytest.approx(1.0)
    assert confidence(Signals(-5, -5, -5, -5, "unique"), (), CFG) == 0.0


# --- hard caps --------------------------------------------------------------------------------

CAP_CASES = [
    ("not_covered", {"parse_ok": False}),
    ("no_template", {"has_template": False}),
    ("unresolved_conflict", {"conflict_unresolved": True}),
    ("aggregate", {"aggregate": True}),
    ("multi_source_list", {"multi_source_list": True}),
    ("several_tiers", {"selection": "several"}),
    ("inferred_entity", {"inferred_without_label_match": True}),
    ("degraded", {"degraded": True}),
    ("change_cue", {"change_cue_not_history": True}),
    ("history_untrusted", {"history_untrusted": True}),
]


def test_no_caps_when_everything_is_clear() -> None:
    assert hard_caps(CLEAR) == ()


@pytest.mark.parametrize(("name", "change"), CAP_CASES)
def test_each_hard_cap_blocks_the_fast_path(name: str, change: dict[str, object]) -> None:
    inputs = dataclasses.replace(CLEAR, **change)  # type: ignore[arg-type]
    assert hard_caps(inputs) == (name,)
    best = Signals(1, 1, 1, 1, "unique")
    c = confidence(best, hard_caps(inputs), CFG)
    assert c <= CFG.tau_accept - CAP_MARGIN + 1e-12
    assert route(c, target_ok=True, selection_none_covered=False, cfg=CFG).route != "fast"


def test_a_cap_never_raises_a_low_score() -> None:
    low = Signals(0.1, 0.1, 0.1, 0.1, "unique")
    assert confidence(low, ("aggregate",), CFG) == pytest.approx(0.1)


def test_a_capped_but_relevant_question_goes_slow_not_refused() -> None:
    c = confidence(Signals(1, 1, 1, 1, "unique"), ("several_tiers",), CFG)
    assert route(c, target_ok=True, selection_none_covered=False, cfg=CFG).route == "slow"


def test_selection_none_and_missing_selection_are_not_caps() -> None:
    assert hard_caps(dataclasses.replace(CLEAR, selection="none")) == ()
    assert hard_caps(dataclasses.replace(CLEAR, selection=None)) == ()


def test_several_caps_are_all_reported_in_a_fixed_order() -> None:
    inputs = dataclasses.replace(CLEAR, aggregate=True, parse_ok=False, degraded=True)
    assert hard_caps(inputs) == ("not_covered", "aggregate", "degraded")


# --- routing (SPEC 12.2) ----------------------------------------------------------------------


def test_routing_table() -> None:
    def r(c: float, **kw: bool) -> str:
        args = {"target_ok": True, "selection_none_covered": False, **kw}
        return route(c, cfg=CFG, **args).route  # type: ignore[arg-type]

    assert r(0.9, target_ok=False) == "refuse"
    assert r(0.99, selection_none_covered=True) == "refuse"  # fully covered, no tier matches
    assert r(0.29) == "refuse"
    assert r(CFG.tau_floor) == "slow"  # the floor itself is not below it
    assert r(0.5) == "slow"
    assert r(0.79) == "slow"
    assert r(CFG.tau_accept) == "fast"
    assert r(1.0) == "fast"


def test_routing_reasons_are_non_empty_and_name_the_cause() -> None:
    assert "as_of" in route(0.9, target_ok=False, selection_none_covered=False, cfg=CFG).reason
    assert "tier" in route(0.9, target_ok=True, selection_none_covered=True, cfg=CFG).reason
    assert route(0.1, target_ok=True, selection_none_covered=False, cfg=CFG).reason


# --- inputs that come from the parse ----------------------------------------------------------


def _parse(q: str):  # type: ignore[no-untyped-def]
    return parse(q, LINKER, target_ord=2)


def test_cap_inputs_from_a_clean_parse() -> None:
    got = cap_inputs_from_parse(_parse("What is the APR for a 60-month auto loan?"))
    assert got == {"parse_ok": True, "aggregate": False, "change_cue_not_history": False}


def test_a_history_question_with_a_cue_is_not_capped_by_the_cue() -> None:
    got = cap_inputs_from_parse(_parse("Has the share certificate APY changed?"))
    assert got["parse_ok"] is True
    assert got["change_cue_not_history"] is False


def test_a_change_cue_outside_a_history_pattern_is_capped() -> None:
    got = cap_inputs_from_parse(_parse("What is the rate for a new auto loan?"))
    assert got["change_cue_not_history"] is True
    assert got["parse_ok"] is False


def test_aggregate_and_uncovered_parses() -> None:
    assert cap_inputs_from_parse(_parse("Compare auto loan rates"))["aggregate"] is True
    assert cap_inputs_from_parse(_parse("What is the CD APY?"))["parse_ok"] is False
