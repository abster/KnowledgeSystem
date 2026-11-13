"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point KK_WORK_DIR at a temporary directory and clear any API key."""
    monkeypatch.setenv("KK_WORK_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("KK_ENV_FILE", str(tmp_path / "no.env"))  # never read the real .env
    monkeypatch.setenv("KK_EXTRACT_CACHE_DIR", str(tmp_path / "extraction"))
    yield tmp_path
