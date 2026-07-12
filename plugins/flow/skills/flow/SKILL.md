---
name: flow
description: Ticket-to-draft-PR pipeline for Claude Code and Codex. Use when the user invokes /flow or $flow:flow with a ticket, asks to spec and deliver a ticket, resumes a Flow run, or requests Flow init, recall, status, triage, recover, or sync. Plans with one explicit approval gate, binds an isolated worktree, then runs implementation, review, verification, commit, and PR stages. Jira or beads tracker, pluggable handlers, and compounding memory. A bare ticket key defaults to spec.
allowed-tools: Bash(.flow/flow:*), Bash(*/.flow/flow:*), Bash(python3:*), Bash(git:*), Bash(bd:*), Bash(jq:*), Bash(cat:*), Bash(mkdir:*), Bash(mktemp:*), Bash(rm:*), Bash(gh:*), Bash(claude:*), Read, Write, Edit, Agent, Skill, AskUserQuestion, PushNotification, EnterWorktree
---

# Flow

One continuous ticket pipeline.
You spec the work and review the PR; the machine owns everything in between.

```
ME                       MACHINE                          ME
spec ──→ plan approval ──→ worktree → implement → … → draft PR ──→ PR review
          the one gate          one rooted session            the deliverable
```

`/flow <ticket>` in Claude Code, `$flow:flow <ticket>` in Codex, or the equivalent
natural-language skill request runs the same read-only front half: fetch the ticket and
design the plan WITH the user. Explicit plan approval is the single human gate.
On approval Flow seeds a git worktree, binds this conversation to its absolute root,
and runs the autonomous tail (implement → code_review → e2e → commit → draft PR) with
the planning context intact. Backgrounding is a separate, host-owned choice; Flow does
not assume `/bg`, a persistent terminal, or any particular task UI.
See `references/background-pipeline.md`.

`/flow do` is the **executor primitive** — the full pipeline, resuming at the next pending stage.
`spec` enters the seeded worktree and flows into it in the same session; `do` also runs standalone to resume a run.
`/flow revise <ticket|pr> ["instruction"]` turns a delivered run's OPEN PR into a revision sub-run that ingests review feedback (or a free-text change-request) and updates the SAME PR (`references/verb-revise.md`).
`group` proposes run-level groupings (lead + covers) for the multi-ticket fold — the read-only front half that feeds `spec --covers` (`references/verb-group.md`).
`slice` splits a wide refactor into an expand→migrate→contract ladder of independently-landable children — group's inverse (`references/verb-slice.md`).
Everything else (`recall`, `status`, `triage`, `recover`, `sync`) is a work-state verb around the same pipeline.

Built on a multi-tracker engine: the tracker is pluggable (Jira | beads); stages, handlers, and the memory namespace come from `.flow/workspace.toml` + `stage-registry.toml`.
The memory layer compounds across tickets (reflect-stage extraction, plan-phase recall), and the harness fixes its own bugs from inside a run — see `references/self-evolution.md`.

This file is the router plus the two things that stay on the hot path: the **spec gate** and the **do-loop skeleton**. Every verb's step-by-step detail lives in a `references/verb-*.md` the agent loads on demand (pointers in the table below).

## Harness and rooted execution contract

Read `references/harness.md` before running any Flow command. Bind its logical
`arguments`, `skill_root`, `task_root`, `run_root`, `facade`, `harness`, and
`capabilities` values.
Do not store them only in shell variables: an export, `cd`, or command cwd may not
persist across host calls or into subagents.

Every `.flow/flow` recipe in this skill is notation for the absolute `facade` value.
On Codex, the notation also means prefix that same command invocation with
`FLOW_HARNESS=codex`; never depend on a persistent export. Claude Code may use the
explicit `FLOW_HARNESS=claude-code` selector or its compatibility default; a generic
adapter uses `FLOW_HARNESS=generic`. Run it with explicit workdir `run_root`;
`--workspace-root .` therefore resolves to that same root. Reads, edits, git/test/forge
commands, artifacts, and subagent paths must be absolute or explicitly rooted there.
After worktree bootstrap, immediately replace `run_root` with the returned absolute
worktree path and replace `facade` with `<run_root>/.flow/flow`. Never fall back to the
checkout where the request started.
If a command tool has no workdir field, root each individual call with `git -C`, an
absolute path, or `cd "<run_root>" && ...` in that same call. Never issue a standalone
`cd` and depend on it later.

## Post-init command gate

For every verb except `init`, use the workspace-local facade. Resolve the initialized
workspace from `task_root`, then inspect `.flow/skill_dir` and `.flow/flow`. If either
launcher file is absent in a legacy workspace, repair it from the currently loaded
absolute `skill_root`. If metadata exists but its installation is stale, the loaded
skill is also the only valid repair source:

```bash
FLOW_HARNESS="<codex|claude-code|generic>" \
  python3 "<skill-root>/scripts/flow_launcher.py" --workspace-root "<task-root>"
```

Do not search arbitrary plugin caches or marketplace directories. A successful init,
reconfigure, worktree create, or worktree reload writes both files. After repair, set
`run_root` to the absolute workspace and `facade` to its absolute launcher.

## Argument parsing

Call the adapter-supplied request text `arguments` (Claude Code supplies `$ARGUMENTS`;
Codex supplies the text after the skill mention or the equivalent user request). Match
its **first whitespace-delimited token** against the verb set below by exact equality.
If it equals a verb, route there.
An **alias** is matched by the same exact-equality rule and resolves to its canonical verb BEFORE routing: `resume`→`do`, `mem`/`memory`→`recall`. Aliases are additive — the canonical names always route, and matching stays exact (so `sync-42` still ≠ any verb or alias).
If `arguments` is empty, print the verb listing, grouped by the sections in the table below.
Otherwise — a first token that is not any verb (a bare ticket key like `FT-123`, or a beads key like `sync-42`) — route to **spec**, taking that positional token as the ticket key (same key-resolution as spec step 2).
Spec is the default because fire-and-forget is the primary path.
**Multiple positional ticket keys** (e.g. `/flow FT-1 FT-2 FT-3`): spec handles ONE ticket per run. Do not silently consume only the first: surface all the keys and use the adapter's user-input capability to ask whether to spec them sequentially (one plan + tail each) or **fold related ones into a single piece of work**, then proceed on that answer. **Fold = run-level grouping (`covers`):** pick a LEAD key that owns the run (lease / state / branch / memory stay lead-keyed) and pass the rest as `--covers FT-2,FT-3`. The lead's spec gate covers all of them, the PR carries one `Closes <KEY>` per cover, and the commit/PR/reflect steps fan out to close each (`references/verb-spec.md`). Group only tickets that are one coherent change (same files / shared deps); independent tickets stay sequential. A cover must be a distinct, live, non-epic ticket; the bootstrap refuses otherwise.
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
| `slice <ticket>` | slice | `references/verb-slice.md` |
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

The read-only front half: fetch the ticket, design the plan WITH the user, then seed a
worktree, bind the rooted execution context, and flow into the `do` pipeline in this
SAME session. This is the human/machine boundary: the user owns the spec and PR
review; the machine owns everything between. Backgrounding the tail is a host-level
choice.

**Plan approval = Gate 1, the only human gate.** Claude Code presents it with
`ExitPlanMode`. Codex uses its native Plan boundary when active; otherwise it presents
the complete plan, ends the turn, and waits for explicit approval. The generic adapter
uses the same soft turn boundary. Gate on an INDEPENDENT confidence rating (`advisor`
where available, otherwise a fresh independent subagent or second model call), never
self-scored. **On any lane, dissolve forks before asking:** a *fact* reachable by
read-only investigation is yours to resolve; only a *decision* requiring user-only
input reaches the user (`references/verb-spec.md` step 4). On the `full` lane, a rating
below 90% means keep investigating before presenting the gate. The plan always carries
its confidence evidence, verification lane, and (unless disabled) settled e2e recipe.
An effective `express`/`light` lane skips the confidence probe because approval is its
vetting; a hot change still clamps to `full`. Detail: `references/verb-spec.md` and
`references/harness.md`.

On approval (normal mode), bootstrap persists the plan, seeds the worktree, and stamps
`planned_files`, commit metadata, and the e2e recipe into frontmatter. Parse the
absolute returned worktree, update `run_root` and `facade`, optionally call
`EnterWorktree` on Claude Code, verify the binding, and **continue straight into the
`do` loop below**. Correctness never depends on the native switch or a preceding `cd`.

In `--auto` mode the gate never parks: if the headless planner cannot self-approve (a clarifying question, sub-90% confidence, or a `BAIL`), the run defers the ticket in place (status → `deferred`, open questions commented) and exits, rather than asking via `AskUserQuestion` or `ExitPlanMode`.

**Full procedure — interactive steps 1-7, the `--auto` headless path (incl. the defer-and-exit recipe), and the exact `flow_worktree.py create` command: `references/verb-spec.md`.**

The complete Claude Code, Codex, and generic capability mapping is in
`references/harness.md`.

## do verb — the loop

The **executor primitive**: the full ticket→PR pipeline, driven off the dispatcher state machine. `spec` flows into it in the same session (resuming at `implement`); it also runs standalone (`/flow do <ticket>`). The dispatcher (`dispatch_stage.py`) owns `state.json`, the lease, and the canonical snapshot; this prose acts on each descriptor it emits.

The verbose detail — full exit-code matrices, the PR-ready notification protocol + fallback, friction logging, the post-implement reconcile, timeout/drift — lives in **`references/verb-do.md`**. The skeleton below stays inline because it runs every iteration (including in backgrounded runs).

**Friction logging (in-flight):** whenever a step hits a snag the run works around (drift, lost lease, reconcile, missing tool, blocker, failed/retried stage), append one `flow_friction.py` entry before acting on it — it is the evidence the `reflect` stage turns into harness fixes (`references/self-evolution.md`). The trigger→type table + the command are in `references/verb-do.md`.

1. Resolve the ticket key. If `arguments` had a positional, use it. Else:
   ```bash
   KEY=$(.flow/flow branch-ticket --workspace-root .)
   ```
   Exit 0 → use `$KEY`. Exit 3 → no key on branch; ask through the adapter's
   user-input capability. Exit 1 → workspace not initialized; abort with the Flow
   init hint.

2. HARD GATE the workspace:
   ```bash
   .flow/flow validate --workspace-root .
   ```
   Non-zero → surface stderr violations; abort.

3. Initialize the run (acquires the per-ticket lease + writes the canonical snapshot):
   ```bash
   .flow/flow dispatch init \
     --workspace-root . --ticket "$KEY"
   ```
   Capture the `run_id` AND the `session_nonce` from stdout JSON; carry that nonce (`$NONCE`) verbatim on every later `next`/`advance`/`release` call below — it is the per-session lease component that blocks a second `/flow do` from re-acquiring this live lease. Exit 0 → proceed to the loop. Exit 1 (with a `holder` block) or Exit 5 (stale lease) → surface the holder + `/flow recover <ticket>`, abort; do NOT call `release` (nothing was acquired). Full matrix: `references/verb-do.md`.

4. **Orchestration loop** — repeat until done:

   a. Obtain the next `DESCRIPTOR`. On the FIRST iteration (right after `init`), call `next`; on every later iteration, reuse the payload `advance` already returned in step (e) and skip this standalone `next`:
      ```bash
      DESCRIPTOR=$(.flow/flow dispatch next \
        --workspace-root . --ticket "$KEY" --session-nonce "$NONCE")
      ```
      `next` refreshes the lease + verifies the snapshot. Exit 0 → continue to (b) (a self-inflicted *owned* drift — a planned `workspace.toml`/`stage-registry.toml` edit — auto-reconciles upstream in dispatch and returns exit 0 with a `reconciled_drift` marker, so it never trips this exit-1 path). Exit 1 (drift/violations/corrupt) or Exit 7 (lost lease) → surface + `/flow recover <ticket>`, break the loop. Full matrix: `references/verb-do.md`.

   b. Parse `DESCRIPTOR` (JSON):
      - `{"done": true}` → all stages completed. Break to step 5.
      - `{"done": false, "blocked_by": "<stage>", "reason": "<text>"}` → a prior stage is `failed`. Surface the block + reason + `/flow recover <ticket>` hint. Break to step 5.
      - Otherwise → handler descriptor with `stage`, `handler_type`, `head_sha`, `ticket_dir`, `output_path`, `roles`, optional `reference_doc`, `subagent_type`, `skill_name`, `skill_args`.

   c. **Pre-handler hook (records_diff_baseline):** if `descriptor.roles` includes `"records_diff_baseline"`:
      ```bash
      .flow/flow diff record-baseline \
        --stage "$STAGE" --ticket "$KEY" \
        --ticket-dir "$TICKET_DIR" \
        --files "$PLANNED_FILES" \
        --capture-blobs --cwd .
      ```
      `PLANNED_FILES` comes from `.flow/tickets/<KEY>.md` frontmatter (`planned_files = [...]`); if absent, use the adapter's user-input capability. Exit non-zero aborts the stage with status=failed. After implement returns, widen `planned_files` if it touched needed files outside the set; see the **post-implement reconcile** in `references/verb-do.md`.

   d. Dispatch by `handler_type`:

      - **`inline`:** Resolve `REFERENCE_PATH` as the absolute
        `<skill_root>/<descriptor.reference_doc>`, Read it, and follow its prose.
        Determine `status = completed | failed`. An inline stage MAY write a captured
        report to the descriptor's absolute `output_path`; if it does, pass that path
        on `advance`. If not, omit it (an absent inline `.out` is normal).

      - **`subagent:<type>`:** Resolve and Read the absolute `REFERENCE_PATH` first
        when present; it carries the per-stage protocol. If `descriptor.roles` includes `"model_routed"`,
        resolve `M` through the facade. Bind
        `model_pin_applied=false` before spawning. The Claude Code adapter passes
        `model=$M` only when non-empty and then sets `model_pin_applied=true`. Codex and
        any adapter whose spawn API does not accept Claude model names omit the
        parameter, inherit the active model, and leave `model_pin_applied=false`.
        Spawn the adapter's independent agent with this rooted prompt:
        ```
        Workspace root: <absolute run_root>
        Skill root: <absolute skill_root>
        Harness: <claude-code|codex|generic>
        Ticket: <KEY>
        Stage: <STAGE>
        Ticket dir: <absolute descriptor.ticket_dir>
        Reference path: <absolute REFERENCE_PATH, or none>
        Artifact path: <absolute descriptor.output_path>

        You are the <subagent_type> agent for this Flow stage. Your inherited cwd is
        non-authoritative. Use Workspace root as the explicit workdir for every
        command and invoke Flow through the absolute `<Workspace root>/.flow/flow`.
        Prefix every Flow facade call with `FLOW_HARNESS=<Harness>` in that same
        command; never rely on an export. If the command tool has no workdir field,
        self-root every individual call.
        Read ticket.json beneath Ticket dir and the ticket frontmatter at
        <Workspace root>/.flow/tickets/<KEY>.md. Keep every repository read and write
        beneath Workspace root. Follow the embedded per-stage protocol and return the
        complete report; the orchestrator owns Artifact path.

        Per-stage protocol:
        <contents read from REFERENCE_PATH>
        ```
        Capture the complete response with the adapter's exact file-write primitive at
        the absolute `descriptor.output_path`. If that primitive is unavailable, use
        the collision-safe artifact fallback in `references/verb-do.md`; never embed
        model output in a shell command. Verify the file exists before `advance`.

      - **`skill:<name>[:<args>]`** — The descriptor carries `skill_name` + `skill_args` (no raw handler string), and usually NO `reference_doc` (a skill stage's own SKILL.md is the protocol; do not read `reference_doc` for it, and never treat a missing one as an error). Reconstruct the handler string `skill:<skill_name>[:<skill_args>]`, then:
        1. Verify the handler is installed:
           ```bash
           .flow/flow handler \
             --handler "<handler_string>"
           ```
           Exit 1 (not installed) or Exit 2 (manifest invalid) → surface the error, set `STATUS=failed`, fall through to (e) to record it (do NOT bare-break). Exit 0 → the stdout JSON gives authoritative `skill_name` / `skill_args` / `invocation`.
        2. Invoke the skill through the adapter's native skill loader using
           `skill_name`, passing `skill_args` verbatim. If no loader exists, fail this
           configured stage rather than claiming it ran.
        3. Capture its final response at the absolute `descriptor.output_path` with the
           same exact-write contract as the subagent branch. Set `STATUS=completed`
           (or `failed` if the skill reported failure).

      - **`none`** — Skip; transition to (e) with status=completed.

      - **`unknown`** — Should never reach here (validate_workspace catches it). If it does, surface and abort.

   e. Advance the stage — finish it AND fetch the next descriptor in one call:
      ```bash
      DESCRIPTOR=$(.flow/flow dispatch advance \
        --workspace-root . --ticket "$KEY" --session-nonce "$NONCE" \
        --stage "$STAGE" --status "$STATUS" \
        [--output-path "$OUTPUT_PATH"])
      ```
      `advance` is `finish` + `next` in one round-trip: it records HEAD itself (do not pass it), finishes `$STAGE` with `$STATUS`, and returns the NEXT descriptor (parses EXACTLY like `next` in (b), plus a `finished` object). `--output-path` is for subagent/skill stages (and any inline stage that captured output); omit otherwise. It must name an existing, already-written file — `advance` exits 1 without finishing the stage if it is missing; write the file, then re-run the same `advance`. Handle its exit codes exactly as `next` in (a).
      **PR-ready notification (non-`--auto` runs):** when `$STAGE` is
      `review_loop` finishing `completed`, use the adapter's best-effort notification
      capability with the PR URL. Claude Code may use `PushNotification`; Codex and
      generic adapters surface it in-thread, with the durable forge fallback described
      in `references/verb-do.md`. `--auto` skips the host notification.

   f. Loop back to (b) with the `DESCRIPTOR` that `advance` just returned. The standalone `next` in (a) runs only once, for the first stage.

5. After the loop exits — on **every** path (clean done, blocked, drift, or lost lease) — release the lease:
   ```bash
   .flow/flow dispatch release \
     --workspace-root . --ticket "$KEY" --session-nonce "$NONCE"
   ```
   `release` is a no-op when the lease is not ours — the exit-7 takeover case, now including a rotated `session_nonce` — so it is safe to call unconditionally here. Do not call it on the init-abort paths of step 3.

   **`--auto` self-teardown (last act):** when this run was launched with `--auto`, after `release` (and after reading `create_pr.out` for the PR link), schedule the session's own panel teardown as the **last tool call** of the run, then emit the final summary. Recipe + guards: `references/verb-do.md`. An attended run NEVER does this.

   When the loop exited cleanly: surface "ticket <KEY> pipeline complete. State: `cat .flow/runs/<KEY>/state.json | jq`."

   **Then end the turn with the PR link as a distinct, highlighted block — the LAST thing in your message, after a `---` rule, nothing below it.** Full rendering rules (the `---` example, caveat-above-the-rule placement, the skipped-`create_pr` omit case): `references/verb-do.md`.
