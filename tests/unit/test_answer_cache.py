"""Tests for the answer cache (SPEC 11: CACHE-1 to CACHE-4)."""

from __future__ import annotations

import pytest

from contract import Answer, Source
from kk.query.cache import AnswerCache


def _answer(text: str = "The APY is 1.25%.") -> Answer:
    return Answer(
        text=text,
        sources=[Source(url="https://example.test/a", snapshot_id="01_x", quote="q")],
        confident=True,
        conflict_note="strategy=effective_date: 1.25% used; 1.10% superseded",
    )


def test_miss_then_hit_returns_the_stored_answer() -> None:
    cache = AnswerCache(max_entries=4)
    assert cache.get("k") is None
    cache.put("k", _answer())
    assert cache.get("k") == _answer()
    assert len(cache) == 1


def test_a_hit_is_a_copy_so_callers_cannot_change_the_stored_answer() -> None:  # CACHE-4
    cache = AnswerCache(max_entries=4)
    cache.put("k", _answer())
    first = cache.get("k")
    assert first is not None
    first.text = "changed"
    first.sources.append(Source(url="https://example.test/other"))
    first.sources[0].quote = "changed"
    assert cache.get("k") == _answer()


def test_the_stored_value_is_a_copy_of_what_was_put() -> None:
    cache = AnswerCache(max_entries=4)
    original = _answer()
    cache.put("k", original)
    original.text = "changed after put"
    original.sources.clear()
    assert cache.get("k") == _answer()


def test_no_key_is_never_read_or_written() -> None:  # CACHE-1
    cache = AnswerCache(max_entries=4)
    cache.put(None, _answer())
    assert cache.get(None) is None
    assert len(cache) == 0


def test_least_recently_used_entry_is_evicted_first() -> None:  # CACHE-2
    cache = AnswerCache(max_entries=2)
    cache.put("a", _answer("A 1%"))
    cache.put("b", _answer("B 2%"))
    assert cache.get("a") is not None  # "a" is now the most recently used
    cache.put("c", _answer("C 3%"))
    assert cache.get("b") is None
    assert cache.get("a") is not None
    assert cache.get("c") is not None
    assert len(cache) == 2


def test_putting_an_existing_key_replaces_it_without_growing() -> None:
    cache = AnswerCache(max_entries=2)
    cache.put("a", _answer("old 1%"))
    cache.put("a", _answer("new 2%"))
    got = cache.get("a")
    assert got is not None
    assert got.text == "new 2%"
    assert len(cache) == 1


def test_clear_empties_the_cache() -> None:  # CACHE-2: cleared by every ingest
    cache = AnswerCache(max_entries=4)
    cache.put("a", _answer())
    cache.clear()
    assert cache.get("a") is None
    assert len(cache) == 0


@pytest.mark.parametrize("size", [0, -1])
def test_the_size_must_be_positive(size: int) -> None:
    with pytest.raises(ValueError, match="max_entries"):
        AnswerCache(max_entries=size)
