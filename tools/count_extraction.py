"""Developer tool: count the extraction-model calls a full ingest would make (no API calls).

Chunks and parses every real snapshot, applies the same candidate rules and cache keys as
ingest, subtracts what the on-disk extraction cache already holds, and prints counts with a
rough cost estimate. It never prints page text and never uses the API key::

    PYTHONPATH=submission:harness uv run python -m tools.count_extraction
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from kk.config import load_config
from kk.ingest.extract_cache import ExtractionCache, cache_key, default_cache_dir
from kk.ingest.pipeline import IngestState, _collect_labels, _PageResults, _read_pages
from kk.ingest.prompts import extract as prompts
from kk.ingest.prose import header_path, is_candidate, user_message
from kk.ingest.rowcheck import user_message as row_message
from kk.ingest.tables import RowParse
from tools.measure_ingest import ROOT, load_snapshots

# Haiku 5.5 list prices per million tokens for prompts up to 100K tokens (claude-api skill).
INPUT_USD_PER_MTOK = 0.10
OUTPUT_USD_PER_MTOK = 0.50
CHARS_PER_TOKEN = 3.0  # conservative: the newer tokenizer yields more tokens per character
PROSE_OUTPUT_TOKENS = 200
VERIFY_OUTPUT_TOKENS = 30


def _tokens(chars: int) -> int:
    return round(chars / CHARS_PER_TOKEN)


def count(root: Path) -> dict[str, Any]:
    """Distinct calls an ingest of ``root`` would make, with and without the current cache."""
    config = load_config()
    model = config.llm.extract_model
    cache = ExtractionCache(default_cache_dir())
    state = IngestState()
    prose: dict[str, str] = {}
    rows: dict[str, RowParse] = {}
    per_snapshot = []
    for snapshot in load_snapshots(root):
        pages = list(snapshot.pages)
        state.labels |= _collect_labels(pages)
        results = _PageResults()
        _read_pages(pages, state.labels, results)
        candidates = [c for c in results.chunks if is_candidate(c)]
        for chunk in candidates:
            key = cache_key(prompts.EXTRACT_VERSION, model, chunk.text, header_path(chunk))
            prose.setdefault(key, user_message(chunk))
        for row in results.rows:
            key = cache_key(prompts.VERIFY_VERSION, model, row_message(row.parse), "")
            rows.setdefault(key, row.parse)
        per_snapshot.append(
            {
                "snapshot": snapshot.snapshot_id,
                "candidate_chunks": len(candidates),
                "row_parses": len(results.rows),
            }
        )
    pending_prose = {k: v for k, v in prose.items() if cache.get(k) is None}
    pending_rows = {k: p for k, p in rows.items() if cache.get(k) is None}
    in_chars = sum(len(prompts.EXTRACT_SYSTEM) + len(v) for v in pending_prose.values())
    in_chars += sum(len(prompts.VERIFY_SYSTEM) + len(row_message(p)) for p in pending_rows.values())
    tokens_in = _tokens(in_chars)
    tokens_out = PROSE_OUTPUT_TOKENS * len(pending_prose) + VERIFY_OUTPUT_TOKENS * len(pending_rows)
    usd = tokens_in / 1e6 * INPUT_USD_PER_MTOK + tokens_out / 1e6 * OUTPUT_USD_PER_MTOK
    return {
        "model": model,
        "extract_version": prompts.EXTRACT_VERSION,
        "verify_version": prompts.VERIFY_VERSION,
        "per_snapshot": per_snapshot,
        "distinct_prose_calls": len(prose),
        "distinct_verify_calls": len(rows),
        "already_cached": len(prose) - len(pending_prose) + len(rows) - len(pending_rows),
        "calls_to_make": len(pending_prose) + len(pending_rows),
        "estimated_input_tokens": tokens_in,
        "estimated_output_tokens": tokens_out,
        "estimated_cost_usd": round(usd, 2),
        "concurrency": config.llm.extract_concurrency,
    }


def main() -> None:
    """Print the counts as JSON."""
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "snapshots"
    print(json.dumps(count(root), indent=2))  # noqa: T201


if __name__ == "__main__":
    main()
