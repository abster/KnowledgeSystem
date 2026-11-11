---
name: verification-first-change
description: Verification-first workflow for every code change in this repository. Measure the public eval baseline before editing, make the change, rerun, assert accuracy improved (for steps meant to improve it) or is unchanged (for everything else), show the results and get the user's approval before committing. Use before writing or modifying any code, tests, build configuration, Docker files or documentation, and before any git commit. Supersedes the user-level code-change-practices skill in this repository.
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
   Record the environment in every saved report (`env.txt` next to `report.json`): git SHA,
   API key present or not (never the key itself), extraction cache present or not. Compare
   only runs with the same environment; if it differs, rerun the baseline.
3. **Write tests first** when the change is code: unit tests (and golden tests for extraction
   rules, property tests for parsers) that describe the intended behaviour. Run them and
   confirm they fail for the expected reason, not because of an import error.
4. **Make the change.**
5. **Verify.** Run `make verify` and the eval again. Save the report as
   `results/after_<short-sha>/`.
6. **Assert** before committing:
   * For a step **meant to improve** results: `qa_score` is **strictly higher** than the
     baseline, **no category** drops by more than 0.02, `stale_trap_hits` does not increase,
     `errors` is 0, `speed.p95_s` is not worse than the baseline by more than 0.5 s and stays
     within 2 s on the fast path, and `conflicts.f1` and `consistency.score` do not fall.
     From step 9, held-out accuracy is reported next to the public figure and is part of the
     regression check. Flag any gain concentrated in one or two questions, and any
     public-minus-held-out gap above 0.03.
   * For a step that **cannot change accuracy** (refactor, documentation, tooling, or a
     pure-logic step before the fast path exists): the eval must run with `errors` = 0 and
     every metric must be **unchanged**, and `make verify` must pass.
   * **Any regression in any metric stops the commit.** Report the questions that changed and
     your best explanation. A regression may be committed only on the user's explicit
     approval, and the commit message must name it. Failing tests, lint, type checks or
     security scans are never approvable.
   * If an assertion fails and approval is not given: do not commit. Diagnose, fix or revert,
     and report what happened. Do not loosen the thresholds to make it pass.
7. **Report and ask.** The report contains: the full `git diff`; every command run with its
   result; a before/after table (overall, per category, consistency, speed p95, conflicts,
   stale-trap hits, errors, ingest seconds, and from step 9 held-out accuracy); the questions
   that changed (fixed and newly broken); the environment of both runs; and which exit
   criteria are met. Ask for explicit approval with AskUserQuestion. If the change needs
   files or behaviour beyond what was planned, ask again.
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
