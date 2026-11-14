"""Answer cache (SPEC 11, CACHE-1 to CACHE-4).

In memory, keyed by the canonical key of a fully understood question (SPEC 8.8). The key already
holds ``target_ord``, so a different snapshot is a different entry. Least-recently-used entries
are dropped beyond ``cache.max_entries``. A stored or returned answer is always a copy.
"""

from __future__ import annotations

import copy
from collections import OrderedDict

from contract import Answer


class AnswerCache:
    """LRU map from canonical key to ``Answer``."""

    def __init__(self, max_entries: int) -> None:
        """Create an empty cache holding at most ``max_entries`` answers."""
        if max_entries <= 0:
            msg = "max_entries must be positive"
            raise ValueError(msg)
        self.max_entries = max_entries
        self._entries: OrderedDict[str, Answer] = OrderedDict()

    def __len__(self) -> int:
        """Number of stored answers."""
        return len(self._entries)

    def get(self, key: str | None) -> Answer | None:
        """A copy of the stored answer (and mark it recently used), or ``None`` (CACHE-1, 4)."""
        if key is None or key not in self._entries:
            return None
        self._entries.move_to_end(key)
        return copy.deepcopy(self._entries[key])

    def put(self, key: str | None, answer: Answer) -> None:
        """Store a copy of ``answer``; a ``None`` key stores nothing (CACHE-1)."""
        if key is None:
            return
        self._entries[key] = copy.deepcopy(answer)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        """Drop every entry (CACHE-2: every ``ingest`` clears the cache)."""
        self._entries.clear()
