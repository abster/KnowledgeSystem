"""On-disk cache of document vectors (SPEC 6.8, EMB-5).

Embedding the corpus takes about 8 minutes on a laptop CPU, so vectors are stored in a small
SQLite file keyed by a fingerprint of the model files and the exact text. A different model
(or a different text) can never reuse a vector. The cache is an optimisation only: if the
file cannot be opened, read or written, the cache switches itself off with one warning and the
model embeds everything, as before.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np

from kk.ingest.embed import DIM, EmbedderLike
from kk.log import get_logger

_FILE = "vectors.sqlite"
_FLOAT_BYTES = 4
_log = get_logger("embed_cache")


def default_cache_dir() -> Path:
    """``KK_EMBED_CACHE_DIR`` if set, else ``submission/cache/embeddings`` (git-ignored)."""
    configured = os.environ.get("KK_EMBED_CACHE_DIR")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "cache" / "embeddings"


def model_fingerprint(model_dir: Path) -> str:
    """Hash of the model's ONNX and tokenizer files, so a changed model changes every key."""
    digest = hashlib.sha256()
    files = sorted(
        p for p in (model_dir / "models--Qdrant--bge-small-en-v1.5-onnx-Q").rglob("*")
        if p.is_file() and p.suffix in {".onnx", ".json"} and "snapshots" in p.parts
    )  # fmt: skip
    for path in files:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class EmbeddingCache:
    """A SQLite table ``vectors(key, vector)``; every failure turns the cache off."""

    def __init__(self, directory: Path, *, fingerprint: str) -> None:
        """Open (or create) the cache file under ``directory``."""
        self._fingerprint = fingerprint
        self._db: sqlite3.Connection | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(directory / _FILE)
            db.execute(
                "CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector BLOB NOT NULL)"
                " WITHOUT ROWID"
            )
            db.commit()
            self._db = db
        except (OSError, sqlite3.Error):
            _log.warning("embedding cache unavailable; embedding without it")

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self._fingerprint}\x00{text}".encode()).hexdigest()

    def _disable(self) -> None:
        _log.warning("embedding cache failed; embedding without it")
        self._db = None

    def get_many(self, texts: Sequence[str]) -> dict[str, np.ndarray]:
        """The cached vectors of ``texts`` (misses and malformed entries are left out)."""
        found: dict[str, np.ndarray] = {}
        if self._db is None or not texts:
            return found
        try:
            for text in dict.fromkeys(texts):
                row = self._db.execute(
                    "SELECT vector FROM vectors WHERE key = ?", (self._key(text),)
                ).fetchone()
                if row is not None and len(row[0]) == DIM * _FLOAT_BYTES:  # else damaged: a miss
                    found[text] = np.frombuffer(row[0], dtype=np.float32).copy()
        except sqlite3.Error:
            self._disable()
            return {}
        return found

    def put_many(self, vectors: Mapping[str, np.ndarray]) -> None:
        """Store vectors (replacing any entry under the same key)."""
        if self._db is None or not vectors:
            return
        rows = [(self._key(t), v.astype(np.float32).tobytes()) for t, v in vectors.items()]
        try:
            self._db.executemany("INSERT OR REPLACE INTO vectors VALUES (?, ?)", rows)
            self._db.commit()
        except sqlite3.Error:
            self._disable()


class CachedEmbedder:
    """An ``EmbedderLike`` that serves document vectors from the cache. Queries are not cached."""

    def __init__(self, inner: EmbedderLike, cache: EmbeddingCache) -> None:
        """Wrap ``inner`` with ``cache``."""
        self._inner = inner
        self._cache = cache

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        """Cached rows for known texts; the model embeds only the rest, once per distinct text."""
        if not texts:
            return np.zeros((0, DIM), dtype=np.float32)
        known = self._cache.get_many(texts)
        missing = [t for t in dict.fromkeys(texts) if t not in known]
        if missing:
            fresh = self._inner.embed_documents(missing)  # a failure propagates, nothing cached
            computed = dict(zip(missing, fresh, strict=True))
            self._cache.put_many(computed)
            known.update(computed)
        return np.array([known[t] for t in texts], dtype=np.float32).reshape(-1, DIM)

    def embed_query(self, text: str) -> np.ndarray:
        """Questions go straight to the model."""
        return self._inner.embed_query(text)
