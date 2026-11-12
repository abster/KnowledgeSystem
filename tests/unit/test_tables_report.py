"""Tests for the coverage report command (Step 4)."""

from __future__ import annotations

import json
from pathlib import Path

from kk.ingest.tables_report import build_report, load_snapshots, main
from tests.fixtures import tables as fx

PAGE = f"""\
# Example Rates

## Rate Tables

{fx.SAVINGS_TABLE}
{fx.AMORTIZATION_TABLE}
{fx.BLANK_HEADER_TABLE}
Rates are effective as of 06/02/2026.
"""


def _write_snapshot(root: Path, name: str, snapshot_id: str, taken_on: str, markdown: str) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "rates.md").write_text(markdown, encoding="utf-8")
    manifest = {
        "snapshot_id": snapshot_id,
        "taken_on": taken_on,
        "pages": [
            {
                "url": "https://www.example-cu.test/rates",
                "title": "Example Rates",
                "file": "rates.md",
                "fetched_at": f"{taken_on}T00:00:00Z",
            }
        ],
    }
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_load_snapshots_reads_manifests_in_name_order(tmp_path: Path) -> None:
    _write_snapshot(tmp_path, "02_b", "snap-b", "2026-06-01", PAGE)
    _write_snapshot(tmp_path, "01_a", "snap-a", "2026-05-01", PAGE)
    snaps = load_snapshots(tmp_path)
    assert [s.snapshot_id for s in snaps] == ["snap-a", "snap-b"]
    assert snaps[0].pages[0].markdown == PAGE


def test_build_report_counts_outcomes_facts_and_unmapped_headers(tmp_path: Path) -> None:
    _write_snapshot(tmp_path, "01_a", "snap-a", "2026-05-01", PAGE)
    report = build_report(load_snapshots(tmp_path))
    (snap,) = report["snapshots"]
    assert snap["tables"] == 3
    assert snap["layout_tables"] == 1
    assert snap["data_tables"] == 2
    assert snap["outcomes"] == {"ACCEPT": 1, "REJECT": 2}
    assert snap["rejected_by_gate"] == {"P2": 1, "TBL-1": 1}
    assert snap["tables_with_facts"] == 1
    assert snap["share_data_tables_with_facts"] == 0.5
    assert snap["facts"] == 6
    assert snap["facts_by_attribute"] == {"apy": 3, "dividend_rate": 3}
    assert snap["unmapped_headers"]["interest"] == 1
    assert report["unmapped_headers_total"]["interest"] == 1


def test_build_report_runs_the_snapshot_diff(tmp_path: Path) -> None:
    many = "\n".join(f"| Example Savings | ${i}00 - ${i}99.99 | {i}.00% |" for i in range(5, 0, -1))
    head = (
        "# Example Rates\n\n| Account Type | Balance Tier | Dividend Rate |\n| --- | --- | --- |\n"
    )
    big = f"{head}{many}\n"
    small = (
        "# Example Rates\n\n| Account Type | Balance Tier | Dividend Rate |\n| --- | --- | --- |\n"
        "| Example Savings | $100 - $199.99 | 1.00% |\n"
    )
    _write_snapshot(tmp_path, "01_a", "snap-a", "2026-05-01", big)
    _write_snapshot(tmp_path, "02_b", "snap-b", "2026-06-01", small)
    report = build_report(load_snapshots(tmp_path))
    assert report["snapshots"][0]["degraded"] == []
    assert report["snapshots"][1]["degraded"] == [["example savings", "dividend_rate"]]


def test_main_writes_the_report_file(tmp_path: Path) -> None:
    _write_snapshot(tmp_path / "snaps", "01_a", "snap-a", "2026-05-01", PAGE)
    out = tmp_path / "out" / "coverage.json"
    assert main(["--snapshots", str(tmp_path / "snaps"), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["snapshots"][0]["snapshot"] == "snap-a"


def test_main_fails_cleanly_when_there_are_no_snapshots(tmp_path: Path) -> None:
    assert main(["--snapshots", str(tmp_path), "--out", str(tmp_path / "x.json")]) == 1
