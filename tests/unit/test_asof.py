"""``as_of`` to ``target_ord`` (SPEC 8.1, ASOF-1 to ASOF-4)."""

from __future__ import annotations

from typing import Any

import pytest

from kk.query.asof import target_ordinal
from tests.fixtures.facts import SNAPS, snaps

# Snapshots 1..5 taken on 2026-05-12, 05-19, 06-09, 08-27, 09-28 (the SPEC 8.1 example).


def test_none_is_the_latest_snapshot() -> None:
    result = target_ordinal(None, SNAPS)
    assert (result.ordinal, result.reason) == (5, "ok")


@pytest.mark.parametrize(
    ("as_of", "ordinal"),
    [
        ("2026-05-31", 2),  # the dup-006 shape: between snapshots 2 and 3
        ("2026-05-19", 2),  # inclusive: the date of snapshot 2 itself
        ("2026-05-12", 1),
        ("2026-06-08", 2),
        ("2026-06-09", 3),
        ("2026-09-27", 4),
        ("2026-09-28", 5),
        ("2030-01-01", 5),  # after the last snapshot
    ],
)
def test_highest_ordinal_on_or_before_the_date(as_of: str, ordinal: int) -> None:
    result = target_ordinal(as_of, SNAPS)
    assert (result.ordinal, result.reason) == (ordinal, "ok")


def test_date_before_the_first_snapshot_is_a_refusal() -> None:
    result = target_ordinal("2026-05-11", SNAPS)
    assert (result.ordinal, result.reason) == (None, "before_first_snapshot")


@pytest.mark.parametrize(
    ("as_of", "ordinal"),
    [
        ("2026-05-12T10:30:00", 1),
        ("2026-05-12 10:30:00", 1),
        ("2026-05-19T00:00:00Z", 2),
        ("2026-05-19T23:59:59+02:00", 2),  # only the first ten characters count
        ("2026-05-31T08:00:00.123456", 2),
    ],
)
def test_datetime_uses_its_first_ten_characters(as_of: str, ordinal: int) -> None:
    assert target_ordinal(as_of, SNAPS).ordinal == ordinal


def test_datetime_before_the_first_snapshot_is_a_refusal() -> None:
    result = target_ordinal("2026-05-11T23:59:59", SNAPS)
    assert (result.ordinal, result.reason) == (None, "before_first_snapshot")


@pytest.mark.parametrize(
    "as_of",
    [
        "",
        "yesterday",
        "latest",
        "2026-13-01",
        "2026-02-30",
        "2026-5-31",
        "05/31/2026",
        "20260531",
        "2026-05-31x",
        " 2026-05-31",
        "2026-05",
        "May 31, 2026",
    ],
)
def test_unparseable_value_is_a_refusal_not_latest(as_of: str) -> None:
    result = target_ordinal(as_of, SNAPS)
    assert (result.ordinal, result.reason) == (None, "invalid_as_of")


@pytest.mark.parametrize("value", [20260531, 3.5, b"2026-05-31", ["2026-05-31"], object()])
def test_non_string_value_is_invalid(value: Any) -> None:
    assert target_ordinal(value, SNAPS).reason == "invalid_as_of"


def test_no_snapshots_is_a_refusal() -> None:
    assert target_ordinal(None, ()).reason == "no_snapshots"
    assert target_ordinal("2026-05-31", ()).reason == "no_snapshots"


def test_order_of_the_snapshot_list_does_not_matter() -> None:
    shuffled = (SNAPS[3], SNAPS[0], SNAPS[4], SNAPS[2], SNAPS[1])
    assert target_ordinal("2026-05-31", shuffled).ordinal == 2
    assert target_ordinal(None, shuffled).ordinal == 5


def test_a_snapshot_with_a_bad_date_is_never_chosen() -> None:
    bad = snaps("2026-05-12", "not-a-date", "2026-06-09")
    assert target_ordinal("2026-12-31", bad).ordinal == 3
    assert target_ordinal("2026-05-31", bad).ordinal == 1
