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

## Step 4: Table fact extraction and aliases (pure)
* Commit: 27e08cd
* Eval A: `qa_score` 0.093, errors 0, unchanged and per-question identical (`results/baseline_263af7d/`, `results/after_263af7d_step4/`).
* Eval S: `qa_score` 0.2, final 0.17, conflicts F1 0.0, unchanged (`results/selftest_*_263af7d*/`).
* Environment: API key present, no extraction cache. `make verify` passes: 322 tests, coverage 97.4%, about 4 minutes.
* Real data (`results/coverage_step4.json`): latest snapshot has 233 data tables, 55 with facts (23.6%), 786 facts; snapshots 01 to 04 have 15 to 17 such tables. No REJECT or QUARANTINE table gives a fact. The P7 quarantine in snapshots 01 to 03 is a real table with dividend rate above APY.
* Approved regressions: none.
* Deviations (approved with the report): the Step 3 test `test_text_without_digits_is_never_a_number` now filters characters whose NFKC form has a digit; a dash or `None` in a qualifier column means no tier; TBL-2 also strips a footnote digit after `)`; `chunk.carry_down` and `table_row_text` are public; `make coverage-report` added.
* Open items for the lexicon review: heading-only entities rarely resolve (103 P2 rejects, 18 TBL-10 tables: certificates, `Term | Rate | Points | APR`), so consider using the entity-column header as the entity for qualifier-only labels (a rule change, raise as drift); unmapped `apr (as low as)`, `annual percentage rate (apr)`, `card type`, and the `% APR` value suffix; bold-line headings are not used.
* Open items for Step 5: facts have no `fact_id` or interval ordinals yet (Step 7); `degraded` pairs come from `diff_fact_keys` and must reach the answer path.

## Step 5: Conflicts, fact resolution, tiers, history and templates (pure)
* Commit: 7ff5b5e
* Eval A: `qa_score` 0.093, errors 0, unchanged and per-question identical (`results/baseline_16d02b2/`, `results/after_16d02b2_step5c/`).
* Eval S: `qa_score` 0.2, final 0.17, conflicts F1 0.0, unchanged (`results/selftest_*_16d02b2*/`).
* Environment: API key present, no extraction cache. `make verify` passes: 519 tests, coverage 97%, about 4 minutes.
* Approved regressions: none.
* Decisions made with the owner (SPEC and DESIGN updated in the same commit): all-expired or all-not-yet-in-force values are unresolved; resolution order is current values only, latest effective date among dated values, authority (table over prose), then snapshot recency, with a shared top rank going to recency; the reference date is the snapshot `taken_on`; tier selection is `one` only for a certain single-tier match, `several` (slow path) for a boundary or several tiers, `none` (fast-path refusal) otherwise; a none-valued history winner is untrusted (slow path); "as low as" is part of the attribute label.
* Deviations (approved with the report): `FactVersion` pairs a `Fact` with its chunk `Interval` (the stored fact row carries the ordinals in Step 7); history segments live in `kk/query/candidates.py` (SPEC 2.1 names no module); `parse_tier_text` added to `normalise.py` and `is_not_yet_in_force` to `meta.py`; `meta.is_expired` now includes the snapshot date itself; the 3-loser and 60-character reason caps are named constants, not config values; the 0.5% boundary band applies to cap tiers only; history counts use words ("twice") so no ungrounded digit appears; the topic shows "360-month (30-year)" for whole years.
* Open items for Step 6: question parsing supplies the `AmountConstraint` and the other qualifiers that `select_siblings` takes, and the status template's `yes_when_past` and phrases; hard cap 5 now means `several` only, and a fully covered `none` selection is a refusal (SPEC 12.1, 12.2); `degraded` pairs from `diff_fact_keys` still have to reach `HistoryContext`.
* Open items for Step 7: persist the interval ordinals on the facts table and build `FactVersion` from the facts-to-chunks join; the selftest conflict (2.95% against 3.50%) is plain text, so it needs Step 8 prose facts, and the Step 7 check "the selftest conflict is found and resolved" cannot pass before Step 8.

## Step 6: Question understanding, validators and confidence (pure)
* Commit: f09190f
* Eval A: `qa_score` 0.093, errors 0, unchanged and per-question identical (`results/baseline_c4c70e4/`, `results/after_c4c70e4_step6/`).
* Eval S: `qa_score` 0.2, final 0.17, conflicts F1 0.0, unchanged (`results/selftest_*_c4c70e4*/`).
* Environment: API key present, no extraction cache. `make verify` passes: 995 tests, coverage 97.59%, about 4.5 minutes. One run failed in `pip-audit` on a network reset; the rerun passed.
* Real data (`results/step6_questions.json`, counts only, nothing tuned): of the 40 public questions, 1 is fully covered and 1 is fast-eligible at parse time. Main blockers: entity words that do not link (`branch`, `certificate`, `lobby`, `mortgage`, which need Step 7 and Step 8 data) and shape `other` (6). The brand word was the largest single blocker before it was added to the neutral list.
* Approved regressions: none.
* Decisions made with the owner: `about` removed from the COV-3 neutral words (COV-4 requires it to fail); the brand word `dupaco` and `dupaco's` added to them; the scorer's number regex is copied into `kk.query.validators`, with a dev-only test that compares it to `harness/scoring.py`; alias plurals left as an open item; the `refusal_text` fix approved.
* Deviations (approved with the report): SPEC COV-1 clarified (the word `term` after a term, and a matched pattern's cue words, are consumed); `parse(question, linker, target_ord=None)` takes an `EntityLinker` for the alias index; `ParsedQuestion` has an `aggregate` field for hard cap 4; model years are stored as one canonical form (`2024-2027`); `loan`, `account` and `card` words and the brand word are glue between slots; questions over 1,000 characters fail closed; no `personal`/`business` qualifier is extracted because Step 4 facts carry none; H1 pairs `has`/`have` with the participle and `did` with the base form; 11 patterns (prose, hours and address follow the measure in `wh_be`); `normalise` gained `find_amount_constraints` and `find_model_years`; `render.refusal_text` (Step 5) names the entity label only if its numbers, as the scorer reads them, are also in the question. Measure words and product words are my choice (see the exit review).
* Open items for the lexicon review: `new` is both a neutral word and a change cue; the neutral list, change-cue list, measure words and product words set the width of the fast path and are reviewed again at the Step 9 calibration.
* Open items for Step 7: `aliases._toggle_plural` makes `branchs` from `branch` and `saving` from `savings`, so such words never link; entity linking needs prose entities from Step 8 to cover branch, lobby and similar questions; the BM25 upper bound for the similarity signal is a parameter of `similarity_signal` until retrieval exists.
* Open items for Step 9: set hard cap 9 (`history_untrusted`) also when a history question has no measure, and when the HSEG-3 preconditions (unique entity, measure and sibling selection) fail; `cap_inputs_from_parse` supplies only the caps that come from the parse.
