"""The clean-room runner's offline guard (Step 10, AC-8): no network, no key."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tools.cleanroom import clean_env, write_offline_guard

CONNECT = "import socket; socket.create_connection(('example.com', 80), timeout=2)"


def test_the_guard_blocks_every_network_connection(tmp_path: Path) -> None:
    guard = write_offline_guard(tmp_path / "guard")
    env = clean_env({"PATH": "/usr/bin"}, guard, tmp_path / "work")
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", CONNECT], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert "offline" in result.stderr


def test_the_clean_environment_has_no_key_and_is_offline(tmp_path: Path) -> None:
    env = clean_env(
        {"PATH": "x", "ANTHROPIC_API_KEY": "secret", "KK_ENV_FILE": "y"}, tmp_path, tmp_path / "w"
    )
    assert "ANTHROPIC_API_KEY" not in env
    assert env["KK_ENV_FILE"].endswith("no.env")
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["KK_WORK_DIR"] == str(tmp_path / "w")
