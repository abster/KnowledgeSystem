# Implementation Steps

Status: draft for review. `DESIGN.md` says why, `SPEC.md` says what, this document says in which order and how each piece is verified. Section references like `SPEC 6.7.2` point into `SPEC.md`.

## Progress

A step is ticked only after its work is committed and you have approved it. A session implements the first unticked step, then ticks it (see `prompts/implement-step.md`).

- [x] Step 1: Project facts and the verification skill
- [x] Step 2: Tooling, stub submission and Docker
- [x] Step 3: Normaliser, parsing, chunking and validity intervals (pure)
- [x] Step 4: Table fact extraction and aliases (pure)
- [x] Step 5: Conflicts, fact resolution, tiers, history and templates (pure over in-memory facts)
- [x] Step 6: Question understanding, validators and confidence (pure)
- [x] Step 7: Storage, embeddings, retrieval and ingest wiring
- [x] Step 8: LLM client, prose facts and the extraction cache
- [x] Step 9: Fast-path `answer()`, cache and calibration
- [x] Step 10: Slow path, packaging and README

## 1. How every step works

There are ten steps. Each is self-contained: it can be finished, tested and evaluated without any later step, and it leaves the repository in a state where `make verify` passes and the public eval runs. A step is one reviewable change, which MAY be split into several commits inside the step as long as every commit passes `make verify`.

The per-step procedure is the `design-drift-detection` and `verification-first-change` skills, in this order:

1. **Drift check.** Read the DESIGN and SPEC sections listed for the step. State which sections were checked. If the plan would differ from them, stop and ask.
2. **Baseline, before any edit.** `make verify` and the eval, saved as `results/baseline_<sha>/`, with the environment recorded (git SHA, API key present or not, extraction cache present or not).
3. **Tests first.** Write the tests listed for the step and watch them fail for the right reason.
4. **Implement.**
5. **Verify.** `make verify` and the eval again, saved as `results/after_<sha>/`.
6. **Report.** The full `git diff`, every command that was run with its result, and a before/after table (overall, per category, consistency, conflicts, speed p95, stale-trap hits, errors, ingest seconds).
7. **Your decision.** You review the report. If a metric regressed, the report gives the questions that changed and a reason, and the commit happens only if you approve it explicitly. A failing test, lint or type check can never be approved away.
8. **Commit** with the attribution line, and the approved regression, if any, named in the message.

### Evaluations used

| Name | Command | Used for |
|---|---|---|
| **Eval A** | `python harness/run_eval.py --submission submission --eval eval/public_eval.jsonl --out results/<run>` | The gate on every step |
| **Eval S** | `python harness/run_eval.py --submission submission --snapshots harness/selftest/snapshots --eval harness/selftest/eval.jsonl --conflicts-gold harness/selftest/conflicts_gold.json --out results/<run>` | Conflicts and as-of behaviour on the small selftest data |

`--judge` (LLM judge) is **not** used in the gate, because it needs the API key and adds nondeterminism. It MAY be run once at the end for information.

### Eval expectation by step

Until the fast path exists, `answer()` is the refuse-all stub from step 2, so Eval A is expected to be **unchanged** (only the three `should_refuse` items score). That makes accidental behaviour changes visible and proves the pipeline runs without errors on the real snapshots.

| Step | Eval A expectation |
|---|---|
| 1 | Not runnable yet (no `submission/`); record the starter's 0.093 for reference |
| 2 | Record the refuse-all baseline |
| 3 to 8 | Unchanged from the baseline, `errors = 0`. From step 7 the real `ingest` runs inside the eval, so `ingest_seconds` is recorded and watched |
| 9 | Strictly higher; first real score; reported per category with the share answered on the fast path, and held-out accuracy next to the public figure |
| 10 | Strictly higher; accuracy of at least 0.80 (SPEC AC-1, lowered from 0.90 in Step 10, decision 46) with held-out close accuracy within 0.15 of it (SPEC AC-14, lowered from 0.03). 0.80 is the pass line; the work does not push toward 99% on the 40 public questions |

## 2. Test data policy

**Decision to confirm (D8).** The snapshots may not be redistributed, so the repository must not contain real page text. Therefore:

* Committed tests use **synthetic fixtures** that copy the *structure* of the real data (header shapes, footers, tier layouts, wording patterns) with invented names and values. Golden outputs are written by hand from the structure.
* A second set of tests marked `real_data` runs against `snapshots/` when it is present locally and is skipped when it is not. It checks the real-data properties (table coverage, that no real table is wrongly accepted, `dup-006`). It is part of the gate on the developer machine, not in CI.
* The extraction cache and the generated calibration questions that quote site content stay out of git.

## 3. The steps

### Step 1: Project facts and the verification skill

**Goal.** Capture non-negotiable facts once, and make the commit gate match your rules.

**Depends on.** Nothing.

**SPEC/DESIGN.** Skills section of DESIGN; SPEC 0, 1.

**Scope.**
* `CLAUDE.md` at the repo root: tech stack (Python 3.12 dev, 3.10+ compatible; uv; LanceDB; fastembed `bge-small-en-v1.5`; `anthropic` SDK; markdown-it-py; ruff, mypy `--strict`, bandit, pip-audit, pytest, hypothesis; Docker for the toolchain only); the layout in SPEC 2; the commands; and the non-negotiables:
  * `as_of` is the only source of the target date; never parse dates from question text.
  * Conflict = coexisting contradiction only; resolution order effective date, source authority, snapshot recency, unresolved; the strategy is named in `reason` and `conflict_note`.
  * Loser, stale or expired values never appear in `Answer.text`; refusals have `confident=False` and no digits beyond the question's.
  * Precision first: any doubt in extraction means no fact. The fast path is fail closed (full-coverage check).
  * Tunable numbers live only in `config.json`; thresholds come from calibration, never from hand tuning against the public questions.
  * Report public accuracy and held-out accuracy together (once the held-out set exists, from step 9). Never tune thresholds, patterns or prompts against the public questions; a gain on the public eval alone is weak evidence.
  * Never edit `harness/`, `eval/` or the snapshots; never commit snapshot text, `.env`, the extraction cache or model weights.
  * Never print or log the API key.
  * Workflow: the two skills; ask before deviating from DESIGN or SPEC.
* Update `.claude/skills/verification-first-change/SKILL.md`:
  * Add the approved-regression path (any regression in any metric stops the commit; report questions changed and a reason; commit only on your explicit approval; name the regression in the commit message; tests, lint and type failures are never approvable).
  * Require the full `git diff` and the list of commands run with results in the report.
  * Require recording the environment in each saved report and comparing only like with like.
  * Define the gate for steps that cannot change accuracy: the eval must run and be unchanged.
  * Replace "strictly higher" with "strictly higher for steps that are meant to improve it".
  * From step 9, add held-out accuracy to the report and the regression check, and flag any gain concentrated in one or two questions or a public-minus-held-out (close) gap above 0.15.
* Add `submission/cache/` and `submission/models/` to `.gitignore`.

**Out of scope.** Any code.

**Tests.** None (documentation). Check that `CLAUDE.md` is under about 150 lines and contains no secrets.

**Eval.** Not runnable. Record the starter's 0.093 for reference.

**Exit criteria.** You agree the non-negotiables list is complete and the revised skill matches the approval flow you described.

---

### Step 2: Tooling, stub submission and Docker

**Goal.** Make `make verify` and the eval gate real, with a harmless submission.

**Depends on.** Step 1.

**SPEC/DESIGN.** SPEC 2, 3, 4, 19; DESIGN Technology Choices.

**Scope.**
* `pyproject.toml` with uv, ruff, mypy `--strict`, bandit, pip-audit, pytest, pytest-cov, hypothesis, pre-commit; `uv.lock`; `Makefile` with `verify`, `eval`, `eval-selftest`, `export-requirements`, `check-key`; `.pre-commit-config.yaml`.
* `submission/solution.py` (shim) and package `kk` with: `kk.system.KnowledgeSystem` whose `ingest` records the snapshot and whose `answer` returns the safe refusal (SPEC 12.3) and `conflicts` returns `[]`; `kk.config` loading and validating `submission/config.json` (SPEC 3, unknown keys are an error); `kk.log`; `kk.llm.key` that loads `.env` and exposes whether a key exists without ever returning it for logging.
* `make check-key`: one tiny call to the extraction model to confirm key and billing work; prints only pass or fail.
* `submission/requirements.txt` exported from the lock; `NOTES.md` skeleton.
* `Dockerfile` (pinned `python:3.12-slim`) and `docker-compose.yml` with `verify` and `eval` services; no database service.

**Out of scope.** Any real logic.

**Tests.**
* The stub satisfies the `KnowledgeSystem` protocol; `answer` never raises, including before any `ingest`; refusal text has no digits beyond the question's; config validation accepts the shipped file and rejects an unknown key and a bad type.
* The key loader never includes the key in `repr`, logs or exceptions.

**Verify.** `make verify`; `docker compose run verify`; Eval A and Eval S run.

**Eval.** Record the refuse-all baseline for both.

**Exit criteria.** `make verify` passes locally and in Docker; `check-key` passes; the baseline report exists.

---

### Step 3: Normaliser, parsing, chunking and validity intervals (pure)

**Goal.** Turn a page into chunks with metadata, with no database.

**Depends on.** Step 2.

**SPEC/DESIGN.** SPEC 6.1 to 6.6, 7; DESIGN Chunking Strategy, Metadata and Effective Dates.

**Scope.**
* `kk.query.normalise` (numbers, percent, money, terms, amount constraints, model years, dates, dashes).
* `kk.ingest.parse` (markdown to blocks), `kk.ingest.chunk` (paragraph, list item, table row and parent table, footer notes, self-contained row text), `kk.ingest.dedupe` (hashing, interval extension and gap handling over an in-memory structure), `kk.ingest.meta` (effective and expiry dates, source class, authority rank).

**Out of scope.** Facts, storage, embeddings.

**Tests.**
* Golden per normaliser row in SPEC 7.1 to 7.5, including "not recognised" inputs; property tests (round trip, never raises).
* Golden chunking of synthetic pages shaped like the real ones (a rate table with footer, a layout table, a list, a press release).
* Interval tests: unchanged chunk extends; changed chunk starts a new row; a chunk that disappears and returns gets a second interval; page removal.
* `real_data` test: every real page parses without error and chunking is deterministic.

**Verify.** `make verify`; Eval A unchanged.

**Exit criteria.** The review shows the normaliser table and chunk examples match SPEC; the `real_data` report lists page and chunk counts per snapshot (a first measurement of the corpus size).

---

### Step 4: Table fact extraction and aliases (pure)

**Goal.** Deterministic, precision-first facts from tables.

**Depends on.** Step 3.

**SPEC/DESIGN.** SPEC 6.7.1, 6.7.2, 6.9; DESIGN Table Fact Extraction, Qualifiers.

**Scope.**
* `kk.ingest.tables`: layout-table detection, header lexicon, entity and qualifier parsing, value parsing, carry-down, footer date, fact emission, plausibility gates P1 to P7 with ACCEPT, ACCEPT inferred, QUARANTINE and REJECT outcomes, mapping log, snapshot diff of fact keys and the `degraded` marking.
* `kk.ingest.aliases`: alias builder and ambiguity marking.
* A coverage report command: tables with at least one fact, unmapped headers.

**Out of scope.** Prose facts, LLM verification of row labels (step 8).

**Tests.**
* Golden per header shape (about a dozen) on synthetic tables: the `Business 1st Rate Checking` row, the tiered mortgage table, the 17-month certificate, the share savings bands, the auto loan terms and model years.
* Negative goldens: the amortization table, a creditor form with an `INTEREST RATE` column, a blank-header layout table, a `Balance`-only landing table, `Annual Percentage Rate (As Low As)`.
* Gate tests: each of P1 to P7 fails for the right reason.
* `real_data`: coverage report (target: report only, no number promised); no REJECT-class table produces a fact; the first fact counts per snapshot.

**Verify.** `make verify`; Eval A unchanged.

**Exit criteria.** The coverage report and the unmapped-header list are reviewed; any header you want added to the lexicon is added with a golden test.

---

### Step 5: Conflicts, fact resolution, tiers, history and templates (pure over in-memory facts)

**Goal.** Everything that decides which value is right and how it is worded.

**Depends on.** Steps 3 and 4.

**SPEC/DESIGN.** SPEC 9.3, 9.4, 10, 12.5; DESIGN Conflict Resolution, Tier Policy, Change and History Questions, Template Catalogue.

**Scope.**
* `kk.conflicts`: detection (coexisting contradictions, same-URL exclusion, merge across snapshots), resolution (effective date, authority, recency, unresolved), the output record with topic, values, sources, resolution and reason; stable ordering.
* Sibling selection and tier selection (cap tiers, bands, the boundary band); history segments with the trust conditions HSEG-1 to HSEG-4.
* `kk.query.render`: templates (rate, fee, count, address, hours, status, list, quoted prose, history, refusal) and the qualifier phrase map.

**Out of scope.** Question parsing, retrieval, storage.

**Tests.**
* Conflict goldens: the selftest case (2.95% vs 3.50%, expired promo, resolves to 3.50% by effective date), authority tie-break, recency tie-break, unresolved, tiers and rate-vs-APY not conflicts, same-URL duplicates not conflicts.
* Topic string contains entity and term words; values and sources align.
* Sibling and tier goldens: a point amount, "over", "or less", a value on a cap, within 0.5% of a cap.
* History goldens: changed once, changed several times, unchanged, a gap, an unresolved conflict at some snapshot, more than 5 segments.
* Template goldens for every row of SPEC 12.5; same facts give the same text; no template renders with a missing slot.

**Verify.** `make verify`; Eval A unchanged; Eval S is still the stub (wiring comes in step 7).

**Exit criteria.** The reviewer reads the conflict `reason` strings and the history wording and agrees they are acceptable.

---

### Step 6: Question understanding, validators and confidence (pure)

**Goal.** Decide, without a model, whether a question is understood well enough for the fast path.

**Depends on.** Steps 3 and 5.

**SPEC/DESIGN.** SPEC 8, 12.1, 12.2, 14; DESIGN Qualifiers, Confidence Score and Thresholds, Validators.

**Scope.**
* `kk.query.asof`; `kk.query.question` (shape classifier, entity linking, measure, qualifiers with spans, change cue, residual, coverage check, uncertain flag); `kk.query.grammar` (about twelve anchored patterns including H1 and H2); canonical key.
* `kk.query.confidence`: signals, weights and caps from config, the routing function with placeholder thresholds.
* `kk.query.validators`: V1 to V5 using the scorer's own number extraction.

**Out of scope.** Retrieval, calibration, rendering.

**Tests.**
* `as_of` goldens (inclusive date, before first snapshot, datetime string, invalid value, the dup-006 example).
* Question goldens with spans and residuals; the fail-closed set: "over", "between", "about", "a year and a half", a stray year, an ambiguous alias, a date in the text with `as_of=null`.
* Grammar: at least 5 positives and 5 near-miss negatives per pattern; patterns written from shapes, not from public questions.
* Validators: the 5.39% versus 5.34% example, an ungrounded number, a refusal containing digits, a non-verbatim quote.
* Confidence: signals are in [0, 1]; each hard cap forces the slow path; unresolved conflict and degraded pair behave as specified.
* Property tests: the parser never raises and is deterministic.

**Verify.** `make verify`; Eval A unchanged.

**Exit criteria.** You review the neutral-word list, the change-cue list and the recognised modifiers (SPEC decisions 7 to 9), since they set the width of the fast path.

---

### Step 7: Storage, embeddings, retrieval and ingest wiring

**Goal.** A real index built from the real snapshots, and live `conflicts()`.

**Depends on.** Steps 3 to 6.

**SPEC/DESIGN.** SPEC 5, 6.8, 6.10 to 6.12, 9.1, 9.2; DESIGN Vector Database, Embeddings.

**Scope.**
* `kk.store`: LanceDB in process, schema creation for `chunks`, `facts`, `conflicts`, the indices, interval extension, filtered vector and BM25 search with the validity pre-filter, history queries.
* `kk.ingest.embed`: fastembed with the model loaded from `submission/models/` (weights fetched once by a documented developer command, never at runtime).
* `kk.query.retrieve`: hybrid retrieval with reciprocal rank fusion, deterministic ties; candidate fact selection (SPEC 9.2).
* `KnowledgeSystem.ingest` wired end to end for chunks and table facts; `conflicts()` returns the live records; `answer` is still the stub.

**Out of scope.** Prose facts, the answer path.

**Tests.**
* Store: schema and index creation, interval extension and reappearing chunks, the pre-filter (a chunk outside the target interval never appears), idempotent re-ingest.
* Retrieval: fusion order and tie-breaking on a small fixture; embeddings are unit length and deterministic.
* Integration on `harness/selftest`: ingest both snapshots; the selftest conflict is found and resolved to 3.50%; `as_of` filtering returns the older value in a direct store query.
* `real_data`: ingest all five real snapshots; measure ingest seconds, peak memory (memray) and row counts.

**Verify.** `make verify`; Eval A unchanged with `errors = 0` and `ingest_seconds` recorded; Eval S shows conflicts F1 and resolution accuracy for the selftest case.

**Exit criteria.** Measured ingest time and memory are within the SPEC 17 budgets, or you decide how to adjust them.

---

### Step 8: LLM client, prose facts and the extraction cache

**Goal.** Facts from prose and layout tables, cached for reuse and shipping.

**Depends on.** Step 7.

**SPEC/DESIGN.** SPEC 6.7.2 (TBL-12), 6.7.3; DESIGN Extraction cache and the Anthropic API key.

**Scope.**
* `kk.llm`: client wrapper with timeouts, retries, structured output validation, and the missing-key behaviour (SPEC 13.2 SLOW-5 for later use).
* `kk.ingest.prose`: candidate selection, prompt and schema (versioned), the validation rules (attribute in vocabulary, verbatim quote, digits in quote), cache keyed by `prompt_version`, model, text and header path; failures are not cached; concurrent calls.
* Ingest-time verification of distinct table row-label parses (quarantine on a "no").
* Cache directory `submission/cache/extraction/` (git-ignored).

**Out of scope.** Using prose facts in answers (step 9).

**Tests.**
* With a fake client: schema failure, timeout, rate limit and malformed output yield no facts and no cache entry; a non-verbatim quote and a number not in the quote are dropped; a second run makes no calls.
* No key and no cache: ingest completes, logs one warning, yields no prose facts.
* One live test (marked `live`, run manually): a handful of real chunks produce valid facts.
* `real_data`: full ingest with the key; record call counts, cache size, seconds, and the facts by origin.

**Verify.** `make verify` (live tests excluded); Eval A unchanged, `errors = 0`; rerun ingest and confirm zero new LLM calls.

**Exit criteria.** Ingest cost and time are reported; you approve the prose-fact quality on a sample of 30 facts that you read.

---

### Step 9: Fast-path `answer()`, cache and calibration

**Goal.** The first real score.

**Depends on.** Steps 6 to 8.

**SPEC/DESIGN.** SPEC 8.8, 9.2, 11, 12, 14; DESIGN Query Path, Failure Handling Matrix, Confidence Score and Thresholds.

**Scope.**
* `answer()` wired: `as_of`, parse, cache lookup (canonical key only), retrieve, candidates, siblings, history, confidence and routing, template, validators, safe refusals, error boundaries per stage. Questions routed to the slow path are refused in this step (no slow path yet).
* `kk.query.cache`.
* Calibration: the labelled set (public questions, facts-derived questions, negatives, adversarial phrasings) as fixtures; a deterministic train and held-out split; `make calibrate` chooses `tau_floor` and `tau_accept`, writes `submission/calibration_report.json` and updates `config.json`.

**Out of scope.** The slow path, polish, the LLM cross-check.

**Tests.**
* Routing end to end on the selftest data (answer, refuse, below floor, capped).
* Cache: only fully covered questions are cached; paraphrases with the same canonical key share an entry; cleared on `ingest`.
* Error boundaries: a raising retrieval or a missing table still returns an `Answer`.
* Calibration: split is deterministic and groups stay together; the thresholds satisfy the precision target on the training part; held-out precision is reported.

**Verify.** `make verify`; Eval A strictly higher; report the share answered on the fast path, the share refused, per-category scores, held-out fast-path precision, and the latency distribution.

**Exit criteria.** Held-out fast-path precision meets SPEC AC-9, or you decide how to narrow the grammar. Public and held-out accuracy are reported side by side. Every public question answered wrongly on the fast path is listed with its cause, and a fix is accepted only if it is a general rule, not wording-specific.

---

### Step 10: Slow path, packaging and README

**Goal.** Reach the accuracy target and make the folder runnable by anyone.

**Depends on.** Step 9.

**SPEC/DESIGN.** SPEC 13, 16, 17, 20, 2; DESIGN Query Path, Tier Policy.

**Scope.**
* `kk.query.slow`: payload (resolved facts, parent chunks capped at 12000 characters, conflict notes), structured output call, acceptance rules, the 15 s deadline, one retry, history view for change-cue questions, tier policy for ambiguous siblings, validators on the result, safe refusal on every failure.
* Optional flags left off: `polish`, `llm_crosscheck`. A decision on `llm_crosscheck` is made from the held-out precision measured in step 9.
* Packaging: weights in `submission/models/`, the extraction cache in `submission/cache/`, `requirements.txt` exported, `NOTES.md` (keys, how to run, the no-key limitation, known limits), and `README.md` run instructions.
* A no-key run (the key unset) against the shipped cache, to prove SPEC AC-8.

**Out of scope.** New features. The items in "Carried over from earlier steps" below are not new features: they were found in Steps 8 and 9 and belong to this step.

**Carried over from earlier steps.** Details are in the Step 8 and Step 9 entries of `PROGRESS.md` and in SPEC decisions 26 to 33.
* **Owner decision first, then work: the lexicon review.** Step 9 left Eval A at 0.093 because the fast path answers none of the 40 public questions (2 are fast-eligible, 20 have no linked entity, 18 have blocking words). The neutral-word, measure-word and change-cue lists set the width of the fast path (SPEC decision 8, reviewed at the first calibration run, which is now). Candidates: the word `balances` (the renderer's own phrase for balance bands, 726 of 1,320 generated questions blocked), spelled-out numbers (`six month`), and heading-only entities such as `mortgage` (Step 4 open item, a rule change). Do not change any list or rule on your own: report the blockers by count, propose the changes with alternatives, and let the owner choose. Take proposals from generated and synthetic questions, never from the public ones. After any change, re-run `make calibrate` and report public and held-out figures together.
* **Routing.** `tau_floor` is 0.0 (the SPEC 12.4 rule's output; the owner kept it). Step 9 refuses a question with no candidate fact directly; with a floor of 0.0 it must go to the slow path, which reads raw chunks. Once the slow path exists, re-set `tau_floor` from measured slow-path results (how often it answers correctly or invents, per score range) and ask the owner before changing it.
* **Fast-path wording.** `status` and `list` questions have no fast-path template (SPEC decision 31) and go to the slow path.
* **Slow path mechanics (from Step 8).** Sonnet 5.5 rejects a forced `tool_choice`, so use structured outputs; ship `submission/cache/extraction/` (git-ignored) inside the submission; a cold ingest at 8 workers is estimated at about 55 minutes against the 25-minute target, so measure a higher concurrency with the rate-limit-aware retries if the shipped cache could miss.
* **Evaluation.** Public accuracy stays at 0.093 until the lexicon review or the slow path widens what gets answered, so the end-to-end held-out comparison (AC-14) is first meaningful here. Update `tests/eval_baseline.json` in this step, after approval. The prose entity naming problem (one product named differently on each page, so equal facts get different keys and some conflicts are missed) is still deferred; raise it as drift if it limits the result.
* **Local-only diagnostics.** `results/` is git-ignored. The calibration run writes `results/calibration/items.jsonl` (question text, local only); regenerate it with `make calibrate`.

**Tests.**
* Fake client: accepted answer, `answerable=false`, unknown evidence id, validator failure, timeout, rate limit then success, deadline exceeded, no key.
* Tier-ambiguous and history-fallback payload contents; a loser value in the model output is rejected.
* A clean-room run: a fresh virtualenv from `requirements.txt`, offline, builds and ingests using only the submission folder.

**Verify.** `make verify`; `docker compose run eval`; Eval A strictly higher and at least 0.80 accuracy (SPEC AC-1); Eval S; the no-key run.

**Exit criteria.** All acceptance criteria in SPEC 20 (AC-1 to AC-14) are checked and reported, with any miss explained, including the public versus held-out comparison and the leakage review if public accuracy exceeds 0.95. You approve the final report.

## 4. Plan notes

### Why this order

* Steps 3 to 6 are pure functions: they can be tested without a database, a model, or a key, and each failure points at one rule.
* The database (step 7) and the LLM (step 8) are added only once the logic that feeds them is verified, so an ingest failure is never a parsing mystery.
* The answer path (step 9) comes after ingest because it is the first place where all earlier pieces interact; calibration is part of it because thresholds only mean something once the full fast path runs.
* The slow path (step 10) is last because it is a fallback; its absence makes the fast path's refusals visible, which is what calibration needs to see.

### Main risks and where they are caught

| Risk | Caught in |
|---|---|
| Table coverage lower than hoped, so many questions fall to the slow path | Step 4 coverage report; step 9 fast-path share |
| Ingest slower or larger than the budget (LLM extraction, embeddings) | Step 7 and 8 measurements |
| Fast-path precision below target because the grammar is too wide | Step 9 held-out precision |
| Thresholds overfit to 40 public questions | Step 9 train and held-out split, generated questions |
| Tier policy fits only `dup-009` and `dup-011` | Step 10 held-out paraphrases |
| fastembed, LanceDB or uv install problems on the grader or in Docker | Step 2 Docker; step 10 clean-room run |
| API key missing at grading | Step 10 no-key run, `NOTES.md` |
| Real snapshot text leaks into git | Test data policy (section 2); `.gitignore`; review of every diff |
| Approved regressions accumulate unnoticed | Each commit message names its approved regression; the final report lists them |

### Key and cost

The key is loaded from `.env` (git-ignored). It is used in step 2 (`check-key`), step 8 (extraction and its verification, cached) and step 10 (slow path). Extraction is paid once because the cache is keyed by content. A spend limit in the console is the guard against surprises. The eval gate does not use `--judge`.

### Changes to this plan

If a measurement (ingest time, table coverage, fast-path share) shows an assumption in SPEC is wrong, the change goes through the drift process: the step stops, the drift is reported, and DESIGN.md or SPEC.md is updated in the same commit as the code that needs it.

## 5. Decisions to confirm

| # | Decision | Default in this document |
|---|---|---|
| D8 | Test data policy (section 2) | Synthetic fixtures committed; real-data tests local only |
| D9 | `check-key` makes one tiny paid call | Included in step 2 |
| D10 | Questions routed to the slow path are refused until step 10 | Yes |
| D11 | Calibration lives in step 9, not its own step | Yes |
| D12 | Docker is introduced in step 2 with the tooling, not separately | Yes |
