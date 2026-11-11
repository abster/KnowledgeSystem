# Prompt: implement the next step in a fresh Claude session

Usage: start a new Claude Code session at the repository root and paste everything below the
line. The session finds the first unticked step in the Progress checklist of
`IMPLEMENTATION_STEPS.md`, implements it, and ticks the box after you approve and the work is
committed. `PROGRESS.md` is created by the first session that finishes a step; it carries
context between sessions.

---

You are implementing the next step of the Knowledge System in this repository. This is an
independent session: you have no memory of earlier work, so build your context from the
files below, not from assumptions.

## 1. Find your step
Open the "Progress" checklist in IMPLEMENTATION_STEPS.md. Your step is the **first unticked
box**. Call its number N. If every box is ticked, say so and stop. If a later box is ticked
while an earlier one is not, stop and tell me.

## 2. Read first (in this order, fully)
1. CLAUDE.md (if it does not exist yet, N is 1; skip it)
2. IMPLEMENTATION_STEPS.md: sections 1, 2, 4 and the text of Step N. Also read the
   "Exit criteria" of Step N-1 and any entries for earlier steps in PROGRESS.md (if it exists).
3. SPEC.md and DESIGN.md: only the sections that Step N lists, plus SPEC 0 (conventions).
   If they disagree with each other or with IMPLEMENTATION_STEPS.md, stop and tell me.
4. `git log --oneline` and `git status`, to see what is already done.

## 3. Scope
* Do only Step N. Do not start later steps, and do not "tidy" earlier ones except where
  Step N says so.
* Check that the Step N-1 work is in the repo and `make verify` passes before you begin.
  If something a step depends on is missing, stop and tell me.

## 4. Process (the two project skills are mandatory)
Use `design-drift-detection` and `verification-first-change`. In order:
1. Drift check: say which DESIGN and SPEC sections you checked and that the plan is
   consistent with them.
2. Baseline before any edit: `make verify` and the evals the step names, saved under
   `results/`, with the environment recorded (git SHA, API key present or not, extraction
   cache present or not).
3. Write the tests listed for the step first. Show that they fail for the right reason.
4. Implement.
5. Run `make verify` and the same evals again.
6. Report to me: the full `git diff`, every command you ran with its result, a before/after
   table, and which exit criteria are met. If any metric regressed, say which questions
   changed and your best explanation.
7. Stop and wait for my decision. Commit only after I explicitly approve. If a metric
   regressed, commit only if I approve that regression, and name it in the commit message.
   Failing tests, lint or types are never approvable.

## 5. Rules that apply to every step
* Ask me when something is unclear or the docs leave a choice open. Do not make silent
  assumptions. Use short numbered questions with a recommended option.
* Follow SPEC.md exactly. If you think the spec is wrong, report the drift with a proposed
  next step and alternatives, and let me choose.
* Never edit `harness/`, `eval/` or the snapshots. Never commit snapshot text, `.env`, the
  extraction cache, model weights or API keys. Committed tests use synthetic fixtures.
* The API key is in `.env`. Do not read, print, log or paste it. Test only that it exists
  or works.
* Never tune thresholds, patterns or prompts against the public eval questions. A gain
  concentrated in one or two questions is suspect; say so.
* Keep the diff small and focused. One reviewable change per step; several commits are fine
  if each passes `make verify`.
* Commit identity: use `-c user.name="Abhijeet Kumar" -c user.email=abhijeet.kumar61087@gmail.com`
  for each commit. End the message with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## 6. Finishing
After I approve and you commit the step's work:
1. Tick the box for Step N in the Progress checklist of IMPLEMENTATION_STEPS.md (change
   `- [ ]` to `- [x]`). Tick nothing else.
2. Add a short entry to PROGRESS.md: step number, commit SHA(s), Eval A and Eval S numbers,
   any approved regressions, deviations from the docs, and open items for the next step.
3. Commit those two files together as a final commit ("Mark Step N done").
4. Stop. Do not begin the next step.

If I do not approve the step, leave the box unticked.

Start by finding your step and reading the files, then tell me in a few lines what the step
will change and which questions, if any, you need answered before you begin.
