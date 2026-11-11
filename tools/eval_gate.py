"""Eval gate: run the public eval and fail if a metric regresses against the committed baseline."""

from __future__ import annotations

import json
import subprocess  # nosec B404 - dev tool runs a fixed command, no user input
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tests" / "eval_baseline.json"
OUT = ROOT / "results" / "gate"
EPS = 1e-9
P95_SLACK_S = 0.5


def compare(base: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Return a list of regressions of ``new`` against ``base`` (empty means pass)."""
    problems: list[str] = []
    if new["errors"] != 0:
        problems.append(f"errors = {new['errors']} (must be 0)")
    if new["qa_score"] < base["qa_score"] - EPS:
        problems.append(f"qa_score {new['qa_score']} < baseline {base['qa_score']}")
    if new["stale_trap_hits"] > base["stale_trap_hits"]:
        problems.append(f"stale_trap_hits {new['stale_trap_hits']} > {base['stale_trap_hits']}")
    for cat, entry in base["by_category"].items():
        got = new["by_category"].get(cat, {}).get("score", 0.0)
        if got < entry["score"] - EPS:
            problems.append(f"category {cat}: {got} < baseline {entry['score']}")
    if new["consistency"]["score"] < base["consistency"]["score"] - EPS:
        problems.append("consistency.score fell")
    if new["speed"]["p95_s"] > base["speed"]["p95_s"] + P95_SLACK_S:
        problems.append(f"speed.p95_s {new['speed']['p95_s']} worse by more than {P95_SLACK_S}s")
    return problems


def main() -> int:
    """Run Eval A into ``results/gate`` and compare with the baseline."""
    cmd = [
        sys.executable,
        str(ROOT / "harness" / "run_eval.py"),
        "--submission",
        str(ROOT / "submission"),
        "--snapshots",
        str(ROOT / "snapshots"),
        "--eval",
        str(ROOT / "eval" / "public_eval.jsonl"),
        "--out",
        str(OUT),
    ]
    result = subprocess.run(cmd, cwd=ROOT, check=False)  # noqa: S603  # nosec B603 - fixed argument list
    if result.returncode != 0:
        print("eval gate: harness failed")  # noqa: T201
        return 1
    if not BASELINE.exists():
        print(f"eval gate: FAIL (missing {BASELINE.relative_to(ROOT)})")  # noqa: T201
        return 1
    new = json.loads((OUT / "report.json").read_text())
    base = json.loads(BASELINE.read_text())
    problems = compare(base, new)
    for problem in problems:
        print(f"eval gate: REGRESSION {problem}")  # noqa: T201
    print(f"eval gate: {'FAIL' if problems else 'PASS'} (qa_score={new['qa_score']})")  # noqa: T201
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
