# do verb — detail

The do-loop **skeleton** lives in SKILL.md (it is the hot path, run every iteration of a possibly-backgrounded run). This file carries the verbose detail the skeleton points to: the full exit-code matrices, the PR-ready notification protocol, the --auto self-teardown, friction logging, the post-implement reconcile, and the timeout / drift notes.

## PR-ready notification (best-effort; non-`--auto` runs)

When the PR becomes genuinely review-ready, ping the user via the PushNotification tool: fire after the `review_loop` stage finishes `completed` (CI green AND every actionable reviewer thread resolved — the true ready-to-review point, NOT at `create_pr`, which only opens the draft; when `review_loop`'s handler is `none` so no CI/review loop is wired, fall back to after `create_pr` completes), with the PR URL (`"flow <KEY>: PR ready for review — <url>"`). On a packet-gated interactive run the ping fires earlier — at packet-open inside `review_loop`'s tail (`references/review-packet.md`) — which satisfies this same firing point (the single-fire rule below).
**Skip this whole block on an `--auto` run — the PushNotification ping AND its no-`PushNotification` fallbacks below.** A drain-launched `--auto` run's completion is already surfaced by the drain orchestrator report + the bead close, and its PR link still lands in `create_pr.out`, so a phone ping there is pure noise — and the ping's stall-detection value does not apply either, since the drain orchestrator watches a drained run's lifecycle via lease/fleet. Detect `--auto` by session context, exactly as the self-teardown section below does ("## Self-teardown at run completion (--auto only)"): this session ran the spec `--auto` path, or was launched `/flow <key> --auto`; never infer it from state files — `--auto` is stamped nowhere. On every OTHER run — attended, or backgrounded via `/bg` — fire it: PushNotification is harness-local (your terminal, plus your phone if Remote Control is on), so it renders harmlessly in-terminal when you are attached and reaches your phone when you have backgrounded a non-auto session; either way it does not ride MCP/claude.ai auth, so it fires even if the tail's tracker calls have 401'd — which is how you learn an unattended `/bg` run stalled.
If the PushNotification tool is NOT available in the current harness (some surfaces do not expose it — a `ToolSearch` for it returns nothing), do not abort and do not treat its absence as a blocker. Fall back to BOTH: (a) surface the message in-thread, and (b) a DURABLE channel a detached console can see later — post it as a PR comment via the workspace's forge backend (`[forge] backend`): for a GitHub forge, `gh pr comment <PR_URL> --body "flow <KEY>: <message>"`; for a Bitbucket forge, `bkt api "2.0/repositories/<ws>/<repo>/pullrequests/<id>/comments" -X POST -d "$(jq -n --arg b "flow <KEY>: <message>" '{content:{raw:$b}}')" --json`. The in-thread echo alone is invisible to a truly detached run; the PR comment is what makes the fallback real. The notification is best-effort; the pipeline state in `state.json` is the source of truth.
A blocker needs no special ping: an `AskUserQuestion` surfaces natively as "needs input" in `claude agents` when the session is backgrounded, and inline when it is attached. (Off Claude Code, `references/harness.md` is the canonical matrix for the `PushNotification` and `AskUserQuestion` fallbacks.)

**Firing point (do-loop step e):** on a non-`--auto` run, fire only when `$STAGE` is `review_loop` with `$STATUS` completed (CI green and every actionable reviewer thread resolved), reading the PR URL from the captured `create_pr.out`. Only when `review_loop`'s handler is `none` (no CI/review loop wired) do you fall back to firing at `create_pr` completed. On an `--auto` run, skip the notification at both points (the block above).

**Single-fire rule (packet-gated runs).** The PR-ready notification fires exactly once per run. On a packet-gated run it fires at packet-open — this satisfies the do-loop step-e firing point (no duplicate ping), and the packet loop then runs inside `review_loop`'s tail (see `references/review-packet.md`). On a gate-failed run (the packet never opens) it fires at step e exactly as today. The packet never attaches at the `create_pr` fallback firing point (`review_loop` handler `none` → skip line, no packet). The gate-2 review packet is a sanctioned lavish site per `references/verb-spec.md` step 4's boundary sentence; a degraded packet adds one visible `Lavish: skipped — <reason>` / `Lavish: degraded mid-loop — <reason>` line and never changes this firing behavior. The once-per-run tracking is session context — the same mechanism as the `--auto` detection above, no marker file; a crash-resumed retry of `review_loop` that re-opens the packet may legitimately ping again (the notification is best-effort on every path).

## Covers PR-link fan-out (grouped runs only)

When `create_pr` completes, read `covers` from `.flow/tickets/<KEY>.md` frontmatter. For each cover key, post the PR URL as a comment so every co-delivered ticket links to the PR that closes it:
```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . \
  comment --key <COVER> --text "Covered by <KEY> — PR: <PR_URL>"
```
Best-effort (a failed cover comment never blocks the run); agent-followed, not dispatcher-enforced (v1 non-goal). The lead's own PR-link presentation is unchanged.

## PR-link presentation (do-loop step 5, run completion)

After the loop exits cleanly and the lease is released, end the turn with the PR link as a distinct, highlighted block — the LAST thing in your message, visually separated from the rest of the summary. The PR URL is the one thing the user clicks first, so it must not be buried in a paragraph or a bullet list. Read it from `.flow/runs/<KEY>/stages/create_pr.out` (the `PR_URL=` line `create_pr` printed — the inline handler `create_pr.py` prints it; ship-it is only one legacy backend) and render it on its own, after a `---` rule, e.g.:
```
---
🚀 **PR ready for review →** <PR_URL>
```
Put any one-line caveats (residual risks) ABOVE the rule; nothing goes below the PR link. Draft state is the normal end state, not a caveat: never flag it. If `create_pr` was skipped (handler `none`, or the run blocked before it), omit the block rather than printing an empty rule.

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
    python3 -c 'import os,sys
if os.fork(): sys.exit(0)
os.setsid()
if os.fork(): sys.exit(0)
os.execvp("sh", ["sh", "-c", sys.argv[1]])' "$TEARDOWN" >/dev/null 2>&1 </dev/null &
  fi
fi
true
```

**Guards:**
- `$CLAUDE_JOB_DIR` unset/empty → `JOB_ID` empty → the 8-hex grep fails → **silent skip**. A foreground or attended session has no job dir, so this one guard covers both.
- `claude stop` takes the **8-hex job id** — the `$CLAUDE_JOB_DIR` basename — NOT the session UUID. Passing the UUID fails "No job matching".
- **Stop before rm:** the daemon re-materializes a still-registered job dir, so an rm-first teardown silently undoes itself.
- The rm path is validated under `~/.claude/jobs/` with an 8-hex basename BEFORE the single destructive line.
- Detached via `setsid` when available (own session — survives the stop's process-group kill); macOS has no setsid binary, so the fallback double-forks via `python3` (`os.setsid()` between the forks) into its own session before exec'ing the teardown. A bare `nohup ... &` child stays in the session's process group, so the group kill at session teardown murders it mid-`sleep 30` and the panel leaks (flow-al8o). `$TEARDOWN` rides in as `argv[1]` — never interpolate it into the program text. stdin/stdout/stderr detached on both branches.
- **Best-effort:** a teardown failure must never fail, block, or delay the run. No friction entry on failure.
- Non-destructive to history: the transcript lives outside the job dir, so `claude attach <session_id>` still works after stop + dir removal.

**Why sleep 30.** The schedule is the last *tool call*; the final summary (including the PR-link block) streams *after* it. 10s risks stopping the session mid-stream of its own completion message; 30s is still effectively instant next to A2's event-driven-turn + 300s-idle bar. Do not optimize it back down.

## Exit-code handling (init / next / advance)

**`dispatch_stage.py init` (do-loop step 3):**
- Exit 0 → run initialized; proceed to the loop. The stdout JSON carries a `session_nonce` — the per-acquire lease component minted on this acquire (a fresh run) or carried forward (a same-session resume that presented it). Capture it and pass it back as `--session-nonce` on every later `next`/`advance`/`release`; it is what lets a refresh/release detect a force/takeover, and (on a re-init) what distinguishes the same session resuming from a second `/flow do`.
- An init payload (exit 0) MAY also carry `state_recovered_from_backup: true` → the dispatcher found a corrupt `state.json` at init, quarantined it, and restored the newest `.bak` (rewriting it to disk, so the subsequent `next` reads clean state and never re-emits the marker). On this marker the do-loop MUST (1) append a `STATE_ROLLBACK` friction entry; (2) carry it forward and apply the same re-verify as the `next`/`advance` marker to the FIRST non-idempotent stage it dispatches this run — `create_pr`, `merge`, `commit` — since an init-recovered `.bak` can be one write behind, a stage that already landed its external effect may show pending; re-verify whether the effect landed (`gh pr view` for `create_pr`/`review_loop`, `git log` / `gh pr view` for `merge`/`commit`) and if it did, finish the stage `completed` WITHOUT re-running it.
- Exit 1 **with a `holder` block in the stdout JSON** → the ticket is locked by a live run.
  This now also fires when a SECOND `/flow do` re-inits a live lease without the owner's `session_nonce` (the bug flow-8i6l closed: run_id alone, read from `state.json`, no longer re-acquires a live lease). A genuine same-session resume passes `--session-nonce`; a crash-resume waits for the lease to expire (then init resumes via the expired-owner path) or uses `/flow recover`.
  Surface the holder JSON and the hint `/flow recover <ticket>`, then abort.
- Exit 1 *without* a `holder` block — distinguish by the payload:
  - `violations` present → a validate-workspace failure: surface the violations and abort, same as the step-2 hard gate.
  - bare `error` `unrecoverable state.json at <dir>` → corrupt `state.json` with no usable `.bak`; minting a fresh all-pending run over it would replay a shipped ticket, so init refuses. Surface the error + the `/flow recover <ticket>` hint and abort — WITHOUT `release` (nothing was acquired). `init --force` is the operator-explicit reset: it replaces the unrecoverable state, and the exit-0 payload then carries `state_unrecoverable_replaced: true`.
  - `error` `corrupt run.lock` (with a `detail`) → lease ownership cannot be confirmed; do NOT auto-clear. Surface the detail + the payload's recover hint (`/flow recover <ticket>`, human-driven takeover, which quarantines the corrupt lock) and abort.
- Exit 5 → a stale lease from a dead run holds the ticket.
  Surface the holder JSON and the hint `/flow recover <ticket>`, then abort.
- Do NOT auto-clear a lease on exit 1 or 5.
  The run acquired nothing on these paths, so do not call `release`.

**`dispatch_stage.py next` / `advance` (do-loop steps a + e — same codes):**
- Exit 0 → continue.
  A `next`/`advance` payload MAY also carry `state_recovered_from_backup: true` → the dispatcher found a corrupt `state.json`, quarantined it, and rolled back to the newest `.bak`; the stage about to be dispatched may have already completed before the corruption (the rollback is by design, the silence on it was the gap). On this marker the do-loop MUST (1) append a `STATE_ROLLBACK` friction entry; (2) before executing a NON-IDEMPOTENT stage — `create_pr`, `merge`, `commit` — re-verify whether the stage's external effect already landed (`gh pr view` for `create_pr`/`review_loop`, `git log` / `gh pr view` for `merge`/`commit`) and if it did, finish the stage `completed` WITHOUT re-running it; an idempotent stage just proceeds normally.
- Exit 1 → distinguish by the stdout JSON payload, then break the loop:
  - `detail` present → config/version drift (the workspace.toml, the stage-registry, or a handler plugin changed mid-run).
    Surface the drift detail + the hint `/flow recover <ticket>`.
    Before any such exit 1, dispatch auto-reconciles an *owned* drift whose changed snapshot component(s) (`workspace_toml` and/or `stage_registry`) ALL map to files in this run's `planned_files` (a deliberate self-inflicted edit, e.g. a ticket whose deliverable edits `stage-registry.toml`) by reloading the snapshot baseline and continuing; the descriptor then carries `reconciled_drift: "<components>"` (the do-loop MAY log a `RECONCILE` friction entry on that marker). The two tree-hash components are never owned via `planned_files` (each names no single file, so neither can map to a planned file): a handler-tree drift always halts fail-closed (recover with `recover.py reload-snapshot`). An `engine` drift (a mid-run `git pull` / `claude plugin marketplace update` on the main checkout swapped the engine tree) no longer unconditionally halts: a COMMITTED advance (lagging-main / marketplace pull, working tree clean vs `HEAD`) self-heals by re-anchoring the snapshot (descriptor marker `engine_reanchored`), and a transient concurrent-read race re-verifies clean (marker `engine_drift_reverified`); ONLY a dirty / uncommitted engine-tree mutation still halts fail-closed (recover with `recover.py reload-snapshot`). Foreign/concurrent drift still halts at exit 1.
  - `violations` present → a validate-workspace failure.
    Surface the violations and abort.
  - bare `error` (e.g. `unrecoverable state.json`) → the run state is corrupt.
    Surface the error + the `/flow recover <ticket>` hint.
- Exit 7 → lost lease; another run took over this ticket (a changed run_id/boot/host, a gone lock, or — since flow-8i6l — a rotated `session_nonce` from a `--force`/takeover). Passing `--session-nonce` is what surfaces the rotated-nonce case; omit it (or pass none) and the check degrades to run_id-only, never a false positive.
  Surface the hint `/flow recover <ticket>`, then break the loop.

A `--status failed` advance returns `{blocked_by}`, which the skeleton's descriptor parse treats as the block-and-break case.

## Friction logging (in-flight)

Whenever a step hits a snag the run has to work around, append one friction entry before you act on it. This is the high-fidelity evidence the `reflect` stage synthesizes into machinery findings (a backgrounded reflect agent cannot reconstruct it from `state.json` alone). See `references/self-evolution.md` for how this feeds the self-modification loop. Trigger → `--type`: `next`/`advance` drift exit 1 → `DRIFT`; lost-lease exit 7 → `LEASE_LOSS`; the records_diff_baseline post-implement reconcile OR a dispatch owned-drift reconcile (`next`/`advance` returns a `reconciled_drift` marker) → `RECONCILE`; a skill handler not installed → `MISSING_TOOL`; an `AskUserQuestion` blocker → `BLOCKER`; a stage finished `failed` → `STAGE_FAILED`; a retried stage → `RETRY`; a `next`/`advance` payload carrying a `state_recovered_from_backup` marker → `STATE_ROLLBACK`. The call (best-effort — never let a logging failure abort the run):
```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/flow_friction.py \
  --ticket "$KEY" --run-id "$RUN_ID" --stage "$STAGE" \
  --type <TYPE> --body "<one line: what snagged>" [--detail "<context>"] \
  --workspace-root . || true
```

## Work-stage opus retry (one-shot)

When the `implement` stage returns `failed` on a run whose work subagents were sonnet-pinned — i.e. `model_resolve.py --workspace-root . --ticket "$KEY" --stage implement` prints a non-empty model (a full-lane run with the downshift active, which is the default) — re-dispatch `implement` EXACTLY ONCE with NO `model` pin (inherit the opus session) before recording the failure. Under per-subagent pinning a sonnet work-DNF returns control to the still-live opus session and marks the bead `blocked`/`in_progress` (not `open`), so the drain reappearance-trigger that feeds the whole-run sonnet→opus ladder never fires; this in-run retry closes that gap.

- **Trigger:** `STATUS=failed` from the `implement` dispatch AND `model_resolve.py --stage implement` returns non-empty for this ticket AND no prior opus retry has run for `implement` this run (one-shot).
- **Action:** append a `RETRY` friction entry (`flow_friction.py ... --type RETRY --stage implement`), then re-run the implement subagent spawn (the SKILL.md `subagent:<type>` branch) OMITTING the `model=` argument so it inherits the opus session. Re-evaluate `STATUS` from the retry's report.
- **Bound:** exactly one opus retry. It does NOT distinguish a sonnet-capacity DNF from a genuine code failure — any `implement` failure on a downshifted run buys one opus re-run of a stage that would otherwise dead-end. If the opus retry also returns `failed`, advance `implement --status failed` as normal (the dispatcher then blocks the run for `/flow recover`).
- **Scope:** `implement` ONLY. The inline `review_loop` fix subagent is NOT retried this way — re-running it would restart the whole CI-wait+fix loop and its interaction with the §2 3-fix-cycle cap is undefined; a `review_loop` capacity failure rides its own cap.
- The whole-run sonnet→opus ladder (`references/verb-evolve.md` §C) is UNCHANGED: it still escalates a drain-launched `sonnet`-*session* run that DNFs the whole run (the bead re-appears `open`). This in-run retry covers the DIFFERENT case where only the work *subagent* is sonnet under an opus session.

## Post-implement reconcile (records_diff_baseline stages only)

After the implement stage returns, if its report flags files it created/modified OUTSIDE the recorded `planned_files` (a package `__init__.py`, a `.gitignore` negation, etc.) that genuinely must ship, expand the set BEFORE `finish`. The commit stage reads `planned_files` from `baseline.json`, so a needed file missing there is silently dropped from the commit. To widen it, rewrite the `planned_files` array in `.flow/tickets/<KEY>.md` frontmatter with the full set — pass a bracketed TOML array literal, NOT a bare comma list (a bare `a,b,c` is stored as the string `"a,b,c"`, not an array):
```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/ticket_frontmatter.py update \
  .flow/tickets/<KEY>.md \
  --set 'planned_files=["a/b.py", "c/d.py"]'
```
then re-run the `record-baseline` command (do-loop step c) with the full comma-separated `--files` list. HEAD is unchanged (no commit has landed), so this only widens ownership and re-captures any modified tracked file's original blob. Confirm with `diff_extract.py capture-implement-diff --ticket <KEY> --ticket-dir <ticket-dir> --cwd .` + `git apply --cached --check --binary <ticket-dir>/implement.diff` that the patch carries every file and applies cleanly. (`capture-implement-diff` takes ONLY `--ticket`/`--ticket-dir`/`--cwd` — NOT `--stage`; passing `--stage` errors with `unrecognized arguments`.) Run the apply-check with a clean index: if anything is staged (e.g. via `git rm`), `git reset` first — the check validates against the index, and a pre-staged deletion fails with `does not exist in index` even though the patch is fine.

**Binary `planned_files` are orchestrator-copied here.** The implement subagent emits text only (`Write`/`Edit` produce UTF-8), so a binary deliverable in `planned_files` (an `.xlsx` template, an image, a compiled fixture) is one it flags but cannot produce — see the binary-deliverable callout in `references/stage-implement.md`. After implement returns, copy each flagged binary into the worktree from its source, post-implement and BEFORE the commit gate's `capture-implement-diff`. This is NOT the widening reconcile above: a planned binary is already in-set, so it needs no `planned_files` change — it is just a missing addition. Do NOT `git add` the copied file; `capture-implement-diff` runs `git add --intent-to-add` on untracked planned paths, so an untracked copy surfaces as an addition in `implement.diff`. (Copy order is otherwise unconstrained: the baseline blob comes from `git ls-files -s` over the index, which never holds an untracked copy, so a copied binary is never baked into the baseline even if a re-record runs.) Confirm with the same `capture-implement-diff` + `git apply --cached --check --binary` pre-flight that the binary lands in the patch.

## Timeout note (mvp hole)

The descriptor's `timeout_min` is informational only.
Agent tool does not accept a timeout argument; nothing in the prose enforces it.
The prose-driven model has no live poller, so hung detection is post-hoc: `/flow recover` reads the lease state (after a stage returns, or on demand) to surface and take over a stalled run.

### Spawned stage subagents run long commands in the foreground

Because `timeout_min` is unenforceable and the Agent tool takes no timeout, a spawned stage subagent's own Bash-level `timeout` is the only remaining lever. A subagent dispatched for a `work`-role stage (the `implement` stage, and any `subagent:<type>` handler SKILL.md step d spawns) MUST run its tests and other long commands in the FOREGROUND, each a single `Bash` call with an explicit `timeout` <= 600000ms (the Bash ceiling), and MUST NEVER use `run_in_background` or `Monitor`. A spawned subagent does not receive background-task completions once its turn ends — those route to the top-level orchestrator session — so a backgrounded command leaves the subagent's turn hung "waiting for the notification" and the stage stalls until the orchestrator `SendMessage`-resumes it (FT-1328, observed twice). This is unconditional, not the headless turn-boundary fallback of `references/stage-review_loop.md` §1. Chunk any suite that would run long into per-path / per-module foreground calls (for this repo, the `scripts/tests` + `hooks/tests` split), which also keeps each call under the ~360s harness idle-watchdog (a distinct kill below the 600000ms ceiling — flow-rbr). `references/stage-implement.md` Step 5 carries the operative wording; `references/stage-review_loop.md` §1 is the mirrored bounded-foreground style.

## Working-tree drift

If `git apply --cached --binary <implement.diff>` fails in stage-commit, the working tree has drifted from the baseline.
The commit stage handler documents the recovery path.
Do not silently overwrite or `--force`.

## Inline-edit path discipline (not bg-only)

Every inline write the orchestrator makes itself must target a worktree-absolute (or worktree-relative) path, never a main-checkout-absolute one. This is NOT a bg-only concern: a main-checkout-absolute path escapes the active worktree even in an ATTENDED run. The session cwd is inside the worktree, so a relative path is safe, but an absolute repo-root path still resolves to the main checkout's working tree and silently writes there, invisible until a later step (pytest) cannot find the file. The spots this governs are the inline-implement stage's `Edit`/`Write` (handler `inline`), the review_loop fix edits, the post-implement reconcile binary copies, and the orchestrator's own `.out` capture. Nothing catches the escaping write: the bg-isolation guard is satisfied the moment cwd sits inside the worktree and never validates the write TARGET (that gap is the bug), so the discipline rests on the author, not on a check. The **Backgrounded `--auto` run** section below states the same rule for the bg case (where the guard additionally forces the subagent fallback); this subsection generalizes it to attended inline edits rather than restating the mechanism (flow-cjgy).

## Backgrounded `--auto` run (cwd pinned at repo root)

A `claude --bg /flow <key> --auto` run has its session cwd pinned at the repository root, so `EnterWorktree(path=<worktree>)` refuses. Before the loop runs, `cd` the persistent Bash cwd into the seeded worktree once; then `--workspace-root .` resolves against the worktree for every dispatch call. The bg-isolation guard keys on cwd: spawned subagents keep their cwd pinned at the repo root, so the guard blocks their `Edit`/`Write` inside the linked worktree and they fall back to Bash/Python string-replace edits against absolute worktree paths (worktree-absolute only — a main-checkout-absolute path bypasses the guard and silently writes main). The orchestrator's own `Edit`/`Write` has been observed to work on absolute worktree paths once the `cd` moves its Bash cwd inside (flow-kykn/PR#296) — a single observation, possibly harness-version-specific; Bash/heredoc remains the documented safe-superset fallback. See `references/verb-spec.md` step 7 for the canonical explanation.

### Orchestrator `.out` capture when Write is blocked

The same guard blocks the orchestrator's own step-d capture of a subagent/skill response: `$TICKET_DIR/stages/<STAGE>.out` lives inside the worktree, so the Write tool SKILL.md step d prescribes is rejected. The orchestrator holds the response only as in-context text it must emit into a shell command — a heredoc is the mechanism, and the robustness lever is the delimiter, not the transport. Write the `.out` with a quoted heredoc using a long collision-safe sentinel:

```bash
mkdir -p "$TICKET_DIR/stages"
cat > "$TICKET_DIR/stages/<STAGE>.out" <<'FLOW_OUT_SENTINEL_9f3a'
<the subagent/skill response, emitted verbatim>
FLOW_OUT_SENTINEL_9f3a
```

then pass `--output-path "$TICKET_DIR/stages/<STAGE>.out"` to `advance` in step (e) exactly as the Write path would have. Two properties make this safe: the sentinel `FLOW_OUT_SENTINEL_9f3a` is long and random so it will not appear on a line by itself in the body (if it ever does, extend both sentinels and retry); and because the delimiter is **quoted** (`<<'...'`), the shell expands nothing inside the body — `$`, backticks, and `\` pass through literally, which is the exact safety the SKILL.md "NOT shell redirect — `"`/`\` would break it" parenthetical protects. `cat >` is the default writer; `python3 <<'FLOW_OUT_SENTINEL_9f3a' ...` is interchangeable (the delimiter and quoting are what matter, not the writer binary). This is the orchestrator analogue of the subagent string-replace fallback above; observed first on flow-495l/PR#233, where all four stage `.out` files were written via heredoc.

**`code_review`'s taxonomy `.out`.** `code_review` is an inline stage that AUTHORS a finding-taxonomy `.out` — the `pr_body.md` analogue: inline-authored by the orchestrator, not a subagent/skill response capture — so the later `create_pr` stage can read its ask-user items. It uses the same quoted-heredoc mechanism above; see the code_review stage's reference doc for the section contract.
