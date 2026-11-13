"""On-disk cache of LLM extraction results (SPEC 6.7.3 PRO-5, TBL-12).

A small SQLite file keyed by ``sha256(prompt_version + model + text + header_path)``. The cache
is an optimisation and a shipping vehicle (SPEC 6.7.3, DESIGN "Extraction cache"): if the file
cannot be opened, read or written, it switches itself off with one warning and ingest goes on.
Only successful results are stored; the caller never stores a failure.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

from kk.log import get_logger

_FILE = "extraction.sqlite"
_log = get_logger("extract_cache")


def default_cache_dir() -> Path:
    """``KK_EXTRACT_CACHE_DIR`` if set, else ``submission/cache/extraction`` (git-ignored)."""
    configured = os.environ.get("KK_EXTRACT_CACHE_DIR")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "cache" / "extraction"


def cache_key(version: str, model: str, text: str, header_path: str) -> str:
    """``sha256(prompt_version + model + text + header_path)`` as hex (PRO-5).

    The parts are joined with a NUL so that moving a character from one part to the next
    gives another key.
    """
    joined = "\x00".join((version, model, text, header_path))
    return hashlib.sha256(joined.encode()).hexdigest()


class ExtractionCache:
    """SQLite table ``entries(key, value)``; any failure turns the cache off with one warning."""

    def __init__(self, directory: Path) -> None:
        """Open (or create) the cache file under ``directory``."""
        self._db: sqlite3.Connection | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(directory / _FILE, check_same_thread=False)
            db.execute(
                "CREATE TABLE IF NOT EXISTS entries (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                " WITHOUT ROWID"
            )
            db.commit()
            self._db = db
        except (OSError, sqlite3.Error):
            _log.warning("extraction cache unavailable; continuing without it")

    @property
    def enabled(self) -> bool:
        """False once the file could not be opened, read or written."""
        return self._db is not None

    def _disable(self) -> None:
        _log.warning("extraction cache failed; continuing without it")
        self._db = None

    def get(self, key: str) -> str | None:
        """The stored value, or ``None`` for a miss."""
        if self._db is None:
            return None
        try:
            row = self._db.execute("SELECT value FROM entries WHERE key = ?", (key,)).fetchone()
        except sqlite3.Error:
            self._disable()
            return None
        return None if row is None else str(row[0])

    def put(self, key: str, value: str) -> None:
        """Store ``value`` under ``key`` (replacing an entry)."""
        if self._db is None:
            return
        try:
            self._db.execute("INSERT OR REPLACE INTO entries VALUES (?, ?)", (key, value))
            self._db.commit()
        except sqlite3.Error:
            self._disable()

    def __len__(self) -> int:
        """Number of stored entries (0 when disabled)."""
        if self._db is None:
            return 0
        try:
            return int(self._db.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
        except sqlite3.Error:
            self._disable()
            return 0
