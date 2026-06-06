# do verb — detail

The do-loop **skeleton** lives in SKILL.md (it is the hot path, run every iteration of a possibly-backgrounded run). This file carries the verbose detail the skeleton points to: the full exit-code matrices, the PR-ready notification protocol, friction logging, the post-implement reconcile, and the timeout / drift notes.

## PR-ready notification (unconditional, best-effort)

When the PR becomes genuinely review-ready, ping the user via the PushNotification tool: fire after the `review_loop` stage finishes `completed` (CI green AND every actionable reviewer thread resolved — the true ready-to-review point, NOT at `create_pr`, which only opens the draft; when `review_loop`'s handler is `none` so no CI/review loop is wired, fall back to after `create_pr` completes), with the PR URL (`"flow <KEY>: PR ready for review — <url>"`).
This is unconditional — fire it on every run. PushNotification is harness-local (your terminal, plus your phone if Remote Control is on), so it renders harmlessly in-terminal when you are attached and reaches your phone when you have backgrounded the session; either way it does not ride MCP/claude.ai auth, so it fires even if the tail's tracker calls have 401'd — which is how you learn an unattended run stalled.
If the PushNotification tool is NOT available in the current harness (some surfaces do not expose it — a `ToolSearch` for it returns nothing), do not abort and do not treat its absence as a blocker. Fall back to BOTH: (a) surface the message in-thread, and (b) a DURABLE channel a detached console can see later — post it as a `bkt` PR comment (`bkt` is already in hand on the create_pr / review_loop path): `bkt api "2.0/repositories/<ws>/<repo>/pullrequests/<id>/comments" -X POST -d "$(jq -n --arg b "flow <KEY>: <message>" '{content:{raw:$b}}')" --json`. The in-thread echo alone is invisible to a truly detached run; the PR comment is what makes the fallback real. The notification is best-effort; the pipeline state in `state.json` is the source of truth.
A blocker needs no special ping: an `AskUserQuestion` surfaces natively as "needs input" in `claude agents` when the session is backgrounded, and inline when it is attached.

**Firing point (do-loop step e):** fire only when `$STAGE` is `review_loop` with `$STATUS` completed (CI green and every actionable reviewer thread resolved), reading the PR URL from the captured `create_pr.out`. Only when `review_loop`'s handler is `none` (no CI/review loop wired) do you fall back to firing at `create_pr` completed.

## Exit-code handling (init / next / advance)

**`dispatch_stage.py init` (do-loop step 3):**
- Exit 0 → run initialized; proceed to the loop.
- Exit 1 **with a `holder` block in the stdout JSON** → the ticket is locked by a live run.
  Surface the holder JSON and the hint `/flow recover <ticket>`, then abort.
  (Exit 1 *without* a `holder` block is a validate-workspace failure: surface stderr violations and abort, same as the step-2 hard gate.)
- Exit 5 → a stale lease from a dead run holds the ticket.
  Surface the holder JSON and the hint `/flow recover <ticket>`, then abort.
- Do NOT auto-clear a lease on exit 1 or 5.
  The run acquired nothing on these paths, so do not call `release`.

**`dispatch_stage.py next` / `advance` (do-loop steps a + e — same codes):**
- Exit 0 → continue.
- Exit 1 → distinguish by the stdout JSON payload, then break the loop:
  - `detail` present → config/version drift (the workspace.toml, the stage-registry, or a handler plugin changed mid-run).
    Surface the drift detail + the hint `/flow recover <ticket>`.
    Before any such exit 1, dispatch auto-reconciles an *owned* drift whose changed snapshot component(s) (`workspace_toml` and/or `stage_registry`) ALL map to files in this run's `planned_files` (a deliberate self-inflicted edit, e.g. a ticket whose deliverable edits `stage-registry.toml`) by reloading the snapshot baseline and continuing; the descriptor then carries `reconciled_drift: "<components>"` (the do-loop MAY log a `RECONCILE` friction entry on that marker). A handler-tree drift is never owned (it names no single file). Foreign/concurrent drift still halts at exit 1.
  - `violations` present → a validate-workspace failure.
    Surface the violations and abort.
  - bare `error` (e.g. `unrecoverable state.json`) → the run state is corrupt.
    Surface the error + the `/flow recover <ticket>` hint.
- Exit 7 → lost lease; another run took over this ticket.
  Surface the hint `/flow recover <ticket>`, then break the loop.

A `--status failed` advance returns `{blocked_by}`, which the skeleton's descriptor parse treats as the block-and-break case.

## Friction logging (in-flight)

Whenever a step hits a snag the run has to work around, append one friction entry before you act on it. This is the high-fidelity evidence the `reflect` stage synthesizes into machinery findings (a backgrounded reflect agent cannot reconstruct it from `state.json` alone). See `references/self-evolution.md` for how this feeds the self-modification loop. Trigger → `--type`: `next`/`advance` drift exit 1 → `DRIFT`; lost-lease exit 7 → `LEASE_LOSS`; the records_diff_baseline post-implement reconcile OR a dispatch owned-drift reconcile (`next`/`advance` returns a `reconciled_drift` marker) → `RECONCILE`; a skill handler not installed → `MISSING_TOOL`; an `AskUserQuestion` blocker → `BLOCKER`; a stage finished `failed` → `STAGE_FAILED`; a retried stage → `RETRY`. The call (best-effort — never let a logging failure abort the run):
```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/flow_friction.py \
  --ticket "$KEY" --run-id "$RUN_ID" --stage "$STAGE" \
  --type <TYPE> --body "<one line: what snagged>" [--detail "<context>"] \
  --workspace-root . || true
```

## Post-implement reconcile (records_diff_baseline stages only)

After the implement stage returns, if its report flags files it created/modified OUTSIDE the recorded `planned_files` (a package `__init__.py`, a `.gitignore` negation, etc.) that genuinely must ship, expand the set BEFORE `finish`. The commit stage reads `planned_files` from `baseline.json`, so a needed file missing there is silently dropped from the commit. To widen it, rewrite the `planned_files` array in `.flow/tickets/<KEY>.md` frontmatter with the full set — pass a bracketed TOML array literal, NOT a bare comma list (a bare `a,b,c` is stored as the string `"a,b,c"`, not an array):
```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/ticket_frontmatter.py update \
  .flow/tickets/<KEY>.md \
  --set 'planned_files=["a/b.py", "c/d.py"]'
```
then re-run the `record-baseline` command (do-loop step c) with the full comma-separated `--files` list. HEAD is unchanged (no commit has landed), so this only widens ownership and re-captures any modified tracked file's original blob. Confirm with `diff_extract.py capture-implement-diff --ticket <KEY> --ticket-dir <ticket-dir> --cwd .` + `git apply --cached --check --binary <ticket-dir>/implement.diff` that the patch carries every file and applies cleanly. (`capture-implement-diff` takes ONLY `--ticket`/`--ticket-dir`/`--cwd` — NOT `--stage`; passing `--stage` errors with `unrecognized arguments`.)

## Timeout note (mvp hole)

The descriptor's `timeout_min` is informational only.
Agent tool does not accept a timeout argument; nothing in the prose enforces it.
The prose-driven model has no live poller, so hung detection is post-hoc: `/flow recover` reads the lease state (after a stage returns, or on demand) to surface and take over a stalled run.

## Working-tree drift

If `git apply --cached --binary <implement.diff>` fails in stage-commit, the working tree has drifted from the baseline.
The commit stage handler documents the recovery path.
Do not silently overwrite or `--force`.
