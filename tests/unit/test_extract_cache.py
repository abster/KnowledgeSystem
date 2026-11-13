"""The extraction cache: keys, persistence and failing open (PRO-5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kk.ingest.extract_cache import ExtractionCache, cache_key, default_cache_dir


def test_key_depends_on_every_part() -> None:
    base = cache_key("v1", "model", "text", "A > B")
    assert base == cache_key("v1", "model", "text", "A > B")
    assert len({base, cache_key("v2", "model", "text", "A > B"),
                cache_key("v1", "other", "text", "A > B"),
                cache_key("v1", "model", "text2", "A > B"),
                cache_key("v1", "model", "text", "A > C")}) == 5  # fmt: skip


def test_key_does_not_confuse_where_one_part_ends() -> None:
    assert cache_key("v", "m", "ab", "c") != cache_key("v", "m", "a", "bc")


def test_round_trip_and_persistence(tmp_path: Path) -> None:
    cache = ExtractionCache(tmp_path / "x")
    assert cache.get("k") is None
    cache.put("k", '{"facts": []}')
    cache.put("k2", "v2")
    assert cache.get("k") == '{"facts": []}'
    assert len(cache) == 2
    assert ExtractionCache(tmp_path / "x").get("k2") == "v2"


def test_put_replaces(tmp_path: Path) -> None:
    cache = ExtractionCache(tmp_path)
    cache.put("k", "old")
    cache.put("k", "new")
    assert cache.get("k") == "new"
    assert len(cache) == 1


def test_an_unopenable_directory_disables_the_cache_without_raising(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    cache = ExtractionCache(blocker / "sub")
    assert not cache.enabled
    cache.put("k", "v")
    assert cache.get("k") is None
    assert len(cache) == 0


def test_default_directory_follows_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KK_EXTRACT_CACHE_DIR", str(tmp_path / "e"))
    assert default_cache_dir() == tmp_path / "e"
    monkeypatch.delenv("KK_EXTRACT_CACHE_DIR")
    assert default_cache_dir().parts[-3:] == ("submission", "cache", "extraction")
