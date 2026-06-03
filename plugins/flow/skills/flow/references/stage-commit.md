# Stage: commit

## Purpose

Compose a conventional commit, apply the recorded implement-stage diff, and transition the tracker ticket.
Bare workspace default.

The commit message header is deterministic (built by `compose_commit.py`); the body is filled in by the main agent based on the implement-stage context.
The applied patch comes from the recorded `implement.diff` — NOT from `git add .` — so unrelated edits in the working tree are NOT included.

## Inputs

- `<ticket-dir>/baseline.json` — written by implement-stage's pre-handler
  `record-baseline` hook.
- `<ticket-dir>/implement.diff` — the captured implement-stage diff (binary
  + raw).
- `.flow/tickets/<KEY>.md` — ticket frontmatter (needs
  `commit_type` + `commit_summary` fields per `lint_ticket` HARD GATE; these
  feed `compose_commit.py` in step 3).
- Current working tree.

## Steps

1. HARD GATE: validate ticket frontmatter has `commit_type` + `commit_summary`
   (the fields `compose_commit.py` consumes in step 3):
   ```bash
   ${CLAUDE_SKILL_DIR}/scripts/lint_ticket.py \
     --stage commit \
     --ticket-path .flow/tickets/<KEY>.md
   ```
   - Exit 0 → continue.
   - Exit 1 → frontmatter missing a required field.
     Surface stderr; ask user to populate `commit_type` + `commit_summary` in `.flow/tickets/<KEY>.md` then rerun.
     Abort with status=failed.

2. Capture the implement-stage diff (idempotent if already captured):
   ```bash
   ${CLAUDE_SKILL_DIR}/scripts/diff_extract.py capture-implement-diff \
     --ticket <KEY> \
     --ticket-dir <ticket-dir> \
     --cwd .
   ```
   - Exit 0 → `<ticket-dir>/implement.diff` exists.
   - Exit 1 → no baseline.
     Abort; surface `/flow recover --reset-baseline` hint.
   - Exit 2 → git error. Abort.

3. Compose the commit skeleton.
   Read `commit_type` + `commit_summary` from the ticket frontmatter (or ask the user if missing):
   ```bash
   ${CLAUDE_SKILL_DIR}/scripts/compose_commit.py \
     --ticket <KEY> \
     --type <feat|fix|chore|...> \
     --summary "<short summary>" \
     [--scope <scope>] \
     [--files <comma-list-from-baseline.planned_files>] \
     > /tmp/flow-commit-<KEY>.txt
   ```
   - Exit 0 → commit skeleton at `/tmp/flow-commit-<KEY>.txt`.
   - Exit 1 → empty/whitespace `--summary` or `--ticket`. Abort.
   - Exit 2 → invalid `--type` (not in the allowed set) or a missing
     required flag (argparse usage error). Abort and fix the invocation.

4. Fill in the body.
   Read the skeleton, append a body section describing *why* (not what — the diff shows what).
   Reference any failing-tests-now-green progress from implement stage.
   Write the completed message back to the same path.

5. Apply the recorded patch:
   ```bash
   git apply --cached --binary <ticket-dir>/implement.diff
   ```
   If apply fails:
   - The working tree drifted from the baseline. Surface the error.
   - Abort with status=failed; `/flow recover --reapply-implement` (phase 8c).

6. Commit:
   ```bash
   git commit -F /tmp/flow-commit-<KEY>.txt
   ```

7. Transition the tracker ticket:
   ```bash
   ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py \
     --workspace-root . \
     transition --key <KEY> --to-state in_review
   ```
   The commit already landed in git before this step, so a *transient* tracker failure must not fail the stage.
   A *hard* failure (permission / validator / wrong-state) must, because it means the transition will never succeed without intervention.
   Read the printed JSON for `failure_kind` + `failure_detail`.
   Exit-code handling:
   - Exit 0 → continue. Stage completes.
   - Exit 1 → transient/unknown tracker error (network / auth / retryable, or
     an unmapped `failure_kind`).
     Commit is already made; log a warning surfacing `failure_kind` + `failure_detail` from the printed JSON if present, else the stderr message (a raised `TrackerError` prints to stderr with no stdout JSON).
     Continue; stage completes (not status=failed — the diff is in git, the ticket transition is best-effort under transient faults).
   - Exit 2 → workspace config invalid.
     Surface stderr; do not retry.
     Mark the stage status=failed (workspace is misconfigured, not a tracker hiccup).
   - Exit 3 → no transition to `in_review` available (workflow lacks it).
     Try `--to-state done` as fallback.
     If the fallback also returns exit 3, surface and continue with a warning (commit is in git).
     Any other exit code from the fallback is handled by its own rule below.
   - Exit 4 → hard failure (`permission_denied` / `validator_failed` /
     `missing_required_field`).
     Do NOT swallow and do NOT try the `done` fallback.
     Surface `failure_kind` + `failure_detail` and mark the stage status=failed.
   - Exit 5 → not applicable (`wrong_source_state` / `ambiguous_transition`).
     Do NOT swallow.
     Surface `failure_kind` + `failure_detail` and mark the stage status=failed.

## Outputs

- A git commit on the current branch.
- `.flow/tickets/<KEY>.md` — frontmatter stays unchanged (status mutation
  belongs to ticket / reflect stages, not commit).

## Errors

- `lint_ticket.py` exit 1 → user must populate `commit_type` +
  `commit_summary` frontmatter.
- `git apply --cached` fail → working tree drift. `/flow recover` in 8c.
- `tracker_cli.py transition` exit 1 → transient; log warning, do not block.
  The commit is the source of truth.
- `tracker_cli.py transition` exit 3 → no `in_review` transition; try `done`,
  else warn and continue.
- `tracker_cli.py transition` exit 2 / 4 / 5 → hard stop. Surface
  `failure_kind` + `failure_detail`; mark stage status=failed.

## Skip conditions

- Skipped entirely if `workspace.toml [pipeline.handlers] commit = "none"`.
  (Bare workspace never sets this; rare configuration.)
