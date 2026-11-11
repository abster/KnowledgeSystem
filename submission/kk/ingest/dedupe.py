"""Content hashing and validity intervals (SPEC 6.4, ING-7 to ING-10).

The interval table lives in memory here; Step 7 persists the same rows in LanceDB. An
unchanged chunk extends its interval, a changed or returning chunk starts a new one, and a
chunk missing from a snapshot is simply not extended.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from kk.query.normalise import clean_text
from kk.types import Chunk

HEADER_SEP = " > "


def normalise_for_hash(text: str) -> str:
    """NFKC, collapsed whitespace, trimmed ends; case is preserved (ING-7)."""
    return clean_text(text)


def make_chunk_id(url: str, parent_headers: tuple[str, ...], text: str) -> str:
    """Hex SHA-256 of url, header path and normalised text, newline separated (SPEC 5.1)."""
    header_path = HEADER_SEP.join(parent_headers)
    payload = f"{url}\n{header_path}\n{normalise_for_hash(text)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


@dataclass
class Interval:
    """One chunk row with its validity interval. Primary key: (chunk_id, first_seen_ord)."""

    chunk: Chunk
    first_seen_ord: int
    last_seen_ord: int
    first_seen_date: date | None
    last_seen_date: date | None
    fetched_at: str


@dataclass(frozen=True)
class SnapshotDelta:
    """What adding one snapshot did: new rows, extended rows and in-snapshot repeats."""

    new: tuple[Interval, ...]
    extended: tuple[Interval, ...]
    repeated: int


@dataclass
class IntervalStore:
    """In-memory interval table, in insertion order."""

    intervals: list[Interval] = field(default_factory=list)
    _latest: dict[str, Interval] = field(default_factory=dict, repr=False)

    def add_snapshot(self, ordinal: int, taken_on: str, chunks: Iterable[Chunk]) -> SnapshotDelta:
        """Add the chunks of snapshot ``ordinal`` (ING-8).

        A chunk whose latest interval ended at ``ordinal - 1`` is extended. Anything else
        (brand new, changed, or back after a gap) opens a new interval.
        """
        taken_date = _iso_date(taken_on)
        new: list[Interval] = []
        extended: list[Interval] = []
        repeated = 0
        for chunk in chunks:
            existing = self._latest.get(chunk.chunk_id)
            if existing is not None and existing.last_seen_ord == ordinal:
                repeated += 1
            elif existing is not None and existing.last_seen_ord == ordinal - 1:
                existing.last_seen_ord = ordinal
                existing.last_seen_date = taken_date
                existing.fetched_at = chunk.fetched_at
                extended.append(existing)
            else:
                interval = Interval(
                    chunk, ordinal, ordinal, taken_date, taken_date, chunk.fetched_at
                )
                self.intervals.append(interval)
                self._latest[chunk.chunk_id] = interval
                new.append(interval)
        return SnapshotDelta(tuple(new), tuple(extended), repeated)

    def visible_at(self, target_ord: int) -> list[Interval]:
        """Rows whose interval contains ``target_ord`` (the validity pre-filter, SPEC 5.4)."""
        return [i for i in self.intervals if i.first_seen_ord <= target_ord <= i.last_seen_ord]
