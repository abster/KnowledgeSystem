"""Real-data check for Step 7: all five real snapshots ingest end to end.

Skipped when ``snapshots/`` or the model weights are absent. Writes counts only to ``results/``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kk.ingest.embed import MODEL_DIR
from tools.measure_ingest import load_snapshots, measure

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOTS = ROOT / "snapshots"
pytestmark = [
    pytest.mark.real_data,
    pytest.mark.skipif(not SNAPSHOTS.exists(), reason="snapshots/ not present"),
    pytest.mark.skipif(not (MODEL_DIR / "CACHEDIR.TAG").exists(), reason="model not fetched"),
]


def test_all_five_snapshots_ingest_within_the_spec_budget(
    work_dir: Path,
) -> None:
    snapshots = load_snapshots(SNAPSHOTS)
    assert len(snapshots) == 5
    report = measure(snapshots)
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "ingest_measure.json").write_text(
        json.dumps(report, indent=2, sort_keys=True)
    )
    assert report["rows"]["chunks"] > 0 and report["rows"]["facts"] > 0
    assert all(s["skipped_pages"] == 0 for s in report["snapshots"])
    assert report["build_seconds"] < 15  # SPEC 17
    assert report["ingest_seconds"] < 30 * 60  # SPEC 17 hard limit
