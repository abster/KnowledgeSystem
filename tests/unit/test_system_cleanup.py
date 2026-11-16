"""The default work folder is temporary and is removed; a configured one is never touched."""

from __future__ import annotations

import gc
import tempfile
from pathlib import Path

import pytest

from kk.system import KnowledgeSystem
from tests.fixtures.embedder import FakeEmbedder


@pytest.fixture
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make ``tempfile`` create folders under ``tmp_path`` and read no real key or cache."""
    root = tmp_path / "tmp-root"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    monkeypatch.delenv("KK_WORK_DIR", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("KK_ENV_FILE", str(tmp_path / "no.env"))
    monkeypatch.setenv("KK_EXTRACT_CACHE_DIR", str(tmp_path / "extraction"))
    return root


def _folders(root: Path) -> list[Path]:
    return [p for p in root.iterdir() if p.name.startswith("kk-")]


def test_a_new_system_uses_a_temporary_folder_of_its_own(temp_root: Path) -> None:
    ks = KnowledgeSystem(embedder=FakeEmbedder())
    assert ks.work_dir.parent == temp_root
    assert ks.work_dir.exists()
    ks.close()


def test_close_removes_the_temporary_folder_and_may_be_repeated(temp_root: Path) -> None:
    ks = KnowledgeSystem(embedder=FakeEmbedder())
    ks.close()
    assert _folders(temp_root) == []
    ks.close()  # a second call does nothing


def test_a_system_that_is_dropped_leaves_no_folder_behind(temp_root: Path) -> None:
    ks = KnowledgeSystem(embedder=FakeEmbedder())
    assert len(_folders(temp_root)) == 1
    del ks
    gc.collect()
    assert _folders(temp_root) == []


def test_a_configured_work_folder_is_never_removed(
    temp_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = tmp_path / "mine"
    monkeypatch.setenv("KK_WORK_DIR", str(mine))
    ks = KnowledgeSystem(embedder=FakeEmbedder())
    ks.close()
    del ks
    gc.collect()
    assert mine.exists()
    assert _folders(temp_root) == []
