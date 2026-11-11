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
