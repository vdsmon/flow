# Stage: implement

## Purpose

Implement the ticket against its approved plan using strict TDD, and report only when the tests are green.
You are the `general-purpose` subagent for the `implement` stage of `/flow`.
This stage absorbs the old separate test stage: you write the production code AND the unit tests in one pass.

TDD discipline is MANDATORY.
Write or update the tests that pin the new behavior, watch them fail, make them pass with the smallest sufficient change, then confirm the whole relevant suite is green before you return.

**Express-lane relaxation (only when frontmatter `lane: express`).** An `express` run is a producer-stamped `tier:trivial` bead — vetted behavior-preserving (a doc-drift fix, a proven-dead-code deletion, a comment correction). For these a *new* test is NOT mandatory: there is no new behavior to pin, and authoring one is the redundant work the lane removes. You MUST still (a) run the whole relevant existing suite and confirm it stays green, and (b) write the test anyway if your change turns out to touch behavior after all — in which case treat the run as ordinary TDD, the relaxation does not apply. The net for an express run is existing-suite-green + the unchanged CI + review-bot review at the tail. `lane: light` (and the absent/`full` case) keep full mandatory TDD — a `tier:light` finding can be behavior-changing, so it needs the pinning test.

## Revision mode (a revision sub-run)

When `<ticket-dir>` contains `/revisions/`, this is a **revision** (see `references/verb-revise.md`): there is no `plan.out` (a revision has no plan stage). The fix SOURCE is, in order:

1. `<ticket-dir>/instruction.md` if it exists — a free-text change-request the maintainer gave to `/flow revise`. Its text IS the work to do; treat it as the plan.
2. else the PR's unresolved human review threads as the Major+ fix set. Resolve the PR from the branch and fetch its threads through the forge seam:
   ```bash
   PR_ID=$(python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . detect-pr --branch "$(git rev-parse --abbrev-ref HEAD)" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("id","") if d else "")')
   python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . review-threads --pr "$PR_ID"
   ```
   The unresolved Major+ threads (each carries `file` / `line` / `title` / `body`) are the work list.
3. if BOTH are absent (no instruction, no unresolved Major+ thread) → there is nothing to revise. Finish this stage `completed` with a one-line "no actionable revision input" note; the review_loop terminal then passes on the already-green PR.

Apply the fix with the same TDD discipline where a behavior change is involved (add or adjust the test that pins it). The implement subagent's "plan" is the instruction text / the thread list.

`planned_files` / baseline: the do-loop's `records_diff_baseline` pre-hook reads `planned_files` from the shared frontmatter `.flow/tickets/<KEY>.md` — for a revision that is the ORIGINAL run's planned set, i.e. the PR's own files, which is the right starting baseline (a review comment mostly touches the PR's files). WIDEN via the existing post-implement reconcile (Steps below) for any new file the fix needs. No new baseline mechanism.

The normal-run Steps below apply once the fix source is in hand.

You do NOT commit.
The commit stage owns staging, the commit message, and the tracker transition.
Leave your work as uncommitted changes in the working tree.

## Inputs

- `<ticket-dir>/stages/plan.out` — the approved implementation plan (files to
  change, approach, test strategy, risks).
  Read it if present and follow it.
  The plan stage is optional; if `plan.out` does not exist, work from
  `.flow/runs/<KEY>/ticket.json` + `.flow/tickets/<KEY>.md` directly.
- `.flow/runs/<KEY>/ticket.json` — full ticket context.
- `.flow/tickets/<KEY>.md` — frontmatter, including `planned_files`.
  Your edits must stay within this set (see Steps).
- The project's test command AND its lint/format/type-check gate — discover both
  from the repo (pyproject / package.json / Makefile / mise / existing CI config).

## Steps

1. Read `plan.out` if present, else the ticket context.
   Pin down the exact behavior to build and the test cases that prove it.

2. Confine edits to the planned files.
   The set comes from the plan's "files to change" and the frontmatter `planned_files`.
   The dispatcher recorded a diff baseline BEFORE this stage ran, and the commit stage enforces content ownership against it — edits to files outside the planned set will be rejected downstream.
   If you discover a file you genuinely must also touch (a package `__init__.py`, a `.gitignore` rule, a config), add it and call it out PROMINENTLY in your report with one line on why. Files outside the planned set are NOT silently committed: the commit stage stages from a diff captured over `planned_files` only, so anything you add that is not in that set vanishes from the commit unless the orchestrator expands the set. Naming it in the report is what lets the orchestrator widen `planned_files` before commit.

   **Committability check — do NOT skip for fixture / data / generated files.** Before you finish, confirm every file you expect to be committed is actually trackable: run `git check-ignore -v <path>` on each new fixture, data, or generated file. The repo root often ignores broad patterns (e.g. `**/*.csv`), so a planned fixture can be silently ignored. A gitignored planned file is worse than missing: the commit stage's `capture-implement-diff` runs `git add --intent-to-add` on untracked planned paths, and that command HARD-FAILS (non-zero) on an ignored path, aborting the whole commit stage with a confusing git error. If a file you need committed is ignored, add the narrowest negation rule that un-ignores it (mirror any existing sibling negation, e.g. an `expected/*.csv` rule already in `.gitignore`), add `.gitignore` to your touched-files set, and flag it.

   **Binary `planned_files` are orchestrator-copied, not subagent-written.** You emit text only — `Write`/`Edit` produce UTF-8 — so a binary deliverable in `planned_files` (an `.xlsx` template, an image, a compiled fixture) is one you CANNOT produce here. Do not try to fabricate it as text. Flag every binary `planned_files` entry PROMINENTLY in your report; the orchestrator copies it into the worktree post-implement, before the commit gate's `capture-implement-diff` (the post-implement reconcile in `references/verb-do.md`). The committability + pre-flight checks then see it as an ordinary addition.

   **Inline-implement path discipline.** When this stage runs inline (handler `inline`) instead of via a spawned subagent, the orchestrator follows this prose directly, so ITS `Edit`/`Write` must target worktree-absolute (or worktree-relative) paths, never main-checkout-absolute. Nothing validates the write target: an absolute repo-root path silently lands the edit on the main checkout's working tree, invisible until a later step (pytest) cannot find the file (flow-cjgy / flow-8sk4). The spawned-subagent path already carries this discipline (the subagent's cwd is pinned at the repo root, so it falls back to Bash/Python string-replace against worktree-absolute paths, per `references/verb-do.md`); this callout brings the same rule to the inline executor.

   **Pre-flight the commit gate (recommended).** Once tests are green, dry-run what the commit stage will do, so a packaging problem surfaces here instead of at commit: `diff_extract.py capture-implement-diff --ticket <KEY> --ticket-dir <ticket-dir> --cwd .` then `git apply --cached --check --binary <ticket-dir>/implement.diff`. (`capture-implement-diff` takes only `--ticket`/`--ticket-dir`/`--cwd`, NOT `--stage`.) If the captured diff is missing a file you created, or the check fails, you have an unowned/ignored file to reconcile (above) before finishing. Run the apply-check with a clean index (index == HEAD): `git apply --cached --check` validates against the index, so anything already staged — e.g. `git rm` on a deletion ticket, which stages the deletion — makes the check fail with a misleading `does not exist in index` error even though the patch is fine. `git reset` first if you staged anything; staging is never needed, because `capture-implement-diff` diffs the working tree against the baseline (and `git reset` leaves the working-tree deletion intact).

   **Definition of done is the whole change, not just code + tests.** Whatever this class of change conventionally ships alongside the code lands in THIS commit: the committed fixture, a short provenance / synthetic-data note for a NEW test fixture, a doc stub the repo expects per existing siblings. Check what comparable existing code carries (e.g. a sibling fixture dir's `provenance/` or `README`) and match it. This is the only point in the pipeline where completeness is free: reflect runs after the PR is open, so any artifact discovered missing later costs a new commit that re-triggers the entire CI + review loop. Completeness caught after the PR opens is completeness caught too late.

   **Do NOT bump the plugin version here.** The plugin version is no longer bumped per-PR; it is stamped post-merge on `main` by the server-side `version-stamp.yml` GitHub Action (which runs `version.py stamp`). The implement stage does NOT touch `plugins/flow/.claude-plugin/plugin.json` or `.claude-plugin/marketplace.json`.

3. Write the failing test(s) first.
   Add or update unit tests that encode the new behavior.
   Run them and confirm they fail for the right reason.

4. Implement the production code.
   Smallest change that makes the tests pass.
   Match the surrounding file's style and conventions.

   **Comment density follows the user's CLAUDE.md, not the file's existing habit.** Default to no comments, and add one only for a real non-obvious WHY. A file with bloated comments already in it doesn't excuse more of the same, so before you return, reread every comment you just wrote and cut anything that restates the code or narrates the diff.

5. Run the project's FULL CI-equivalent gate before declaring green — not just the tests.
   Discover the gate the same way you discover the test command (CI config / mise / package.json / Makefile), and run every part CI runs:
   - the linter;
   - the formatter in CHECK mode (e.g. `--check`) — call this out as a DISTINCT step: a file can pass the linter yet still be reformatted by CI, so lint-clean does not mean format-clean;
   - the type-checker, if the project runs one;
   - the project's full relevant test suite (not only your new tests).
   (For this repo the gate is `mise run lint` = ruff check + ruff format --check + ty, plus `mise run test`, run from the scripts dir — that is an example, not a default; use whatever THIS project's CI enforces.)
   Iterate until the whole gate is green.
   Do not return on red.

6. Report what changed: the files touched, the tests added or updated, and the final test run result (command + pass summary).
   If you stepped outside the planned files, say so prominently.
   Return this as your response.

## Outputs

- Uncommitted code + test changes in the working tree.
- A report of what changed plus the green test results, returned as your stage report.
  The do-loop captures it to `<ticket-dir>/stages/implement.out`; you do not write that file yourself.
  The commit stage separately extracts the diff against the recorded baseline.

## Errors

- Tests cannot be made green → do NOT return success.
  Report the failing cases, what you tried, and the blocking cause, then return with the stage unfinished so the user can intervene.
  A red suite is a failed stage.
- Project test command not discoverable → report that you could not locate a test runner; surface what you looked for.
  Do not silently skip tests.
- The change needs files outside `planned_files` → include them, but flag the expansion in your report.
  Silent scope creep gets rejected at commit.

## Skip conditions

- Skipped entirely if `workspace.toml [pipeline.handlers] implement = "none"`.
  In that case the do-loop short-circuits and this doc is never read.
  (Bare workspaces always run implement; `none` is a rare configuration.)
