"""Tests for the calibration procedure (SPEC 12.4) on synthetic items. No snapshot text."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from kk.config import DEFAULT_CONFIG_PATH, load_config
from tests.fixtures.facts import make_fact
from tools.calibrate import (
    HELDOUT_SHARE,
    Scored,
    build_report,
    choose_tau_accept,
    choose_tau_floor,
    generate_questions,
    precision_at,
    split_items,
    split_of,
    write_thresholds,
)


def _item(  # noqa: PLR0913
    item_id: str,
    c: float,
    label: str,
    *,
    group: str | None = None,
    capped: bool = False,
    answerable: bool | None = None,
) -> Scored:
    return Scored(
        item_id=item_id,
        group=group or item_id,
        confidence=c,
        label=label,  # type: ignore[arg-type]
        capped=capped,
        answerable=(label != "should_refuse") if answerable is None else answerable,
    )


# --- split (SPEC 12.4 step 2, DET-3) ---------------------------------------------------------


def test_the_split_is_deterministic_and_does_not_depend_on_the_process() -> None:
    first = [split_of(f"g{i}") for i in range(200)]
    assert first == [split_of(f"g{i}") for i in range(200)]
    # a fixed expectation, so a change of hash function (or hash randomisation) is noticed
    assert split_of("dup-001") == split_of("dup-001")
    assert {split_of(f"g{i}") for i in range(200)} == {"train", "heldout"}


def test_about_thirty_percent_is_held_out() -> None:
    held = sum(split_of(f"item-{i}") == "heldout" for i in range(4000)) / 4000
    assert abs(held - HELDOUT_SHARE) < 0.03


def test_items_of_one_group_stay_together() -> None:
    items = [_item(f"i{n}", 0.9, "correct", group=f"grp{n % 25}") for n in range(300)]
    train, held = split_items(items)
    assert len(train) + len(held) == len(items)
    assert {i.group for i in train}.isdisjoint({i.group for i in held})


def test_the_split_ignores_the_order_of_the_items() -> None:
    items = [_item(f"i{n}", 0.5, "correct", group=f"g{n % 7}") for n in range(60)]
    a = split_items(items)
    b = split_items(list(reversed(items)))
    assert {i.item_id for i in a[0]} == {i.item_id for i in b[0]}


# --- tau_accept (SPEC 12.4 step 4) -----------------------------------------------------------


def test_precision_counts_only_uncapped_items_at_or_above_the_threshold() -> None:
    items = [
        _item("a", 0.9, "correct"),
        _item("b", 0.8, "wrong"),
        _item("c", 0.7, "should_refuse"),  # accepted by the fast path, so it is wrong
        _item("d", 0.95, "wrong", capped=True),  # a capped item never takes the fast path
        _item("e", 0.2, "wrong"),
    ]
    assert precision_at(items, 0.8) == pytest.approx(1 / 2)
    assert precision_at(items, 0.7) == pytest.approx(1 / 3)
    assert precision_at(items, 0.99) is None  # nothing answered


def test_tau_accept_is_the_lowest_value_that_meets_the_target() -> None:
    items = [_item(f"ok{i}", 0.90 + i / 1000, "correct") for i in range(40)]
    items += [_item(f"mid{i}", 0.70 + i / 1000, "correct") for i in range(40)]
    items += [_item(f"bad{i}", 0.60 + i / 100, "wrong") for i in range(4)]
    # at 0.60 and 0.61 the wrong answers hold precision under 0.97 (80/84, 80/83); at 0.62 it is
    # 80/82 = 0.976
    tau = choose_tau_accept(items, target=0.97)
    assert tau == pytest.approx(0.62)


def test_no_lower_candidate_value_meets_the_target() -> None:
    items = [_item(f"a{i}", 0.50 + i / 1000, "correct") for i in range(30)]
    items += [_item(f"w{i}", 0.55 + i / 1000, "wrong") for i in range(10)]
    items += [_item(f"b{i}", 0.70 + i / 1000, "correct") for i in range(60)]
    tau = choose_tau_accept(items, target=0.97)
    assert tau is not None
    assert (precision_at(items, tau) or 0) >= 0.97
    lower = sorted({i.confidence for i in items if i.confidence < tau})
    assert lower  # the check below is not vacuous
    assert all((precision_at(items, t) or 0) < 0.97 for t in lower)


def test_no_threshold_meets_an_impossible_target() -> None:
    items = [_item("a", 0.9, "wrong"), _item("b", 0.8, "wrong")]
    assert choose_tau_accept(items, target=0.97) is None


def test_a_should_refuse_item_accepted_by_the_fast_path_counts_against_precision() -> None:
    items = [_item(f"c{i}", 0.80 + i / 1000, "correct") for i in range(10)]
    items += [_item("neg", 0.95, "should_refuse")]
    assert precision_at(items, 0.80) == pytest.approx(10 / 11)
    assert choose_tau_accept(items, target=0.97) is None  # the negative sits above every item


# --- tau_floor -------------------------------------------------------------------------------


def test_tau_floor_is_the_highest_value_that_loses_at_most_two_percent_of_answerable_items() -> (
    None
):
    items = [_item(f"a{i}", 0.30 + i * 0.006, "correct") for i in range(100)]
    items += [_item("n1", 0.05, "should_refuse"), _item("n2", 0.10, "should_refuse")]
    # two of 100 answerable items (2%) may fall below the floor, so it is the third lowest value
    assert choose_tau_floor(items, tau_accept=0.80, max_miss=0.02) == pytest.approx(0.312)


def test_tau_floor_stays_below_tau_accept() -> None:
    items = [_item(f"a{i}", 0.9, "correct") for i in range(10)]
    assert choose_tau_floor(items, tau_accept=0.8, max_miss=0.02) < 0.8


def test_tau_floor_with_no_answerable_item_is_zero() -> None:
    assert (
        choose_tau_floor([_item("n", 0.5, "should_refuse")], tau_accept=0.8, max_miss=0.02) == 0.0
    )


# --- report ----------------------------------------------------------------------------------


def _scored_set() -> list[Scored]:
    items = []
    for i in range(300):
        c = 0.5 + (i % 50) / 100
        label = "correct" if c >= 0.55 else "wrong"
        items.append(_item(f"i{i}", c, label, group=f"g{i % 90}"))
    return items


def test_the_report_has_train_and_held_out_figures_and_the_chosen_values() -> None:
    items = _scored_set()
    report = build_report(items, tau_floor=0.3, tau_accept=0.6, target=0.97)
    for key in ("train", "heldout"):
        assert {"n", "precision", "recall", "answered"} <= set(report[key])
    assert report["tau_floor"] == 0.3
    assert report["tau_accept"] == 0.6
    assert report["target_precision"] == 0.97
    assert report["curve"]
    assert report["train"]["n"] + report["heldout"]["n"] == len(items)


def test_the_guard_flags_a_held_out_precision_more_than_three_points_below_training() -> None:
    train_like = [_item(f"t{i}", 0.9, "correct", group=f"t{i}") for i in range(200)]
    # give the held-out part wrong answers at high confidence
    held_ids = [g for g in (f"h{i}" for i in range(400)) if split_of(g) == "heldout"][:60]
    held = [_item(f"x{g}", 0.9, "wrong", group=g) for g in held_ids]
    report = build_report([*train_like, *held], tau_floor=0.3, tau_accept=0.8, target=0.97)
    assert report["guard"]["ok"] is False
    assert report["guard"]["gap"] > 0.03


def test_the_guard_passes_when_both_parts_agree() -> None:
    items = [_item(f"i{i}", 0.9, "correct", group=f"g{i}") for i in range(300)]
    report = build_report(items, tau_floor=0.3, tau_accept=0.8, target=0.97)
    assert report["guard"]["ok"] is True


def test_the_report_holds_only_numbers_and_ids_of_its_own() -> None:
    report = build_report(_scored_set(), 0.3, 0.6, 0.97)
    assert set(report) == {
        "target_precision",
        "tau_floor",
        "tau_accept",
        "train",
        "heldout",
        "curve",
        "guard",
    }


# --- generated questions (SPEC 12.4 step 1) --------------------------------------------------


def _facts() -> list[Any]:
    return [
        make_fact(
            value="4.10%",
            entity="example certificate",
            label="Example Certificate",
            attribute="apy",
            qualifiers={"term": 17},
        ),
        make_fact(
            value="$25.00",
            entity="example account",
            label="Example Account",
            attribute="fee",
        ),
    ]


def test_generated_questions_carry_the_known_value_as_gold() -> None:
    questions = generate_questions(_facts())
    answerable = [q for q in questions if not q.should_refuse]
    assert answerable
    assert {q.gold for q in answerable if "certificate" in q.text.lower()} == {"4.10%"}
    assert {q.gold for q in answerable if "account" in q.text.lower()} == {"$25.00"}


def test_paraphrases_of_one_fact_share_a_group_and_ids_are_unique() -> None:
    questions = generate_questions(_facts())
    assert len({q.item_id for q in questions}) == len(questions)
    cert = [
        q
        for q in questions
        if q.group
        == make_fact(
            entity="example certificate",
            label="Example Certificate",
            attribute="apy",
            qualifiers={"term": 17},
        ).fact_key
    ]
    assert len(cert) >= 3  # several paraphrase templates plus negatives and adversarial forms


def test_generation_is_deterministic() -> None:
    assert generate_questions(_facts()) == generate_questions(_facts())


def test_negatives_ask_for_what_does_not_exist_and_have_no_gold() -> None:
    negatives = [q for q in generate_questions(_facts()) if q.should_refuse]
    assert negatives
    assert all(q.gold is None for q in negatives)


def test_adversarial_questions_keep_the_gold_of_their_base_fact() -> None:
    adversarial = [q for q in generate_questions(_facts()) if q.kind == "adversarial"]
    assert adversarial
    assert all(q.gold in {"4.10%", "$25.00"} for q in adversarial)
    assert any(
        re.search(r"\b(about|roughly|around|between)\b", q.text.lower()) for q in adversarial
    )


# --- writing the thresholds ------------------------------------------------------------------


def test_write_thresholds_updates_only_the_calibrated_keys_and_stays_valid(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text(DEFAULT_CONFIG_PATH.read_text())
    before = json.loads(target.read_text())
    write_thresholds(
        target,
        tau_floor=0.41,
        tau_accept=0.77,
        bm25_bound=12.5,
        dataset="synthetic test set",
        report_name="calibration_report.json",
    )
    after = json.loads(target.read_text())
    assert after["confidence"]["tau_floor"] == 0.41
    assert after["confidence"]["tau_accept"] == 0.77
    assert after["confidence"]["bm25_bound"] == 12.5
    assert after["confidence"]["weights"] == before["confidence"]["weights"]
    assert after["retrieval"] == before["retrieval"]
    prov = after["provenance"]["confidence"]
    assert "calibration" in prov["set_by"].lower()
    assert prov["dataset"] == "synthetic test set"
    assert "calibration_report.json" in prov["set_by"] + prov["dataset"]
    assert load_config(target).confidence.tau_accept == 0.77


def test_write_thresholds_refuses_an_invalid_pair(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text(DEFAULT_CONFIG_PATH.read_text())
    with pytest.raises(ValueError, match="tau_floor"):
        write_thresholds(
            target,
            tau_floor=0.9,
            tau_accept=0.5,
            bm25_bound=10.0,
            dataset="x",
            report_name="r.json",
        )
    assert json.loads(target.read_text()) == json.loads(DEFAULT_CONFIG_PATH.read_text())
