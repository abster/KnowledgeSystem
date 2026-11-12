"""``as_of`` to ``target_ord`` (SPEC 8.1). The only place a target date comes from."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from kk.types import SnapshotRef

Reason = Literal["ok", "invalid_as_of", "before_first_snapshot", "no_snapshots"]

_DATE_PREFIX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_DATE_LENGTH = 10
_SEPARATORS = ("T", " ")


@dataclass(frozen=True)
class TargetOrdinal:
    """``ordinal`` is ``None`` when the answer must be the safe refusal (``reason`` says why)."""

    ordinal: int | None
    reason: Reason


def _parse_iso(value: str) -> date | None:
    """A date, or a datetime whose first ten characters are an ISO date (ASOF-3)."""
    if len(value) > _DATE_LENGTH and value[_DATE_LENGTH] not in _SEPARATORS:
        return None
    match = _DATE_PREFIX.match(value)
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def target_ordinal(as_of: str | None, snapshots: Sequence[SnapshotRef]) -> TargetOrdinal:
    """ASOF-1 to ASOF-4: the highest ordinal whose ``taken_on`` is on or before ``as_of``.

    ``None`` means the latest snapshot (ASOF-2). A value that cannot be read as a date is a
    refusal, never "latest" (SPEC decision 3). A snapshot whose own date cannot be read is
    never chosen.
    """
    if not snapshots:
        return TargetOrdinal(None, "no_snapshots")
    if as_of is None:
        return TargetOrdinal(max(s.ordinal for s in snapshots), "ok")
    if not isinstance(as_of, str):
        return TargetOrdinal(None, "invalid_as_of")
    limit = _parse_iso(as_of)
    if limit is None:
        return TargetOrdinal(None, "invalid_as_of")
    chosen: int | None = None
    for snap in snapshots:
        taken = _parse_iso(snap.taken_on)
        if taken is not None and taken <= limit and (chosen is None or snap.ordinal > chosen):
            chosen = snap.ordinal
    if chosen is None:
        return TargetOrdinal(None, "before_first_snapshot")
    return TargetOrdinal(chosen, "ok")
