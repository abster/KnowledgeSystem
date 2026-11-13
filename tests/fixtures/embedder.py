"""A deterministic stand-in for the embedding model: hashed bag of words, unit length."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

import numpy as np

DIM = 384
_WORD = re.compile(r"[a-z0-9.%$]+")


class FakeEmbedder:
    """Texts that share words get a higher cosine similarity. Counts what it was asked to embed."""

    def __init__(self) -> None:
        """Start with no recorded calls."""
        self.document_texts: list[str] = []
        self.query_texts: list[str] = []

    @staticmethod
    def _vector(text: str) -> np.ndarray:
        vec = np.zeros(DIM, dtype=np.float32)
        for word in _WORD.findall(text.lower()):
            slot = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big") % DIM
            vec[slot] += 1.0
        norm = float(np.linalg.norm(vec))
        if norm == 0.0:
            vec[0] = 1.0
            return vec
        return vec / norm

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        """One unit vector per text."""
        self.document_texts.extend(texts)
        return np.array([self._vector(t) for t in texts], dtype=np.float32).reshape(-1, DIM)

    def embed_query(self, text: str) -> np.ndarray:
        """One unit vector for a question."""
        self.query_texts.append(text)
        return self._vector(text)
