# Progress

## Step 1: Project facts and the verification skill
* Commit: d74455d
* Eval A: not runnable (no `submission/`). Starter reference run: `qa_score` 0.093, errors 0 (`results/baseline_03ac00b/`, API key present, no extraction cache).
* Eval S: not runnable.
* Approved regressions: none.
* Deviations: none. `design-drift-detection` left untouched by decision; `submission/models/` was already covered by the `models/` pattern in `.gitignore`, now listed explicitly.
* Open items for Step 2: `CLAUDE.md` lists the Makefile commands as "arriving in Step 2" and mentions `kk.llm.key`; update the wording once they exist.
