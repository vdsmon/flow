# Stage: code_review

## Purpose

Inline main-agent self-review of the implement-stage diff.
Bare workspace default; richer review is wired by installing a code-review skill via the init wizard.

This is the lowest-cost gate against regressions.
The main agent is the same context that just produced the implement-stage code, so the review is biased toward what it just wrote.
That bias is acceptable for personal-mode flow; work-mode users opt in to `skill:code-review` via init.

## Inputs

- `<ticket-dir>/state.json` — `stages.implement.started_at_sha` for the diff range.
- The current working tree (uncommitted changes from the implement stage).

## Steps

1. Pull the implement-stage diff:
   ```bash
   ${CLAUDE_SKILL_DIR}/scripts/diff_extract.py since-stage \
     --stage implement \
     --ticket <KEY> \
     --ticket-dir <ticket-dir> \
     --cwd .
   ```
   - Exit 0 → JSON with `files_touched / insertions / deletions / binary`.
   - Exit 1 → no started_at_sha (implement didn't run).
     Abort with status=failed; rerun `/flow do --stage implement` first.
   - Exit 2 → git error. Surface stderr.

   **Empty `files_touched` is expected, not "nothing to review".** `since-stage` diffs the committed range `started_at_sha..HEAD`, but implement leaves its work UNCOMMITTED (the commit stage runs later), so `started_at_sha == HEAD` and the committed range is empty. The real change is in the working tree. When `files_touched` is empty, get the actual file list from the working tree instead: `git diff HEAD --name-only` (or `git status --porcelain`). Only treat the stage as a genuine no-op if the working tree is also clean.

2. For each file (from `files_touched`, or the working-tree list above when `since-stage` was empty), Read the file and read the diff via `git diff <started_at_sha> -- <path>` (no `..HEAD`, so it includes the uncommitted working tree).
   Assess for:
   - Obvious bugs (off-by-one, null-deref, missing await, etc.).
   - Regressions in nearby tests not updated by implement stage.
   - Style violations against existing file conventions.
   - Security-sensitive patterns (eval, raw SQL, missing escape).

3. Report findings inline as a structured list:
   - **Critical** — blocks the stage (status=failed).
   - **Major** — should fix but not blocking.
   - **Minor** — nitpick / style.

4. If any Critical finding: abort stage with status=failed.
   Surface the finding so the user can decide between rerunning implement vs overriding.

5. Otherwise: stage completes with status=completed.
   Major/Minor findings are logged but do not block.

## Outputs

- No file outputs. Findings are surfaced inline to the user.

## Errors

- `diff_extract.py` exit 1 → implement stage never ran.
- `diff_extract.py` exit 2 → git environment broken; abort.

## Skip conditions

- Skipped entirely if `workspace.toml [pipeline.handlers] code_review =
  "none"`.
- Replaced if `workspace.toml [pipeline.handlers] code_review =
  "skill:<name>"` — dispatcher dispatches the skill instead.
