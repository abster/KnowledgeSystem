from __future__ import annotations

import json
from pathlib import Path

import pytest

from kk.log import get_logger, setup_logging


def test_logs_are_json_lines_in_work_dir(work_dir: Path) -> None:
    setup_logging(work_dir)
    get_logger("test").info("hello", extra={"route": "refusal"})
    lines = (work_dir / "kk.log").read_text().strip().splitlines()
    record = json.loads(lines[-1])
    assert record["msg"] == "hello" and record["route"] == "refusal"


def test_log_scrubs_api_key(work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-LEAKME-123")
    setup_logging(work_dir)
    get_logger("test").info("oops sk-ant-LEAKME-123 in text")
    assert "LEAKME" not in (work_dir / "kk.log").read_text()
