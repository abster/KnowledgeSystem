---
name: verification-first-change
description: Verification-first workflow for every code change in this repository. Measure the public eval baseline before editing, make the change, rerun, assert accuracy improved (or did not regress for refactors), show the results and get the user's approval before committing. Use before writing or modifying any code, tests, build configuration, Docker files or documentation, and before any git commit. Supersedes the user-level code-change-practices skill in this repository.
---

# Verification-first change

Every change is judged by the public eval, not by intuition. Measure first, change second,
measure again, then ask the user before committing.

This skill supersedes the user-level `code-change-practices` skill for this repository (that
one was written for a different project). Also follow `design-drift-detection`: check the
change against DESIGN.md first.

## Commands

* Eval: `python harness/run_eval.py --submission <folder> --eval eval/public_eval.jsonl --out results/<run-name>`
  Use `--submission submission` once the solution exists. Before then, use `--submission starter`
  (the baseline score is 0.093). If the conflicts gold file is available, add `--conflicts-gold <file>`.
* Build and verify: `make verify` (lint, format check, types, security scan, tests with
  coverage, package build, eval gate). If the Makefile does not exist yet, run the tools that do
  exist and say which are missing.

Key fields in the report: `qa_score` (accuracy), per-category scores in `by_category`,
`consistency.score`, `speed.p95_s`, `conflicts.f1` and `resolution_accuracy`, `stale_trap_hits`,
`errors`, `final_score`, `ingest_seconds`.

## Steps

1. **Plan.** List the files you expect to change, the purpose, and which DESIGN.md sections
   apply. Run the drift check.
2. **Baseline, before any edit.** Run `make verify` (if available) and the eval. Save the
   report as `results/baseline_<short-sha>/` and note the numbers. If the working tree is
   already dirty, say so; do not blame the change for earlier failures.
3. **Write tests first** when the change is code: unit tests (and golden tests for extraction
   rules, property tests for parsers) that describe the intended behaviour. Run them and
   confirm they fail for the expected reason, not because of an import error.
4. **Make the change.**
5. **Verify.** Run `make verify` and the eval again. Save the report as
   `results/after_<short-sha>/`.
6. **Assert** before committing:
   * For a change intended to improve results: `qa_score` is **strictly higher** than the
     baseline, **no category** drops by more than 0.02, `stale_trap_hits` does not increase,
     `errors` is 0, `speed.p95_s` is not worse than the baseline by more than 0.5 s and stays
     within 2 s on the fast path, and `conflicts.f1` and `consistency.score` do not fall.
   * For a refactor, documentation or tooling change: every metric is **unchanged or better**
     (no regression), and `make verify` passes.
   * If an assertion fails: do not commit. Diagnose, fix or revert, and report what happened.
     Do not loosen the thresholds to make it pass.
7. **Report and ask.** Show a before/after table (overall, per category, consistency, speed
   p95, conflicts, stale-trap hits), the questions that changed (fixed and newly broken), the
   `git diff --stat` and a short description. Ask for explicit approval with AskUserQuestion.
   If the change needs files or behaviour beyond what was planned, ask again.
8. **Commit** only after approval. Use a clear message and end it with the attribution line
   Claude Code was told to use for commits (currently `Co-Authored-By: Claude Sonnet 5.5
   <noreply@anthropic.com>`). One logical change per commit.

## Guard against overfitting

The public eval has only 40 questions. An improvement on those items alone is weak evidence.

* Do not special-case a question's wording or expected answer in the code.
* When tuning thresholds, calibrate on the training split of the labelled set (public eval
  plus generated questions, negatives and paraphrases) and report a held-out split as well.
* Treat a gain concentrated in one or two questions as suspect; say so in the report.
* Never edit `eval/public_eval.jsonl` or the harness to improve a score.
