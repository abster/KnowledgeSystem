"""Developer command: run the packaged submission in a clean room (Step 10, AC-8, AC-11).

Builds a fresh virtualenv, installs only ``requirements.txt`` from the package (this is the one
step that uses the network), then runs the evaluation harness against a copy of the package
with no API key, no ``.env`` and every network connection blocked. It proves that ``build()``,
``ingest`` (with the shipped extraction cache) and ``answer`` need nothing but the folder.
Run it with ``make cleanroom``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess  # nosec B404 - dev tool runs fixed commands, no user input
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = (
    "import socket\n"
    "_connect = socket.socket.connect\n"
    "def _blocked(self, address):\n"
    "    if self.family == getattr(socket, 'AF_UNIX', None):\n"
    "        return _connect(self, address)\n"
    "    raise OSError('offline clean room: network access is blocked')\n"
    "socket.socket.connect = _blocked\n"
)


def write_offline_guard(folder: Path) -> Path:
    """A ``sitecustomize.py`` that makes every non-local socket connection fail."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "sitecustomize.py").write_text(GUARD, encoding="utf-8")
    return folder


def clean_env(base: Mapping[str, str], guard: Path, work_dir: Path) -> dict[str, str]:
    """The environment of the run: no key, no ``.env``, offline model hub, a fresh work dir."""
    env = {k: v for k, v in base.items() if k != "ANTHROPIC_API_KEY"}
    env.update(
        {
            "KK_ENV_FILE": str(work_dir / "no.env"),
            "KK_WORK_DIR": str(work_dir),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PYTHONPATH": str(guard),
        }
    )
    return env


def main(argv: list[str] | None = None) -> int:
    """Command line entry point; returns the exit code of the harness run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=ROOT / "dist" / "submission")
    parser.add_argument("--public", action="store_true", help="use the public eval, not selftest")
    args = parser.parse_args(argv)
    if not (args.package / "solution.py").is_file():
        sys.stderr.write("run `make package` first\n")
        return 1
    with tempfile.TemporaryDirectory(prefix="kk-cleanroom-") as tmp:
        root = Path(tmp)
        venv = root / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)  # noqa: S603  # nosec B603 - fixed argument list
        python = venv / ("Scripts" if os.name == "nt" else "bin") / "python"
        sys.stdout.write("installing requirements.txt into a fresh virtualenv\n")
        subprocess.run(  # noqa: S603  # nosec B603 - fixed argument list
            [
                str(python),
                "-m",
                "pip",
                "install",
                "-q",
                "-r",
                str(args.package / "requirements.txt"),
            ],
            check=True,
        )
        copy = root / "submission"
        shutil.copytree(args.package, copy)
        guard = write_offline_guard(root / "guard")
        env = clean_env(os.environ, guard, root / "work")
        command = [str(python), str(ROOT / "harness" / "run_eval.py"), "--submission", str(copy)]
        if args.public:
            command += ["--eval", str(ROOT / "eval" / "public_eval.jsonl")]
        else:
            selftest = ROOT / "harness" / "selftest"
            command += [
                "--snapshots",
                str(selftest / "snapshots"),
                "--eval",
                str(selftest / "eval.jsonl"),
                "--conflicts-gold",
                str(selftest / "conflicts_gold.json"),
            ]
        command += ["--out", str(root / "out")]
        sys.stdout.write("running the harness offline, with no API key\n")
        return subprocess.run(command, env=env, check=False).returncode  # noqa: S603  # nosec B603 - fixed argument list


if __name__ == "__main__":
    raise SystemExit(main())
