---
name: flow
argument-hint: <ticket> | spec <ticket> | do | revise | status | recall | triage | recover | group | sync | init
description: Ticket pipeline. /flow <ticket> plans in plan mode (ExitPlanMode = the one gate), then enters a worktree and runs the autonomous implement→PR tail in the same session; background it (/bg) anytime to run unattended. You spec and review the draft PR. Multi-tracker engine (Jira | beads), pluggable handlers, compounding memory.
when_to_use: User runs /flow <ticket> or /flow spec <ticket> to spec a ticket and run it to a draft PR, /flow do <ticket> to run/resume the pipeline standalone, or /flow init, recall, status, triage, recover, sync. A bare ticket key with no verb defaults to spec.
allowed-tools: Bash(python3:*), Bash(git:*), Bash(bd:*), Bash(jq:*), Bash(cat:*), Bash(mkdir:*), Bash(mktemp:*), Bash(rm:*), Bash(gh:*), Bash(claude:*), Read, Write, Edit, Agent, Skill, AskUserQuestion, PushNotification, EnterWorktree
---

# /flow

One continuous ticket pipeline.
You spec the work and review the PR; the machine owns everything in between.

```
ME                       MACHINE                          ME
spec ──→ ExitPlanMode ──→ worktree → implement → … → draft PR ──→ PR review
plan mode    the one gate    one session, background anytime (/bg)   the deliverable
```

`/flow <ticket>` (or `/flow spec <ticket>`) runs the read-only front half — fetch the ticket, design the plan WITH you, in plan mode.
`ExitPlanMode` is the single human gate.
On approval it seeds a git worktree, enters it (`EnterWorktree`), and runs the autonomous tail (implement → code_review → e2e → commit → draft PR) in this same conversation — the planning context carries straight through, no handoff.
The pipeline is background-agnostic: it never asks whether it is attached. Running it unattended is your separate call — `/bg` (or `←`) backgrounds the session at any point, and `claude agents` is the cockpit (attach to peek, answer a needs-input blocker, detach). Background several tickets that way to run them in parallel. The deliverable is a draft PR you review.
See `references/background-pipeline.md`.

`/flow do` is the **executor primitive** — the full pipeline, resuming at the next pending stage.
`spec` enters the seeded worktree and flows into it in the same session; `do` also runs standalone to resume a run.
`/flow revise <ticket|pr> ["instruction"]` turns a delivered run's OPEN PR into a revision sub-run that ingests review feedback (or a free-text change-request) and updates the SAME PR (`references/verb-revise.md`).
`group` proposes run-level groupings (lead + covers) for the multi-ticket fold — the read-only front half that feeds `spec --covers` (`references/verb-group.md`).
Everything else (`recall`, `status`, `triage`, `recover`, `sync`) is a work-state verb around the same pipeline.

Built on a multi-tracker engine: the tracker is pluggable (Jira | beads); stages, handlers, and the memory namespace come from `.flow/workspace.toml` + `stage-registry.toml`.
The memory layer compounds across tickets (reflect-stage extraction, plan-phase recall), and the harness fixes its own bugs from inside a run — see `references/self-evolution.md`.

This file is the router plus the two things that stay on the hot path: the **spec gate** and the **do-loop skeleton**. Every verb's step-by-step detail lives in a `references/verb-*.md` the agent loads on demand (pointers in the table below).

## Argument parsing

Match the **first whitespace-delimited token** of `$ARGUMENTS` against the verb set below by exact string equality.
If it equals a verb, route there.
An **alias** is matched by the same exact-equality rule and resolves to its canonical verb BEFORE routing: `resume`→`do`, `mem`/`memory`→`recall`. Aliases are additive — the canonical names always route, and matching stays exact (so `sync-42` still ≠ any verb or alias).
If `$ARGUMENTS` is empty, print the verb listing, grouped by the sections in the table below.
Otherwise — a first token that is not any verb (a bare ticket key like `FT-123`, or a beads key like `sync-42`) — route to **spec**, taking that positional token as the ticket key (same key-resolution as spec step 2).
Spec is the default because fire-and-forget is the primary path.
**Multiple positional ticket keys** (e.g. `/flow FT-1 FT-2 FT-3`) — spec handles ONE ticket per run. Do not silently consume only the first: surface all the keys you were given and ask (via `AskUserQuestion`) whether to spec them sequentially (one plan + tail each) or **fold related ones into a single piece of work**, then proceed on that answer. **Fold = run-level grouping (`covers`):** pick a LEAD key that owns the run (lease / state / branch / memory stay lead-keyed) and pass the rest as `--covers FT-2,FT-3`. The lead's spec gate covers all of them, the PR carries one `Closes <KEY>` per cover, and the commit/PR/reflect steps fan out to close each (`references/verb-spec.md`). Group only tickets that are one coherent change (same files / shared deps); independent tickets stay sequential. A cover must be a distinct, live, non-epic ticket — the bootstrap refuses otherwise.
(Exact-token match is what keeps this unambiguous: `sync-42` ≠ the verb `sync`, so a ticket key never collides with a verb.)
`spec` also accepts the optional flags `--auto` (aliases `--aa`, `--yolo`), `--e2e-recipe "<recipe>"`, `--covers FT-2,FT-3` (sibling keys this run co-delivers), and `--lane express|light|full` (the verification lane this run takes — interactive-only, also read from natural language; `--auto` ignores it and derives the lane from the bead's tier labels) anywhere after the verb; they are ignored when reading the positional ticket key. A bare ticket key carries these flags through to spec too: `/flow --auto FT-123` routes to spec with `--auto` set, `/flow --lane express FT-123` with that lane preset.

| First token | Verb | Reference |
|------|------|------|
| **— core pipeline —** | | |
| `spec` (optionally `<ticket>`, `--auto`, `--e2e-recipe "..."`) | spec (gate below) | `references/verb-spec.md` |
| `do` (alias `resume`) (optionally `<ticket>`) | do (skeleton below) | `references/verb-do.md` |
| `revise <ticket\|pr> [<instruction>]` | revise | `references/verb-revise.md` |
| **— multi-ticket —** | | |
| `group` (optionally `<key> ...`, `--mine`, `--filter open`) | group | `references/verb-group.md` |
| **— work state —** | | |
| `status` (optionally `<ticket>`) | status | `references/verb-status.md` |
| `recall` (aliases `mem`, `memory`) `<query> [--branch X --top-n N]` | recall | `references/verb-recall.md` |
| `recall --metric tickets-per-week [...]` | metric (recall passthrough) | `references/verb-recall.md` |
| `triage` (optionally `<key> "<answer>"`) | triage | `references/verb-triage.md` |
| `recover` (optionally `<ticket>`) | recover | `references/verb-recover.md` |
| `sync` | sync | `references/verb-sync.md` |
| **— setup —** | | |
| `init` (optionally `--reconfigure`, `--resume`) | init | `references/verb-init.md` |
| `new` | new | `references/verb-new.md` |
| **— maintainer —** | | |
| `evolve <audit\|propose\|epic\|drain>` (maintainer-only) | evolve namespace (dispatch in the ref) | `references/verb-evolve.md` |
| `queue` (optionally `--dry-run`) (maintainer-only) | queue | `references/verb-queue.md` |
| **— fallback —** | | |
| (empty) | print verb listing (grouped by the sections above) | — |
| anything else (e.g. `FT-123`) | spec; that positional token is the ticket key | `references/verb-spec.md` |

## spec verb — the one gate

The read-only front half: fetch the ticket, design the plan WITH you in plan mode, then seed a worktree, enter it, and flow into the `do` pipeline in this SAME session. This is the human/machine boundary — you own the spec and the PR review; the machine owns everything between. Backgrounding the tail (`/bg`) to run unattended is your call at any point.

**`ExitPlanMode` with the plan = Gate 1, the only human gate.** Gate on an INDEPENDENT confidence rating (the `advisor` tool — it auto-forwards the transcript; or a `general-purpose` `Agent` if advisor is absent — on Fable models it always is, by design: skip the probe), never self-scored. **On any lane, dissolve forks before you ask:** before surfacing a clarifying question to the user, classify it — a *fact* (its answer is reachable by read-only investigation: Read/Grep/Glob, an `Explore` agent, recall, Context7, web) is yours to look up and fold in, never to surface, while a *decision* (it needs user-only input the repo does not encode) is the only thing that reaches the user — so resolve every fact yourself and surface only the decisions (`references/verb-spec.md` step 4). This is suppression-only and lane-independent; it never adds a gate. The confidence rating is the separate `full`-lane gate: **< 90% → do NOT `ExitPlanMode` yet:** first exhaust every reachable read-only artefact (Read/Grep/Glob, an `Explore` agent, WebSearch/WebFetch, read-only MCPs), then for a gap that needs user action ask via `AskUserQuestion` with specifics, then re-assess. Library-API claims must be Context7-verified. The rating is part of the plan, surfaced unprompted, every time — present only at >=90%, or when every reachable source is exhausted and the residual is documented as a risk. Unless the workspace explicitly disabled e2e, the plan also settles the **e2e recipe** here (while live auth is present). The plan also PROPOSES a verification **lane** (a `## Lane` section, conservative — `express` only for behavior-preserving, tightly-bounded work; else `full`); approve it with the plan or override at the gate (`--lane …`, or in words). An effective `express`/`light` lane makes the human `ExitPlanMode` approval the vetting and SKIPS the confidence probe — the <90% rule above is for the `full` lane. A hot change (a guard file in `planned_files`) clamps to `full` regardless, computed before the probe-skip, so a forced `--lane express` on a guard-file change still runs the full probe. Detail: `references/verb-spec.md`.

On approval (normal mode): the bootstrap (`flow_worktree.py create`) persists the plan, seeds the worktree, and stamps `planned_files` + `commit_type` + `commit_summary` (+ `e2e_recipe`) into frontmatter so the tail never pauses to ask — then `EnterWorktree(path="<worktree>")` switches this session in and you **continue straight into the `do` loop below** (its `init` resumes the spec-seeded run at `implement`).

In `--auto` mode the gate never parks: if the headless planner cannot self-approve (a clarifying question, sub-90% confidence, or a `BAIL`), the run defers the ticket in place (status → `deferred`, open questions commented) and exits, rather than asking via `AskUserQuestion` or `ExitPlanMode`.

**Full procedure — interactive steps 1-7, the `--auto` headless path (incl. the defer-and-exit recipe), and the exact `flow_worktree.py create` command: `references/verb-spec.md`.**

flow is Claude-Code-first. Running it under another harness (Codex, Cursor) — how each Claude-Code primitive used here (`ExitPlanMode`, `EnterWorktree`, `advisor`, `Skill`/`Agent`, `PushNotification`, `AskUserQuestion`, `${CLAUDE_SKILL_DIR}`) degrades when absent — is in `references/harness.md`.

## do verb — the loop

The **executor primitive**: the full ticket→PR pipeline, driven off the dispatcher state machine. `spec` flows into it in the same session (resuming at `implement`); it also runs standalone (`/flow do <ticket>`). The dispatcher (`dispatch_stage.py`) owns `state.json`, the lease, and the canonical snapshot; this prose acts on each descriptor it emits.

The verbose detail — full exit-code matrices, the PR-ready notification protocol + fallback, friction logging, the post-implement reconcile, timeout/drift — lives in **`references/verb-do.md`**. The skeleton below stays inline because it runs every iteration (including in backgrounded runs).

**Friction logging (in-flight):** whenever a step hits a snag the run works around (drift, lost lease, reconcile, missing tool, blocker, failed/retried stage), append one `flow_friction.py` entry before acting on it — it is the evidence the `reflect` stage turns into harness fixes (`references/self-evolution.md`). The trigger→type table + the command are in `references/verb-do.md`.

1. Resolve the ticket key. If `$ARGUMENTS` had a positional, use it. Else:
   ```bash
   KEY=$(python3 ${CLAUDE_SKILL_DIR}/scripts/branch_ticket.py --workspace-root .)
   ```
   Exit 0 → use `$KEY`. Exit 3 → no key on branch; ask via `AskUserQuestion`. Exit 1 → workspace not initialized; abort with the `/flow init` hint.

2. HARD GATE the workspace:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/validate_workspace.py --workspace-root .
   ```
   Non-zero → surface stderr violations; abort.

3. Initialize the run (acquires the per-ticket lease + writes the canonical snapshot):
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/dispatch_stage.py init \
     --workspace-root . --ticket "$KEY"
   ```
   Capture the `run_id` AND the `session_nonce` from stdout JSON; carry that nonce (`$NONCE`) verbatim on every later `next`/`advance`/`release` call below — it is the per-session lease component that blocks a second `/flow do` from re-acquiring this live lease. Exit 0 → proceed to the loop. Exit 1 (with a `holder` block) or Exit 5 (stale lease) → surface the holder + `/flow recover <ticket>`, abort; do NOT call `release` (nothing was acquired). Full matrix: `references/verb-do.md`.

4. **Orchestration loop** — repeat until done:

   a. Obtain the next `DESCRIPTOR`. On the FIRST iteration (right after `init`), call `next`; on every later iteration, reuse the payload `advance` already returned in step (e) and skip this standalone `next`:
      ```bash
      DESCRIPTOR=$(python3 ${CLAUDE_SKILL_DIR}/scripts/dispatch_stage.py next \
        --workspace-root . --ticket "$KEY" --session-nonce "$NONCE")
      ```
      `next` refreshes the lease + verifies the snapshot. Exit 0 → continue to (b) (a self-inflicted *owned* drift — a planned `workspace.toml`/`stage-registry.toml` edit — auto-reconciles upstream in dispatch and returns exit 0 with a `reconciled_drift` marker, so it never trips this exit-1 path). Exit 1 (drift/violations/corrupt) or Exit 7 (lost lease) → surface + `/flow recover <ticket>`, break the loop. Full matrix: `references/verb-do.md`.

   b. Parse `DESCRIPTOR` (JSON):
      - `{"done": true}` → all stages completed. Break to step 5.
      - `{"done": false, "blocked_by": "<stage>", "reason": "<text>"}` → a prior stage is `failed`. Surface the block + reason + `/flow recover <ticket>` hint. Break to step 5.
      - Otherwise → handler descriptor with `stage`, `handler_type`, `head_sha`, `ticket_dir`, `output_path`, `roles`, optional `reference_doc`, `subagent_type`, `skill_name`, `skill_args`.

   c. **Pre-handler hook (records_diff_baseline):** if `descriptor.roles` includes `"records_diff_baseline"`:
      ```bash
      python3 ${CLAUDE_SKILL_DIR}/scripts/diff_extract.py record-baseline \
        --stage "$STAGE" --ticket "$KEY" \
        --ticket-dir "$TICKET_DIR" \
        --files "$PLANNED_FILES" \
        --capture-blobs --cwd .
      ```
      `PLANNED_FILES` comes from `.flow/tickets/<KEY>.md` frontmatter (`planned_files = [...]`); if absent, ask via `AskUserQuestion`. Exit non-zero aborts the stage with status=failed. After implement returns, widen `planned_files` if it touched needed files outside the set — the **post-implement reconcile** in `references/verb-do.md`.

   d. Dispatch by `handler_type`:

      - **`inline`** — Read `${CLAUDE_SKILL_DIR}/${descriptor.reference_doc}` and follow its prose (explicit script invocations + exit handling). Determine `status = completed | failed`. An inline stage MAY write a captured report to `$TICKET_DIR/stages/<STAGE>.out`; if it does, pass `--output-path` on `advance`. If not, omit it (an absent inline `.out` is normal).

      - **`subagent:<type>`** — If `descriptor.reference_doc` is present, Read `${CLAUDE_SKILL_DIR}/${descriptor.reference_doc}` first (e.g. `references/stage-plan.md`, `references/stage-implement.md`) — it carries the per-stage protocol. **Stage model pin:** when `descriptor.roles` includes `"model_routed"`, resolve this stage's model first — `M=$(python3 ${CLAUDE_SKILL_DIR}/scripts/model_resolve.py --workspace-root . --ticket "$KEY" --stage "$STAGE")` — and pass `model=$M` in the Agent call below only when `$M` is non-empty (the downshift is on by default on a full-lane run; `$M` is empty on express/light lanes or when the workspace opts that stage out); omit the `model=` line otherwise, inheriting the session (opus plans, sonnet writes). Then spawn an Agent embedding that protocol:
        ```
        Agent(
          subagent_type=descriptor.subagent_type,
          description="<stage> for <ticket>",
          model=<$M when non-empty; omit this line otherwise>,
          prompt="""
          Ticket: <KEY>
          Stage: <STAGE>
          Ticket dir: <TICKET_DIR>

          You are the <subagent_type> agent for the <STAGE> stage of /flow.
          Read .flow/runs/<KEY>/ticket.json for ticket context. Read
          .flow/tickets/<KEY>.md for ticket frontmatter.

          Per-stage protocol (from <reference_doc>):
          <contents of the reference doc, or its path if it is large>

          Do the stage's work and return your report.
          """
        )
        ```
        **Capture the Agent's response** with the Write tool (NOT shell redirect — `"`/`\` would break it): `mkdir -p "$TICKET_DIR/stages"`, then Write `file_path = <TICKET_DIR>/stages/<STAGE>.out`, `content = <the Agent's full response>`. Remember that path for `--output-path` on `advance`. (In a backgrounded `--auto` run the worktree-isolation guard blocks the Write tool here — fall back to a Bash heredoc to the same path; see the "Backgrounded `--auto` run" section of `references/verb-do.md` for the collision-safe recipe.)

      - **`skill:<name>[:<args>]`** — The descriptor carries `skill_name` + `skill_args` (no raw handler string), and usually NO `reference_doc` (a skill stage's own SKILL.md is the protocol; do not read `reference_doc` for it, and never treat a missing one as an error). Reconstruct the handler string `skill:<skill_name>[:<skill_args>]`, then:
        1. Verify the handler is installed:
           ```bash
           python3 ${CLAUDE_SKILL_DIR}/scripts/resolve_handler.py \
             --handler "<handler_string>"
           ```
           Exit 1 (not installed) or Exit 2 (manifest invalid) → surface the error, set `STATUS=failed`, fall through to (e) to record it (do NOT bare-break). Exit 0 → the stdout JSON gives authoritative `skill_name` / `skill_args` / `invocation`.
        2. Invoke the skill via the Skill tool using `skill_name`, passing `skill_args` verbatim. Wait for it to finish.
        3. Capture its final response: `mkdir -p "$TICKET_DIR/stages"`, then Write to `<TICKET_DIR>/stages/<STAGE>.out` (same as the subagent branch, including the bg `--auto` Write-blocked heredoc fallback in `references/verb-do.md`). Set `STATUS=completed` (or `failed` if the skill reported failure).

      - **`none`** — Skip; transition to (e) with status=completed.

      - **`unknown`** — Should never reach here (validate_workspace catches it). If it does, surface and abort.

   e. Advance the stage — finish it AND fetch the next descriptor in one call:
      ```bash
      DESCRIPTOR=$(python3 ${CLAUDE_SKILL_DIR}/scripts/dispatch_stage.py advance \
        --workspace-root . --ticket "$KEY" --session-nonce "$NONCE" \
        --stage "$STAGE" --status "$STATUS" \
        [--output-path "$OUTPUT_PATH"])
      ```
      `advance` is `finish` + `next` in one round-trip: it records HEAD itself (do not pass it), finishes `$STAGE` with `$STATUS`, and returns the NEXT descriptor (parses EXACTLY like `next` in (b), plus a `finished` object). `--output-path` is for subagent/skill stages (and any inline stage that captured output); omit otherwise. It must name an existing, already-written file — `advance` exits 1 without finishing the stage if it is missing; write the file, then re-run the same `advance`. Handle its exit codes exactly as `next` in (a).
      **PR-ready notification (non-`--auto` runs):** when `$STAGE` is `review_loop` finishing `completed`, fire the best-effort PushNotification with the PR URL — UNLESS this is an `--auto` run, which skips the notification entirely (its PR link still lands in `create_pr.out`; the drain report + bead close surface completion). Detect `--auto` by session context, same as the self-teardown case (full protocol + the `create_pr` fallback + the no-PushNotification fallback: `references/verb-do.md`).

   f. Loop back to (b) with the `DESCRIPTOR` that `advance` just returned. The standalone `next` in (a) runs only once, for the first stage.

5. After the loop exits — on **every** path (clean done, blocked, drift, or lost lease) — release the lease:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/dispatch_stage.py release \
     --workspace-root . --ticket "$KEY" --session-nonce "$NONCE"
   ```
   `release` is a no-op when the lease is not ours — the exit-7 takeover case, now including a rotated `session_nonce` — so it is safe to call unconditionally here. Do not call it on the init-abort paths of step 3.

   **`--auto` self-teardown (last act):** when this run was launched with `--auto`, after `release` (and after reading `create_pr.out` for the PR link), schedule the session's own panel teardown as the **last tool call** of the run, then emit the final summary. Recipe + guards: `references/verb-do.md`. An attended run NEVER does this.

   When the loop exited cleanly: surface "ticket <KEY> pipeline complete. State: `cat .flow/runs/<KEY>/state.json | jq`."

   **Then end the turn with the PR link as a distinct, highlighted block — the LAST thing in your message, after a `---` rule, nothing below it.** Full rendering rules (the `---` example, caveat-above-the-rule placement, the skipped-`create_pr` omit case): `references/verb-do.md`.
