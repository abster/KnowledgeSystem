"""Local embeddings with ``bge-small-en-v1.5`` through fastembed (SPEC 6.8, EMB-1 to EMB-4).

The weights are read from ``submission/models/`` and never fetched at runtime: a missing model
is a packaging bug, reported by ``ModelMissingError`` (``make fetch-model`` is the developer
command that downloads the weights once).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import numpy as np

from kk.log import get_logger

MODEL_NAME = "BAAI/bge-small-en-v1.5"
MODEL_DIR = Path(__file__).resolve().parents[2] / "models"
DIM = 384
BATCH_SIZE = 64  # EMB-2: at most 64 texts per batch
MODEL_WINDOW_CHARS = 2000  # the model truncates at 512 tokens, about this many characters
# bge models take no prefix on documents and this instruction on queries.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

_log = get_logger("embed")


class ModelMissingError(RuntimeError):
    """The model weights are not in the submission folder (a packaging bug, EMB-1)."""


class EmbeddingError(RuntimeError):
    """A batch failed twice: the index would be incomplete (SPEC 6.11)."""


class EmbedderLike(Protocol):
    """What ingest and retrieval need from an embedding model."""

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        """Unit-length float32 rows, one per text, shape ``(len(texts), DIM)``."""
        ...

    def embed_query(self, text: str) -> np.ndarray:
        """A unit-length float32 vector for a question, shape ``(DIM,)``."""
        ...


def normalise_rows(matrix: np.ndarray) -> np.ndarray:
    """L2-normalise each row (EMB-4); an all-zero row stays zero."""
    rows = np.asarray(matrix, dtype=np.float64).reshape(-1, matrix.shape[-1])
    norms = np.linalg.norm(rows, axis=1, keepdims=True)
    safe = np.where(norms == 0.0, 1.0, norms)
    return (rows / safe).astype(np.float32)


class Embedder:
    """``bge-small-en-v1.5`` loaded from local files only."""

    def __init__(self, model_dir: Path | None = None) -> None:
        """Load the model.

        Raises:
            ModelMissingError: The weights are not under ``model_dir``.
        """
        directory = model_dir or MODEL_DIR
        hint = f"model weights not found under {directory}; run `make fetch-model` once"
        if not directory.is_dir():
            raise ModelMissingError(hint)
        os.environ.setdefault("HF_HUB_OFFLINE", "1")  # EMB-1: never touch the network
        try:
            from fastembed import TextEmbedding  # noqa: PLC0415 - heavy import, kept lazy

            self._model = TextEmbedding(MODEL_NAME, cache_dir=str(directory))
        except Exception as exc:  # noqa: BLE001 - any load failure means the package is broken
            raise ModelMissingError(f"{hint} ({type(exc).__name__})") from exc

    def _embed(self, texts: Sequence[str]) -> np.ndarray:
        return normalise_rows(np.array(list(self._model.embed(list(texts), batch_size=BATCH_SIZE))))

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        """Embed documents; the model itself works in batches of 64 (EMB-2)."""
        if not texts:
            return np.zeros((0, DIM), dtype=np.float32)
        return self._embed(texts)

    def embed_query(self, text: str) -> np.ndarray:
        """Embed a question with the query instruction (RET-1)."""
        rows = self._embed([QUERY_PREFIX + text])
        vector: np.ndarray = rows[0]
        return vector


def _batches(texts: Sequence[str], max_batch_chars: int) -> list[list[int]]:
    """Positions of ``texts`` grouped into batches, shortest first (see ``embed_in_batches``)."""
    order = sorted(range(len(texts)), key=lambda i: (len(texts[i]), i))
    batches: list[list[int]] = []
    current: list[int] = []
    for position in order:
        cost = min(len(texts[position]), MODEL_WINDOW_CHARS)  # the longest so far, as it is sorted
        if current and (len(current) >= BATCH_SIZE or (len(current) + 1) * cost > max_batch_chars):
            batches.append(current)
            current = []
        current.append(position)
    if current:
        batches.append(current)
    return batches


def embed_in_batches(
    embedder: EmbedderLike, texts: Sequence[str], *, max_batch_chars: int
) -> np.ndarray:
    """Embed ``texts``; a failed batch is retried once, then raises (SPEC 6.11).

    Texts are batched in order of length, which avoids padding short texts to the longest one
    in the batch (about 2.7 times faster, measured on the real chunks). A batch holds at most
    64 texts and at most ``max_batch_chars`` characters (each text counting at most
    ``MODEL_WINDOW_CHARS``), because the memory of a batch grows with the square of its text
    length: without the limit, the longest chunks pushed the peak of ingest to 4 GB. The rows
    come back in the order of ``texts``.

    Raises:
        EmbeddingError: A batch failed twice, so the index would be incomplete.
    """
    if not texts:
        return np.zeros((0, DIM), dtype=np.float32)
    result = np.zeros((len(texts), DIM), dtype=np.float32)
    for positions in _batches(texts, max_batch_chars):
        batch = [texts[i] for i in positions]
        try:
            rows = embedder.embed_documents(batch)
        except Exception:  # noqa: BLE001 - any model failure is retried once
            _log.warning("embedding batch failed; retrying once")
            try:
                rows = embedder.embed_documents(batch)
            except Exception as exc:  # noqa: BLE001
                raise EmbeddingError("embedding failed twice for one batch") from exc
        result[positions] = rows
    return result


def load_embedder() -> EmbedderLike:
    """Load the shipped model (``build()`` calls this once, IF-1)."""
    return Embedder()
