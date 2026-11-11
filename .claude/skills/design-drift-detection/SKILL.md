---
name: design-drift-detection
description: Checks every change against DESIGN.md before and after it is made. Use before editing any code, tests, configuration, Docker files, tooling or documentation in this repository, and whenever a spec, plan or instruction seems to differ from DESIGN.md. If a change would make the system differ from the design, stop, surface the drift with a proposed next step and alternatives, and ask the user to choose. Supersedes the user-level design-drift-prevention skill in this repository.
---

# Design drift detection

DESIGN.md is the source of truth for how the knowledge system behaves. Code that quietly
differs from it makes the document wrong. Never resolve a discrepancy silently in either
direction: surface it and let the user decide.

This skill supersedes the user-level `design-drift-prevention` skill for this repository
(that one was written for a different project).

## Before making a change

1. Read the DESIGN.md sections relevant to the change. Do not rely on memory or on a
   summary; the document may have changed. Typical sections:
   * `as_of` Handling: the date comes only from the `as_of` field, `target_ord`, the
     `first_seen_ord` / `last_seen_ord` filter.
   * Vector Database, Tables, Chunking Strategy, Embeddings.
   * Fact Model and Conflict Detection: conflict scope (coexisting contradictions only),
     the resolution order (effective_date, source_authority, snapshot_recency, unresolved).
   * Table Fact Extraction: precision-first principle, header lexicon, plausibility gates.
   * Query Path: cache, fast and slow paths, failure matrix, confidence thresholds,
     validators, templates, polish (off by default).
   * Technology Choices and Docker.
2. If a spec or plan exists for the step, read it as well. If it disagrees with DESIGN.md,
   that is itself a discrepancy: report it before writing code.
3. State which sections you checked and that the planned change is consistent with them.

## What counts as drift

A change drifts if the system would behave or be structured differently from DESIGN.md:

* a different source for the target date (for example parsing a date out of the question
  text), or a different snapshot filter,
* changing the conflict definition, the resolution order or the wording of the strategy
  named in `Conflict.reason` / `Answer.conflict_note`,
* loser or stale values appearing in `Answer.text`, or refusals containing digits,
* emitting a fact when the design says to skip (a looser header match, skipping a
  plausibility gate),
* a different library, vector store, embedding model or LLM tier than Technology Choices,
* a new runtime server dependency (the design is embedded and in-process),
* changing a template, threshold policy or failure outcome in the failure matrix,
* adding scope the design does not describe, or leaving out scope it does.

Details the design does not specify (internal names, helper structure, log wording) are not
drift. Pick something consistent with the design and mention any notable choice in your
summary.

## When you find drift

Do not make the drifting change. Present it, then ask the user to choose with the
AskUserQuestion tool:

```
Design drift detected

Design says: <quote or precise paraphrase, with the DESIGN.md section name>
The change would: <what the code would do instead>
Why this came up: <what made the design hard to follow, or what new information suggests it is wrong>

Proposed next step: <your recommendation and why>
Alternatives:
  A. Keep the design: <how the change is done to match DESIGN.md, and its cost>
  B. Update the design: <the exact DESIGN.md edit, and why it is better>
  C. Other: <any third option, if there is one>
```

* If the user chooses **A**, redo the change so it matches DESIGN.md.
* If the user chooses **B**, update DESIGN.md first, or in the same commit as the code, so
  the two never disagree in a committed state. Mention the design change in the commit
  message.
* If the user chooses **C**, do what they describe and re-check it against this skill.

Report drift even when you are confident the design is wrong. The user may know something
you do not.

## After a change

Before committing, re-read the relevant sections and compare them with what you wrote. If
anything differs, treat it as drift and follow the steps above. Work together with the
`verification-first-change` skill: the design check comes first, the evaluation gate second.
