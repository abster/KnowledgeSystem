"""Tests for the on-disk embedding cache (SPEC 6.8, EMB-5)."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from kk.ingest.embed import DIM, MODEL_DIR, Embedder, load_embedder
from kk.ingest.embed_cache import CachedEmbedder, EmbeddingCache, default_cache_dir
from tests.fixtures.embedder import FakeEmbedder


class _Spy(FakeEmbedder):
    """A fake model that records what it was asked to embed, and can be made to fail."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[str]] = []
        self.fail = False

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        if self.fail:
            raise RuntimeError("model hiccup")
        self.calls.append(list(texts))
        return super().embed_documents(texts)


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "embeddings"


def test_first_call_embeds_and_second_call_is_served_from_the_cache(cache_dir: Path) -> None:
    spy = _Spy()
    cached = CachedEmbedder(spy, EmbeddingCache(cache_dir, fingerprint="model-a"))
    texts = ["6 month certificate", "auto loan rate"]
    first = cached.embed_documents(texts)
    second = cached.embed_documents(texts)
    assert spy.calls == [texts]  # the second call never reached the model
    assert np.array_equal(first, second)
    assert first.shape == (2, DIM) and first.dtype == np.float32


def test_only_the_missing_texts_are_embedded_and_order_is_kept(cache_dir: Path) -> None:
    spy = _Spy()
    cached = CachedEmbedder(spy, EmbeddingCache(cache_dir, fingerprint="model-a"))
    cached.embed_documents(["b", "d"])
    spy.calls.clear()
    result = cached.embed_documents(["a", "b", "c", "d"])
    assert spy.calls == [["a", "c"]]
    assert np.array_equal(result, FakeEmbedder().embed_documents(["a", "b", "c", "d"]))


def test_duplicate_texts_in_one_call_are_embedded_once(cache_dir: Path) -> None:
    spy = _Spy()
    cached = CachedEmbedder(spy, EmbeddingCache(cache_dir, fingerprint="model-a"))
    result = cached.embed_documents(["x", "y", "x"])
    assert spy.calls == [["x", "y"]]
    assert np.array_equal(result[0], result[2])


def test_the_cache_survives_a_new_process_object(cache_dir: Path) -> None:
    CachedEmbedder(_Spy(), EmbeddingCache(cache_dir, fingerprint="model-a")).embed_documents(["a"])
    spy = _Spy()
    CachedEmbedder(spy, EmbeddingCache(cache_dir, fingerprint="model-a")).embed_documents(["a"])
    assert spy.calls == []


def test_a_different_model_fingerprint_never_reuses_vectors(cache_dir: Path) -> None:
    CachedEmbedder(_Spy(), EmbeddingCache(cache_dir, fingerprint="model-a")).embed_documents(["a"])
    spy = _Spy()
    CachedEmbedder(spy, EmbeddingCache(cache_dir, fingerprint="model-b")).embed_documents(["a"])
    assert spy.calls == [["a"]]


def test_a_failing_model_caches_nothing_and_the_error_propagates(cache_dir: Path) -> None:
    spy = _Spy()
    spy.fail = True
    cached = CachedEmbedder(spy, EmbeddingCache(cache_dir, fingerprint="model-a"))
    with pytest.raises(RuntimeError):
        cached.embed_documents(["a"])
    spy.fail = False
    cached.embed_documents(["a"])
    assert spy.calls == [["a"]]  # not served from a poisoned entry


def test_queries_are_never_cached(cache_dir: Path) -> None:
    spy = _Spy()
    cached = CachedEmbedder(spy, EmbeddingCache(cache_dir, fingerprint="model-a"))
    cached.embed_query("what is the rate")
    cached.embed_query("what is the rate")
    assert spy.query_texts == ["what is the rate"] * 2


def test_an_unreadable_cache_file_is_ignored_not_fatal(cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True)
    (cache_dir / "vectors.sqlite").write_bytes(b"this is not a database")
    spy = _Spy()
    cached = CachedEmbedder(spy, EmbeddingCache(cache_dir, fingerprint="model-a"))
    result = cached.embed_documents(["a"])
    assert spy.calls == [["a"]] and result.shape == (1, DIM)


def test_an_unwritable_location_is_ignored_not_fatal(tmp_path: Path) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    spy = _Spy()
    cached = CachedEmbedder(spy, EmbeddingCache(blocker / "inside", fingerprint="model-a"))
    assert cached.embed_documents(["a"]).shape == (1, DIM)
    assert cached.embed_documents(["a"]).shape == (1, DIM)
    assert spy.calls == [["a"], ["a"]]  # nothing could be stored, so nothing was reused


def test_a_stored_vector_of_the_wrong_size_is_treated_as_a_miss(cache_dir: Path) -> None:
    cache = EmbeddingCache(cache_dir, fingerprint="model-a")
    CachedEmbedder(_Spy(), cache).embed_documents(["a"])
    with sqlite3.connect(cache_dir / "vectors.sqlite") as db:
        db.execute("UPDATE vectors SET vector = ?", (b"\x00" * 10,))
    spy = _Spy()
    result = CachedEmbedder(spy, EmbeddingCache(cache_dir, fingerprint="model-a")).embed_documents(
        ["a"]
    )
    assert spy.calls == [["a"]]
    assert np.array_equal(result, FakeEmbedder().embed_documents(["a"]))


def test_no_texts_gives_an_empty_matrix(cache_dir: Path) -> None:
    cached = CachedEmbedder(_Spy(), EmbeddingCache(cache_dir, fingerprint="model-a"))
    assert cached.embed_documents([]).shape == (0, DIM)


def test_the_default_directory_can_be_overridden_by_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KK_EMBED_CACHE_DIR", str(tmp_path / "elsewhere"))
    assert default_cache_dir() == tmp_path / "elsewhere"
    monkeypatch.delenv("KK_EMBED_CACHE_DIR")
    assert default_cache_dir().parts[-2:] == ("cache", "embeddings")


@pytest.mark.skipif(not (MODEL_DIR / "CACHEDIR.TAG").exists(), reason="model not fetched")
def test_the_real_model_is_cached_and_fingerprinted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KK_EMBED_CACHE_DIR", str(tmp_path / "real"))
    first = load_embedder()
    one = first.embed_documents(["6 month certificate APY"])
    calls: list[int] = []
    original = Embedder.embed_documents

    def counting(self: Embedder, texts: Sequence[str]) -> np.ndarray:
        calls.append(len(texts))
        return original(self, texts)

    monkeypatch.setattr(Embedder, "embed_documents", counting)
    two = load_embedder().embed_documents(["6 month certificate APY"])
    assert calls == [] and np.array_equal(one, two)
