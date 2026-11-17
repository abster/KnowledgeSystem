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

Run everything from the top folder of the kit (the one that contains `harness/`, `eval/` and this
folder). In these commands this folder is called `submission`; use its real name if you renamed it.

1. Install the packages (Python 3.10 or newer). From the kit's top folder:
   `pip install -r submission/requirements.txt`
   (from inside this folder it is `pip install -r requirements.txt`). Nothing else is needed: the
   embedding weights are in `models/` and the extraction cache is in `cache/extraction/`.
2. Score it with the harness from the kit's top folder:
   `python harness/run_eval.py --submission submission --eval eval/public_eval.jsonl --out results/run`
   The harness prints one line per snapshot while it ingests, then nothing while it answers the
   questions, and the score at the end. With the key set, expect roughly 5 to 10 minutes in total
   (see Timing). A quiet terminal after the last `ingest` line is normal. Keep the machine awake
   (on macOS, prefix the command with `caffeinate -i`): a sleeping laptop inflates the measured
   latency.
3. Working files (the LanceDB directory, logs) go under `KK_WORK_DIR`; the default is a fresh
   temporary directory per `build()`, removed when the system is closed (`close()`), dropped or the
   process ends. A folder you set is never removed. Snapshot folders are never written to.
4. Environment overrides: `KK_WORK_DIR`, `KK_ENV_FILE` (path of the `.env` file),
   `KK_EXTRACT_CACHE_DIR`.

To rebuild this folder from the repository: `make package` writes `dist/submission/` and
`dist/submission.zip` (code, weights and extraction cache, without bytecode or secrets).

**From a clone of the repository** (no `models/`, no `cache/`): nothing to prepare. The first
build downloads the embedding weights once (about 64 MB; `KK_FETCH_MODEL=0` disables this) and the
first ingest builds the extraction cache (needs the key, tens of minutes, see Timing) and the vector
cache (8 to 10 minutes); later runs reuse all three. Run the harness from the kit folder with
`--submission <path to clone>/submission`. The full steps are in the repository `README.md`.

**Using the pre-built files (shortcut).** From the clone's top folder:

```bash
unzip -o submission.zip "submission/cache/*" "submission/models/*"   # weights, extraction cache, vector cache
unzip -o extraction_cache.zip                                        # or: the extraction cache only
```

`submission.zip` has everything, so the first ingest takes about a minute and nothing is downloaded.
`extraction_cache.zip` holds only the extraction cache (the prose facts, which is what needs the API
key and the most time); the weights are then downloaded once and the vectors are rebuilt (8 to 10
minutes). Neither zip contains a key or any snapshot page.

## Timing

* `build()` loads the embedding model (a few seconds).
* Ingest of all five snapshots with the shipped cache and the embedding cache: about a minute;
  without the vector cache about 8 to 10 minutes. With no extraction cache and a key the first
  ingest takes much longer (tens of minutes at 8 workers); the cache removes that cost.
* Fast path: the decision stage takes about 0.13 s (median), but real wording rarely passes its
  coverage check, so on the public questions every answer takes the slow path: median about 7 s,
  p95 8 to 10.5 s, slowest 10 to 12 s over five runs. The slow-path deadline is 15 s (one retry)
  and the speed score reaches zero at 20 s. Latency was traded for accuracy on purpose. Two evaluation
  runs reported answers of 1,003 s and 115 s; the power log shows the laptop asleep during both, and
  the harness times with wall-clock time, so keep the machine awake while evaluating. The 15 s
  deadline wraps the slow-model call only; the query rewrite and rerank calls before it have their
  own 6 s limits, so a whole answer can pass 15 s if those are slow. Treat the deadline as a target.

## How the slow path reads the site

* It searches the question and up to three rewritten queries (one short Haiku call) and collapses
  chunks with identical text before ranking (closing paragraphs copied onto many pages would
  otherwise hide the answer). A second short Haiku call then picks the passages most likely to
  hold the answer from the 150 best candidates. The model reads the top 60 chunks and gets each table (all rows, header, footer date) or each prose hit
  with its neighbours under the same heading. Link targets are kept in prose (`text (url)`).
* News and blog pages are labelled with their publication date. The model is told, as its first
  rule, to use the value in the evidence with the latest effective or publication date when the
  evidence disagrees (a branch count that changed over time, for example), and the payload lists
  the newest dated evidence. The newest page's copy of a sentence that changes between pages is
  also kept ahead of older copies when the payload is cut to its size limit. This is an
  instruction to a model, so it holds most of the time but not always.
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
