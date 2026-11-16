# Knowledge System Specification

Status: draft for review. This document says **what** each part of the system must do, precisely enough to write tests against. `DESIGN.md` says **why** and shows the architecture; where the two disagree, that is a bug in one of them and must be raised (see the `design-drift-detection` skill). Implementation steps and the plan come in a separate document.

## 0. Conventions

* **MUST**, **MUST NOT**, **SHOULD**, **MAY** are used in their RFC 2119 sense.
* Requirements carry an ID (for example `ING-4`) so tests and commits can cite them. An ID never changes meaning; a retired requirement is marked *(retired)*.
* **Spec decision** marks a choice the design left open. All of them are collected in section 21 for review.
* "Initial value" means a starting number that is replaced by the calibration procedure in section 12. It is never tuned by hand against the public questions.
* A *snapshot ordinal* (`ord`) is the 1-based position of an `ingest` call. The *target ordinal* (`target_ord`) is the ordinal that represents the website at the question's date (section 8.1).

## 1. Scope and confirmed decisions

In scope: the `kk` Python package, the `submission/` folder that the harness imports, the configuration file, the shipped extraction cache and embedding weights, the test suite and the developer tooling. Out of scope: a web service, any live crawling, any runtime server dependency.

Decisions confirmed with the product owner while drafting this spec:

| # | Decision | Effect on the spec |
|---|---|---|
| D1 | Answer-cache key is the exact canonical key built by question extraction (shape, entity, sorted qualifiers, `target_ord`). No embedding-similarity cache. Only fully understood questions are cached. | Section 11 |
| D2 | The embedding weights are bundled in the submission folder. | Sections 2, 6.8 |
| D3 | Change and history questions that match an anchored history pattern are answered on the fast path by a `history` template built from stored fact intervals. Other change-cue questions go to the slow path with a fact-history view. | Sections 8.6, 9.4, 12.5, 13.3 |
| D4 | One chunk row per unique chunk with a validity interval, and `snapshot_recency` as a third conflict tie-break, are approved. | Sections 5, 6.4, 10 |
| D5 | An Anthropic API key exists at development time and is **not** assumed at grading. The extraction cache is generated at development time and shipped. Without a key, slow-path questions are refused and `NOTES.md` says so. | Sections 6.7, 13, 14 |
| D6 | `conflicts()` scans all snapshots, de-duplicated by topic and value set. | Section 10 |
| D7 | This spec is behavioural: interfaces, schemas, rules, algorithms in prose and tables, acceptance criteria. No code and no pseudocode. | the whole document |

## 2. Deliverable layout

```
repo root
  DESIGN.md  SPEC.md  pyproject.toml  uv.lock  Makefile  Dockerfile  docker-compose.yml
  .pre-commit-config.yaml  .claude/skills/...
  tests/                      unit, golden, property, integration, fixtures
  submission/                 THE HAND-IN: self-contained folder
    solution.py               thin shim: adds this folder to sys.path, exposes build()
    requirements.txt          exported from uv.lock
    NOTES.md                  keys needed, how to run, known limits
    config.json               all tunable values (section 3)
    models/bge-small-en-v1.5/ ONNX weights and tokenizer (kept out of git, shipped)
    cache/extraction/         LLM extraction cache (kept out of git, shipped)
    kk/                       the package (section 2.1)
```

`submission/` MUST run with `pip install -r submission/requirements.txt` and no other setup, offline, with Python 3.10 or newer. It MUST NOT write into the snapshot folders and MUST write its working data (the LanceDB directory, logs) under a path given by the `KK_WORK_DIR` environment variable, defaulting to a fresh temporary directory per `build()`.

### 2.1 Package modules

| Module | Responsibility |
|---|---|
| `kk.system` | `KnowledgeSystem` class: `ingest`, `answer`, `conflicts`; wiring and error boundaries |
| `kk.config` | Loads and validates `config.json`; read-only after load |
| `kk.types` | Internal dataclasses (`Chunk`, `Fact`, `ConflictRecord`, `ParsedQuestion`, `Candidate`) |
| `kk.ingest.parse` | Markdown to blocks |
| `kk.ingest.chunk` | Blocks to chunks; table rendering |
| `kk.ingest.dedupe` | Content hashing and validity intervals |
| `kk.ingest.meta` | Source class, authority rank, effective and expiry dates |
| `kk.ingest.tables` | Deterministic table fact extraction and plausibility gates |
| `kk.ingest.prose` | LLM prose fact extraction and its cache |
| `kk.ingest.aliases` | Product alias list |
| `kk.ingest.embed` | Local embeddings |
| `kk.store` | LanceDB access: schema, upserts, filtered search, history queries |
| `kk.conflicts` | Conflict detection and resolution |
| `kk.query.asof` | `as_of` to `target_ord` |
| `kk.query.normalise` | The one shared normaliser |
| `kk.query.question` | Shape, qualifiers, entity, change detection, coverage check |
| `kk.query.grammar` | Fast-path pattern registry |
| `kk.query.retrieve` | Hybrid retrieval |
| `kk.query.candidates` | Fact candidates, sibling check, resolution state |
| `kk.query.confidence` | Score, caps, routing |
| `kk.query.render` | Templates and refusal text |
| `kk.query.slow` | Slow-path prompt, call and parsing |
| `kk.query.validators` | The validators |
| `kk.query.cache` | Answer cache |
| `kk.llm` | Anthropic client wrapper: timeouts, retries, schema validation |
| `kk.log` | Structured logging and counters |

## 3. Configuration

All numbers that this spec calls "initial" live in `submission/config.json`, are validated on load (unknown keys are an error, so typos are caught), and are recorded with their provenance (who or what set them, on which dataset). The file is the only place thresholds and weights are defined.

| Key | Meaning | Initial value |
|---|---|---|
| `retrieval.top_k` | Chunks returned after fusion | 20 |
| `retrieval.per_index_k` | Candidates taken from each of the vector and keyword searches | 40 |
| `retrieval.rrf_k` | Reciprocal-rank-fusion constant | 60 |
| `retrieval.slow_top_k` | Chunks the slow path reads after fusing the question and its rewrites (decision 36) | 60 |
| `retrieval.slow_per_index_k` | Candidates kept from each search for the slow path, after copies of the same text are collapsed | 120 |
| `retrieval.slow_pool_k` | Rows read from each search before copies are collapsed (SRET-4) | 1500 |
| `retrieval.rerank_candidates` | Fused candidates shown to the reranker (SRET-6) | 150 |
| `retrieval.rerank_keep` | Candidates the reranker may pick | 30 |
| `confidence.weights` | similarity, margin, qualifier, entity | 0.30, 0.20, 0.30, 0.20 |
| `confidence.tau_floor` | Relevance floor | 0.30 |
| `confidence.tau_accept` | Fast-path acceptance | 0.80 |
| `confidence.bm25_bound` | Upper bound that maps a BM25 score to [0, 1] in the similarity signal (section 12.1) | 20.0 placeholder; set by calibration |
| `confidence.resolution_state` | unique, strategy 1-2, strategy 3, unresolved | 1.0, 0.9, 0.7, 0.0 |
| `tier.default_by_attribute` | Default tier selector per attribute | `entry` for all |
| `llm.slow_model` | Slow-path model | `claude-sonnet-5-5` |
| `llm.extract_model` | Ingest extraction model | `claude-haiku-5-5` |
| `llm.slow_deadline_s` | Total time allowed for one slow-path answer | 15 |
| `llm.slow_effort` | Reasoning depth of the slow call (`low`, `medium`, `high`; decision 38) | medium |
| `llm.extract_concurrency` | Parallel extraction calls at ingest | 8 |
| `flags.polish` | Light-model polish of template text | false |
| `flags.llm_crosscheck` | Query-time LLM cross-check of the question parse | false |
| `flags.ingest_llm_verify` | Ingest-time LLM verification of table row-label parses | true |
| `chunk.max_tokens` | Cap on a returned parent table | 1200 |
| `embed.max_batch_chars` | Most characters in one embedding batch (EMB-2) | 8000 |
| `cache.max_entries` | Answer-cache size | 10000 |

`tau_floor`, `tau_accept` and the weights in this table are placeholders from `DESIGN.md`'s worked examples. The calibration procedure (section 12.4) produces the real values.

## 4. External interface

The contract is fixed by `harness/contract.py` and MUST NOT be changed. `submission/solution.py` MUST define `build() -> KnowledgeSystem` and nothing else the harness depends on.

* **IF-1** `build()` MUST be cheap enough to call once and MUST load the embedding model, open the (empty) store and compile all patterns, so that the first `answer()` pays no start-up cost. It MUST NOT call the network.
* **IF-2** `ingest(snapshot)` is called once per snapshot, oldest first, and MUST be complete (all indices and the conflicts table updated) before it returns. It MUST NOT raise for a malformed page: the page is skipped, counted and logged.
* **IF-3** `answer(question, as_of)` MUST return an `Answer` in every case, including internal errors, and MUST finish well inside 60 s (target: section 17).
* **IF-4** `conflicts()` MUST return the records built by the last `ingest`, with no new analysis, in a stable order (section 10.6).
* **IF-5** `answer` and `conflicts` MAY be called before any `ingest`: `answer` then returns the safe refusal (section 12.3) and `conflicts` returns an empty list.
* **IF-6** The object MUST be safe to call from one thread. Thread safety across threads is not required (the harness is sequential).

### 4.1 Answer fields

| Field | Rule |
|---|---|
| `text` | Member-facing, produced by a template or by a validated slow-path answer. Contains only the winning value(s). MUST NOT contain a loser, stale or expired value. A refusal contains no digits beyond those in the question (section 12.3). |
| `sources` | One `Source(url, snapshot_id, quote)` per piece of winning evidence, at most 3. `snapshot_id` is the snapshot whose page was used. `quote` is a verbatim excerpt of at most 200 characters from the evidence. A refusal has none. |
| `confident` | `False` for every refusal and for every answer with an unresolved conflict; `True` otherwise. |
| `conflict_note` | Set whenever the answer touched a fact key that has a conflict record. Names the strategy and the loser value(s). The only place a loser value may appear. |

## 5. Data model

Two logical stores: the LanceDB tables below, and the shipped extraction cache (section 6.7). All tables are rebuilt from the snapshots on every run; nothing in LanceDB is shipped.

### 5.1 Table `chunks`

One row per unique chunk **interval**. Primary key is `(chunk_id, first_seen_ord)`.

| Column | Type | Rules |
|---|---|---|
| `chunk_id` | string | `sha256(url + "\n" + header_path + "\n" + normalised_text)`, hex |
| `url`, `file_name`, `title` | string | `file_name` is the last path segment of the URL |
| `parent_headers` | list of string | Page title first, then each heading from the root to the chunk |
| `chunk_type` | string | `text_paragraph`, `table_row`, `table`, `list_item` |
| `parent_id` | string, nullable | `chunk_id` of the table or section a child belongs to |
| `text` | string | The text that is embedded and searched |
| `vector` | fixed list of float32, 384 | Normalised to unit length |
| `first_seen_ord`, `last_seen_ord` | int | Not null; `first_seen_ord <= last_seen_ord` |
| `first_seen_date`, `last_seen_date` | date | `taken_on` of those snapshots |
| `effective_date`, `expiry_date` | date, nullable | Never defaulted to a snapshot date |
| `source_class` | string | Section 6.6 |
| `authority_rank` | int | Section 6.6 |
| `fetched_at` | string | `Page.fetched_at` of the most recent page that contained the chunk |

Parent rows (`table`, section) are stored in the same table with `vector` null. They are returned, not searched.

### 5.2 Table `facts`

One row per `(fact_key, value, chunk_id, first_seen_ord)`.

| Column | Type | Rules |
|---|---|---|
| `fact_id` | string | `sha256(fact_key + value + chunk_id + first_seen_ord)` |
| `entity`, `entity_label` | string | `entity` is the canonical alias id; `entity_label` is the display label saved for templates |
| `attribute` | string | From the attribute vocabulary (section 6.7.1) |
| `qualifiers` | string (JSON with sorted keys) | Normalised qualifier values (section 7) |
| `fact_key` | string | `sha256(entity + "|" + attribute + "|" + qualifiers)` |
| `value_text` | string | The value as displayed in the source (`5.39%`, `$2.50`) |
| `value_num` | decimal, nullable | Normalised; null for text values |
| `unit` | string, nullable | `percent`, `usd`, `count`, `months`, null |
| `none_value` | bool | True for an explicit "none" (a dash in a table) |
| `quote` | string | Verbatim text of the row or sentence it came from |
| `chunk_id`, `first_seen_ord`, `last_seen_ord` | | Denormalised from the chunk interval and kept in sync at ingest |
| `effective_date`, `expiry_date` | date, nullable | From the chunk or the table footer |
| `authority_rank` | int | Copied from the chunk |
| `entity_inferred` | bool | True when the entity came from a heading, not a row label |
| `origin` | string | `table_rule` or `prose_llm` |

### 5.3 Table `conflicts`

Rebuilt in full at the end of each `ingest`. Fields: `conflict_id`, `topic`, `fact_key`, `values` (list of displayed values), `sources` (list of `{url, snapshot_id, quote}`), `resolution` (displayed winning value or null), `strategy` (`effective_date`, `source_authority`, `snapshot_recency`, `unresolved`), `reason`, `ords` (snapshot ordinals where the contradiction coexisted).

### 5.4 Indices

* `chunks.vector`: flat (brute-force) search; the corpus is below 100k rows.
* `chunks.text`: full-text (BM25) index.
* Scalar indices on `chunks.chunk_id`, `facts.fact_key`, `facts.chunk_id`, `facts.attribute`.
* Every search MUST apply the validity filter `first_seen_ord <= target_ord AND last_seen_ord >= target_ord` **before** ranking, as a pre-filter.

## 6. Ingest

### 6.1 Ordering and ordinals

* **ING-1** The ordinal of a snapshot is the number of `ingest` calls so far (1-based). `taken_on` is stored with it in a `snapshots` list of `(ord, snapshot_id, taken_on)`.
* **ING-2** If `taken_on` is not later than the previous snapshot's, the snapshot is still ingested with the next ordinal, a warning is logged, and `as_of` resolution uses the ordinal order (so the latest ordinal is "newest").
* **ING-3** If `taken_on` is not a valid ISO date, the snapshot is still ingested but can never be selected by a dated `as_of`; it can be selected by `as_of=None` only if it is the latest ordinal.

### 6.2 Markdown parsing

* **ING-4** A page's markdown is parsed into ordered blocks: heading (level, text), paragraph, list (items), table (header row, alignment row, data rows), code block, thematic break. Parsing uses `markdown-it-py` with the GFM table extension.
* **ING-5** Raw HTML tags and image links are removed from text but their alt text is kept. A link in a paragraph or list item keeps its target after its text, as `text (target)`, unless the target repeats the text or is only a fragment (decision 37); headings and table cells keep the text only. Footnote markers in table headers are not removed here (section 6.7.2 does it).
* **ING-6** A page that fails to parse, is empty, or exceeds 5 MB is skipped and counted.

### 6.3 Chunking

* **CH-1** Each paragraph is a `text_paragraph` chunk, stored with its header path. A list is split into one `list_item` chunk per top-level item; a list with 3 or fewer items short enough to fit in 200 tokens is kept as one chunk.
* **CH-2** Each table produces one parent `table` row and one `table_row` child per data row. The child `text` is self-contained: `"<page title> - <nearest heading> | <header 1>: <cell> | <header 2>: <cell> ..."`. A blank row label is carried down from the row above (section 6.7.2) before rendering.
* **CH-3** The parent `table` text is the header row, all data rows and the footer notes (CH-4). If it exceeds `chunk.max_tokens`, the returned parent is the header row, the footer notes and the matched row plus its two neighbours on each side.
* **CH-4** The **footer notes** of a table are the paragraphs that follow it up to the next heading, if they contain a date or the words "effective", "rates", "APY", "APR" or "as of".
* **CH-5** A chunk shorter than 20 characters, or with no letters or digits, is dropped. Headings are not chunks; they become `parent_headers`.
* **CH-6** Chunking MUST be deterministic: the same page produces the same chunks in the same order.

### 6.4 Deduplication and validity intervals

* **ING-7** `normalised_text` is the text with Unicode NFKC normalisation, collapsed whitespace and trimmed ends. Case is preserved.
* **ING-8** For each chunk in the snapshot with ordinal `k`:
  * If a row with the same `chunk_id` and `last_seen_ord == k - 1` exists **and** the chunk's `effective_date`, `expiry_date` and `parent_id` equal those of the row, extend it: set `last_seen_ord = k`, update `last_seen_date`, `fetched_at` and the position fields (`position`, `row_index`, `row_count`, `footer_count`). No new embedding or extraction is done.
  * Otherwise create a new row with `first_seen_ord = last_seen_ord = k`. This covers a brand-new chunk, a chunk that disappeared and came back, and a chunk whose dates or parent changed while its text did not (a new interval, with a new primary key, because `first_seen_ord` differs; the vector of the earlier interval is reused, so nothing is embedded again).
  * The dates and the parent are not part of `chunk_id` but each row carries them, so they must not be hidden by an extension: a footer that gets a new "effective as of" date, or another row of the table changing (which changes the parent table's `chunk_id`), would otherwise leave a stale date or a parent that is no longer visible at the target ordinal (SPEC decision 23).
* **ING-9** A chunk not present in snapshot `k` is not extended. The page-removal rule follows: a page missing from a snapshot has no visible chunks in it.
* **ING-10** The facts of an extended chunk have their interval extended in the same step. A fact is never re-extracted for an unchanged chunk.

### 6.5 Effective and expiry dates

* **META-1** Dates are normalised to ISO (section 7.5).
* **META-2** `effective_date` is taken, in this order, from: (a) the table footer notes (CH-4) for table chunks; (b) the paragraph's own text; (c) a dated header line within three lines under the page title (press releases, newsroom). The patterns, matched case-insensitively:
  * `effective (as of|on|date:)? <date>`, `rates? (are )?(effective )?as of <date>`, `as of <date>`
  * `valid (through|until) <date>`, `offer (ends|expires|ended) <date>`, `expires? <date>`, `through <date>`, `ended <date>` (these set `expiry_date`)
* **META-3** If several dates match, the one closest after the matched cue word wins; ties go to the later date. If none match, both dates are null.
* **META-4** A chunk is *expired at ordinal k* if `expiry_date` is not null and on or before the `taken_on` of snapshot `k`. A chunk is *not yet in force at ordinal k* if `effective_date` is not null and later than that `taken_on`. The `taken_on` of the ordinal is the only reference date (it stands for `as_of`, which selects the snapshot); the wall clock is never used.

### 6.6 Source class and authority

* **META-5** `source_class` and `authority_rank` are assigned by first matching rule over the URL path and page title (case-insensitive substring, in this order). **Spec decision:** the rule list below is the initial version and is verified against a URL survey during implementation (section 21).

| Rank | Class | Matches (URL path or title) |
|---|---|---|
| 6 | `rate_sheet` | `rates`, `rate-sheet`, `disclosure`, `fee-schedule`, `fees`, `truth-in-savings` |
| 5 | `product_page` | `checking`, `savings`, `loans`, `certificates`, `cards`, `mortgage`, `membership` |
| 4 | `calculator` | `calculator`, `calc` |
| 3 | `faq_learn` | `faq`, `learn`, `resources`, `help`, `support` |
| 2 | `blog` | `blog`, `article` |
| 1 | `newsroom_promo` | `news`, `press`, `promo`, `offer`, `special`, `banner` |
| 3 | `other` | no rule matched; counted and logged |

### 6.7 Fact extraction

Everything is extracted at ingest. Nothing is extracted at query time.

#### 6.7.1 Attribute vocabulary

`rate`, `dividend_rate`, `apy`, `apr`, `min_to_open`, `min_to_earn`, `fee`, `count`, `address`, `phone`, `hours`, `status`, `eligibility`, `prose`. Table rows use the first nine that apply; prose uses any. A fact whose attribute is not in this list MUST NOT be created.

#### 6.7.2 Table rules (deterministic)

* **TBL-1** Detect GFM pipe tables. A table is a *layout table* if its header row has any blank cell or fewer than two non-blank cells, or if most data cells contain list markers, links or line breaks. Layout tables are chunked as prose and go to 6.7.3.
* **TBL-2** Normalise headers: strip footnote digits and markers (`APY1` becomes `APY`, `\*Payment Example` becomes `Payment Example`), unescape, collapse whitespace, lowercase.
* **TBL-3** Match the **whole** normalised header (no substring matching) against the lexicon:

| Header (after TBL-2) | Role | Attribute |
|---|---|---|
| program, account type, certificate, ira type, loan program | entity / row label | |
| balance, balance tier, term | qualifier column | |
| dividend rate | measure | `dividend_rate` |
| apy, annual percentage yield (apy) | measure | `apy` |
| apr, annual percentage rate | measure | `apr` |
| annual percentage rate (as low as) | measure | `apr` with `bound: as_low_as` |
| rate | measure | `rate` |
| min. to open | measure | `min_to_open` |
| min. to earn | measure | `min_to_earn` |

  A header not in this lexicon yields no facts; it is logged and counted by header text.
* **TBL-4** Entity: the entity column if present; otherwise the nearest preceding heading or bold line, accepted only if it resolves to a known alias (section 6.9). The fact is then marked `entity_inferred`.
* **TBL-5** Qualifiers are parsed from the row label and qualifier columns by the normaliser (section 7): money ranges and caps, terms to months, product words (fixed, ARM, conventional, jumbo, purchase, refinance, taken from the row label and the section heading), model-year ranges.
* **TBL-6** Values: percent, currency, integer, or the explicit *none* for a dash or "None". Trailing footnote digits are stripped (`0.10%1` becomes `0.10%`; only digits that directly follow `%` or a value and are not part of it).
* **TBL-7** A blank row label is carried down from the row above.
* **TBL-8** `effective_date` of every fact in the table is the one found in the table's footer notes (META-2a).
* **TBL-9** Emit one fact per data row per measure column. The fact key is entity, attribute and sorted qualifiers.
* **TBL-10** **Principle: precision first.** If any rule cannot decide, no fact is emitted for that cell or table. The unmapped case is logged.

**Plausibility gates.** Run on the whole table before any row becomes a fact.

| Gate | Passes when | On failure |
|---|---|---|
| P1 structure | at least 2 non-blank headers, consistent column count, at least 1 data row | REJECT |
| P2 entity anchor | an entity column from the lexicon, or a heading that resolves to a known alias | REJECT |
| P3 measure present | at least one header maps to a measure | REJECT |
| P4 value type | at least 80% of each measure column's cells parse as that attribute's type; failing rows are dropped | REJECT if the column falls below 80% |
| P5 qualifier coherence | a qualifier column holds one kind of value; money bands are ordered and do not overlap | QUARANTINE |
| P6 key uniqueness | no two rows give the same fact key different values | QUARANTINE |
| P7 cross-column invariants | `apy >= dividend_rate` and `apr >= rate` where both exist | QUARANTINE |

Outcomes (one logged decision per table): ACCEPT (facts emitted); ACCEPT inferred entity (facts emitted with a lower confidence cap, section 12.1); QUARANTINE (no facts, flagged); REJECT (no facts, table goes down the prose path). A quarantined or rejected table is a false negative: its questions go to the slow path, which reads the raw table.

* **TBL-11** *Snapshot diff.* After each ingest, the set of fact keys per `(entity, attribute)` of the new snapshot is compared with the previous snapshot. A key family that disappeared (more than 3 fact keys of one entity and attribute vanishing while their page still exists) is logged as an anomaly and the affected `(entity, attribute)` pairs are marked `degraded`. A question that resolves to a degraded pair MUST go to the slow path.
* **TBL-12** *Ingest-time verification.* When `flags.ingest_llm_verify` is true and a key is present, each distinct row-label string (not each row) is sent once to the extraction model with its parsed qualifiers, asking only whether the parse is correct. A "no" quarantines every fact from that row label. The result is cached in the extraction cache. With no key and no cached answer, the step is skipped.

#### 6.7.3 Prose and layout-table facts (LLM)

* **PRO-1** Candidate chunks: all `text_paragraph`, `list_item` and layout-table chunks that contain a digit, a currency sign or a percent sign, or contain a cue word (`hours`, `open`, `closed`, `closing`, `located`, `address`, `phone`, `fee`, `eligible`, `member`). Other chunks yield no facts.
* **PRO-2** For each candidate the extraction model (`llm.extract_model`, structured output through a forced tool call, no sampling parameters, decision 26) receives the chunk text, its header path and the attribute vocabulary, and returns a list of `{entity, attribute, qualifiers, value, unit, quote}` where `quote` is a verbatim substring of the chunk.
* **PRO-3** A returned fact is accepted only if: the attribute is in the vocabulary; the quote is a verbatim substring of the chunk text; and every digit sequence in `value` appears in the quote. Otherwise it is dropped and counted.
* **PRO-4** Accepted facts get `origin = prose_llm`, a lower authority tie-break than table facts when the rank is equal, and qualifiers normalised through section 7.
* **PRO-5** *Cache.* The cache key is `sha256(prompt_version + model + chunk_text + header_path)`. A hit returns the stored result with no call. A call result (including "no facts") is stored. A failed call (timeout, rate limit, malformed output) is **not** stored, so the next run retries.
* **PRO-6** Calls run concurrently up to `llm.extract_concurrency`. Transient errors (timeout, connection, HTTP 429 and 5xx) are retried. When the server states a wait (`retry-after`), that wait is honoured, at least 1 s and at most 60 s, for up to five retries, and every worker waits with it (one shared pause). With no stated wait, up to two retries after 1 s and 4 s (deterministic, jitter-free). A call that still fails gives no facts and is not cached (PRO-5).
* **PRO-7** With no API key and no cache entry, the chunk yields no facts. This is logged once per run, not per chunk, and appears in the ingest summary.
* **PRO-8** The prompt, its version string and the response schema are stored under `kk/ingest/prompts/` and changing them MUST change `prompt_version`.

### 6.8 Embeddings

* **EMB-1** Model: `bge-small-en-v1.5` loaded with `fastembed` from the bundled `submission/models/` path. Loading MUST NOT use the network; if the weights are missing, `build()` raises a clear error (this is a packaging bug, not a runtime condition).
* **EMB-2** Only `table_row`, `text_paragraph` and `list_item` chunks are embedded, in batches of at most 64 texts and at most `embed.max_batch_chars` characters (a text counts at most 2,000 characters, the model's 512-token window), using the model's document prefix. Texts are batched in order of length and the vectors are returned in input order. The character limit exists because the memory of a batch grows with the square of its text length (SPEC decision 24). Questions use the model's query prefix.
* **EMB-3** Only new chunk rows are embedded. Extended intervals reuse the stored vector.
* **EMB-4** Vectors are L2-normalised so cosine similarity equals the dot product.
* **EMB-5** Document vectors are cached on disk (`submission/cache/embeddings/vectors.sqlite`, or the directory in `KK_EMBED_CACHE_DIR`; git-ignored). The key is `sha256(model fingerprint + NUL + exact text)`, where the fingerprint hashes the model's ONNX and tokenizer files, so another model or another text never reuses a vector. Query vectors are not cached. The cache is an optimisation only: if it cannot be opened, read or written, it switches itself off with one warning and everything is embedded, and a stored vector of the wrong size counts as a miss. A model failure is never cached (SPEC decision 25).

### 6.9 Alias list

* **AL-1** For every distinct entity label produced by TBL-4, TBL-3 entity columns, and page titles, store a canonical `entity` id (lower-cased, punctuation-stripped label) and its aliases: the label, its words minus stop words, a singular/plural pair, and known abbreviations from a short hand-maintained list in `kk/ingest/aliases.py` (for example `cd` and `share certificate`, `apy`).
* **AL-2** Ambiguous aliases (one alias linking to two or more entities) are recorded as ambiguous. Question entity linking (section 8.4) MUST NOT treat an ambiguous alias as a unique match.

### 6.10 Conflict detection

At the end of every `ingest`, the `conflicts` table is rebuilt from the whole `facts` table (section 10). This is cheap (a group-by) and avoids needing to know which call is the last.

### 6.11 Failure behaviour at ingest

| Failure | Outcome |
|---|---|
| Page parse error | Skip the page; count; log the URL |
| Table rejected or quarantined | No facts; table text still searchable as chunks |
| LLM extraction failure | No facts for that chunk; not cached; retried next run |
| Embedding failure on a batch | Retry once; on a second failure raise (the index would be incomplete) |
| Store write failure | Raise (the index would be incomplete) |

The two raise cases are the only ones where `ingest` may raise; they indicate a broken environment, not bad data.

### 6.12 Ingest summary

After each `ingest`, log one record: pages, skipped pages, chunks new/extended/dropped, tables accepted/inferred/quarantined/rejected, unmapped headers (top 20), facts by origin, extraction cache hits/calls/failures, embed seconds, total seconds.

## 7. The shared normaliser (`kk.query.normalise`)

The same functions are used on table cells, prose-fact values, and question text. Each returns a canonical value or "not recognised". "Not recognised" is never silently turned into a guess.

### 7.1 Numbers, percent, money

| Input | Output |
|---|---|
| `3.5%`, `3.50 %`, `3.5 percent` | `percent 3.5` (compare as decimal, display as in source) |
| `0.10%1` (table cell) | `percent 0.10` (trailing footnote digit stripped) |
| `$100,000`, `$100000`, `100,000 dollars` | `usd 100000` |
| `$100k`, `100K` | `usd 100000` |
| `$1.5k` | `usd 1500` |
| `$2.50` | `usd 2.5` |
| `$1.2 million`, `$1.2M` | `usd 1200000` |
| `1,234` | `count 1234` |

A number with unmatched letters attached (`3.5x`, `12abc`) is not recognised.

### 7.2 Terms

A term is converted to whole months. Recognised: `<n>[- ]?(month|months|mo)`, `<n>[- ]?(year|years|yr|yrs)`, number words `one` to `twenty`, `twenty-four`, `thirty`, `thirty-six`, `forty-eight`, `sixty`, (`half a year` is **not** recognised). Examples: `five year`, `5-year`, `5 yr`, `60 months` all give `60`; `six-month` and `6 months` give `6`; `17-Month` gives `17`; `30-Year` gives `360`. `a year and a half` and `18 months or so` are not recognised (the second because of "or so" in the coverage check, section 8.5).

### 7.3 Amount constraints

An amount constraint is a pair of bounds with inclusive flags.

| Phrase (question side) | Constraint |
|---|---|
| `$X or less`, `up to $X`, `at most $X`, `no more than $X`, `under $X` (strict), `below $X` (strict), `less than $X` (strict) | upper bound `X` |
| `$X or more`, `at least $X`, `over $X` (strict), `above $X` (strict), `more than $X` (strict), `greater than $X` (strict) | lower bound `X` |
| `between $X and $Y`, `$X to $Y`, `$X - $Y` | lower `X` and upper `Y`, both inclusive |
| `$X` alone | the point `X` |

Table side: `$100,000 or less` is a cap tier with upper bound 100000; `$25,000 - $99,999.99` is a band; `$100,000+` is a lower bound. A **cap tier** with siblings is interpreted by *tier selection* (section 9.3): it covers amounts greater than the next-smaller sibling's cap, up to its own cap.

Any modifier word attached to an amount that is **not** in the list above is "not recognised", which fails the coverage check (section 8.5).

### 7.4 Model years

`2024-27`, `2024-2027`, `2024 - 2027`, `2024 to 2027` give the range 2024..2027. `2025 model`, `2025 model year`, `'25 model` give the point 2025. A four-digit year near the word "model" or "vehicle" is a model year; a four-digit year elsewhere is an unconsumed token (coverage check) unless it is a date.

### 7.5 Dates (table, prose, footers; never the question)

Recognised: `09/25/2026`, `9/25/26`, `2026-09-25`, `September 25, 2026`, `Sept. 25, 2026`, `25 September 2026`. Output ISO `2026-09-25`. A two-digit year `yy` maps to `20yy`. An unparseable date is "not recognised". **Dates are never parsed from the question text** (the `as_of` field is the only source of the target date).

### 7.6 Dashes and whitespace

`-`, `–` (en dash), `—` (em dash) and `to` between numbers are all range separators. All whitespace collapses to one space. Unicode is NFKC-normalised.

### 7.7 Test requirements

The normaliser MUST have a golden test per row above, property tests (parsing then formatting a canonical value round-trips; no input makes it raise), and tests that "not recognised" inputs return "not recognised" and never a number.

## 8. Question understanding (`kk.query`)

### 8.1 `as_of` to `target_ord`

* **ASOF-1** `as_of` is the only source of the target date.
* **ASOF-2** `as_of=None` gives `target_ord` = the highest ordinal ingested.
* **ASOF-3** `as_of=D` (an ISO date, or an ISO datetime whose first ten characters are an ISO date) gives `target_ord` = the highest ordinal whose `taken_on <= D` (inclusive). **Spec decision:** a value that is not parseable this way is treated as unanswerable (safe refusal), not as `None`.
* **ASOF-4** If no snapshot has `taken_on <= D`, the answer is the safe refusal.
* **ASOF-5** Snapshots above `target_ord` MUST NOT be read by any search, fact lookup or history view for this question.
* **Example:** snapshots 1..5 taken on 2026-05-12, 05-19, 06-09, 08-27, 09-28. `as_of = 2026-05-31` gives `target_ord = 2`, the date of snapshot 2. `as_of = 2026-05-19` also gives 2. `as_of = 2026-05-11` is a refusal.

### 8.2 Parsed question

`parse(question)` produces a `ParsedQuestion` with: normalised text; `shape`; `entity` candidates; `measure`; `qualifiers` with their source spans; `change_cue` flag; `consumed` spans; `residual` text; `uncertain` flag with reasons; `canonical_key` (or none). The function MUST be deterministic and MUST NOT call a model.

### 8.3 Shape classifier

Ordered rules over the lower-cased question; first match wins.

| Order | Shape | Trigger |
|---|---|---|
| 1 | `history` | an anchored history pattern matches (section 8.6) |
| 2 | `count` | starts with `how many`, or `number of` |
| 3 | `yes_no` | starts with `is`, `are`, `does`, `do`, `can`, `was`, `were`, `will`, `has`, `have` |
| 4 | `date` | starts with `when` |
| 5 | `list` | contains `list`, `types of`, `which ... do you offer`, `what ... do you offer`, `all the` |
| 6 | `what` | anything else that begins with `what`, `how much`, `which`, `where`, `who`, or is imperative (`tell me`) |
| 7 | `other` | none of the above |

`other` always goes to the slow path.

### 8.4 Entity linking

* **ENT-1** Candidate entities come from the alias list (section 6.9) by longest-match over question tokens after stop-word removal.
* **ENT-2** A candidate set of exactly one entity is a unique link. Two or more, or an ambiguous alias, is *ambiguous* and marks the parse uncertain (reason `entity_ambiguous`).
* **ENT-3** Entity match score (section 12.1): 1.0 for a unique link by label match, 0.7 for a unique link by alias, 0.4 for a link only by embedding similarity, 0.0 for none.

### 8.5 Coverage check (fail closed)

* **COV-1** Every rule that extracts something (term, amount, model year, measure, segment, entity, shape cue) records the character span it consumed. A word that belongs to what a rule recognised is part of its span: on the question side the word `term` right after a term (`60-month term`) and the cue words of a matched pattern (`has`, `what's`, `still`, `located`, `changed`...).
* **COV-2** The *residual* is the question with all consumed spans removed. The parse is **fully covered** iff every remaining token is a neutral word, punctuation, or an entity token.
* **COV-3** Neutral words (initial list, lower case): `a an the is are was were be do does did can could will would should what which who where when how much many for of on in at to my our your me i we you it its this that these those please tell give show find there any current currently now right today latest new rate rates loan loans account accounts card cards fee fees apply with and or dupaco dupaco's`. `about` is deliberately **not** on the list: COV-4 requires it to fail the check. The brand word `dupaco` (and its possessive) is on it because it never changes which fact a question means. **Spec decision:** this list is reviewed with the product owner after the first calibration run, since each addition widens the fast path.
* **COV-4** Any other word or any number left in the residual makes the parse *not fully covered* (reason `residual:<token>`). This includes unrecognised modifiers (`about`, `roughly`, `around`, `approximately`), unrecognised years, and "half" in "a year and a half".
* **COV-5** A parse that is not fully covered, is `uncertain`, or whose shape is `other`, MUST NOT use the fast path, MUST NOT be cached, and MUST NOT have a `canonical_key`.

### 8.6 Change and history questions

* **CHG-1** A question has a **change cue** if the normalised text contains any of: `changed`, `change`, `changes`, `still`, `used to`, `no longer`, `any longer`, `anymore`, `since`, `history`, `previously`, `before`, `earlier`, `ever`, `new`, `increase`, `decrease`, `went up`, `went down`, `gone up`, `gone down`, `closed`, `closing`, `open again`. **Spec decision:** a deliberately broad list. A cue alone never selects the fast path.
* **CHG-2** The shape is `history` only when the question fully matches an anchored **history pattern**: `H1` = `(has|have|did) [the] <entity> [qualifiers] [measure] (change|changed|increase|increased|decrease|decreased|go up|gone up|go down|gone down)`; `H2` = `(what|which) (was|were) [the] [measure] [qualifiers] <entity> (before|previously|earlier)`. The cue words in a matched pattern are consumed spans, so the coverage check (section 8.5) can pass.
* **CHG-3** A question with a change cue that does **not** match a history pattern takes the slow path with the fact-history view (section 13.3). A status question such as `Is the Hy-Vee branch still open?` matches the `STATUS` pattern (which consumes `still`) and is answered by the `status` template, not as a history question.
* **CHG-4** The question text is not parsed for dates. The target date still comes only from `as_of`. A question that carries a date in its text with `as_of=null` is not fully covered (the date words are residual) and therefore goes to the slow path.

### 8.7 Fast-path grammar

* **GR-1** The fast path accepts a question only if it matches one pattern in the registry (`kk/query/grammar.py`). A pattern is an anchored description over the extracted slots, for example: `<wh-word> [be] [the] [measure] [for|on|of] [a|an|the] [qualifiers] <entity>`.
* **GR-2** Initial pattern families, one per template type: `VALUE` (rate, APY, APR, minimums), `FEE`, `COUNT`, `ADDRESS`, `HOURS`, `STATUS` (yes/no and `when`), `LIST` (single table), `PROSE`, `HISTORY` (`H1`, `H2`, section 8.6). About twelve patterns in total.
* **GR-3** Patterns are written from question **shapes**, not from individual public questions. The public questions are used only afterwards to report which share of them each pattern covers.
* **GR-4** Each pattern has at least 5 positive and 5 negative golden cases, including near-misses that must not match.
* **GR-5** A partial match is a non-match.

### 8.8 Canonical key

A parse that passes COV-5 has `canonical_key = (shape, entity, measure, sorted qualifier items, target_ord)`, serialised as sorted JSON. Two questions with the same canonical key MUST receive the same answer.

## 9. Retrieval and candidate facts

### 9.1 Hybrid retrieval

* **RET-1** Embed the normalised question with the query prefix.
* **RET-2** Run a vector search and a BM25 search on `chunks.text`, each limited to `retrieval.per_index_k` rows, each with the validity pre-filter for `target_ord`.
* **RET-3** Fuse the two ranked lists with reciprocal rank fusion, `1 / (rrf_k + rank)` summed per `chunk_id`, and keep the top `retrieval.top_k`. Ties are broken by lower `chunk_id` to keep order deterministic.
* **RET-4** Keep the raw cosine similarity and the BM25 score of each hit. They feed the confidence score; the fused rank does not.
* **RET-5** For each hit, fetch its parent (if any) for the slow path and for citations.

### 9.2 Candidate facts

* **CAND-1** Candidate facts are those in the `facts` table whose `chunk_id` is among the retrieved hits (or their children), with the validity filter. If the question has a unique entity link, facts for other entities are removed.
* **CAND-2** If the question has a measure, candidates with another attribute are removed; for `rate` with no measure stated, `rate`, `apy` and `apr` candidates from the same row are kept together.
* **CAND-3** Candidate score `s = r * (0.5 + 0.5 * q) * e`, where `r` is the hit's retrieval score scaled to [0, 1] by dividing by the highest RRF score possible, `q` the qualifier match (section 12.1), `e` the entity match score.
* **CAND-4** Candidates are grouped by `fact_key`. The group value is resolved (section 10.3) to the winning fact.

### 9.3 Sibling selection and tier selection

* **SIB-1** *Siblings* are candidates with the same entity and attribute that differ only in one qualifier key (the *varying key*, for example `amount_tier`).
* **SIB-2** The question *selects* exactly one sibling when its qualifier for the varying key matches exactly one sibling under the rules below. Otherwise the selection is **none** or **several**.
* **SIB-3** *Tier selection.* The question's amount constraint (section 7.3) is a region of amounts. A sibling tier is *touched* if it holds at least one amount in the region: a cap tier covers the amounts above the next-lower tier's cap up to its own cap, a band covers its range, and a floor tier covers the amounts above its bound. The selection is:
  * **one** if the constraint equals a tier's own constraint exactly (same kind, bound and inclusivity, for example "up to $300,000" for a "$300,000 or less" row), or if exactly one tier is touched and the whole region lies inside it ("at most $50,000" when the lowest tier is "$100,000 or less");
  * **several** if two or more tiers are touched ("$150,000 or less"), or one tier is touched but the region extends beyond it ("over $250,000" when the top tier is a cap, since larger amounts are in no published tier), or SIB-4 applies;
  * **none** if no tier is touched ("$400,000" when the top cap is $300,000).
* **SIB-4** *Boundary.* An endpoint of the region within 0.5% of a cap tier's cap, on either side but not exactly on it ("$100,300" against a $100,000 cap), or an upper bound with a strict modifier exactly at a cap ("under $100,000"), makes the selection **several**, the tier that owns the cap and the next tier. The 0.5% band applies to cap tiers only, not to bands, whose edges are stated to the cent.
* **SIB-5** A selection of **several**, including a boundary case, where the question's own qualifiers do not resolve it caps confidence (section 12.1) and the question goes to the slow path, which applies the tier policy (section 13.4). A selection of **none** is not capped: no published tier or sibling matches, and a fully covered question is answered on the fast path with the safe refusal (section 12.2).
* **SIB-6** A fact whose qualifier contradicts one the question stated (for example the question says 60 months and the fact says 36 months) is removed from the candidates before scoring.

### 9.4 History segments (fast-path history)

For a `history` question with a unique entity link, a unique measure and a unique sibling selection, the fact key `K` is fixed. The system builds the history of `K` for ordinals `1..target_ord`:

* **HSEG-1** At each ordinal `i`, the value is the winner of the fact key after conflict resolution at that ordinal (section 10.3), or *absent* when the key has no visible fact at `i`.
* **HSEG-2** Consecutive ordinals with the same value form a **segment** with `first_date` (the `taken_on` of its first snapshot) and `last_date` (the `taken_on` of its last snapshot). The history is the list of segments, oldest first. Leading absent ordinals (before the key existed) are not part of the history.
* **HSEG-3** The fast path is allowed only if: the key is present at every ordinal from its first appearance to `target_ord` (no gap); no ordinal has an unresolved conflict for `K`; no winning fact is a `none_value` (an explicit "none" or dash is not turned into wording by a rule; the slow path judges it); `(entity, attribute)` is not `degraded` (TBL-11); and there are at most 5 segments. Otherwise the hard cap in section 12.1 applies and the question goes to the slow path.
* **HSEG-4** Snapshot dates are the only dates the answer can give. They bound the change (it happened after the last snapshot with the old value and no later than the first snapshot with the new one), so the wording uses "as of" and "by", never "on".

## 10. Conflicts

### 10.1 Definition

A conflict is a **coexisting contradiction**: within one snapshot, two or more **live** pages (chunks visible at that ordinal, expired or not) carry facts with the **same `fact_key`** and **different normalised values**. Not conflicts: different qualifiers, rate versus APY, formatting differences, a value that was replaced over time, and two facts on the **same URL** (that is a P6 key-uniqueness issue, not a conflict between pages).

### 10.2 Detection algorithm

1. For each ordinal `k`, take all facts whose interval contains `k`.
2. Keep only facts with a numeric value (`value_num` is not null). Group them by `fact_key` and compare `value_num`. Free-text facts (`prose`, `hours`, `address`, `phone`, `eligibility`, `status`) never take part: they have no normal form, and two different sentences about one entity are not a contradiction (decision 27).
3. Groups with at least two distinct values on at least two distinct URLs are conflict candidates for `k`.
4. Merge across ordinals by `(fact_key, frozenset of normalised values)`, collecting the list of ordinals where it coexisted.
5. Resolve each merged candidate (section 10.3) using its **latest** ordinal.

Facts with `entity_inferred = true` or from a quarantined table do not take part (precision first). Facts with `none_value` or without a numeric value do not take part.

### 10.3 Resolution

The reference date `D` is the `taken_on` of the ordinal being resolved (META-4). A value is *current* if at least one chunk carrying it is neither expired nor not yet in force. Applied in order; the first step that picks a unique winner decides.

| Step | Applies when | Result |
|---|---|---|
| 0. current values | always | Values with no current chunk are removed: an expired value always loses, and a value whose effective date is after `D` always loses. If **no** value is current, the candidate is unresolved (nothing current can be called right). If exactly **one** value is current, it wins, with strategy `effective_date`. |
| 1. `effective_date` | two or more current values, every one has an `effective_date`, and the dates differ | the latest `effective_date` wins. If two or more share the latest date, only those continue to step 2. If any current value has no effective date, this step is skipped and all current values continue. |
| 2. `source_authority` | two or more values remain | the highest `authority_rank`. If several share it and `origin` differs, `table_rule` beats `prose_llm`. If one value remains it wins; otherwise the values that share the top rank continue to step 3. |
| 3. `snapshot_recency` | two or more values remain | the value whose page has the newest `fetched_at`; if equal, the highest `first_seen_ord` |
| 4. unresolved | step 3 also ties | no winner |

* **CR-1** A shared top rank is not by itself unresolved: those values go on to step 3. The candidate is unresolved only when step 0 leaves no current value or step 3 ties.
* **CR-2** Step 3 is weaker evidence than steps 0 to 2 and the reason text says so.
* **CR-3** A value carried by several pages is represented by its best current page: highest `authority_rank`, then later `effective_date`, then newer `fetched_at`, then lower URL. A value is current if any of its pages is.

### 10.4 Output record

| Field | Content |
|---|---|
| `topic` | `"<entity label> <qualifier phrase> <attribute label>"`, for example `6-month share certificate APY`. It MUST contain the entity words and the term or tier words, so topic keywords in the scorer match. |
| `values` | The competing values as displayed in the sources (`2.95%`, `3.50%`), de-duplicated, sorted ascending by numeric value |
| `sources` | One `Source` per value: the URL, `snapshot_id` and quote of the highest-authority page that carries it, in the same order as `values` |
| `resolution` | The displayed winning value, or `None` |
| `reason` | `strategy=<name>: <winner> supersedes <loser>; <loser>`. Every value is described the same way, winner first: `<value> (<effective date or "no effective date">, <"live", "expired <date>" or "not yet in force">, [<evidence>,] "<page title>")`. The evidence is `<source_class> rank <n>, <origin>` for `source_authority` and `fetched <date>` for `snapshot_recency`. Titles are cut at 60 characters, at most 3 losers are named (the rest become `and N more`), the winner is always complete, and `snapshot_recency` is written `snapshot_recency (weaker evidence)`. Unresolved: `strategy=unresolved: <values> cannot be separated by effective date, source authority or snapshot recency`, or `strategy=unresolved: every competing value is expired: <values>`. Example: `strategy=effective_date: 3.50% (no effective date, live, "Certificates") supersedes 2.95% (no effective date, expired 2025-09-30, "Promo")` |

### 10.5 Answer interaction

* **CA-1** If an answer touches a fact key with a conflict record, `Answer.conflict_note` carries the strategy and the loser, for example `strategy=effective_date: 3.50% used; 2.95% superseded`.
* **CA-2** If the record is unresolved, the answer is `confident=False` unless the slow path justifies a choice from the text, in which case its structured output must cite the evidence and the note says `strategy=slow_path_judgement`.

### 10.6 Ordering

`conflicts()` returns records sorted by topic, then by first value. The order MUST NOT depend on hash randomisation or insertion order.

## 11. Cache

* **CACHE-1** Key = `canonical_key` (section 8.8). Questions with no canonical key are neither read from nor written to the cache.
* **CACHE-2** The cache is in memory, bounded by `cache.max_entries` (least-recently-used eviction), and is cleared by every `ingest` call.
* **CACHE-3** Stored: answers that passed the validators, and refusals that are deterministic (below `tau_floor`, no snapshot at `as_of`). Not stored: any result produced after an error, timeout or exception.
* **CACHE-4** A cache hit returns a copy of the stored `Answer`, so callers cannot change it.

## 12. Confidence, routing and rendering

### 12.1 Confidence score

`c` is in [0, 1], computed locally with no model call.

| Signal | Definition |
|---|---|
| similarity `sim` | Mean of (a) the top hit's raw cosine similarity and (b) its BM25 score divided by an upper bound for the query (a value fixed in implementation and recorded in config; it MUST map to [0, 1]) |
| margin `mar` | `(s1 - s2) / s1`, where `s1` is the best candidate score and `s2` the best candidate with a different value or fact key (candidates with an identical value on another page do not shrink the margin). If there is no second candidate, 1.0 |
| qualifier match `q` | Qualifiers the question supplied that agree with the candidate's, divided by qualifiers the question supplied. 1.0 if the question supplied none and there are no siblings |
| entity match `e` | Section 8.4 (ENT-3) |
| resolution state `rs` | From `confidence.resolution_state` (1.0 unique, 0.9 strategy 1-2, 0.7 strategy 3, 0.0 unresolved) |

`c = (w1*sim + w2*mar + w3*q + w4*e) * rs`, weights from config.

**Hard caps.** `c` is limited to at most `tau_accept - 0.01` (so a capped question never takes the fast path) when any of these holds:

1. The parse is not fully covered, is uncertain, or does not match the fast-path grammar (COV-5, GR-1).
2. There is no template for the (shape, attribute type) pair.
3. The conflict is unresolved.
4. The question is an aggregate (sum, total, average, compare) or asks for a list whose items come from more than one page or section.
5. The sibling selection is **several**, including a boundary case (SIB-5).
6. The winning fact has `entity_inferred = true` and the entity link is not a unique label match.
7. The winning fact's `(entity, attribute)` is `degraded` (TBL-11).
8. The question has a change cue but its shape is not `history` (CHG-3).
9. The shape is `history` and the history fails HSEG-3 (a gap, an unresolved conflict, a none value, a degraded pair, or more than 5 segments).

### 12.2 Routing

| Condition | Route |
|---|---|
| No snapshot at `as_of`, or `as_of` invalid | Safe refusal |
| Parse fully covered and grammar matched, but the sibling selection is **none** (SIB-5) | Safe refusal, no model call |
| `c < tau_floor` (after caps) | Safe refusal, no model call |
| `tau_floor <= c < tau_accept` (after caps) | Slow path |
| `c >= tau_accept` | Fast path |

If the fast answer fails the validators (section 14), or the fast path raises, the question is escalated to the slow path. If the slow path is unavailable or its answer fails the validators, the result is the safe refusal.

### 12.3 Safe refusal

`Answer(text="I couldn't find <phrase> on Dupaco's website. Please contact Dupaco.", sources=[], confident=False)`.

* `<phrase>` is the question's unique entity label if it linked one, otherwise `that information`. **Spec decision:** this is more cautious than "the question's own phrase" in `DESIGN.md` because it cannot carry stray digits.
* The text MUST contain no digit that is not already in the question, so the scorer's `should_refuse` check passes.

### 12.4 Calibration procedure

1. **Labelled set.** The 40 public questions; questions generated from the facts table (`What is the {attribute} for {qualifiers}?` and several fixed paraphrase templates, each with the known value as gold); negatives (attributes or qualifiers that do not exist, such as a 7/1 ARM); adversarial questions with unusual modifiers, ranges and terms. Generated items quote site content, so `make calibrate` writes them only under `results/calibration/` (git-ignored); committed tests use synthetic facts, and only numbers and thresholds are committed (decision 29).
2. **Split.** Deterministic split by a hash of the item's `group` (its id when it has none) into a training part (about 70%) and a held-out part (about 30%). Public-question groups (same `group`) stay together.
3. **Measure.** Compute `c` for each item (before the hard caps; a capped item never takes the fast path and is left out of precision); label the fast answer correct, wrong, or should-refuse by the eval's own scoring rule. `confidence.bm25_bound` is the largest top-hit BM25 score on the training part, rounded up to 0.5 (decision 30).
4. **Choose.** `tau_accept` is the lowest value on the training part where fast-path precision is at least the target (initial: 0.97). `tau_floor` is the highest value where at most 2% of answerable items fall below it.
5. **Report.** Held-out precision and recall, the calibration curve, and the chosen values with provenance, written to `submission/calibration_report.json` and referenced from `config.json`.
6. **Guard.** If held-out precision is more than 0.03 below training precision, the grammar is narrowed or the thresholds raised before accepting.

### 12.5 Templates

Hand-written, versioned in code, unit-tested, chosen by (attribute type, shape) from the lookup table. Slot values come only from stored fact fields.

| Template id | Used for | Text pattern |
|---|---|---|
| `rate` | `what` / `value` with rate-like attributes | `The {entity_label} {attribute_label} for {qualifier_phrase} is {value_text}.` If the question did not name rate or APR and the row has both, `{value_text} rate / {apr} APR` |
| `fee` | `fee` | `The {entity_label} fee is {value_text}{per}.` |
| `count` | `count` | `There are {value_text} {entity_label}.` |
| `address` | `address` | `The {entity_label} is at {value_text}.` |
| `hours` | `hours` | `{entity_label} hours are {value_text}.` |
| `status` | `yes_no`, `date` with `status` | `{Yes or No} - {entity_label} {status_phrase} {date}.` |
| `list` | `list` over one table | `{intro}:` then one `- {item}` line per row, items rendered with `rate` or `fee`, in table order |
| `quoted_prose` | `prose` | `{entity_label}: "{sentence}"` |
| `history` | `history` | Changed (2 segments): `Yes - the {entity_label} {attribute_label} for {qualifier_phrase} was {v1} as of {d1_last} and had changed to {v2} by {d2_first}.` Three or more segments: `Yes - the {entity_label} {attribute_label} for {qualifier_phrase} has changed {n} times:` then one line `- {v} from {first_date} to {last_date}` per segment. Unchanged: `No - the {entity_label} {attribute_label} for {qualifier_phrase} has been {v} in every snapshot from {first_date} to {last_date}.` For `H2` the same text is used. |
| `refusal` | section 12.3 | |

* **TPL-1** `{qualifier_phrase}` is built by a fixed map in a fixed key order (`term` to "a 60-month term", `amount_tier` to "loans of $100,000 or less", `model_year` to "2024-27 model years").
* **TPL-2** `status` compares the fact's date with the **target snapshot's `taken_on`**, never the wall clock, to choose Yes or No.
* **TPL-3** Text numbers appear in the source's format (`5.39%`, `$2.50`).
* **TPL-4** A template is rendered only when every slot has a value; otherwise there is no template (hard cap 2).
* **TPL-5** Polish (`flags.polish`, default off) sends only the rendered text to the light model, with a 1.5 s timeout. The result is discarded unless its set of numbers equals the template's. On timeout, error or mismatch, the template text is returned unchanged.
* **TPL-6** The same facts MUST give the same text.
* **TPL-7** The `history` template prints snapshot dates in ISO format (`2026-05-19`) and nothing else about timing. It never states an exact change date. Sources are the page of the newest segment and the page of the oldest segment.

### 12.6 Sources

The fast path cites the chunk (and its page) of the winning fact. The slow path cites the pages of the `evidence_ids` it returned, each verified to be in the retrieved set. At most 3 sources; the most authoritative first.

## 13. Slow path

### 13.1 Inputs

The slow path receives: the question (original text); the target snapshot date; the resolved facts for the candidate fact keys (value, qualifiers, effective and expiry dates, authority, snapshot id); the matched parent chunks (a table row is replaced by its parent table with header and footer notes; a paragraph or list item comes with up to three neighbouring chunks on each side under the same heading, labelled with its heading path, decision 35), capped at 12000 characters in total, ordered by candidate score, taken from the deeper search of 13.5; conflict notes for in-scope keys; for change-cue questions that do not take the fast history template, the fact-history view (13.3); and, for tier-ambiguous questions, all sibling facts (13.4).

### 13.5 Retrieval for the slow path

* **SRET-1** The slow path searches the question and up to three alternative queries from one short call to `llm.extract_model` (no key, a timeout of 4 s or any error gives no alternatives), each with `retrieval.slow_top_k` and `retrieval.slow_per_index_k`, and fuses the rankings by reciprocal rank (decision 36). The fast path never makes this call.
* **SRET-2** A retrieved chunk is left out of the payload, and its numbers become known losers (V2), when it is superseded: its fact lost its key on effective date or on source authority (losing on snapshot recency alone does not count), or it is a table whose later generation exists, meaning another table with the same section heading and columns, at least three shared row labels with at least 70% overlap, and a later effective date (decision 39).
* **SRET-3** Several differing statements for one non-numeric key are not a conflict (decision 27): they are listed side by side with their pages. Only an unresolved numeric conflict blocks its values.
* **SRET-4** Each search reads `retrieval.slow_pool_k` rows and keeps the best-ranked row of each distinct text (compared without case or spacing differences) before the first `retrieval.slow_per_index_k` are fused. A closing paragraph repeated on hundreds of pages otherwise fills the ranking and hides the one page that answers (decision 40).
* **SRET-5** A news or blog page is labelled `published <date>` when one of its first six chunks is short (200 characters or fewer) and ends with a date written as `June 20, 2025`. The date is a label only: it is never an `effective_date`, never enters fact resolution, and is not stored. The prompt's first rule (RECENCY) tells the model to use the value in the evidence with the latest effective date, or latest publication date, when evidence disagrees, and the payload ends with a note that lists the newest dated evidence (effective date, else publication date) newest first (decisions 41 and 47).
* **SRET-6** After fusion, one call to `llm.extract_model` (6 s timeout, no key or any error means no reranking) receives the question and a one-line snippet of each of the first `retrieval.rerank_candidates` fused chunks (page title, publication date, heading path, start of the text) and returns the numbers of up to `retrieval.rerank_keep` chunks most likely to answer. Those chunks come first in the payload; the rest keep their fused order. The order given to the model is the order the payload is built in (decision 42).
* **SRET-7** After the chunks are assembled and before the 12,000 character cap, a sentence that holds a digit and appears on at least two dated pages with different figures (the same sentence once digits are masked) is a changing statement. The chunk with the newest copy (ties: the better ranked) is promoted to the highest score, so the cap cannot drop it. Nothing is removed and nothing is reordered except by score; undated chunks and tables are never compared (decision 48).

### 13.2 Call and output

* **SLOW-1** One call to `llm.slow_model` with structured output matching (no sampling parameters, decision 26): `answerable` (bool), `answer` (string), `evidence_ids` (list of chunk or fact ids), `loser_values_mentioned` (list of strings, must be empty), and `reason` (optional string: one sentence on what is missing when `answerable` is false; logged, never shown, decision 38).
* **SLOW-2** The system prompt instructs: answer only from the supplied evidence; never use a value marked stale, expired or superseded; state the assumed tier when the question omits it; say `answerable=false` when the evidence does not contain the answer.
* **SLOW-3** The answer is accepted only if `answerable` is true, every `evidence_id` was supplied, the validators pass, and the response parsed against the schema.
* **SLOW-4** Total time for one slow-path answer is bounded by `llm.slow_deadline_s`. One retry is allowed only for rate-limit or server errors and only if more than half the deadline remains. After the deadline the result is the safe refusal.
* **SLOW-5** With no key, the slow path is not called and the result is the safe refusal (a single warning is logged per run).
* **SLOW-6** The model never sees the other snapshots' values except in the fact-history view (13.3), and then labelled with their dates.

### 13.3 Fact-history view (slow-path fallback for change cues)

Used when a question has a change cue but is not a `history` shape (CHG-3), or when a `history` question fails HSEG-3.

* **HIST-1** For the facts matched by the question, the view lists every distinct `value_text` with `first_seen_date`, `last_seen_date`, and the page URL, for ordinals up to `target_ord`, oldest first.
* **HIST-2** A value that appears in no snapshot at or below `target_ord` MUST NOT appear in the view.
* **HIST-3** The view also lists pages that were added or removed for the matched entity between consecutive snapshots up to `target_ord`.
* **HIST-4** The view is capped at 40 lines, preferring the most recent changes.

### 13.4 Tier policy

* **TIER-1** Applies when the question omits a qualifier or the sibling selection is **several** (SIB-5). It is applied only by the slow path.
* **TIER-2** The default tier for an attribute comes from `tier.default_by_attribute`. The initial rule is the **entry tier** (the lowest cap or band).
* **TIER-3** The slow path states the tier it used (for example "for loans up to $100,000").
* **TIER-4** For a question about one specific product with no amount ("a 30-year mortgage"), the answer MAY give the range from the entry tier to the top tier. For a generic question ("mortgage rates"), it leads with the default tier and does not use another tier as the headline.
* **TIER-5** Every number in the answer must still belong to a sibling fact on an in-scope key (the validators apply).
* **TIER-6** Status: a hypothesis that fits the public questions `dup-009` and `dup-011`; it MUST be checked on held-out paraphrases before it is relied on.

## 14. Validators

Run on every answer from both paths, in this order. A failure on the fast path escalates to the slow path; a failure on the slow path is the safe refusal.

| ID | Check | Rule |
|---|---|---|
| V1 | Number grounding | Extract numbers from `text` using the scorer's own number extraction (value and unit). Every number MUST be in the allowed set: the winning fact value(s); qualifier numbers on the winning keys (tier amounts, terms, model years); numbers in the question; the numbers in the dates a template prints (ISO date parts of the snapshot or fact date); for `history` answers, every segment value of the key; for the slow path also the numbers in the cited facts' own row, dates and qualifiers, and the numbers in the cited page text (beside tier siblings only dollar amounts and plain numbers, never a percentage: a rate must come from a resolved sibling fact, TIER-5) |
| V2 | Stale and loser | If `text` contains a number that is a known loser or expired value for an in-scope key and is not a winner for any in-scope key, reject. Known losers come from the conflicts table, earlier snapshots and expired chunks. On the slow path each loser carries a scope, the chunks that hold the value that replaced it (decision 43): a loser is excused only when the answer cites none of those chunks and cited evidence from elsewhere prints the number; a loser without a scope, or that no cited evidence prints, is rejected as before |
| V3 | Refusal shape | A refusal has `confident=False`, no digit that is not in the question, and no sources |
| V4 | Citation | Every cited URL belongs to the winning evidence (fast path) or to retrieved evidence (slow path) |
| V5 | Quote verbatim (prose facts) | A quoted sentence appears verbatim in the source chunk |

Stated limit: V2 can only catch stale values that became facts; V1 is the main safeguard for the rest.

## 15. Determinism and consistency

* **DET-1** LLM calls send no sampling parameters (decision 26), so a repeat of an uncached call is not guaranteed to match. Where a result matters for later behaviour (extraction), it is cached by content hash.
* **DET-2** No unordered container's iteration order may affect output; sorting is explicit everywhere (conflicts, candidates, sources, list items).
* **DET-3** There is no randomness in ranking, tie-breaking, sampling or splitting; the calibration split uses a stable hash.
* **DET-4** Answers are rendered from fact fields, not copied from raw text, so questions that map to the same facts give the same numbers.
* **DET-5** The same question and `as_of` asked twice in a run MUST give the same `text` numbers, whether or not the cache is used.

## 16. Errors and degradation

`answer` wraps each stage in its own error boundary and never raises.

| Failure | Outcome |
|---|---|
| Invalid `as_of` | Safe refusal |
| No snapshot at `as_of` | Safe refusal |
| Parse raises | Slow path (with the raw question); logged as a bug |
| Retrieval or store raises | Retried once; if it still fails, safe refusal (the slow path would have no evidence) |
| A stage after retrieval raises | Slow path |
| Fast answer fails a validator | Slow path |
| Slow path: no key, timeout, rate limit, malformed output, validator failure | Safe refusal |
| `c < tau_floor` | Safe refusal, no model call |
| Polish times out or mismatches | Template text, unchanged |
| Cache read or write error | Ignore the cache and continue |

Failures and refusals caused by errors are logged and never cached.

## 17. Performance budgets

| Item | Budget |
|---|---|
| `build()` | at most 15 s (model load) |
| Ingest, all five snapshots, with the extraction cache | target under 10 minutes, hard limit 30 minutes |
| Ingest, no cache, with a key | target under 25 minutes; measured in implementation |
| `answer`, cache hit | under 20 ms |
| `answer`, fast path | median under 150 ms, p95 under 400 ms, p99 under 2 s (measured for the decision stage only: median 129 ms, slowest 247 ms; no public question is answered on the fast path) |
| `answer`, slow path | under 15 s (deadline); measured p50 6.8 to 7.4 s, p95 8.1 to 10.5 s, slowest 10.3 to 12.4 s over five clean runs of the public eval |
| Peak memory during ingest | under 3 GB on an 8 GB machine |

If more than 5% of questions go to the slow path, the p95 equals the slow-path latency; the eval report states the slow-path share so this trade-off is visible. On the public eval the share is 100%, so the p95 is the slow-path latency (decision 49).

## 18. Logging and metrics

* **LOG-1** One structured JSON record per `answer`: question hash, `target_ord`, route (`cache`, `fast`, `slow`, `refusal`), confidence and its signals, caps fired, validator results, latency per stage, candidate fact ids.
* **LOG-2** Logs go to a file under `KK_WORK_DIR`; nothing is printed to stdout or stderr except one-line warnings, so the harness output stays clean.
* **LOG-3** Ingest records follow section 6.12.
* **LOG-4** Logs never contain the API key, and never contain the full text of a snapshot page.

## 19. Tooling, tests and verification

### 19.1 Make targets

`make verify` runs, in this order and failing fast: `ruff check`, `ruff format --check`, `mypy --strict` on `kk`, `bandit`, `pip-audit`, `pytest` with coverage (under `python -X dev -W error`), `python -m build`, then the public-eval gate. Also: `make eval` (public eval into `results/`), `make calibrate`, `make export-requirements`, `make docker-verify`.

### 19.2 Test layers

| Layer | Content |
|---|---|
| Unit | Each module in isolation, including error paths |
| Golden | A fixed input and a hand-checked output: one per table header shape (about a dozen), one per normaliser row, one per fast-path pattern with near-miss negatives, the template catalogue, the conflict topic and reason strings. Golden outputs come from real snapshot content and hand-checked answers, never from running the code and saving the result |
| Property (hypothesis) | Money, term and percent parsers round-trip and never raise; chunking is deterministic; interval extension never produces overlapping intervals for one `chunk_id` |
| Integration | The `harness/selftest` snapshots end to end (the selftest conflict must be found and resolved to `3.50%`); a small multi-snapshot fixture for `as_of` and intervals |
| Eval | `harness/run_eval.py` on the public eval; the generated calibration set |

### 19.3 Coverage and reports

Line coverage of at least 90% on the extraction, normaliser, resolution and `as_of` modules, and at least 80% overall. The eval report MUST list: score by category, consistency, conflicts F1 and resolution accuracy, p50/p95/p99, the share of questions answered by each route, the share answered by template, and the table-coverage report (tables with at least one fact, unmapped headers).

### 19.4 Docker

`docker compose` provides `verify` (runs `make verify`) and `eval` (runs the public eval). There is no database service; the submission itself needs no Docker.

### 19.5 Held-out question set (`tools/heldout.py`)

This is the set behind AC-14 for the slow path. It is a developer tool, not part of the submission, and its questions and answers are local files under `results/heldout/` (git-ignored, because they contain site text).

* **Generation** (`make heldout-generate`, needs a key). It samples paragraphs and list items from the latest snapshot, half of each where possible, and has the extraction model write one member-style question per passage plus 1 to 3 key phrases. A phrase is kept only if it occurs verbatim in the passage, so the gold comes from the page and not from the system's own extraction. Questions are never written from the public questions.
* **Split.** Each item belongs to a part by a stable hash of its group (`HELD_SHARE` 0.5): `dev` is the half the owner may read and tune against, `held` is the half that is never used to tune anything. Both are reported. Reading failures from `held` weakens it, so any change motivated by it is noted in the step report.
* **Run** (`make heldout PART=dev|held|all`). The whole system answers each question with the answer cache off, and the tool prints strict and close accuracy, accuracy by passage kind and the reasons for refusals.
* **Scope.** Every item is answerable from one passage of the latest snapshot. The set has no negative, paraphrased, adversarial, as-of or conflict items, so it measures prose coverage on the slow path and does not replace the public eval.

**Scoring criteria.** The slow path is a model, so the same correct answer can use different words. No string rule is exact, so two rules are reported and the truth lies between them.

| Score | Rule | Reading |
|---|---|---|
| Strict (lower bound) | The scorer's own value-aware match (`harness/scoring.py`): every key phrase appears in the answer, numbers by value and unit, text as a normalised substring. A refusal is wrong. | Never credits a wrong answer by this rule, but counts a correct paraphrase as wrong ("8 steps" for "eight steps"). |
| Close (upper bound) | The strict rule, or for each phrase: every number in it appears in the answer, number words equal digits, case and punctuation are ignored, and at least 80% of its content words appear (matched on their first five letters). A refusal is wrong. | Credits correct paraphrases, but can credit an answer that has the right words and states the wrong thing. |

Both figures are reported next to the public accuracy. AC-14 is judged on the gap to the close score and the strict score is the floor beside it. The set was not graded by hand or by a model, so the true accuracy is not measured, only bracketed. The close score is the owner's choice for the headline figure.

## 20. Acceptance criteria

Each criterion is checked by a named test or report.

| ID | Criterion |
|---|---|
| AC-1 | On the public eval, overall accuracy is above 0.78 and no category is below 0.70, except `conflict_resolution`, whose floor is 0.30 (it is the weakest category and has only 3 questions). The bar was 0.90 and 0.80 until Step 10 (decision 46), and the overall line was then set to above 0.78 to match DESIGN.md. 0.78 is the pass line, not a target to push toward 100%: the public eval has 40 questions, so each question is 2.5 points and the last points can only come from fitting to those questions. |
| AC-2 | `stale_trap_hits` is 0 and `errors` is 0. |
| AC-3 | Every `should_refuse` item is refused with `confident=False` and no digit beyond the question's. |
| AC-4 | `dup-006` (`as_of=2026-05-31`) returns 3.90% and does not contain 4.00%. |
| AC-5 | Reworded question groups return identical sets of numbers; consistency score at least 0.90. |
| AC-6 | The selftest conflict (`2.95%` versus `3.50%`) is reported with `resolution = 3.50%` and a reason that names a strategy. |
| AC-7 | Every answer in the public eval finishes under the 20 s ceiling at which the speed score reaches 0, and p95 is under 15 s (the slow-path deadline). Measured p95 is 8.1 to 10.5 s; the old fast-path limit (p95 at most 2 s) is kept as a design budget only (decision 49). One run stalled far beyond the deadline, so the deadline is not a guarantee. |
| AC-8 | With no API key, `build()`, `ingest` (using the shipped cache) and `answer` all complete without raising, and slow-path questions are refused. |
| AC-9 | Held-out fast-path precision is at least 0.97 and within 0.03 of training precision. |
| AC-10 | No answer text contains a value the conflicts table marks as a loser for an in-scope key (V2 holds on every public question). |
| AC-11 | `make verify` passes locally and in Docker. |
| AC-12 | Deleting the shipped extraction cache and re-running ingest with a key reproduces the same facts (compared by fact key and value). |
| AC-13 | Every `history` golden case (changed once, changed several times, unchanged, gap in the history, unresolved conflict at some ordinal) renders the expected text or escalates to the slow path as specified, and no history answer states an exact change date. |
| AC-14 | End-to-end accuracy on the held-out set (generated, paraphrased, negative and adversarial questions that were not used to tune anything) is reported next to the public accuracy, and the held-out figure is within 0.15 of it. The held-out figure used for this is the close score and the strict score is reported beside it as the lower bound (19.5); the limit was 0.03 until Step 10 (decision 46). A public accuracy above 0.95 triggers a leakage review (special-cased wording, thresholds tuned on public items, grammar patterns copied from public questions) before it is accepted. |

## 21. Spec decisions for review

These were chosen while writing the spec because `DESIGN.md` did not settle them. Each is open to change.

1. **Package location.** `kk` lives in `submission/kk/` so the hand-in folder is self-contained.
2. **Config format.** `submission/config.json` (standard library, works on Python 3.10).
3. **Invalid `as_of`.** Unparseable values give a safe refusal rather than being treated as "latest".
4. **Refusal phrase.** Uses the unique entity label or "that information", never the question's own phrase.
5. **Answer cache.** In memory and cleared on every `ingest`; not persisted to disk.
6. **Slow-path deadline.** 15 s total with at most one retry, below the 20 s point where the speed score reaches zero.
7. **Recognised amount modifiers.** The list in 7.3; anything else fails the coverage check.
8. **Neutral-word list.** The list in COV-3 (without `about`, removed in Step 6 because COV-4 requires it to fail; with the brand word `dupaco` and `dupaco's`, added in Step 6 by the owner), to be reviewed after the first calibration run.
9. **Change-cue list.** The broad list in CHG-1; a cue alone sends the question to the slow path, never to the fast path.
10. **Conflict rebuild.** Rebuilt after every `ingest` rather than once at the end.
11. **Chunk parent cap.** `chunk.max_tokens = 1200`.
12. **Authority rules.** The URL-and-title table in 6.6 is provisional until a URL survey of the real snapshots.
13. **Tier selection.** The 0.5% boundary band and the cap-tier interpretation in SIB-3 and SIB-4 are hypotheses to be tested on the real rate tables.
14. **History-view size.** 40 lines, 12000 characters of context for the slow path.
15. **Hard-cap rule.** A cap lowers `c` to at most `tau_accept - 0.01`, so the question cannot take the fast path. It still goes to the slow path unless its capped score is also below `tau_floor`, in which case it is refused.
16. **Same-URL duplicates.** Two different values for one fact key on one page are not a conflict (quarantined by P6).
17. **History wording and limits.** `history` answers bound the change by snapshot dates ("as of", "by"), allow at most 5 segments, and require a gap-free history; anything else goes to the slow path.
18. **History patterns.** Only `H1` and `H2` select the fast history template; the broad cue list in CHG-1 never does by itself.
19. **Nothing current means unresolved.** If every competing value is expired or not yet in force, the conflict is unresolved (10.3 step 0). Ranking withdrawn values against each other could present one as current.
20. **Resolution order.** Expired (on or before `D`) and not-yet-in-force values lose first; the latest effective date decides next, and only among dated values; authority and then recency apply only to what remains; a shared top rank goes to recency instead of being unresolved. `D` is the snapshot's `taken_on`. (Changes 10.3, CR-1 and META-4.)
21. **Tier selection errs on precision.** A certain match with exactly one tier is answered on the fast path; a boundary or a match with several tiers goes to the slow path; no match is a fast-path refusal. (Changes SIB-3 to SIB-5, 12.1 cap 5, 12.2 and TIER-1.)
22. **None values in history.** A history whose winning fact is a none value is untrusted and goes to the slow path; no rule words it (HSEG-3).
23. **A re-dated or re-parented chunk is a new interval.** ING-8 extends a row only when `chunk_id`, `effective_date`, `expiry_date` and `parent_id` are all unchanged (the owner chose this in Step 7). Costs a few extra rows; keeps effective dates and parent lookups correct for every `target_ord`.
24. **Embedding batches are limited by characters as well as count.** Measured on the real chunks: at 64 texts per batch the longest texts raised the peak of ingest to 4.0 GB (budget 3 GB); at 8,000 characters per batch the model stays at its idle footprint with about the same throughput. Changes EMB-2; adds `embed.max_batch_chars` (the owner chose this in Step 7).
25. **Embedding cache.** Embedding the corpus takes about 8 minutes on a laptop CPU and `make verify` ingests it twice, so vectors are cached by model fingerprint and text (EMB-5). It does not change any vector, so accuracy is unchanged; it makes a repeat ingest about a minute. Whether the cache directory is shipped with the hand-in is left open (a shipped cache would make grading ingest faster, a missing one only costs time). Added after Step 7 at the owner's request.
26. **No temperature; Haiku 5.5 for extraction.** Current models take no sampling parameters on the default API version, so "temperature 0" (PRO-2, SLOW-1, DET-1) cannot be sent to the chosen models. Haiku 5.5 was kept for cost (about $2.70 against about $20 to $27 for Haiku 4.5 at temperature 0 on API version `2023-06-01`, measured in Step 8). Determinism of LLM output comes from the extraction cache only; DESIGN "Model choice and determinism" records the trade-off (the owner chose this in Step 8). Also: a measured extraction call takes about 1.4 s, and 24 parallel workers were rate limited (HTTP 429 and 503 on about 16% of calls), so retries became rate-limit aware (PRO-6, as amended).
27. **Conflicts use numeric facts only.** After Step 8 added prose facts, the real snapshots gave 482 conflicts, of which 379 were free-text `prose` facts and about 100 more eligibility, address, status, count, phone and hours text; only 3 were numeric. Several different sentences about one entity share one fact key (entity, attribute, no qualifiers), which is not a contradiction. Detection now keeps only facts with a numeric value (the owner chose this in Step 8). Costs: a planted contradiction in a text fact (for example two different opening hours) is not reported.
28. **Row labels drop apostrophes.** `Investor's Choice` becomes `Investors Choice`, not `Investor s Choice` (found in Step 8 when the row-label verifier rejected such rows). Other punctuation still separates words.
29. **Calibration data stays local.** Generated calibration questions quote entity labels and values from the snapshots, so they are written only to git-ignored `results/calibration/`; the committed artefacts are `calibration_report.json` (numbers only) and `config.json`. Changes 12.4 step 1, which said to commit them as fixtures (the owner chose this in Step 9, following CLAUDE.md and the test data policy).
30. **BM25 bound.** The similarity signal needs an upper bound for BM25 scores; it is the new key `confidence.bm25_bound`, set by calibration as the largest top-hit score on the training part, rounded up to 0.5 (the owner chose this in Step 9).
31. **No fast-path wording for `status` and `list` in Step 9.** A `status` fact has no stored phrase or date semantics that the template could use, and a list's completeness is not proven, so those questions hit hard caps 2 and 4 and wait for the slow path (refused until Step 10). A narrowing, not a widening, of SPEC 8.7 GR-2.
32. **Conflict note wording.** `strategy=<name>: <winner> used; <loser values> superseded`, built from the winning group at the target ordinal (CA-1); only a resolved conflict gets one.
33. **A generic "rate" question.** The attributes `rate`, `apy` and `apr` are tried in that order; the qualifier match counts only an exact attribute, so "rate" answered by an APY alone scores low and goes to the slow path (observed on deposit rows, kept: precision first).

34. **Retrieval retry.** A failed search is repeated once (a store hiccup may not repeat); a second failure is the safe refusal. A parse failure still reaches the slow path with the raw question (SPEC 16). Parsing never raised on 1,360 real and generated questions plus 24,000 random and mutated strings (Step 10).
35. **Page context for prose hits.** SPEC 6.3 returned a paragraph as is; in the data most prose chunks are one line of a card or section ("Lobby ...", an address), meaningless alone. The slow path adds up to three neighbours each side under the same heading, with the heading path (the owner approved this in Step 10).
36. **Deeper retrieval and query rewriting for the slow path.** Public recall at 20, 40 and 60 chunks was 0.79, 0.82 and 0.85; the vocabulary gap ("mortgage" against "Home Loans") left the table at rank 158. The slow path reads 60 chunks and adds up to three rewritten queries (the owner approved this in Step 10).
37. **Link targets are kept in prose.** Questions such as "where do I ..." need the address of the link; the parser dropped it. Chunk text changes for every paragraph or list item with a link, so those chunks were embedded and extracted again once (5,461 extraction calls, about $0.80).
38. **Slow-call settings.** `llm.slow_effort` (medium) bounds the model's reasoning time inside the 15 s deadline; the optional `reason` field records why the model declined, for diagnosis. The prompt states that an effective date earlier than the snapshot date does not make a value stale, and allows an answer scoped to one branch, product or program when the question is general. Chunk labels carry the effective and expiry dates, the prompt prefers the latest effective date and forbids mentioning older values unless the question asks how something changed, and embeddings ignore link targets (decision 37).

39. **Stale means replaced.** The first version of SRET-2 treated every non-winning value as stale and removed a chunk that carried the answer (two unrelated articles sharing a key, resolved by snapshot recency only). Staleness now needs evidence of replacement. Rate tables that give no facts are compared as generations of one table (decision above); on the real snapshots this finds exactly one stale table of 216.

## 22. Deferred to the implementation plan

* Exact `bge-small-en-v1.5` weight packaging and size (Step 7: fastembed's quantised ONNX build, about 64 MB, fetched once with `make fetch-model` into `submission/models/`).
* The measured ingest time and the extraction cache size.
* Entity names for prose facts: the model names one product differently on different pages ("6 month certificate", "certificates"), so equal facts get different fact keys and some contradictions are missed (found in Step 8; the selftest conflict is one). To be settled in Step 9 together with entity linking.
* The URL survey behind the authority rules.
* The final initial values for confidence weights, `tau_floor`, `tau_accept`, produced by calibration.
* Whether the query-time LLM cross-check (`flags.llm_crosscheck`) is switched on, decided by held-out precision (AC-9).
* The task breakdown, order of work and milestones.
40. **Collapse copies, then search deeper.** In the public set the page that states who may join ranked about 975th by BM25 because the words "become" and "member" occur in a closing paragraph copied onto hundreds of pages. Collapsing identical text before cutting each ranking removes that crowding without changing the fast path (the owner approved this in Step 10).
41. **Publication date as a label.** Branch counts and similar figures are repeated in press-release boilerplate that changes over time (24, then 23, then 22 branches). No table carries an effective date for them, so the model could not tell which was newest. The byline date of the page is shown as `published`; extending `effective_date` itself would have changed fact resolution for every news fact, so it was not done (the owner approved the label in Step 10).
42. **Rerank by a cheap model.** In four trials the page that states the join rules sat at positions 51 to 81 of the fused list and moved to position 1 after reranking. The reranker is optional (same fail-safe as query rewriting) and costs one short Haiku call. A bug found while measuring it: the payload builder sorted by the original fusion score and so discarded the reranked order; it now uses the slow path's own order.
43. **V2 is scoped to the key a loser belongs to.** A stale table's numbers were losers for every answer in the snapshot, so a correct checking-rate answer was refused because `0.00%` also appeared in an older table of another product (the one public refusal that V2 caused). Each loser now carries the chunks of the value that replaced it. The check fails closed: no scope, an empty scope, citing any chunk in the scope, or a number that no cited evidence prints all still reject. A bare small integer shortcut was considered and dropped: it would not have fixed this case and it weakens the guard for counts.
44. **Scoped answers instead of refusals, and printable label dates.** When the evidence answers only for particular branches, products or programs (hours listed branch by branch), the model gives what it states, names each scope and says it can vary, instead of refusing. The dates shown in a chunk label (effective, expires, published) are part of what the model read, so a cited chunk's label dates may be printed (V1); before, an answer that said "listed April 2024" failed V1.
45. **List numbering is not a number; spelled-out numbers ground digits.** V1 and V2 skip list markers (`(1)`, a line that starts `3.` or `4)`, and `1)` after a colon or sentence end), which are not values and had refused correct step lists (and collided with a stale `4` in V2). In the slow path, a number word in the cited evidence ("eight") grounds the same digit in the answer ("8"); the reverse is not applied, so a word such as "one" in an answer never needs grounding. A figure the evidence does not print, such as a computed "8 years", is still refused. See 19.5 for the held-out scoring bounds.
46. **Acceptance thresholds lowered to what Step 10 measured (owner decision).** AC-1 was 0.90 overall and 0.80 per category; it is now 0.80 overall and 0.70 per category, with 0.30 for `conflict_resolution`. AC-14 was a held-out gap of 0.03; it is now 0.15 on the close score. Evidence at the time: public accuracy 0.79 to 0.84 over five clean runs (about 0.04 run-to-run noise), conflict resolution 0.00 to 0.33, list and temporal 0.75, and a public-minus-held-out-close gap of 0.12 to 0.14 (held-out strict 0.51 to 0.54 is a floor). The change moves the bar to the achieved level; it is not evidence that the original bar was met. On the last measured run conflict resolution (0.00) is below its 0.30 floor, so AC-1 is met overall and not for that category. The overall line was later set to above 0.78 (owner decision, to match DESIGN.md's accuracy target); the per-category floors are unchanged.
47. **Latest date wins in prose, by instruction (owner decision).** A changing figure in prose (a branch count: 23 on pages up to March 2026, 22 from April 2026) is not an extracted fact, so fact resolution cannot rank it, and the model chose the older, more direct sentence in 9 of 10 fresh runs. Extracting such counts as facts was rejected: it changes the extraction prompt, invalidates the whole extraction cache (about 18,800 Haiku calls and a rebuilt shipped cache) and depends on naming the same entity identically on every page. Instead the slow-path prompt gets a first RECENCY rule (latest effective date, else publication date; not the most direct or most frequent statement; undated or silent evidence does not count against a dated value), and the payload lists the newest dated evidence. The publication date remains a label, never an `effective_date`, so DESIGN's resolution order for facts is unchanged. Measured: 10 of 12 fresh runs of the branch-count question give the latest value (22), against 1 of 10 before; the rule is an instruction to a model and can still fail.
48. **The newest copy of a changing sentence is promoted, not the older copies dropped.** The branch-count question failed whenever the April 2026 page (22) ranked below the 12,000 character cap, because older copies of the same closing paragraph (23) filled it; in 5 of 10 fresh runs the newest page was not shown at all, so the RECENCY rule could not help (decision 47). Dropping older copies was built and measured first: it matched nothing in 12 real runs, because the copies are surrounded by different wording on different pages, and it could hide the earlier figure from change questions. Promotion works per sentence and removes nothing, so history and as-of questions keep all evidence. Measured: 11 of 12 fresh runs of the branch-count question give 22 (1 of 10 before decision 47, 9 of 12 with the RECENCY rule alone); the one miss had no older copy in the retrieved set.
49. **Latency target relaxed (owner decision).** The design aimed at a P99 of 1 to 2 s on the common path. The common path on real wording is the slow path: the fast path answered none of the 40 public questions, so p50 is 6.8 to 7.4 s, p95 8.1 to 10.5 s and the slowest answer 10.3 to 12.4 s (five clean runs), and the speed score is about 0.53 to 0.66. The owner weighs accuracy above latency, so the target is now that every answer stays under the 20 s ceiling (slow-path deadline 15 s). The fast-path budgets stay as design budgets for a question the fast path does answer (decision stage measured at a median of 129 ms). Two runs showed stalls above the deadline (a full disk, and one of 115 s of unknown cause); that is open, not solved.
