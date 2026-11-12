"""Coverage report over local snapshots: tables with facts, gate outcomes, unmapped headers.

Run as ``python -m kk.ingest.tables_report --snapshots snapshots --out results/coverage.json``.
The report holds counts and header text only; ``results/`` is git-ignored.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contract import Page
from kk.ingest.aliases import build_aliases
from kk.ingest.parse import ParseError
from kk.ingest.tables import (
    KeyFamilies,
    TableDecision,
    diff_fact_keys,
    entity_labels,
    extract_page,
    family_index,
)
from kk.types import Fact

_TOP_UNMAPPED = 150


@dataclass(frozen=True)
class Snapshot:
    """One snapshot folder: its id, its date and its pages."""

    snapshot_id: str
    taken_on: str
    pages: tuple[Page, ...]


def load_snapshots(root: Path) -> list[Snapshot]:
    """Read every ``<root>/*/manifest.json`` folder in name order."""
    snapshots = []
    for folder in sorted(p for p in root.glob("*") if (p / "manifest.json").exists()):
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        pages = tuple(
            Page(
                url=entry["url"],
                title=entry.get("title", ""),
                markdown=(folder / entry["file"]).read_text(encoding="utf-8"),
                fetched_at=entry.get("fetched_at", manifest["taken_on"]),
            )
            for entry in manifest["pages"]
        )
        snapshots.append(Snapshot(manifest["snapshot_id"], manifest["taken_on"], pages))
    return snapshots


def _count(items: Sequence[str]) -> dict[str, int]:
    return dict(sorted(Counter(items).items()))


def _snapshot_summary(
    snapshot: Snapshot, decisions: Sequence[TableDecision], skipped: int, tables: int
) -> dict[str, Any]:
    layout = sum(1 for d in decisions if d.gate == "TBL-1")
    data = len(decisions) - layout
    with_facts = sum(1 for d in decisions if d.facts)
    facts = [f for d in decisions for f in d.facts]
    unmapped = Counter(h for d in decisions for h in d.unmapped_headers)
    return {
        "snapshot": snapshot.snapshot_id,
        "pages": len(snapshot.pages),
        "skipped_pages": skipped,
        "tables": tables,
        "layout_tables": layout,
        "data_tables": data,
        "outcomes": _count([d.outcome for d in decisions]),
        "rejected_by_gate": _count([d.gate or "" for d in decisions if d.outcome == "REJECT"]),
        "quarantined_by_gate": _count(
            [d.gate or "" for d in decisions if d.outcome == "QUARANTINE"]
        ),
        "tables_with_facts": with_facts,
        "share_data_tables_with_facts": round(with_facts / data, 4) if data else 0.0,
        "facts": len(facts),
        "facts_by_attribute": _count([f.attribute for f in facts]),
        "entity_inferred_facts": sum(1 for f in facts if f.entity_inferred),
        "dropped_rows": sum(d.dropped_rows for d in decisions),
        "unmapped_headers": dict(unmapped.most_common(_TOP_UNMAPPED)),
    }


def build_report(snapshots: Sequence[Snapshot]) -> dict[str, Any]:
    """Aliases, extraction and the TBL-11 diff over the snapshots, as a JSON-ready dict."""
    summaries: list[dict[str, Any]] = []
    total_unmapped: Counter[str] = Counter()
    previous: KeyFamilies | None = None
    for snapshot in snapshots:
        aliases = build_aliases(label for page in snapshot.pages for label in entity_labels(page))
        decisions: list[TableDecision] = []
        facts: list[Fact] = []
        urls: dict[str, str] = {}
        skipped = 0
        for page in snapshot.pages:
            try:
                extraction = extract_page(page, aliases)
            except ParseError:
                skipped += 1
                continue
            decisions.extend(extraction.decisions)
            for fact in extraction.facts:
                facts.append(fact)
                urls[fact.chunk_id] = page.url
        summary = _snapshot_summary(snapshot, decisions, skipped, len(decisions))
        families = family_index(facts, urls)
        diff = diff_fact_keys(
            previous if previous is not None else {},
            families,
            frozenset(p.url for p in snapshot.pages),
        )
        summary["degraded"] = [list(pair) for pair in sorted(diff.degraded)]
        summary["anomalies"] = [list(item) for item in diff.anomalies]
        summaries.append(summary)
        total_unmapped.update(
            {h: n for h, n in Counter(h for d in decisions for h in d.unmapped_headers).items()}
        )
        previous = families
    return {
        "snapshots": summaries,
        "unmapped_headers_total": dict(total_unmapped.most_common(_TOP_UNMAPPED)),
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point; returns the exit code."""
    parser = argparse.ArgumentParser(description="Table fact coverage report.")
    parser.add_argument("--snapshots", type=Path, default=Path("snapshots"))
    parser.add_argument("--out", type=Path, default=Path("results/coverage.json"))
    args = parser.parse_args(argv)
    snapshots = load_snapshots(args.snapshots)
    if not snapshots:
        print(f"no snapshots found under {args.snapshots}", file=sys.stderr)
        return 1
    report = build_report(snapshots)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    for item in report["snapshots"]:
        print(
            f"{item['snapshot']}: data tables {item['data_tables']}, "
            f"with facts {item['tables_with_facts']}, facts {item['facts']}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
