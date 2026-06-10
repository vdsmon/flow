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
     Abort; recover via `/flow recover <KEY>` → `retry --stage implement` (its records_diff_baseline pre-hook re-records the baseline).
   - Exit 2 → git error. Abort.

2b. Content-ownership gate. Verify the working tree carries only planned changes before the commit is composed — a PR must hold only what was planned. `planned_files` has already been widened by the post-implement reconcile, so a legitimately-touched file is owned by now; anything still outside it is unplanned and must not ride along.
   ```bash
   ${CLAUDE_SKILL_DIR}/scripts/diff_extract.py check-ownership \
     --ticket <KEY> \
     --ticket-dir <ticket-dir> \
     --cwd .
   ```
   - Exit 0 → ownership clean; continue.
   - Exit 3 → ownership violation. The printed JSON's `unowned_changes` lists files changed outside `planned_files`. Do NOT commit. Surface the unowned files and resolve by either (a) adding genuinely-needed files to the plan and re-recording the baseline (`record-baseline --files ...` — the reconcile path), or (b) reverting the stray edit; then rerun. If it cannot be resolved, abort with status=failed. Never commit past an unowned change, and never crash on it — exit 3 is a clean refusal to act on, not a fault.
   - Exit 1 → no baseline. Abort; `/flow recover <KEY>` → `retry --stage implement` (re-records the baseline).
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
     > "${TMPDIR:-/tmp}/flow-commit-<KEY>.txt"
   ```
   - Exit 0 → commit skeleton at `${TMPDIR:-/tmp}/flow-commit-<KEY>.txt`.
   - Exit 1 → empty/whitespace `--summary` or `--ticket`. Abort.
   - Exit 2 → invalid `--type` (not in the allowed set) or a missing
     required flag (argparse usage error). Abort and fix the invocation.

4. Fill in the body.
   Step 3 created `${TMPDIR:-/tmp}/flow-commit-<KEY>.txt` via a shell redirect, so the file lives OUTSIDE the harness Read/Write tool tracking. The Write tool refuses to overwrite a path it has not Read in-session ("File has not been read yet"), which otherwise leaves the literal `# body - fill in below this line` skeleton in the commit.
   Use the **Read tool** on `${TMPDIR:-/tmp}/flow-commit-<KEY>.txt` FIRST to register the path with the harness.
   Then append a body section describing *why* (not what — the diff shows what), referencing any failing-tests-now-green progress from implement stage.
   Then use the **Write tool** to write the completed message back to that same path.

5. Apply the recorded patch:
   ```bash
   git apply --cached --binary <ticket-dir>/implement.diff
   ```
   If apply fails:
   - The working tree drifted from the baseline. Surface the error.
   - Abort with status=failed; `/flow recover <KEY>` → `retry --stage implement` (re-records the baseline against the current tree, then commit re-applies cleanly).

6. Commit:
   ```bash
   git commit -F "${TMPDIR:-/tmp}/flow-commit-<KEY>.txt"
   ```

7. Transition the tracker ticket to `in_review`.
   **MCP-first:** when the Atlassian MCP is available, transition via it (`transitionJiraIssue`) — auth-fresh, no env creds needed, the primary path in an attached run (what production already does). **REST fallback** when the MCP is absent (a backgrounded / headless run) or for beads:
   ```bash
   ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py \
     --workspace-root . \
     transition --key <KEY> --to-state in_review --enqueue-on-transient
   ```
   The commit already landed in git before this step, so a *transient* tracker failure must not fail the stage.
   A *hard* failure (permission / validator / wrong-state) must, because it means the transition will never succeed without intervention.
   Read the printed JSON for `failure_kind` + `failure_detail`.
   Exit-code handling:
   - Exit 0 → continue. Stage completes.
   - Exit 1 → transient/unknown tracker error (network / auth / retryable, or
     an unmapped `failure_kind`).
     Commit is already made; log a warning surfacing `failure_kind` + `failure_detail` from the printed JSON if present, else the stderr message (a raised `TrackerError` prints to stderr with no stdout JSON).
     `--enqueue-on-transient` has durably QUEUED the transition to `.flow/pending-mutations.jsonl`; `/flow sync` reconciles it against live tracker state on the next run (no longer logged and dropped).
     Continue; stage completes (not status=failed — the diff is in git, the ticket transition is best-effort under transient faults).
   - Exit 2 → workspace config invalid.
     Surface stderr; do not retry.
     Mark the stage status=failed (workspace is misconfigured, not a tracker hiccup).
   - Exit 3 → no transition to `in_review` available (the tracker has no review state — e.g. beads exposes only `in_progress | blocked | closed`).
     Do **NOT** fall back to `--to-state done`: closing the ticket at commit is premature in a PR-based flow (the PR is not merged yet, and `create_pr` / `review_loop` still run after this stage). Closing here strands the ticket as "done" while review is pending.
     Instead leave the ticket in its current state (`in_progress`), log a warning naming the missing `in_review` transition, and continue (the commit is already in git; a human or a later merge step closes the ticket). The ticket stays open and truthful about where the work actually is.
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
- `diff_extract.py check-ownership` exit 3 → changes outside `planned_files`;
  do NOT commit. Reconcile the plan (`record-baseline --files ...`) or revert
  the stray edit, then rerun. Fail-safe: a clean refusal, never a silent commit.
- `git apply --cached` fail → working tree drift. `/flow recover <KEY>` → `retry --stage implement` re-records the baseline.
- `tracker_cli.py transition` exit 1 → transient; log warning, do not block.
  The commit is the source of truth. `--enqueue-on-transient` queues the
  transition to `.flow/pending-mutations.jsonl` for `/flow sync` to reconcile.
- `tracker_cli.py transition` exit 3 → no `in_review` transition; do NOT
  auto-close via `done` (premature in a PR flow) — leave the ticket
  `in_progress`, warn, and continue.
- `tracker_cli.py transition` exit 2 / 4 / 5 → hard stop. Surface
  `failure_kind` + `failure_detail`; mark stage status=failed.

## Skip conditions

- Skipped entirely if `workspace.toml [pipeline.handlers] commit = "none"`.
  (Bare workspace never sets this; rare configuration.)
