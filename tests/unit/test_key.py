from __future__ import annotations

import logging
import pickle
from pathlib import Path

import pytest

from kk.llm import key as keymod

SECRET = "sk-ant-test-0123456789-SECRET"


def test_no_key_reports_false(work_dir: Path, tmp_path: Path) -> None:
    assert keymod.has_key(env_file=tmp_path / "missing.env") is False
    assert keymod.load_key(env_file=tmp_path / "missing.env") is None


def test_key_from_environment(work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    assert keymod.has_key() is True
    loaded = keymod.load_key()
    assert loaded is not None and loaded.reveal() == SECRET


def test_key_from_env_file_with_quotes_and_comments(work_dir: Path, tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(f'# comment\nOTHER=1\nexport ANTHROPIC_API_KEY="{SECRET}"\n')
    loaded = keymod.load_key(env_file=env)
    assert loaded is not None and loaded.reveal() == SECRET


def test_environment_wins_over_env_file(
    work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=from-file\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    loaded = keymod.load_key(env_file=env)
    assert loaded is not None and loaded.reveal() == "from-env"


def test_key_never_in_repr_str_format_or_logs(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    loaded = keymod.load_key()
    assert loaded is not None
    for text in (repr(loaded), str(loaded), f"{loaded}", f"{loaded!r}", f"{loaded:>40}"):
        assert SECRET not in text
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("kk").warning("key=%s %r", loaded, loaded)
    assert SECRET not in caplog.text


def test_key_not_in_exceptions_or_pickle(work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    loaded = keymod.load_key()
    assert loaded is not None
    with pytest.raises(TypeError) as exc:
        pickle.dumps(loaded)
    assert SECRET not in str(exc.value)


def test_require_key_error_has_no_secret(work_dir: Path, tmp_path: Path) -> None:
    with pytest.raises(keymod.KeyUnavailableError) as exc:
        keymod.require_key(env_file=tmp_path / "missing.env")
    assert "sk-ant" not in str(exc.value)


def test_blank_key_counts_as_absent(work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    assert keymod.has_key(env_file=Path("/nonexistent/.env")) is False
