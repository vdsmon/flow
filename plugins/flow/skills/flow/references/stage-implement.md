# Stage: implement

## Purpose

Implement the ticket against its approved plan using strict TDD, and report only when the tests are green.
You are the `general-purpose` subagent for the `implement` stage of `/flow`.
This stage absorbs the old separate test stage: you write the production code AND the unit tests in one pass.

TDD discipline is MANDATORY.
Write or update the tests that pin the new behavior, watch them fail, make them pass with the smallest sufficient change, then confirm the whole relevant suite is green before you return.

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
- The project's test command — discover it from the repo (pyproject /
  package.json / Makefile / mise / existing CI config).

## Steps

1. Read `plan.out` if present, else the ticket context.
   Pin down the exact behavior to build and the test cases that prove it.

2. Confine edits to the planned files.
   The set comes from the plan's "files to change" and the frontmatter `planned_files`.
   The dispatcher recorded a diff baseline BEFORE this stage ran, and the commit stage enforces content ownership against it — edits to files outside the planned set will be rejected downstream.
   If you discover a file you genuinely must also touch (a package `__init__.py`, a `.gitignore` rule, a config), add it and call it out PROMINENTLY in your report with one line on why. Files outside the planned set are NOT silently committed: the commit stage stages from a diff captured over `planned_files` only, so anything you add that is not in that set vanishes from the commit unless the orchestrator expands the set. Naming it in the report is what lets the orchestrator widen `planned_files` before commit.

   **Committability check — do NOT skip for fixture / data / generated files.** Before you finish, confirm every file you expect to be committed is actually trackable: run `git check-ignore -v <path>` on each new fixture, data, or generated file. The repo root often ignores broad patterns (e.g. `**/*.csv`), so a planned fixture can be silently ignored. A gitignored planned file is worse than missing: the commit stage's `capture-implement-diff` runs `git add --intent-to-add` on untracked planned paths, and that command HARD-FAILS (non-zero) on an ignored path, aborting the whole commit stage with a confusing git error. If a file you need committed is ignored, add the narrowest negation rule that un-ignores it (mirror any existing sibling negation, e.g. an `expected/*.csv` rule already in `.gitignore`), add `.gitignore` to your touched-files set, and flag it.

   **Pre-flight the commit gate (recommended).** Once tests are green, dry-run what the commit stage will do, so a packaging problem surfaces here instead of at commit: `diff_extract.py capture-implement-diff --ticket <KEY> --ticket-dir <ticket-dir> --cwd .` then `git apply --cached --check --binary <ticket-dir>/implement.diff`. (`capture-implement-diff` takes only `--ticket`/`--ticket-dir`/`--cwd`, NOT `--stage`.) If the captured diff is missing a file you created, or the check fails, you have an unowned/ignored file to reconcile (above) before finishing.

   **Definition of done is the whole change, not just code + tests.** Whatever this class of change conventionally ships alongside the code lands in THIS commit: the committed fixture, a short provenance / synthetic-data note for a NEW test fixture, a doc stub the repo expects per existing siblings. Check what comparable existing code carries (e.g. a sibling fixture dir's `provenance/` or `README`) and match it. This is the only point in the pipeline where completeness is free: reflect runs after the PR is open, so any artifact discovered missing later costs a new commit that re-triggers the entire CI + review loop. Completeness caught after the PR opens is completeness caught too late.

3. Write the failing test(s) first.
   Add or update unit tests that encode the new behavior.
   Run them and confirm they fail for the right reason.

4. Implement the production code.
   Smallest change that makes the tests pass.
   Match the surrounding file's style and conventions.

5. Run the project's full relevant test suite (not only your new tests).
   Iterate until green.
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
