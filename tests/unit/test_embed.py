"""Tests for the local embedding model (SPEC 6.8, EMB-1 to EMB-4)."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from kk.ingest.embed import (
    DIM,
    MODEL_DIR,
    Embedder,
    ModelMissingError,
    embed_in_batches,
    normalise_rows,
)
from tests.fixtures.embedder import FakeEmbedder

HAVE_MODEL = (MODEL_DIR / "CACHEDIR.TAG").exists()
needs_model = pytest.mark.skipif(
    not HAVE_MODEL, reason="model weights not fetched (make fetch-model)"
)


def test_missing_weights_raise_a_clear_error_without_touching_the_network(tmp_path: Path) -> None:
    with pytest.raises(ModelMissingError, match="make fetch-model"):
        Embedder(model_dir=tmp_path / "nothing-here")


def test_rows_are_normalised_to_unit_length_and_zero_rows_survive() -> None:
    rows = normalise_rows(np.array([[3.0, 4.0], [0.0, 0.0]], dtype=np.float64))
    assert rows.dtype == np.float32
    assert np.allclose(rows[0], [0.6, 0.8])
    assert np.allclose(rows[1], [0.0, 0.0])


@pytest.fixture(scope="module")
def embedder() -> Embedder:
    return Embedder()


@needs_model
def test_documents_are_384_dimensional_unit_vectors(embedder: Embedder) -> None:
    vectors = embedder.embed_documents(["6 month certificate APY 3.50%", "auto loan rates"])
    assert vectors.shape == (2, DIM) and vectors.dtype == np.float32
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)


@needs_model
def test_embedding_is_deterministic_and_batch_size_independent(embedder: Embedder) -> None:
    texts = [f"certificate number {i} pays 3.{i}% APY" for i in range(70)]  # more than one batch
    one = embedder.embed_documents(texts)
    assert np.array_equal(one, embedder.embed_documents(texts))
    assert np.allclose(one[0], embedder.embed_documents(texts[:1])[0], atol=1e-5)


@needs_model
def test_query_uses_the_query_prefix_and_ranks_the_right_document(embedder: Embedder) -> None:
    docs = embedder.embed_documents(
        ["Auto loan rates for new vehicles", "Six month certificate dividend rates"]
    )
    query = embedder.embed_query("What is the rate on a 6 month certificate?")
    assert query.shape == (DIM,)
    assert abs(float(np.linalg.norm(query)) - 1.0) < 1e-5
    assert int(np.argmax(docs @ query)) == 1
    assert not np.allclose(
        query, embedder.embed_documents(["What is the rate on a 6 month certificate?"])[0]
    )


@needs_model
def test_loading_the_model_is_fast_enough_for_build() -> None:
    start = time.perf_counter()
    Embedder()
    assert time.perf_counter() - start < 15  # SPEC 17: build() at most 15 s


class _Recording(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.batches: list[list[str]] = []

    def embed_documents(self, texts):  # type: ignore[no-untyped-def]
        self.batches.append(list(texts))
        return super().embed_documents(texts)


def test_batches_are_length_sorted_but_rows_come_back_in_input_order() -> None:
    texts = [("word " * n).strip() for n in (30, 1, 200, 5, 90, 2, 60)] * 20  # 140 texts
    recorder = _Recording()
    result = embed_in_batches(recorder, texts, max_batch_chars=10**9)
    expected = FakeEmbedder().embed_documents(texts)
    assert result.shape == (140, DIM)
    assert np.array_equal(result, expected)  # row i is the embedding of texts[i]
    assert [len(b) for b in recorder.batches] == [64, 64, 12]  # EMB-2
    flat = [t for b in recorder.batches for t in b]
    assert [len(t) for t in flat] == sorted(len(t) for t in texts)


def test_no_texts_gives_an_empty_matrix() -> None:
    assert embed_in_batches(FakeEmbedder(), [], max_batch_chars=8000).shape == (0, DIM)


def test_long_texts_get_smaller_batches_so_memory_stays_flat() -> None:
    """Attention memory grows with length squared, so a batch is capped by total characters."""
    recorder = _Recording()
    texts = ["x" * 1000 + str(i) for i in range(40)] + ["short " + str(i) for i in range(70)]
    result = embed_in_batches(recorder, texts, max_batch_chars=4000)
    assert np.array_equal(result, FakeEmbedder().embed_documents(texts))
    for batch in recorder.batches:
        assert len(batch) <= 64
        assert len(batch) * max(len(t) for t in batch) <= 4000 or len(batch) == 1
    long_batches = [b for b in recorder.batches if len(b[0]) > 1000]
    assert long_batches and max(len(b) for b in long_batches) <= 3
    assert max(len(b) for b in recorder.batches) == 64  # short texts still fill a batch


def test_a_text_longer_than_the_model_window_counts_as_the_window() -> None:
    """The model truncates at 512 tokens, so 6,000 characters cost no more than 2,000."""
    recorder = _Recording()
    texts = ["y" * 6000 + str(i) for i in range(8)]
    embed_in_batches(recorder, texts, max_batch_chars=8000)
    assert [len(b) for b in recorder.batches] == [4, 4]


def test_one_oversized_text_is_still_embedded_alone() -> None:
    recorder = _Recording()
    embed_in_batches(recorder, ["z" * 500], max_batch_chars=100)
    assert [len(b) for b in recorder.batches] == [1]


def test_link_targets_are_not_embedded() -> None:  # decision 37: they help answers, not matching
    from kk.ingest.embed import embed_in_batches, strip_link_targets  # noqa: PLC0415
    from tests.fixtures.embedder import FakeEmbedder  # noqa: PLC0415

    text = (
        "Open a savings account (https://join.example.test/p?x=1)"
        " or see rates (/personal/rates) (APY)"
    )
    assert strip_link_targets(text) == "Open a savings account or see rates (APY)"
    assert strip_link_targets("No links here (APY)") == "No links here (APY)"
    embedder = FakeEmbedder()
    embed_in_batches(embedder, [text], max_batch_chars=8000)
    assert embedder.document_texts == ["Open a savings account or see rates (APY)"]
