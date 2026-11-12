"""Sibling selection, tier selection and history segments (SPEC 9.3, 9.4; Step 5)."""

from __future__ import annotations

import pytest

from kk.query.candidates import (
    MAX_SEGMENTS,
    History,
    build_history,
    find_siblings,
    remove_contradicted,
    select_siblings,
)
from kk.query.normalise import AmountConstraint, parse_amount_constraint
from kk.types import Fact, FactVersion, HistoryContext
from tests.fixtures.facts import DATES, SNAPS, make_fact, make_version, snaps

# --- siblings (SIB-1) ------------------------------------------------------------------------

BASE = {"product": "purchase", "rate_type": "fixed", "term": 360}


def _mortgage(tier: str, value: str, **extra: str | int) -> Fact:
    return make_fact(
        entity="mortgage",
        label="Mortgage",
        attribute="rate",
        value=value,
        qualifiers={**BASE, "amount_tier": tier, **extra},
        chunk_id=f"c-{tier}",
    )


CAP100 = _mortgage("<=100000", "7.13%")
CAP300 = _mortgage("<=300000", "7.25%")
FLOOR300 = _mortgage(">300000", "7.40%")


def _amount(text: str) -> AmountConstraint:
    parsed = parse_amount_constraint(text)
    assert parsed is not None, text
    return parsed


def test_find_siblings_one_varying_key() -> None:
    group = find_siblings([CAP100, CAP300])
    assert group is not None
    assert (group.entity, group.attribute, group.varying_key) == ("mortgage", "rate", "amount_tier")
    assert group.members == (CAP100, CAP300)


def test_find_siblings_negatives() -> None:
    other_attr = make_fact(entity="mortgage", label="Mortgage", attribute="apr", value="7.27%")
    two_keys = _mortgage("<=300000", "7.25%", loan_type="jumbo")
    other_entity = make_fact(entity="auto", label="Auto", attribute="rate", value="5.00%")
    assert find_siblings([CAP100]) is None
    assert find_siblings([CAP100, other_attr]) is None
    assert find_siblings([CAP100, two_keys]) is None
    assert find_siblings([CAP100, other_entity]) is None


def test_find_siblings_dedupes_the_same_fact_key() -> None:
    assert find_siblings([CAP100, CAP100]) is None


def test_find_siblings_terms() -> None:
    t36 = make_fact(attribute="apr", value="5.00%", qualifiers={"term": 36}, chunk_id="a")
    t60 = make_fact(attribute="apr", value="5.39%", qualifiers={"term": 60}, chunk_id="b")
    group = find_siblings([t36, t60])
    assert group is not None and group.varying_key == "term"


# --- SIB-6 contradiction ---------------------------------------------------------------------


def test_remove_contradicted_term_year_and_other_keys() -> None:
    t36 = make_fact(attribute="apr", qualifiers={"term": 36, "model_year": "2024-27"}, chunk_id="1")
    t60 = make_fact(attribute="apr", qualifiers={"term": 60, "model_year": "2024-27"}, chunk_id="2")
    old = make_fact(attribute="apr", qualifiers={"term": 60, "model_year": "2021-23"}, chunk_id="3")
    facts = [t36, t60, old]
    assert remove_contradicted(facts, qualifiers={"term": 60}) == (t60, old)
    # 2025 falls inside 2024-27 and outside 2021-23.
    assert remove_contradicted(facts, qualifiers={"model_year": "2025"}) == (t36, t60)
    assert remove_contradicted(facts, qualifiers={"term": 60, "model_year": "2025"}) == (t60,)
    assert remove_contradicted(facts, qualifiers={"unrelated": "x"}) == (t36, t60, old)
    assert remove_contradicted(facts, qualifiers={}) == (t36, t60, old)


# --- tier selection (SIB-3, SIB-4) -----------------------------------------------------------


def _pick(question_amount: str | None, *facts: Fact) -> tuple[str, tuple[str, ...]]:
    amount = _amount(question_amount) if question_amount else None
    sel = select_siblings(list(facts), amount=amount, qualifiers={})
    return sel.status, tuple(f.value_text for f in sel.facts)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("$50,000", ("one", ("7.13%",))),
        ("$100,000", ("one", ("7.13%",))),  # on a cap: the cap tier includes its cap
        ("$99,000", ("one", ("7.13%",))),  # 1% below the cap is outside the boundary band
        # Boundary: within 0.5% of the cap but not on it. The cap's tier and the next one.
        ("$99,700", ("several", ("7.13%", "7.25%"))),  # 0.3% below the cap
        ("$100,300", ("several", ("7.13%", "7.25%"))),  # 0.3% above the cap
        ("$100,500", ("several", ("7.13%", "7.25%"))),  # exactly 0.5% above: inside the band
        ("$100,501", ("one", ("7.25%",))),  # just outside the band
        ("$250,000", ("one", ("7.25%",))),
        ("$300,000", ("one", ("7.25%",))),
        ("$400,000", ("none", ())),  # above the top cap
    ],
)
def test_point_amount_on_cap_tiers(question: str, expected: tuple[str, tuple[str, ...]]) -> None:
    assert _pick(question, CAP100, CAP300) == expected


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("$100,000 or less", ("one", ("7.13%",))),
        ("up to $300,000", ("one", ("7.25%",))),
        ("$150,000 or less", ("several", ("7.13%", "7.25%"))),  # spans two tiers
        ("under $100,000", ("several", ("7.13%", "7.25%"))),  # strict modifier on a cap
        ("at most $50,000", ("one", ("7.13%",))),  # every such amount is in tier 1
    ],
)
def test_upper_bounds_on_cap_tiers(question: str, expected: tuple[str, tuple[str, ...]]) -> None:
    assert _pick(question, CAP100, CAP300) == expected


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("over $50,000", ("several", ("7.13%", "7.25%"))),
        ("at least $100,000", ("several", ("7.13%", "7.25%"))),
        # Only the top tier holds amounts above 250,000, but it stops at its cap: larger
        # amounts are in no published tier, so the lower bound cannot select it.
        ("over $250,000", ("several", ("7.25%",))),
        ("over $300,000", ("none", ())),
    ],
)
def test_lower_bounds_without_an_open_top_tier(
    question: str, expected: tuple[str, tuple[str, ...]]
) -> None:
    assert _pick(question, CAP100, CAP300) == expected


def test_lower_bounds_with_an_open_top_tier() -> None:
    assert _pick("over $350,000", CAP100, CAP300, FLOOR300) == ("one", ("7.40%",))
    assert _pick("over $50,000", CAP100, CAP300, FLOOR300)[0] == "several"
    assert _pick("at least $300,000", CAP100, CAP300, FLOOR300)[0] == "several"


def test_range_inside_one_tier_spanning_two_tiers_or_outside() -> None:
    assert _pick("between $120,000 and $200,000", CAP100, CAP300) == ("one", ("7.25%",))
    assert _pick("between $50,000 and $200,000", CAP100, CAP300)[0] == "several"
    assert _pick("between $400,000 and $500,000", CAP100, CAP300)[0] == "none"


def test_no_amount_in_the_question_is_several() -> None:
    sel = select_siblings([CAP100, CAP300], amount=None, qualifiers={})
    assert (sel.status, sel.varying_key) == ("several", "amount_tier")
    assert sel.facts == (CAP100, CAP300)


def test_single_fact_is_one_and_empty_is_none() -> None:
    assert select_siblings([CAP100], amount=None, qualifiers={}).status == "one"
    assert select_siblings([], amount=None, qualifiers={}).status == "none"


def test_single_tier_outside_the_stated_amount_is_none() -> None:
    assert _pick("$500,000", CAP100)[0] == "none"


def test_amount_is_ignored_when_no_fact_has_an_amount_tier() -> None:
    plain = make_fact(attribute="apy", value="3.50%")
    sel = select_siblings([plain], amount=_amount("$5,000"), qualifiers={})
    assert sel.status == "one"


# --- bands -----------------------------------------------------------------------------------


def _band(tier: str, value: str) -> Fact:
    return make_fact(
        entity="savings",
        label="Share Savings",
        attribute="apy",
        value=value,
        qualifiers={"balance_band": tier},
        chunk_id=f"b-{tier}",
    )


B1, B2, B3 = (
    _band("0.01-9999.99", "0.10%"),
    _band("10000-24999.99", "0.15%"),
    _band(">=25000", "0.20%"),
)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("$5", ("one", ("0.10%",))),
        ("$15,000", ("one", ("0.15%",))),
        ("$24,950", ("one", ("0.15%",))),  # no boundary band on bands, only on cap tiers
        ("$25,000", ("one", ("0.20%",))),
        ("$1,000,000", ("one", ("0.20%",))),
        ("$9,999.995", ("none", ())),  # in the gap between two bands
        ("over $30,000", ("one", ("0.20%",))),
        ("between $10,000 and $24,999.99", ("one", ("0.15%",))),
        ("at least $25,000", ("one", ("0.20%",))),  # equals the floor tier's own constraint
        ("over $25,000", ("one", ("0.20%",))),  # all such amounts are in the floor tier
        ("between $5,000 and $15,000", ("several", ("0.10%", "0.15%"))),
    ],
)
def test_band_selection(question: str, expected: tuple[str, tuple[str, ...]]) -> None:
    assert _pick(question, B1, B2, B3) == expected


# --- other varying keys ----------------------------------------------------------------------


def test_term_siblings_selected_by_question_term() -> None:
    t36 = make_fact(attribute="apr", value="5.00%", qualifiers={"term": 36}, chunk_id="a")
    t60 = make_fact(attribute="apr", value="5.39%", qualifiers={"term": 60}, chunk_id="b")
    one = select_siblings([t36, t60], amount=None, qualifiers={"term": 60})
    assert (one.status, one.facts, one.varying_key) == ("one", (t60,), None)
    none = select_siblings([t36, t60], amount=None, qualifiers={"term": 72})
    assert (none.status, none.facts) == ("none", ())
    several = select_siblings([t36, t60], amount=None, qualifiers={})
    assert (several.status, several.varying_key) == ("several", "term")


def test_non_siblings_are_several() -> None:
    other = make_fact(entity="auto", label="Auto", attribute="rate", value="5.00%")
    sel = select_siblings([CAP100, other], amount=None, qualifiers={})
    assert sel.status == "several"


# --- history (HSEG-1 to HSEG-4) --------------------------------------------------------------

CTX = HistoryContext(snapshots=SNAPS)
URL_A, URL_B = "https://example.test/a", "https://example.test/b"


def _hist(versions: list[FactVersion], target: int = 5, ctx: HistoryContext = CTX) -> History:
    return build_history(
        versions, fact_key=versions[0].fact.fact_key, target_ordinal=target, ctx=ctx
    )


def _runs(history: History) -> list[tuple[str, str, str]]:
    return [(s.value_text, s.first_date, s.last_date) for s in history.segments]


def test_history_unchanged() -> None:
    h = _hist([make_version(value="3.50%", url=URL_A, first=1, last=5)])
    assert h.trusted and h.reasons == ()
    assert _runs(h) == [("3.50%", DATES[0], DATES[4])]


def test_history_changed_once_bounds_the_change_by_snapshot_dates() -> None:
    h = _hist(
        [
            make_version(value="3.50%", url=URL_A, first=1, last=2),
            make_version(value="3.90%", url=URL_A, first=3, last=5),
        ]
    )
    assert h.trusted
    assert _runs(h) == [("3.50%", DATES[0], DATES[1]), ("3.90%", DATES[2], DATES[4])]
    assert [(s.first_ordinal, s.last_ordinal) for s in h.segments] == [(1, 2), (3, 5)]


def test_history_changed_several_times_and_changed_back() -> None:
    h = _hist(
        [
            make_version(value="3.50%", url=URL_A, first=1, last=1),
            make_version(value="3.90%", url=URL_A, first=2, last=3),
            make_version(value="3.50%", url=URL_A, first=4, last=5),
        ]
    )
    assert h.trusted
    assert [s.value_text for s in h.segments] == ["3.50%", "3.90%", "3.50%"]


def test_history_formatting_difference_is_not_a_change() -> None:
    h = _hist(
        [
            make_version(value="3.5%", url=URL_A, first=1, last=2),
            make_version(value="3.50%", url=URL_A, first=3, last=5),
        ]
    )
    assert h.trusted and len(h.segments) == 1


def test_history_starts_at_first_appearance() -> None:
    h = _hist([make_version(value="3.50%", url=URL_A, first=3, last=5)])
    assert h.trusted and _runs(h) == [("3.50%", DATES[2], DATES[4])]


def test_history_respects_target_ordinal() -> None:
    versions = [
        make_version(value="3.50%", url=URL_A, first=1, last=2),
        make_version(value="3.90%", url=URL_A, first=3, last=5),
    ]
    h = _hist(versions, target=2)
    assert h.trusted and _runs(h) == [("3.50%", DATES[0], DATES[1])]


def test_history_ignores_other_fact_keys() -> None:
    mine = make_version(value="3.50%", url=URL_A, first=1, last=5)
    other = make_version(value="9.99%", url=URL_B, first=1, last=5, qualifiers={"term": 12})
    assert _runs(_hist([mine, other])) == [("3.50%", DATES[0], DATES[4])]


def test_history_gap_is_untrusted() -> None:
    h = _hist(
        [
            make_version(value="3.50%", url=URL_A, first=1, last=2),
            make_version(value="3.50%", url=URL_A, first=4, last=5),
        ]
    )
    assert not h.trusted and "gap" in h.reasons


def test_history_unresolved_conflict_at_some_snapshot_is_untrusted() -> None:
    # At ordinal 3 both values first appear together, with equal rank and fetch time: a real tie.
    h = _hist(
        [
            make_version(value="3.50%", url=URL_A, first=1, last=2),
            make_version(value="3.50%", url=URL_A, first=3, last=3),
            make_version(value="3.90%", url=URL_B, first=3, last=3),
            make_version(value="3.50%", url=URL_A, first=4, last=5),
        ]
    )
    assert not h.trusted and "unresolved_conflict" in h.reasons


def test_history_later_arrival_wins_by_snapshot_recency() -> None:
    """SPEC 10.3 strategy 3: with nothing else to separate them, the later first_seen wins."""
    h = _hist(
        [
            make_version(value="3.50%", url=URL_A, first=1, last=5),
            make_version(value="3.90%", url=URL_B, first=3, last=3),
        ]
    )
    assert h.trusted
    assert [s.value_text for s in h.segments] == ["3.50%", "3.90%", "3.50%"]


def test_history_uses_the_conflict_winner_at_each_snapshot() -> None:
    h = _hist(
        [
            make_version(value="3.50%", url=URL_A, first=1, last=5, effective="2026-05-01"),
            make_version(value="2.95%", url=URL_B, first=2, last=3, effective="2026-01-01"),
        ]
    )
    assert h.trusted and _runs(h) == [("3.50%", DATES[0], DATES[4])]


def test_history_degraded_pair_is_untrusted() -> None:
    ctx = HistoryContext(snapshots=SNAPS, degraded=frozenset({("certificate", "apy")}))
    h = _hist([make_version(value="3.50%", url=URL_A, first=1, last=5)], ctx=ctx)
    assert not h.trusted and "degraded" in h.reasons


def test_history_none_value_is_untrusted() -> None:
    h = _hist([make_version(value="—", url=URL_A, first=1, last=5, none=True)])
    assert not h.trusted and "none_value" in h.reasons


def test_history_absent_at_target_is_untrusted() -> None:
    h = _hist([make_version(value="3.50%", url=URL_A, first=4, last=5)], target=3)
    assert h.segments == () and not h.trusted and "absent" in h.reasons


def test_history_segment_limit() -> None:
    six = snaps(*[f"2026-0{m}-01" for m in range(1, 7)])
    values = ["1.00%", "2.00%", "1.00%", "2.00%", "1.00%", "2.00%"]
    versions = [
        make_version(value=v, url=URL_A, first=i, last=i) for i, v in enumerate(values, start=1)
    ]
    ctx = HistoryContext(snapshots=six)
    assert MAX_SEGMENTS == 5
    five = _hist(versions, target=5, ctx=ctx)
    assert five.trusted and len(five.segments) == 5
    over = _hist(versions, target=6, ctx=ctx)
    assert not over.trusted and "too_many_segments" in over.reasons


def test_history_invalid_snapshot_date_is_untrusted() -> None:
    ctx = HistoryContext(snapshots=snaps(DATES[0], "not-a-date"))
    h = _hist([make_version(value="3.50%", url=URL_A, first=1, last=2)], target=2, ctx=ctx)
    assert not h.trusted and "bad_date" in h.reasons
