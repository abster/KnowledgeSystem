# Knowledge System: project facts

Read this first in every session. `SPEC.md` says what to build, `DESIGN.md` says why,
`IMPLEMENTATION_STEPS.md` says in what order. Progress is the checklist at the top of
`IMPLEMENTATION_STEPS.md`; history is in `PROGRESS.md`. One step per session
(`prompts/implement-step.md`).

## Tech stack

* Python 3.12 for development; the submission stays compatible with 3.10+.
* `uv` for environments and the lock file.
* LanceDB (embedded, no database service) for chunks, facts and conflicts.
* fastembed with `bge-small-en-v1.5` (ONNX weights bundled in the submission, offline).
* `anthropic` SDK for extraction (Haiku class) and the slow path (Sonnet class).
* `markdown-it-py` for parsing pages.
* Tooling: ruff, mypy `--strict`, bandit, pip-audit, pytest, pytest-cov, hypothesis, pre-commit.
* Docker is for the toolchain only (`verify`, `eval`); the submission runs without it.

## Layout (SPEC 2)

* `submission/` is the hand-in: `solution.py` (shim exposing `build()`), `config.json`,
  `requirements.txt`, `NOTES.md`, `kk/` (the package), `models/` and `cache/` (shipped, not in git).
* `tests/` holds unit, golden, property, integration tests and synthetic fixtures.
* `harness/`, `eval/`, `snapshots/` are provided and read-only. `starter/` is the reference baseline.
* `results/` holds saved eval runs (git-ignored).

## Commands (the Makefile arrives in Step 2)

* `make verify`: lint, format check, types, security scan, tests with coverage, package build, eval gate.
* `make eval`, `make eval-selftest`, `make check-key`, `make export-requirements`.
* Eval A: `python harness/run_eval.py --submission submission --eval eval/public_eval.jsonl --out results/<run>`
* Eval S: same with `--snapshots harness/selftest/snapshots --eval harness/selftest/eval.jsonl --conflicts-gold harness/selftest/conflicts_gold.json`
* `--judge` is not part of the gate.
* Reference: the starter scores 0.093 on Eval A.

## Non-negotiables

* `as_of` is the only source of the target date. Never parse dates from question text.
* A conflict is a coexisting contradiction only. Resolution order: effective date, source
  authority, snapshot recency, unresolved. The strategy is named in `reason` and `conflict_note`.
* Loser, stale or expired values never appear in `Answer.text`. Refusals have `confident=False`
  and no digits beyond the question's.
* Precision first: any doubt in extraction means no fact. The fast path fails closed
  (full-coverage check).
* Tunable numbers live only in `config.json`. Thresholds come from calibration, never from
  hand tuning against the public questions.
* Report public accuracy and held-out accuracy together (from Step 9, once the held-out set
  exists). Never tune thresholds, patterns or prompts against the public questions; a gain on
  the public eval alone is weak evidence.
* Never edit `harness/`, `eval/` or the snapshots. Never commit snapshot text, `.env`, the
  extraction cache or model weights. Committed tests use synthetic fixtures only.
* Never print or log the API key. It is read only through `kk.llm.key`; test only that it
  exists or works.

## Workflow

* Use the two project skills for every change: `design-drift-detection` and
  `verification-first-change`.
* Ask before deviating from DESIGN.md or SPEC.md; report drift with a proposed next step and
  alternatives, and let the owner choose.
* Commit only after explicit approval. Commit identity: `Abhijeet Kumar
  <abhijeet.kumar61087@gmail.com>`; end messages with the Co-Authored-By line.
