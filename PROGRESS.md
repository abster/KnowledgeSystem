# Progress

## Step 1: Project facts and the verification skill
* Commit: d74455d
* Eval A: not runnable (no `submission/`). Starter reference run: `qa_score` 0.093, errors 0 (`results/baseline_03ac00b/`, API key present, no extraction cache).
* Eval S: not runnable.
* Approved regressions: none.
* Deviations: none. `design-drift-detection` left untouched by decision; `submission/models/` was already covered by the `models/` pattern in `.gitignore`, now listed explicitly.
* Open items for Step 2: `CLAUDE.md` lists the Makefile commands as "arriving in Step 2" and mentions `kk.llm.key`; update the wording once they exist.

## Step 2: Tooling, stub submission and Docker
* Commit: 19abea9
* Eval A: `qa_score` 0.093 (refuse-all stub; only `should_refuse` scores, 1.00), errors 0, stale-trap hits 0, consistency 1.0, p95 0.0 s (`results/after_bb2b516/`). Same in Docker.
* Eval S: `qa_score` 0.2, conflicts F1 0.0, final score 0.17, errors 0 (`results/selftest_after_bb2b516/`).
* Environment: API key present, no extraction cache. `tests/eval_baseline.json` now holds the Eval A baseline for the gate.
* Approved regressions: none.
* Deviations: the eval gate reads a committed `tests/eval_baseline.json` (approved). Added `.dockerignore` (not in the step text). Dependencies limited to `anthropic` and `pydantic` (approved); `lancedb`, `fastembed`, `markdown-it-py` arrive with their steps. `.env` is parsed with stdlib, no `python-dotenv` (approved).
* Open items for Step 3: add `markdown-it-py` and re-run `make export-requirements`. `pip-audit` and the Docker build need network. The DESIGN's pre-baked model cache in the Docker image waits for Step 7. Update `tests/eval_baseline.json` only in a step that changes accuracy.

## Step 3: Normaliser, parsing, chunking and validity intervals (pure)
* Commit: 5c9a12a
* Eval A: `qa_score` 0.093, errors 0, unchanged and per-question identical (`results/baseline_ec01dbe/`, `results/after_ec01dbe/`).
* Eval S: `qa_score` 0.2, final 0.17, conflicts F1 0.0, unchanged (`results/selftest_*_ec01dbe/`).
* Environment: API key present, no extraction cache. `make verify` passes: 249 tests, coverage 97.85%.
* Real data (`results/real_data_step3.json`): 3,294 pages parsed, none skipped; chunks per snapshot 27.9k, 28.4k, 28.3k, 29.5k, 31.3k; 51,120 interval rows in total; tables 182 to 265 per snapshot, of which 21 to 90 are layout tables.
* Approved regressions: none.
* Deviations (approved with the report): `is_layout_table` (TBL-1) lives in `chunk.py` because chunking needs it; footer notes (CH-4) stop at the first non-paragraph block; `chunk_page` takes no `max_tokens` (CH-3 windowing is `window_parent`); `Chunk` has extra fields `row_index`, `row_count`, `footer_count`, `fetched_at`, `position`; heuristic constants (80-character header line, 40-character cue window, 50% for "most" cells) are named constants, not in `config.json`; duplicate chunks within a page are collapsed.
* Open items for Step 4: reuse `is_layout_table`; apply TBL-2 header normalisation (footnote digits are still in `TableBlock.header`); review the `other` source-class URLs in the report against META-5.
* Open items for Step 7: an extended row keeps a stale `parent_id` and `effective_date` when only the table footer changes; `make verify` takes about 100 s because of the `real_data` tests under coverage.
