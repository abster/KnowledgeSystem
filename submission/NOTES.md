# Notes

A knowledge system for the Dupaco website snapshots. `solution.py` exposes `build()`; the
package is `kk/`. Behaviour is specified in `SPEC.md` and explained in `DESIGN.md` (in the
repository, not in this folder).

## Keys needed

* `ANTHROPIC_API_KEY` (environment variable, or a `.env` file at the repository root). It is used
  for two things:
  * **Slow path** (query time): questions that the fast path cannot answer with confidence go to
    one Sonnet-class call (`llm.slow_model`), 15 s deadline, one retry.
  * **Extraction** (ingest time): only for pages whose text is not in the shipped extraction
    cache. Results are cached by content, so a second ingest costs nothing.
* The key is read only through `kk.llm.key`; it is never logged.

### Without a key

* `build()`, `ingest` (using the shipped cache in `cache/extraction/`) and `answer` all complete
  without raising (AC-8). Run `make cleanroom` in the repository to prove it: fresh virtualenv,
  no key, network blocked.
* Questions that the fast path answers (a fully understood question with a confident table
  fact, or a history question that matches an anchored pattern) are still answered.
* **Every question that needs the slow path is refused** with the safe refusal ("I couldn't
  find ... Please contact Dupaco.", `confident=False`, no sources). That includes questions with
  several tiers and no amount, `status` and `list` questions, aggregate questions, and any
  question the fast path does not fully understand. One warning is logged per run.
* Pages that are not in the shipped extraction cache get no prose facts (table facts, which are
  rule-based, are unaffected).

## How to run

1. `pip install -r requirements.txt` (Python 3.10 or newer). Nothing else is needed: the
   embedding weights are in `models/` and the extraction cache is in `cache/extraction/`.
2. Score it with the harness from the kit:
   `python harness/run_eval.py --submission <this folder> --eval eval/public_eval.jsonl --out results/run`
3. Working files (the LanceDB directory, logs) go under `KK_WORK_DIR`; the default is a fresh
   temporary directory per `build()`. Snapshot folders are never written to.
4. Environment overrides: `KK_WORK_DIR`, `KK_ENV_FILE` (path of the `.env` file),
   `KK_EXTRACT_CACHE_DIR`.

To rebuild this folder from the repository: `make package` writes `dist/submission/` and
`dist/submission.zip` (code, weights and extraction cache, without bytecode or secrets).

## Timing

* `build()` loads the embedding model (a few seconds).
* Ingest of all five snapshots with the shipped cache and the embedding cache: about a minute;
  without the vector cache about 8 to 10 minutes. With no extraction cache and a key the first
  ingest takes much longer (tens of minutes at 8 workers); the cache removes that cost.
* Fast path: well under a second. Slow path: a few seconds, never more than 15 s.

## How the slow path reads the site

* It searches the question and up to three rewritten queries (one short Haiku call) and collapses
  chunks with identical text before ranking (closing paragraphs copied onto many pages would
  otherwise hide the answer). A second short Haiku call then picks the passages most likely to
  hold the answer from the 150 best candidates. The model reads the top 60 chunks and gets each table (all rows, header, footer date) or each prose hit
  with its neighbours under the same heading. Link targets are kept in prose (`text (url)`).
* News and blog pages are labelled with their publication date, and the model is told to use the
  newest page when pages disagree (a branch count that changed over time, for example).
* A number that equals a stale value is rejected only when the answer cites the evidence that
  replaced it, so a stale `0.00%` in an old table of one product does not block a correct answer
  about another. Anything unscoped or unexplained is still rejected.
* When hours or terms are listed branch by branch and the question is general, the answer gives
  what is stated, names each branch and says it can vary.
* Chunks that hold a replaced value are not shown: a fact that lost on effective date or source
  authority, or an older generation of the same rate table. Only numbers from resolved facts or
  from the cited page may appear in the answer, and no number known to be stale.
* Every answer is validated; otherwise the safe refusal is returned. Because the slow model is not
  deterministic, repeating an uncached question can change its wording.

## Known limits

* The slow path depends on a live model and an API key; the same question asked twice may be
  worded differently by it. Validated slow answers are cached for the run, so a repeat gives the
  same text. Every number in a slow answer must come from a resolved fact (or, when there are
  no facts, from the cited page text) and no value known to be stale or a conflict loser may
  appear; otherwise the answer is refused.
* Conflict detection compares numeric facts only; a contradiction between two free-text
  statements (for example different opening hours) is not reported.
* Rate tables that produce no facts (heading-only entities such as mortgage rates) are only read as
  page text, so the current tables can be missed when the question's words differ from the page's.
* Prose entity names are not canonicalised, so one product named differently on two pages can
  give two fact keys and a missed conflict.
* Calibration used questions generated from the latest snapshot plus the public questions; see
  `calibration_report.json` for numbers (no question text is shipped).
