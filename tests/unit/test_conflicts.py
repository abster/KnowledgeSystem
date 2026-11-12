"""Conflict detection, resolution and wording (SPEC 10, CR-1, CR-2, CA-1; Step 5)."""

from __future__ import annotations

import random
import re
from dataclasses import replace

import pytest

from contract import Conflict, Source
from kk.conflicts import (
    MAX_NAMED_LOSERS,
    MAX_TITLE_CHARS,
    conflict_note,
    detect_conflicts,
    resolve_key_at,
    to_contract,
)
from kk.types import FactVersion
from tests.fixtures.facts import SNAPS, make_fact, make_version, snaps

A = "https://example.test/rates/certificates"
B = "https://example.test/promo"
C = "https://example.test/calculator"
TERM6 = {"term": 6}


def _cert(**kw: object) -> FactVersion:
    base: dict[str, object] = {"label": "6-month certificate", "qualifiers": TERM6}
    base.update(kw)
    return make_version(**base)  # type: ignore[arg-type]


# --- FactVersion -----------------------------------------------------------------------------


def test_fact_version_exposes_interval_and_page() -> None:
    v = make_version(url=A, title="Rates", first=2, last=4, fetched_at="2026-06-09T00:00:00.000Z")
    assert (v.first_seen_ord, v.last_seen_ord) == (2, 4)
    assert (v.url, v.title, v.source_class) == (A, "Rates", "rate_sheet")
    assert v.fetched_at == "2026-06-09T00:00:00.000Z"
    assert [v.visible_at(i) for i in range(1, 6)] == [False, True, True, True, False]


def test_fact_version_rejects_mismatched_chunk_or_rank() -> None:
    v = make_version()
    with pytest.raises(ValueError, match="different chunks"):
        FactVersion(replace(v.fact, chunk_id="other"), v.interval)
    with pytest.raises(ValueError, match="authority_rank"):
        FactVersion(replace(v.fact, authority_rank=1), v.interval)


# --- the selftest case -----------------------------------------------------------------------


def _selftest() -> list[FactVersion]:
    return [
        _cert(value="2.95%", url=A, title="Certificates", first=1, last=1),
        _cert(value="3.50%", url=A, title="Certificates", first=2, last=2),
        _cert(
            value="2.95%",
            url=B,
            title="Promo",
            first=2,
            last=2,
            expiry="2025-09-30",
            rank=1,
            source_class="newsroom_promo",
            origin="prose_llm",
        ),
    ]


def test_selftest_case_resolves_to_current_value_by_effective_date() -> None:
    records = detect_conflicts(_selftest(), snapshots=snaps("2026-01-05", "2026-03-01"))
    assert len(records) == 1
    rec = records[0]
    assert rec.topic == "6-month certificate APY"
    assert rec.values == ("2.95%", "3.50%")
    assert rec.sources == (
        Source(url=B, snapshot_id="s2", quote="6-month certificate 2.95%"),
        Source(url=A, snapshot_id="s2", quote="6-month certificate 3.50%"),
    )
    assert (rec.resolution, rec.strategy, rec.ords) == ("3.50%", "effective_date", (2,))
    assert rec.reason == (
        'strategy=effective_date: 3.50% (no effective date, live, "Certificates") '
        'supersedes 2.95% (no effective date, expired 2025-09-30, "Promo")'
    )
    assert re.fullmatch(r"[0-9a-f]{16}", rec.conflict_id)


def test_replaced_value_over_time_is_not_a_conflict() -> None:
    assert detect_conflicts(_selftest()[:2], snapshots=snaps("2026-01-05", "2026-03-01")) == []


# --- strategies ------------------------------------------------------------------------------


def test_strategy_1b_later_effective_date_wins() -> None:
    old = _cert(value="2.95%", url=A, title="A", effective="2026-02-10")
    new = _cert(value="3.50%", url=B, title="B", effective="2026-04-20")
    (rec,) = detect_conflicts([old, new], snapshots=SNAPS)
    assert (rec.resolution, rec.strategy) == ("3.50%", "effective_date")
    assert rec.reason == (
        'strategy=effective_date: 3.50% (effective 2026-04-20, live, "B") '
        'supersedes 2.95% (effective 2026-02-10, live, "A")'
    )


def test_strategy_1b_needs_every_value_dated() -> None:
    """One undated value: 1(b) does not apply, so authority decides."""
    dated = _cert(value="3.50%", url=A, effective="2026-04-20", rank=4, source_class="calculator")
    undated = _cert(value="2.95%", url=B, rank=6)
    (rec,) = detect_conflicts([dated, undated], snapshots=SNAPS)
    assert (rec.resolution, rec.strategy) == ("2.95%", "source_authority")


def test_strategy_1a_three_values_one_expired_all_dated_uses_1b() -> None:
    expired = _cert(value="1.00%", url=A, effective="2026-04-30", expiry="2026-05-01")
    live1 = _cert(value="2.00%", url=B, effective="2026-02-01")
    live2 = _cert(value="3.00%", url=C, effective="2026-04-01")
    (rec,) = detect_conflicts([expired, live1, live2], snapshots=SNAPS)
    assert rec.values == ("1.00%", "2.00%", "3.00%")
    # Two live values remain, so 1(a) does not pick one; 1(b) picks the latest effective date,
    # and the expired value is dated later but does not win: it is expired.
    assert (rec.resolution, rec.strategy) == ("3.00%", "effective_date")


def test_strategy_2_higher_rank_wins_and_reason_names_class_rank_and_origin() -> None:
    sheet = _cert(value="5.39%", url=A, title="Rates", rank=6, source_class="rate_sheet")
    calc = _cert(value="5.34%", url=C, title="Calculator", rank=4, source_class="calculator")
    (rec,) = detect_conflicts([calc, sheet], snapshots=SNAPS)
    assert (rec.resolution, rec.strategy) == ("5.39%", "source_authority")
    assert rec.reason == (
        "strategy=source_authority: 5.39% (no effective date, live, rate_sheet rank 6, "
        'table_rule, "Rates") supersedes 5.34% (no effective date, live, calculator rank 4, '
        'table_rule, "Calculator")'
    )


def test_strategy_2_equal_rank_table_rule_beats_prose() -> None:
    """PRO-4: with equal rank, a table fact outranks a prose fact."""
    table = _cert(value="3.50%", url=A, title="T", origin="table_rule")
    prose = _cert(value="3.60%", url=B, title="P", origin="prose_llm")
    (rec,) = detect_conflicts([prose, table], snapshots=SNAPS)
    assert (rec.resolution, rec.strategy) == ("3.50%", "source_authority")


def test_shared_top_rank_goes_to_recency_not_to_unresolved() -> None:
    """Ranks 6, 6 and 4: the lower rank drops out and the two rank-6 values go to recency."""
    vs = [
        _cert(value="1.00%", url=A, title="A", rank=6, fetched_at="2026-05-12T08:00:00.000Z"),
        _cert(value="2.00%", url=B, title="B", rank=6, fetched_at="2026-05-12T09:00:00.000Z"),
        _cert(value="3.00%", url=C, title="C", rank=4, fetched_at="2026-05-12T10:00:00.000Z"),
    ]
    (rec,) = detect_conflicts(vs, snapshots=SNAPS)
    assert (rec.resolution, rec.strategy) == ("2.00%", "snapshot_recency")
    assert rec.reason.startswith("strategy=snapshot_recency (weaker evidence): 2.00% ")


def test_shared_top_rank_with_equal_recency_is_unresolved() -> None:
    vs = [
        _cert(value="1.00%", url=A, rank=6),
        _cert(value="2.00%", url=B, rank=6),
        _cert(value="3.00%", url=C, rank=4),
    ]
    (rec,) = detect_conflicts(vs, snapshots=SNAPS)
    assert (rec.resolution, rec.strategy) == (None, "unresolved")


def test_strategy_3_recency_and_weaker_evidence_wording() -> None:
    old = _cert(value="3.50%", url=A, title="A", fetched_at="2026-06-09T08:00:00.000Z")
    new = _cert(value="3.90%", url=B, title="B", fetched_at="2026-09-28T08:00:00.000Z")
    (rec,) = detect_conflicts([old, new], snapshots=SNAPS)
    assert (rec.resolution, rec.strategy) == ("3.90%", "snapshot_recency")
    assert rec.reason == (
        "strategy=snapshot_recency (weaker evidence): 3.90% (no effective date, live, "
        'fetched 2026-09-28, "B") supersedes 3.50% (no effective date, live, '
        'fetched 2026-06-09, "A")'
    )


def test_strategy_3_ties_on_fetch_time_use_first_seen_ordinal() -> None:
    a = _cert(value="3.50%", url=A, first=1, last=3)
    b = _cert(value="3.90%", url=B, first=2, last=3)
    (rec,) = detect_conflicts([a, b], snapshots=SNAPS)
    assert (rec.resolution, rec.strategy) == ("3.90%", "snapshot_recency")


def test_unresolved_when_everything_ties() -> None:
    a = _cert(value="3.50%", url=A, title="A")
    b = _cert(value="3.90%", url=B, title="B")
    (rec,) = detect_conflicts([a, b], snapshots=SNAPS)
    assert (rec.resolution, rec.strategy) == (None, "unresolved")
    assert rec.reason == (
        'strategy=unresolved: 3.50% (no effective date, live, "A"); '
        '3.90% (no effective date, live, "B") cannot be separated by effective date, '
        "source authority or snapshot recency"
    )


def test_all_values_expired_is_unresolved() -> None:
    """No live value is left, so nothing can be called current, even with differing ranks."""
    a = _cert(value="3.50%", url=A, title="A", rank=6, expiry="2026-05-01")
    b = _cert(
        value="2.95%", url=B, title="B", rank=4, source_class="calculator", expiry="2026-04-01"
    )
    (rec,) = detect_conflicts([a, b], snapshots=SNAPS)
    assert (rec.resolution, rec.strategy) == (None, "unresolved")
    assert rec.reason == (
        "strategy=unresolved: every competing value is expired: "
        '2.95% (no effective date, expired 2026-04-01, "B"); '
        '3.50% (no effective date, expired 2026-05-01, "A")'
    )
    res = resolve_key_at([a, b], ordinal=1, snapshots=SNAPS)
    assert (res.outcome, res.winner) == ("unresolved", None)


def test_expiry_on_the_snapshot_date_is_expired_but_the_next_day_is_live() -> None:
    """Ordinal 1 is 2026-05-12: expiring that day is expired, expiring the day after is not."""
    current = _cert(value="3.50%", url=A, title="A", rank=4, source_class="calculator")
    on_day = _cert(value="2.95%", url=B, title="B", rank=6, expiry="2026-05-12")
    (rec,) = detect_conflicts([current, on_day], snapshots=SNAPS)
    assert (rec.resolution, rec.strategy) == ("3.50%", "effective_date")
    assert 'expired 2026-05-12, "B"' in rec.reason
    next_day = _cert(value="2.95%", url=B, title="B", rank=6, expiry="2026-05-13")
    (rec,) = detect_conflicts([current, next_day], snapshots=SNAPS)
    assert (rec.resolution, rec.strategy) == ("2.95%", "source_authority")


def test_effective_date_after_the_snapshot_always_loses() -> None:
    current = _cert(value="3.50%", url=A, title="A", rank=4, source_class="calculator")
    future = _cert(value="3.90%", url=B, title="B", rank=6, effective="2026-06-01")
    (rec,) = detect_conflicts([current, future], snapshots=SNAPS)
    assert (rec.resolution, rec.strategy) == ("3.50%", "effective_date")
    assert rec.reason == (
        'strategy=effective_date: 3.50% (no effective date, live, "A") supersedes '
        '3.90% (effective 2026-06-01, not yet in force, "B")'
    )


def test_effective_date_on_the_snapshot_date_is_in_force() -> None:
    on_day = _cert(value="3.90%", url=B, title="B", rank=6, effective="2026-05-12")
    other = _cert(value="3.50%", url=A, title="A", rank=4, source_class="calculator")
    (rec,) = detect_conflicts([other, on_day], snapshots=SNAPS)
    assert (rec.resolution, rec.strategy) == ("3.90%", "source_authority")


def test_nothing_current_is_unresolved_when_values_are_not_yet_in_force() -> None:
    a = _cert(value="3.50%", url=A, title="A", effective="2026-06-01")
    b = _cert(value="3.90%", url=B, title="B", effective="2026-07-01")
    (rec,) = detect_conflicts([a, b], snapshots=SNAPS)
    assert (rec.resolution, rec.strategy) == (None, "unresolved")
    assert rec.reason.startswith("strategy=unresolved: no competing value is current: ")
    assert "not yet in force" in rec.reason


def test_a_date_tie_continues_to_authority() -> None:
    older = _cert(value="1.00%", url=A, title="A", effective="2026-03-01", rank=6)
    tie_low = _cert(value="2.00%", url=B, title="B", effective="2026-04-20", rank=4)
    tie_high = _cert(value="3.00%", url=C, title="C", effective="2026-04-20", rank=6)
    (rec,) = detect_conflicts([older, tie_low, tie_high], snapshots=SNAPS)
    assert (rec.resolution, rec.strategy) == ("3.00%", "source_authority")


def test_future_value_is_current_at_a_later_ordinal() -> None:
    """Dated 2026-06-01: not in force at ordinals 1 and 2, in force from ordinal 3 (2026-06-09)."""
    base = _cert(
        value="3.50%", url=A, title="A", rank=4, source_class="calculator", first=1, last=3
    )
    rising = _cert(value="3.90%", url=B, title="B", rank=6, effective="2026-06-01", first=1, last=3)
    (rec,) = detect_conflicts([base, rising], snapshots=SNAPS)
    assert rec.ords == (1, 2, 3)
    assert (rec.resolution, rec.strategy) == ("3.90%", "source_authority")  # judged at ordinal 3


def test_resolution_uses_the_latest_ordinal() -> None:
    """The promo is live at ordinal 1 and expired at ordinal 3: resolved by expiry."""
    current = _cert(value="3.50%", url=A, title="A", first=1, last=3)
    promo = _cert(value="2.95%", url=B, title="B", first=1, last=3, expiry="2026-06-01")
    (rec,) = detect_conflicts([current, promo], snapshots=SNAPS)
    assert rec.ords == (1, 2, 3)
    assert (rec.resolution, rec.strategy) == ("3.50%", "effective_date")
    assert 'expired 2026-06-01, "B"' in rec.reason


# --- what is not a conflict ------------------------------------------------------------------


def test_not_conflicts() -> None:
    different_tier = [
        _cert(value="3.50%", url=A, qualifiers={"term": 6}),
        _cert(value="3.90%", url=B, qualifiers={"term": 12}),
    ]
    rate_vs_apy = [
        _cert(value="3.50%", url=A, attribute="apy"),
        _cert(value="3.40%", url=B, attribute="dividend_rate"),
    ]
    formatting = [_cert(value="3.5%", url=A), _cert(value="3.50 %", url=B)]
    same_url = [_cert(value="3.50%", url=A, first=1), _cert(value="3.90%", url=A, first=1)]
    inferred = [_cert(value="3.50%", url=A), _cert(value="3.90%", url=B, inferred=True)]
    none_value = [_cert(value="3.50%", url=A), _cert(value="—", url=B, none=True)]
    for versions in (different_tier, rate_vs_apy, formatting, same_url, inferred, none_value):
        assert detect_conflicts(versions, snapshots=SNAPS) == []


def test_other_ordinals_do_not_coexist() -> None:
    a = _cert(value="3.50%", url=A, first=1, last=1)
    b = _cert(value="3.90%", url=B, first=2, last=2)
    assert detect_conflicts([a, b], snapshots=SNAPS) == []


# --- merging, ordering, wording limits -------------------------------------------------------


def test_merge_across_ordinals_by_key_and_value_set() -> None:
    a = _cert(value="3.50%", url=A, title="A", first=1, last=3, effective="2026-05-01")
    b1 = _cert(value="2.95%", url=B, title="B", first=1, last=2, effective="2026-04-01")
    b2 = _cert(value="3.10%", url=B, title="B", first=3, last=3, effective="2026-04-01")
    records = detect_conflicts([a, b1, b2], snapshots=SNAPS)
    assert [(r.values, r.ords) for r in records] == [
        (("2.95%", "3.50%"), (1, 2)),
        (("3.10%", "3.50%"), (3,)),
    ]


def test_output_order_is_stable_under_input_order() -> None:
    def pair(entity: str, label: str, v1: str, v2: str) -> list[FactVersion]:
        return [
            make_version(entity=entity, label=label, value=v1, url=f"{A}/{entity}/1", title="X"),
            make_version(entity=entity, label=label, value=v2, url=f"{A}/{entity}/2", title="Y"),
        ]

    versions = (
        pair("zeta", "Zeta Account", "1.00%", "2.00%")
        + pair("alpha", "Alpha Account", "3.00%", "4.00%")
        + pair("mid", "Mid Account", "5.00%", "6.00%")
    )
    expected = detect_conflicts(versions, snapshots=SNAPS)
    assert [r.topic for r in expected] == [
        "Alpha Account APY",
        "Mid Account APY",
        "Zeta Account APY",
    ]
    rng = random.Random(7)  # noqa: S311
    for _ in range(20):
        shuffled = versions[:]
        rng.shuffle(shuffled)
        assert detect_conflicts(shuffled, snapshots=SNAPS) == expected


def test_value_on_several_pages_cites_the_highest_authority_page() -> None:
    low = _cert(value="3.50%", url=A, title="Low", rank=3, source_class="faq_learn")
    high = _cert(value="3.50%", url=B, title="High", rank=6)
    other = _cert(value="2.95%", url=C, title="Other", rank=5)
    (rec,) = detect_conflicts([low, high, other], snapshots=SNAPS)
    assert rec.values == ("2.95%", "3.50%")
    assert [s.url for s in rec.sources] == [C, B]
    assert (rec.resolution, rec.strategy) == ("3.50%", "source_authority")


def test_at_most_three_losers_are_named_winner_always_complete() -> None:
    versions = [
        _cert(value=f"{n}.00%", url=f"{A}/{n}", title=f"P{n}", effective=f"2026-0{n}-01")
        for n in (1, 2, 3, 4, 5)
    ]
    # 5.00% has the latest date and wins; losers sort ascending and four exceed the cap of 3.
    (rec,) = detect_conflicts(versions, snapshots=SNAPS)
    assert MAX_NAMED_LOSERS == 3
    assert rec.resolution == "5.00%"
    assert rec.reason.startswith(
        'strategy=effective_date: 5.00% (effective 2026-05-01, live, "P5") supersedes '
        '1.00% (effective 2026-01-01, live, "P1"); 2.00% (effective 2026-02-01, live, "P2"); '
        '3.00% (effective 2026-03-01, live, "P3"); '
    )
    assert rec.reason.endswith("and 1 more")
    assert "4.00%" not in rec.reason


def test_long_titles_are_cut_with_an_ellipsis() -> None:
    long_title = "T" * 80
    a = _cert(value="3.50%", url=A, title=long_title, effective="2026-04-20")
    b = _cert(value="2.95%", url=B, title="B", effective="2026-02-10")
    (rec,) = detect_conflicts([a, b], snapshots=SNAPS)
    cut = "T" * (MAX_TITLE_CHARS - 1) + "…"
    assert f'"{cut}"' in rec.reason
    assert "T" * 60 not in rec.reason


# --- resolve_key_at (used by history) --------------------------------------------------------


def test_resolve_key_at_outcomes() -> None:
    a = _cert(value="3.50%", url=A, first=1, last=2)
    a_same = _cert(value="3.5%", url=f"{A}/zz", first=1, last=2)
    c_new = _cert(value="3.90%", url=C, first=2, last=2, effective="2026-05-15")
    c_old = _cert(value="3.50%", url=B, first=2, last=2, effective="2026-01-01")
    absent = resolve_key_at([a], ordinal=3, snapshots=SNAPS)
    assert (absent.outcome, absent.winner) == ("absent", None)
    single = resolve_key_at([a, a_same], ordinal=1, snapshots=SNAPS)
    assert (single.outcome, single.strategy) == ("single", None)
    assert single.winner is not None and single.winner.fact.value_text == "3.50%"
    resolved = resolve_key_at([a, c_new, c_old], ordinal=2, snapshots=SNAPS)
    assert (resolved.outcome, resolved.strategy) == ("resolved", "effective_date")
    assert resolved.winner is not None and resolved.winner.fact.value_text == "3.90%"


def test_resolve_key_at_same_url_values_are_unresolved() -> None:
    two = [_cert(value="3.50%", url=A), _cert(value="3.90%", url=A)]
    res = resolve_key_at(two, ordinal=1, snapshots=SNAPS)
    assert (res.outcome, res.winner) == ("unresolved", None)


# --- notes and harness record ----------------------------------------------------------------


def test_conflict_note_forms() -> None:
    (rec,) = detect_conflicts(_selftest(), snapshots=snaps("2026-01-05", "2026-03-01"))
    assert conflict_note(rec) == "strategy=effective_date: 3.50% used; 2.95% superseded"
    tied = detect_conflicts(
        [_cert(value="3.50%", url=A), _cert(value="3.90%", url=B)], snapshots=SNAPS
    )[0]
    assert conflict_note(tied) == "strategy=unresolved: 3.50%, 3.90% conflict; no value chosen"
    many = detect_conflicts(
        [_cert(value=f"{n}.00%", url=f"{A}/{n}", effective=f"2026-0{n}-01") for n in (1, 2, 3)],
        snapshots=SNAPS,
    )[0]
    assert conflict_note(many) == "strategy=effective_date: 3.00% used; 1.00%, 2.00% superseded"


def test_to_contract_keeps_fields_in_order() -> None:
    (rec,) = detect_conflicts(_selftest(), snapshots=snaps("2026-01-05", "2026-03-01"))
    out = to_contract(rec)
    assert isinstance(out, Conflict)
    assert (out.topic, out.values, out.resolution, out.reason) == (
        rec.topic,
        ["2.95%", "3.50%"],
        "3.50%",
        rec.reason,
    )
    assert out.sources == list(rec.sources)


def test_make_fact_helper_matches_fact_key_rule() -> None:
    f = make_fact(qualifiers={"term": 6})
    assert len(f.fact_key) == 64
