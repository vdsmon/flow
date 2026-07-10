# revise verb — detail

`/flow revise <ticket|pr> ["instruction"]` turns a **delivered** run's OPEN PR into a revision sub-run. The original terminal run is never mutated; the revision is a SUB-RUN at `.flow/runs/<ticket>/revisions/<rev-id>/` with its own lease/state/snapshot, driving the generic do-loop over a fix-only stage subset to update the SAME PR — new fix commits pushed, CI re-greened, reviewer threads resolved. Never a new PR.

Two feedback sources feed it: the PR's review threads (the host's review-bot/human comments), and an optional trailing free-text **instruction** — a change-request without the host round-trip (`/flow revise 325 "batch the N+1 query"`). This file plumbs the instruction (persists it) and drives the loop.

> **Scope.** This verb is the USER ENTRY + plumbing: resolve the target through the forge seam, open the revision sub-run, persist the instruction, enter the worktree, drive the generic do-loop, release. The revision EXECUTION semantics — how the `implement` / `review_loop` stages CONSUME the persisted instruction or the PR's human threads as the fix set, the severity floor, the fix-cycle cap — have SHIPPED (flow-kx17.4); their reference docs carry the rules. On an interactive run the step-5a triage board below supplies that fix set as an explicit disposition set (`$REVISION_DIR/dispositions.json`) in place of the inferred severity floor — the board is the surface, the shipped revision mode is the consumer, and the two compose rather than fork.

## Procedure

### 1. Resolve the target through the forge seam (NO raw `gh`/`bkt`)

`revise` is a general user verb, so PR resolution goes through `forge_cli.py`, host-agnostic. Parse the first positional arg; everything after it (quoted) is the optional free-text instruction.

**Numeric arg → a PR number.** Look the PR up by id, then derive the ticket from its branch:
```bash
PR_JSON=$(python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py pr-info \
  --workspace-root . --pr "$ARG")
```
`pr-info` reads the PR in ANY state (so a MERGED PR is detectable). `null` / exit 1 → no such PR; surface the resolution hint and stop. Read `head` (the PR's feature branch), `state`, and `number` from the JSON. Resolve the ticket key from that branch (the run is NOT checked out on it, so pass `--branch`, which skips the git call):
```bash
KEY=$(python3 ${CLAUDE_SKILL_DIR}/scripts/branch_ticket.py \
  --workspace-root . --branch "$HEAD_BRANCH")
```
Exit 0 → `$KEY`. Exit 3 → the branch name carries no ticket key; surface + stop.

**Non-numeric arg → a ticket key.** It is `$KEY` directly. Find the ticket's feature branch with plain git (host-agnostic), then ask the seam for its PR:
```bash
BRANCH=$(git worktree list --porcelain | awk '/^branch /{print $2}' \
  | sed 's,^refs/heads/,,' | grep -E "^feat(ure)?/${KEY}([-/]|$)" | head -1)
PR_JSON=$(python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py detect-pr \
  --workspace-root . --branch "$BRANCH")
```
`detect-pr` returns the OPEN PR for that branch (or `null`). Read `id` and `state`.

### 2. Guard the PR is OPEN

- `state` is MERGED (or any non-open terminal state) → **refuse**: "revise targets an OPEN PR; for post-merge work file a new ticket." Stop.
- No PR found (`null` from `pr-info`/`detect-pr`) → error with the resolution hint (the PR number was wrong, or the ticket has no open PR). Stop.
- OPEN → proceed. Keep the PR `id`/`number` and the branch in hand.

### 3. Open the revision sub-run

```bash
REV_JSON=$(python3 ${CLAUDE_SKILL_DIR}/scripts/dispatch_stage.py revise-open \
  --workspace-root . --ticket "$KEY")
```
Capture from the stdout JSON: `rev_id`, `run_id`, `session_nonce` (→ `$NONCE`), `revision_dir`, `stages` (the fix-only subset). Exit codes:
- **Exit 3** — the original run is not terminal (a stage is still pending or failed). It is not a delivered run; surface "not a done run — use `/flow do` or `/flow recover`" and stop.
- **Exit 4** — a revision is already live for this ticket (only one at a time). Surface it and stop.
- **Exit 0** — the sub-run is seeded with its own lease/state/snapshot.

### 4. Persist the instruction (if any)

If the user gave a trailing free-text instruction, write it to the durable source `.4`'s execution reads:
```bash
printf '%s\n' "$INSTRUCTION" > "$REVISION_DIR/instruction.md"
```
If no instruction was given, the revision's fix source is the PR's review threads (read by `.1`/`.4` via `forge_cli review-threads`), so write nothing here.

### 5. Locate or re-materialize the worktree, then enter it

```bash
WT_JSON=$(python3 ${CLAUDE_SKILL_DIR}/scripts/flow_worktree.py locate-or-reseed \
  --ticket "$KEY" --branch "$BRANCH" --main-root .)
```
Read `worktree` and `reseeded`. `reseeded: true` means the original worktree was externally lost and got re-materialized from the PR branch (note it for the user — the revision applies its fixes on a fresh checkout of the PR head). Then `EnterWorktree(path=<worktree>)` to switch this session in.

(In a backgrounded run whose cwd is pinned at the repo root, `EnterWorktree` refuses; `cd` the Bash cwd into the worktree once instead, exactly as the backgrounded-`--auto` note in `references/verb-do.md` describes. The same worktree-isolation caveats for `Write`/`Edit` and `.out` capture apply.)

### 5a. Lavish revise triage board (interactive runs)

An interactive `revise` renders the PR's unresolved threads as a triage board (fix now / defer / dismiss) whose batched dispositions become the durable fix set the do-loop consumes, instead of feeding the threads to the severity floor blind. Gate, two legs; a failed gate skips and never blocks. Leg (a) is structural — `revise` is human-initiated, so a human is on the other end. Leg (b) is the presence check, run with a real command as the first action, never a judgment call:
```bash
command -v node && command -v npx   # leg (b): both must resolve
```
Fetch the threads capture-then-check through the forge seam (read `$?` first — piping `review-threads` past its exit code reads a flake as zero threads):
```bash
RAW=$(python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . review-threads --pr "$PR_ID"); rc=$?
```
`rc != 0`, a `{"supported": false}` (a host with no thread support), or zero unresolved threads with no `instruction.md` → `Lavish: skipped — <reason>`, and step 6 runs as today (no `dispositions.json` is written, so the floor applies). Otherwise open the board per the `## Revision triage board (/flow revise)` section of `references/review-packet.md` and WAIT for the first poll return — the first batch is what seeds the fix set. Persist each triage batch whole (the step-4 persistence precedent):
```bash
printf '%s\n' "$DISPOSITIONS_JSON" > "$REVISION_DIR/dispositions.json"
```
A post-open failure → `Lavish: degraded mid-loop — <reason>`; the remainder of the run is today's flow (an already-persisted `dispositions.json` stays authoritative). Do NOT issue a dispatcher heartbeat during this wait — a `next` on an all-pending sub-run begins the first pending stage before any triage exists (the board section explains the two-regime rule and the 10-min init-TTL residual).

### 6. Drive the revision do-loop

Drive the dispatcher state machine exactly as the do-loop skeleton in `SKILL.md`, with ONE difference: pass `--revision "$REV_ID"` on every `next` / `advance` / `release` call (alongside `--session-nonce "$NONCE"`), so the dispatcher redirects to the revision sub-run's state, not the original terminal run's.

When `$REVISION_DIR/dispositions.json` exists (a step-5a board opened), the implement and review_loop stages consume it as the fix set — their reference docs carry the rules — and the board stays open across the loop: never re-open it or re-arm the poll, it live-reloads in place per round.

```bash
DESCRIPTOR=$(python3 ${CLAUDE_SKILL_DIR}/scripts/dispatch_stage.py next \
  --workspace-root . --ticket "$KEY" --revision "$REV_ID" --session-nonce "$NONCE")
```
Then per descriptor: run the `records_diff_baseline` pre-hook when the role calls for it, dispatch the stage by `handler_type` (inline / subagent / skill / none), capture the `.out`, and advance:
```bash
DESCRIPTOR=$(python3 ${CLAUDE_SKILL_DIR}/scripts/dispatch_stage.py advance \
  --workspace-root . --ticket "$KEY" --revision "$REV_ID" --session-nonce "$NONCE" \
  --stage "$STAGE" --status "$STATUS" [--output-path "$OUTPUT_PATH"])
```
The per-stage protocols (the inline/subagent/skill dispatch rules, the exit-code matrices for `next`/`advance`, friction logging, the post-implement reconcile, the PR-ready notification) are identical to a `do` run and load at dispatch like any do run — follow **`references/verb-do.md`** and the **`SKILL.md`** do-loop skeleton verbatim; do not re-enumerate them here. The revision stage subset is fix-only: implement → code_review → e2e → commit → reflect → review_loop (the exact set `revise-open` returns in `stages`). The deliverable is the SAME PR updated, so no `create_pr` runs — the existing PR's branch gets the new fix commits.

Friction during the loop logs against the revision the same way (`flow_friction.py` with `--run-id "$RUN_ID"`).

### 7. Release on every exit path

When the loop exits — clean done (`{"done": true}`), blocked, drift, or lost lease — release the revision lease:
```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/dispatch_stage.py release \
  --workspace-root . --ticket "$KEY" --revision "$REV_ID" --session-nonce "$NONCE"
```
`release` is a no-op when the lease is not ours, so it is safe to call unconditionally (do not call it on the step-3 exit-3/exit-4 abort paths — nothing was acquired). Then surface the updated PR's URL as the highlighted closing block, same rendering rules as `references/verb-do.md` (the PR is the deliverable, updated in place).
