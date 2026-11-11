from __future__ import annotations

from pathlib import Path

import pytest

from kk.llm import check_key


def test_check_key_without_key_prints_fail_only(
    work_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = check_key.main(env_file=Path("/nonexistent/.env"))
    out = capsys.readouterr().out.strip()
    assert code == 1 and out == "check-key: FAIL (no key)"


def test_check_key_pass_and_fail_never_print_key(
    work_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRETSECRET")

    def ok(_key: str, _model: str) -> None:
        return None

    def boom(key: str, _model: str) -> None:
        raise RuntimeError(f"auth failed for {key}")

    monkeypatch.setattr(check_key, "_tiny_call", ok)
    assert check_key.main() == 0
    assert capsys.readouterr().out.strip() == "check-key: PASS"
    monkeypatch.setattr(check_key, "_tiny_call", boom)
    assert check_key.main() == 1
    out = capsys.readouterr()
    assert "SECRETSECRET" not in out.out + out.err
    assert out.out.strip() == "check-key: FAIL (RuntimeError)"
