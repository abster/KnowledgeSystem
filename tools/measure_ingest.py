"""Developer tool: ingest the real snapshots and write counts and timings (never page text).

Run it under memray for the peak memory of ingest::

    PYTHONPATH=submission:harness uv run memray run -o results/ingest.bin tools/measure_ingest.py
    uv run memray stats results/ingest.bin
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from contract import Page, Snapshot
from kk.system import KnowledgeSystem

ROOT = Path(__file__).resolve().parents[1]


def load_snapshots(root: Path) -> list[Snapshot]:
    """The snapshots under ``root`` in ordinal order, in the harness' own format."""
    snaps = []
    for folder in sorted(p for p in root.glob("*") if (p / "manifest.json").exists()):
        manifest = json.loads((folder / "manifest.json").read_text())
        pages = [
            Page(
                url=e["url"],
                title=e["title"],
                markdown=(folder / e["file"]).read_text(),
                fetched_at=e.get("fetched_at", manifest["taken_on"]),
            )
            for e in manifest["pages"]
        ]
        snaps.append(Snapshot(manifest["snapshot_id"], manifest["taken_on"], pages))
    return snaps


def measure(snapshots: list[Snapshot]) -> dict[str, Any]:
    """Ingest all snapshots with the real model; return counts, seconds and conflict counts."""
    started = time.perf_counter()
    ks = KnowledgeSystem()
    build_seconds = time.perf_counter() - started
    per_snapshot = []
    for snap in snapshots:
        begin = time.perf_counter()
        ks.ingest(snap)
        summary = ks.last_summary
        if summary is None:
            raise RuntimeError("ingest produced no summary")
        per_snapshot.append(
            {
                "snapshot": snap.snapshot_id,
                "seconds": round(time.perf_counter() - begin, 1),
                "pages": summary.pages,
                "skipped_pages": summary.skipped_pages,
                "chunks_new": summary.chunks_new,
                "chunks_extended": summary.chunks_extended,
                "tables_accepted": summary.tables_accepted,
                "tables_inferred": summary.tables_inferred,
                "tables_quarantined": summary.tables_quarantined,
                "tables_rejected": summary.tables_rejected,
                "facts_by_origin": summary.facts_by_origin,
                "embed_seconds": summary.embed_seconds,
            }
        )
    records = ks.store.conflict_records()
    return {
        "build_seconds": round(build_seconds, 1),
        "ingest_seconds": round(time.perf_counter() - started - build_seconds, 1),
        "rows": {t: ks.store.row_count(t) for t in ("chunks", "facts", "conflicts")},
        "conflicts_by_strategy": {
            s: sum(r.strategy == s for r in records) for s in {r.strategy for r in records}
        },
        "degraded_pairs": len(ks.degraded),
        "snapshots": per_snapshot,
    }


def main() -> int:
    """Write ``results/ingest_measure.json`` for the snapshots under ``snapshots/``."""
    with tempfile.TemporaryDirectory(prefix="kk-measure-") as work:
        import os  # noqa: PLC0415

        os.environ["KK_WORK_DIR"] = work
        report = measure(load_snapshots(ROOT / "snapshots"))
    out = ROOT / "results" / "ingest_measure.json"
    out.write_text(json.dumps(report, indent=2, sort_keys=True))
    sys.stdout.write(f"wrote {out.name}: {report['ingest_seconds']} s, rows {report['rows']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
