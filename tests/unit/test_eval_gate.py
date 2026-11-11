from __future__ import annotations

import copy
from typing import Any

from tools.eval_gate import compare

BASE: dict[str, Any] = {
    "qa_score": 0.1,
    "errors": 0,
    "stale_trap_hits": 0,
    "by_category": {"a": {"score": 0.5}, "b": {"score": 0.2}},
    "consistency": {"score": 0.9},
    "speed": {"p95_s": 0.1},
}


def test_identical_report_passes() -> None:
    assert compare(BASE, copy.deepcopy(BASE)) == []


def test_each_regression_is_reported() -> None:
    worse = copy.deepcopy(BASE)
    worse["qa_score"] = 0.05
    worse["errors"] = 1
    worse["stale_trap_hits"] = 2
    worse["by_category"]["a"]["score"] = 0.4
    worse["consistency"]["score"] = 0.5
    worse["speed"]["p95_s"] = 0.7
    problems = compare(BASE, worse)
    assert len(problems) == 6


def test_improvement_passes() -> None:
    better = copy.deepcopy(BASE)
    better["qa_score"] = 0.5
    assert compare(BASE, better) == []
