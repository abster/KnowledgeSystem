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


CACHE_DIR = ROOT / "submission" / "cache" / "extraction"
MIN_CACHED_RESULTS = 1000


def test_the_shipped_extraction_cache_covers_every_call_so_no_key_is_needed(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 8: with the cache and no API key, ingest needs no call and loses no prose fact."""
    db = CACHE_DIR / "extraction.sqlite"
    if not db.exists() or db.stat().st_size < MIN_CACHED_RESULTS * 100:
        pytest.skip("extraction cache not generated (run tools/measure_ingest.py with a key)")
    monkeypatch.setenv("KK_EXTRACT_CACHE_DIR", str(CACHE_DIR))  # work_dir hides the key
    report = measure(load_snapshots(SNAPSHOTS))
    (ROOT / "results" / "ingest_measure_cached.json").write_text(
        json.dumps(report, indent=2, sort_keys=True)
    )
    for snap in report["snapshots"]:
        assert snap["prose"]["calls"] == 0 and snap["prose"]["failures"] == 0
        assert snap["prose"]["skipped_no_key"] == 0, "a chunk has no cached extraction"
        assert snap["verify"]["calls"] == 0 and snap["verify"]["failures"] == 0
        assert snap["verify"]["skipped_no_key"] == 0, "a row-label parse has no cached verdict"
        assert snap["prose"]["facts"] > 0
    assert report["snapshots"][-1]["facts_by_origin"].get("prose_llm", 0) > 0
