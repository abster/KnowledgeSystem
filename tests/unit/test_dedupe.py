"""Tests for hashing and validity intervals (ING-7 to ING-10)."""

from __future__ import annotations

import hashlib
from datetime import date

from kk.ingest.dedupe import HEADER_SEP, IntervalStore, make_chunk_id, normalise_for_hash
from kk.types import Chunk


def _chunk(text: str, url: str = "https://example.test/p") -> Chunk:
    headers = ("Title", "Section")
    return Chunk(
        chunk_id=make_chunk_id(url, headers, text),
        url=url,
        file_name="p",
        title="Title",
        parent_headers=headers,
        chunk_type="text_paragraph",
        parent_id=None,
        text=text,
        effective_date=None,
        expiry_date=None,
        source_class="other",
        authority_rank=3,
        fetched_at="fetched",
        position=0,
    )


def test_normalise_for_hash() -> None:
    assert normalise_for_hash("  a  b\n\tc ") == "a b c"
    assert normalise_for_hash("Ａbc") == "Abc"  # NFKC, case preserved


def test_chunk_id_formula_and_whitespace_insensitivity() -> None:
    headers = ("Title", "Section")
    expected = hashlib.sha256(
        f"https://x.test/p\n{HEADER_SEP.join(headers)}\nhello world".encode()
    ).hexdigest()
    assert make_chunk_id("https://x.test/p", headers, "hello  world") == expected
    assert make_chunk_id("https://x.test/p", headers, "Hello world") != expected
    assert make_chunk_id("https://x.test/q", headers, "hello world") != expected
    assert make_chunk_id("https://x.test/p", ("Title", "Other"), "hello world") != expected


def _key(store: IntervalStore, text: str) -> list[tuple[int, int]]:
    return [(i.first_seen_ord, i.last_seen_ord) for i in store.intervals if i.chunk.text == text]


def test_unchanged_chunk_extends_and_changed_chunk_starts_a_new_row() -> None:
    store = IntervalStore()
    first = store.add_snapshot(1, "2026-05-12", [_chunk("alpha text"), _chunk("beta text")])
    assert (len(first.new), len(first.extended)) == (2, 0)
    second = store.add_snapshot(2, "2026-05-19", [_chunk("alpha text"), _chunk("gamma text")])
    assert [i.chunk.text for i in second.extended] == ["alpha text"]
    assert [i.chunk.text for i in second.new] == ["gamma text"]
    assert _key(store, "alpha text") == [(1, 2)]
    assert _key(store, "beta text") == [(1, 1)]
    assert _key(store, "gamma text") == [(2, 2)]


def test_extension_updates_dates_and_fetched_at() -> None:
    store = IntervalStore()
    store.add_snapshot(1, "2026-05-12", [_chunk("alpha text")])
    newer = _chunk("alpha text")
    newer = Chunk(**{**newer.__dict__, "fetched_at": "later"})
    store.add_snapshot(2, "2026-05-19", [newer])
    (interval,) = store.intervals
    assert interval.first_seen_date == date(2026, 5, 12)
    assert interval.last_seen_date == date(2026, 5, 19)
    assert interval.fetched_at == "later"


def test_chunk_that_disappears_and_returns_gets_a_second_interval() -> None:
    store = IntervalStore()
    store.add_snapshot(1, "2026-05-12", [_chunk("alpha text"), _chunk("beta text")])
    store.add_snapshot(2, "2026-05-19", [_chunk("alpha text")])
    store.add_snapshot(3, "2026-06-09", [_chunk("alpha text"), _chunk("beta text")])
    assert _key(store, "beta text") == [(1, 1), (3, 3)]
    assert _key(store, "alpha text") == [(1, 3)]
    keys = [(i.chunk.chunk_id, i.first_seen_ord) for i in store.intervals]
    assert len(keys) == len(set(keys))


def test_removed_page_has_no_visible_chunks_and_is_not_extended() -> None:
    store = IntervalStore()
    page_a, page_b = "https://example.test/a", "https://example.test/b"
    store.add_snapshot(1, "2026-05-12", [_chunk("alpha text", page_a), _chunk("beta text", page_b)])
    store.add_snapshot(2, "2026-05-19", [_chunk("alpha text", page_a)])
    assert {i.chunk.url for i in store.visible_at(2)} == {page_a}
    assert {i.chunk.url for i in store.visible_at(1)} == {page_a, page_b}


def test_visible_at_applies_the_validity_filter() -> None:
    store = IntervalStore()
    store.add_snapshot(1, "2026-05-12", [_chunk("alpha text"), _chunk("beta text")])
    store.add_snapshot(2, "2026-05-19", [_chunk("alpha text"), _chunk("gamma text")])
    store.add_snapshot(3, "2026-06-09", [_chunk("gamma text")])
    text = lambda ords: sorted(i.chunk.text for i in store.visible_at(ords))  # noqa: E731
    assert text(1) == ["alpha text", "beta text"]
    assert text(2) == ["alpha text", "gamma text"]
    assert text(3) == ["gamma text"]
    assert text(4) == []


def test_same_chunk_twice_in_one_snapshot_is_one_row() -> None:
    store = IntervalStore()
    delta = store.add_snapshot(1, "2026-05-12", [_chunk("alpha text"), _chunk("alpha text")])
    assert (len(delta.new), delta.repeated, len(store.intervals)) == (1, 1, 1)


def test_invalid_taken_on_gives_no_date_but_still_ingests() -> None:
    store = IntervalStore()
    store.add_snapshot(1, "garbage", [_chunk("alpha text")])
    (interval,) = store.intervals
    assert interval.first_seen_date is None and interval.last_seen_date is None
