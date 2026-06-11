# do verb — detail

The do-loop **skeleton** lives in SKILL.md (it is the hot path, run every iteration of a possibly-backgrounded run). This file carries the verbose detail the skeleton points to: the full exit-code matrices, the PR-ready notification protocol, the --auto self-teardown, friction logging, the post-implement reconcile, and the timeout / drift notes.

## PR-ready notification (unconditional, best-effort)

When the PR becomes genuinely review-ready, ping the user via the PushNotification tool: fire after the `review_loop` stage finishes `completed` (CI green AND every actionable reviewer thread resolved — the true ready-to-review point, NOT at `create_pr`, which only opens the draft; when `review_loop`'s handler is `none` so no CI/review loop is wired, fall back to after `create_pr` completes), with the PR URL (`"flow <KEY>: PR ready for review — <url>"`).
This is unconditional — fire it on every run. PushNotification is harness-local (your terminal, plus your phone if Remote Control is on), so it renders harmlessly in-terminal when you are attached and reaches your phone when you have backgrounded the session; either way it does not ride MCP/claude.ai auth, so it fires even if the tail's tracker calls have 401'd — which is how you learn an unattended run stalled.
If the PushNotification tool is NOT available in the current harness (some surfaces do not expose it — a `ToolSearch` for it returns nothing), do not abort and do not treat its absence as a blocker. Fall back to BOTH: (a) surface the message in-thread, and (b) a DURABLE channel a detached console can see later — post it as a `bkt` PR comment (`bkt` is already in hand on the create_pr / review_loop path): `bkt api "2.0/repositories/<ws>/<repo>/pullrequests/<id>/comments" -X POST -d "$(jq -n --arg b "flow <KEY>: <message>" '{content:{raw:$b}}')" --json`. The in-thread echo alone is invisible to a truly detached run; the PR comment is what makes the fallback real. The notification is best-effort; the pipeline state in `state.json` is the source of truth.
A blocker needs no special ping: an `AskUserQuestion` surfaces natively as "needs input" in `claude agents` when the session is backgrounded, and inline when it is attached.

**Firing point (do-loop step e):** fire only when `$STAGE` is `review_loop` with `$STATUS` completed (CI green and every actionable reviewer thread resolved), reading the PR URL from the captured `create_pr.out`. Only when `review_loop`'s handler is `none` (no CI/review loop wired) do you fall back to firing at `create_pr` completed.

## Self-teardown at run completion (--auto only)

**Why.** A finished `claude --bg "/flow <key> --auto"` session lingers in the `claude agents` panel until a drain turn's A2 cleanup collects it — often a long time (drain turns are event-driven, and A2 waits on a 300s transcript-idle bar). Self-teardown clears the panel at completion. The evolve drain's A2 cleanup (`references/verb-evolve.md`) stays as the safety net for runs that die before reaching this tail.

**When.** `--auto` runs ONLY. Fire it once, as the last tool call of the do-loop's step 5, on every loop-exit path — clean done, blocked, drift, lost lease (the lease is already released; the --auto run takes no further action on any of them). Attended runs — including `/flow do <ticket>` resumes and interactive runs backgrounded via `/bg` — must NEVER kill their own session. Detection is session context: this session ran the spec `--auto` path, or was launched `/flow <key> --auto`. Never infer it from state files — `--auto` is stamped nowhere.

**The command:**
```bash
JOB_DIR="${CLAUDE_JOB_DIR:-}"
JOB_ID="${JOB_DIR##*/}"
if printf '%s' "$JOB_ID" | grep -qxE '[0-9a-f]{8}' \
   && printf '%s' "$JOB_DIR" | grep -qE '/\.claude/jobs/[0-9a-f]{8}$'; then
  TEARDOWN="sleep 30; timeout 90 claude stop $JOB_ID </dev/null; rm -rf \"$JOB_DIR\""
  if command -v setsid >/dev/null 2>&1; then
    setsid nohup sh -c "$TEARDOWN" >/dev/null 2>&1 </dev/null &
  else
    nohup sh -c "$TEARDOWN" >/dev/null 2>&1 </dev/null &
  fi
fi
true
```

**Guards:**
- `$CLAUDE_JOB_DIR` unset/empty → `JOB_ID` empty → the 8-hex grep fails → **silent skip**. A foreground or attended session has no job dir, so this one guard covers both.
- `claude stop` takes the **8-hex job id** — the `$CLAUDE_JOB_DIR` basename — NOT the session UUID. Passing the UUID fails "No job matching".
- **Stop before rm:** the daemon re-materializes a still-registered job dir, so an rm-first teardown silently undoes itself.
- The rm path is validated under `~/.claude/jobs/` with an 8-hex basename BEFORE the single destructive line.
- Detached via `setsid` when available (own session — survives the stop's process-group kill), else `nohup ... &` (macOS has no setsid binary); stdin/stdout/stderr detached.
- **Best-effort:** a teardown failure must never fail, block, or delay the run. No friction entry on failure.
- Non-destructive to history: the transcript lives outside the job dir, so `claude attach <session_id>` still works after stop + dir removal.

**Why sleep 30.** The schedule is the last *tool call*; the final summary (including the PR-link block) streams *after* it. 10s risks stopping the session mid-stream of its own completion message; 30s is still effectively instant next to A2's event-driven-turn + 300s-idle bar. Do not optimize it back down.

## Exit-code handling (init / next / advance)

**`dispatch_stage.py init` (do-loop step 3):**
- Exit 0 → run initialized; proceed to the loop. The stdout JSON carries a `session_nonce` — the per-acquire lease component minted on this acquire (a fresh run) or carried forward (a same-session resume that presented it). Capture it and pass it back as `--session-nonce` on every later `next`/`advance`/`release`; it is what lets a refresh/release detect a force/takeover, and (on a re-init) what distinguishes the same session resuming from a second `/flow do`.
- Exit 1 **with a `holder` block in the stdout JSON** → the ticket is locked by a live run.
  This now also fires when a SECOND `/flow do` re-inits a live lease without the owner's `session_nonce` (the bug flow-8i6l closed: run_id alone, read from `state.json`, no longer re-acquires a live lease). A genuine same-session resume passes `--session-nonce`; a crash-resume waits for the lease to expire (then init resumes via the expired-owner path) or uses `/flow recover`.
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
- Exit 7 → lost lease; another run took over this ticket (a changed run_id/boot/host, a gone lock, or — since flow-8i6l — a rotated `session_nonce` from a `--force`/takeover). Passing `--session-nonce` is what surfaces the rotated-nonce case; omit it (or pass none) and the check degrades to run_id-only, never a false positive.
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
then re-run the `record-baseline` command (do-loop step c) with the full comma-separated `--files` list. HEAD is unchanged (no commit has landed), so this only widens ownership and re-captures any modified tracked file's original blob. Confirm with `diff_extract.py capture-implement-diff --ticket <KEY> --ticket-dir <ticket-dir> --cwd .` + `git apply --cached --check --binary <ticket-dir>/implement.diff` that the patch carries every file and applies cleanly. (`capture-implement-diff` takes ONLY `--ticket`/`--ticket-dir`/`--cwd` — NOT `--stage`; passing `--stage` errors with `unrecognized arguments`.) Run the apply-check with a clean index: if anything is staged (e.g. via `git rm`), `git reset` first — the check validates against the index, and a pre-staged deletion fails with `does not exist in index` even though the patch is fine.

## Timeout note (mvp hole)

The descriptor's `timeout_min` is informational only.
Agent tool does not accept a timeout argument; nothing in the prose enforces it.
The prose-driven model has no live poller, so hung detection is post-hoc: `/flow recover` reads the lease state (after a stage returns, or on demand) to surface and take over a stalled run.

## Working-tree drift

If `git apply --cached --binary <implement.diff>` fails in stage-commit, the working tree has drifted from the baseline.
The commit stage handler documents the recovery path.
Do not silently overwrite or `--force`.

## Backgrounded `--auto` run (cwd pinned at repo root)

A `claude --bg /flow <key> --auto` run has its session cwd pinned at the repository root, so `EnterWorktree(path=<worktree>)` refuses and the bg-isolation guard blocks `Edit`/`Write` inside the linked worktree (for this session and any spawned subagent). Before the loop runs, `cd` the persistent Bash cwd into the seeded worktree once; then `--workspace-root .` resolves against the worktree for every dispatch call, and spawned subagents fall back to Bash/Python string-replace edits against absolute worktree paths. See `references/verb-spec.md` step 7 for the canonical explanation.

### Orchestrator `.out` capture when Write is blocked

The same guard blocks the orchestrator's own step-d capture of a subagent/skill response: `$TICKET_DIR/stages/<STAGE>.out` lives inside the worktree, so the Write tool SKILL.md step d prescribes is rejected. The orchestrator holds the response only as in-context text it must emit into a shell command — a heredoc is the mechanism, and the robustness lever is the delimiter, not the transport. Write the `.out` with a quoted heredoc using a long collision-safe sentinel:

```bash
mkdir -p "$TICKET_DIR/stages"
cat > "$TICKET_DIR/stages/<STAGE>.out" <<'FLOW_OUT_SENTINEL_9f3a'
<the subagent/skill response, emitted verbatim>
FLOW_OUT_SENTINEL_9f3a
```

then pass `--output-path "$TICKET_DIR/stages/<STAGE>.out"` to `advance` in step (e) exactly as the Write path would have. Two properties make this safe: the sentinel `FLOW_OUT_SENTINEL_9f3a` is long and random so it will not appear on a line by itself in the body (if it ever does, extend both sentinels and retry); and because the delimiter is **quoted** (`<<'...'`), the shell expands nothing inside the body — `$`, backticks, and `\` pass through literally, which is the exact safety the SKILL.md "NOT shell redirect — `"`/`\` would break it" parenthetical protects. `cat >` is the default writer; `python3 <<'FLOW_OUT_SENTINEL_9f3a' ...` is interchangeable (the delimiter and quoting are what matter, not the writer binary). This is the orchestrator analogue of the subagent string-replace fallback above; observed first on flow-495l/PR#233, where all four stage `.out` files were written via heredoc.
