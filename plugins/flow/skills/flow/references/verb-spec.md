# spec verb

Full procedure for `/flow spec <ticket>` (and the bare `/flow <ticket>` default). SKILL.md keeps the narrative + the one gate; this is the step-by-step.

The read-only front half: fetch the ticket, design the plan WITH the user, then seed a worktree, enter it, and run the autonomous tail (`do` pipeline) in this same session.
This is the human/machine boundary — you own the spec and the eventual PR review; the machine owns everything between. Backgrounding that tail to run unattended (`/bg`) is your call at any point, not something spec does for you.

If `$ARGUMENTS` carries `--auto` (alias `--aa` / `--yolo`), follow the **Auto-approve path (`--auto`)** below instead of steps 1-7.
That path swaps the interactive plan + `ExitPlanMode` gate for a headless `Plan` subagent that self-approves ONLY when it has no clarifying questions, and otherwise defers the ticket in place and exits (it never parks for a human).
Everything from the bootstrap onward is shared by the self-approve branch; the defer-and-exit branch never reaches the bootstrap.

1. **Be in plan mode.** The front half must perform no writes.
   If you are not already in plan mode, call `EnterPlanMode` before doing anything else.
   (Plan mode also makes `ExitPlanMode` the natural approval gate.)

2. Resolve the ticket key (positional `$ARGUMENTS`, else
   `branch_ticket.py --workspace-root .`).

3. Fetch ticket context **into the conversation** — do NOT write files (plan
   mode forbids it):
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . get --key "$KEY"
   ```
   Read the stdout.
   Explore the codebase read-only (Read/Grep/Glob, or a subagent).
   `recall` is auto-injected at SessionStart; weave relevant prior knowledge into the plan.

4. Iterate the implementation plan with the user: goal, files to change, approach, test strategy, risks.
   This is the same depth a `subagent:Plan` handler would produce — but interactive, so the user shapes it.
   **If the workspace opts into e2e** (`workspace.toml [pipeline.handlers] e2e` is not `none`), the plan MUST also settle the **e2e recipe** — this is the moment to decide it, while you (and any live tracker/AWS auth) are present.
   Elicit from the user: which suite/runner the e2e stage runs, the exact command + any env-prep it needs, the fixture (the concrete input — a sample id, account, dataset), and the expected pass signal.
   If this ticket has no meaningful e2e, settle that too — the recipe value becomes `skip: <reason>` or `test-ci-only`. The point is a conscious decision per ticket, never a silent omission.
   The bootstrap in step 6 **refuses** when e2e is enabled and no recipe is passed, so do not skip this.

   **Confidence rating (MUST, before step 5's gate) — assessed independently, not self-scored.** A plan's author is the worst judge of its confidence; optimism bias makes a self-reported score self-justifying. Hand it to a second mind. Preferred: the `advisor` tool — it auto-forwards the full transcript (the ticket, your exploration, the drafted plan), so in the SAME turn state what you want back — **Score (0-100%)**, **Proven** (bullets directly verified: code read, spec quoted, real data/DB inspected), **Inferred** (from convention / naming / a 1:1-chain argument), **What would raise it** (concrete reachable artefacts) — then call `advisor()`. If `advisor` is not in this harness (a `ToolSearch` for it returns nothing), spawn a `general-purpose` `Agent` instead, handing it the ticket context + the drafted plan text + that same rubric. Record the result as the plan's `## Confidence` section, attributed to the assessor. Library-API claims (a Polars/Pandas idiom, a framework hook, an SDK call) must be Context7-verified, never left under "Inferred".

5. **`ExitPlanMode`** with the plan = Gate 1, the one human gate.
   **Gate on the rating: < 90% → do NOT `ExitPlanMode` yet.** First exhaust every reachable read-only artefact (Read/Grep/Glob, an `Explore` agent, WebSearch/WebFetch, read-only MCPs, `aws s3 ls/cp`), then re-run the assessor. For a gap that needs user action (an SSO refresh, a bucket name, an owner's confirmation, an internal doc), ask via `AskUserQuestion` with specifics — never wave at it. Present only at >=90%, or when every reachable source is exhausted and the residual is documented as a risk the user can weigh. Anti-pattern this directly fixes: producing the confidence number only after the user asks for it — the rating is part of the plan, surfaced unprompted, every time.
   On approval you return to normal mode.

6. (Normal mode) Persist the approved plan and bootstrap the worktree.
   The tail branches off whatever `--base` you pass. Interactive: branch off your integration branch (the example uses the current branch) — stacking on a feature branch is a feature. Autonomous (`--auto`): pass `--base @default` instead, so the run branches off the freshly-fetched default branch and never inherits the launcher's HEAD (see step 5's auto-approve branch).
   ```bash
   PLAN=/tmp/flow-plan-$KEY.md   # write the approved plan text here (Write tool)
   python3 ${CLAUDE_SKILL_DIR}/scripts/flow_worktree.py create \
     --ticket "$KEY" \
     --plan-from "$PLAN" \
     --base "$(git rev-parse --abbrev-ref HEAD)" \
     --branch "feature/$KEY-<slug>" \
     --main-root . \
     --planned-files "<comma-separated files the plan will touch>" \
     --commit-type <feat|fix|chore|...> \
     --commit-summary "<one-line summary from the plan>" \
     --e2e-recipe "<the e2e recipe from step 4 — omit ONLY when e2e is none>"
   ```
   Derive `<slug>` from the ticket summary, and `--planned-files` from the plan's "Files to change" list — which (per stage-plan.md) already includes any anticipated NEW test file paths the TDD implement will create, so the stamped `planned_files` covers them.
   Any version-bump number that appears in the plan is advisory: it was derived pre-bootstrap from the launcher checkout, which can lag `origin/main`, so the implement stage recomputes the bump against the worktree base.
   `--e2e-recipe` carries the recipe settled in step 4 (runner + command + env-prep + fixture + expected, or `skip: <reason>` / `test-ci-only`); pass it whenever e2e is enabled and omit it only when the handler is `none`.
   The bootstrap seeds state (plan pre-completed, ticket left pending), injects the plan, stamps `planned_files` + `commit_type` + `commit_summary` (+ `e2e_recipe` when given) into frontmatter (so the implement pre-hook, the commit stage, and the e2e stage never pause to ask the user — which is what lets the tail run unattended if you background it), points the worktree's memory store at this checkout's `.flow` (shared, so memory compounds across worktrees), copies gitignored config, and `mise trust`s the worktree.
   If e2e is enabled and you omit `--e2e-recipe`, create exits 2 (`_ConfigError`) — go back to step 4 and settle the recipe.
   Surface any `WARN` lines (e.g. mise trust failures — the tail would die on the first `mise run`).

7. **Enter the worktree and continue the pipeline in this same session.**
   The bootstrap printed the worktree path (`result.worktree` in its stdout JSON). Switch this session into it:
   ```
   EnterWorktree(path="<worktree>")
   ```
   This moves the conversation's cwd into the seeded worktree, carrying the full planning context with it. It also pre-empts the harness's auto-worktree-on-first-edit (which is skipped once the session is already inside a linked worktree), so the pipeline runs in *this* base-controlled, config-copied worktree rather than a fresh one.
   Then **continue straight into the `do` verb's orchestration loop** for `$KEY` (the do verb in SKILL.md, from its step 1). `do`'s `init` resumes idempotently under the `run_id` the bootstrap seeded (plan already `completed`, `ticket` pending), so it skips the done plan and lands on `implement`, which reads `plan.out`. The resume is driven entirely by `state.json` on disk, so it behaves identically whether spec flowed in or `do` was invoked standalone.

   **Running unattended is your call, not the pipeline's.** The pipeline is background-agnostic: it runs the same whether this session is attached or detached. At any point — before approving the plan, right after, or mid-implement — you (the user) can `/bg` (or press `←` on an empty prompt) to background this session; it continues the pipeline unattended and shows up in `claude agents` (attach to peek, answer a blocker that surfaces as needs-input, detach). The deliverable is a draft PR you review.
   See `references/background-pipeline.md`.

## Auto-approve path (`--auto`)

For tickets you already know are simple and whose body is descriptive: auto-approve the plan WITHOUT your intervention, but ONLY when the planner has no clarifying questions.
This is a conditional gate, not a blanket skip. It branches on the headless planner's output: a clean, high-confidence plan self-approves; anything else (clarifying questions, sub-90% confidence, or a `BAIL`) defers the ticket in place and exits.
It replaces interactive steps 1-5. The self-approve branch then runs shared steps 6-7 (bootstrap + enter worktree) exactly as above; the defer-and-exit branch runs neither.

1. **Do NOT `EnterPlanMode`.**
   The headless path performs only reads until the intended bootstrap write — there is no interactive plan to gate, so the plan-mode lock is unnecessary.
   Keep the reads read-only by discipline; the first write is the bootstrap in shared step 6 (or, when the planner cannot self-approve, the defer-and-exit comment in step 5, the only non-bootstrap write).

2. Resolve the ticket key (positional `$ARGUMENTS` minus the flags, else `branch_ticket.py --workspace-root .`) — same as step 2.

3. Fetch ticket context into the conversation via `tracker_cli.py --workspace-root . get --key "$KEY"` (read the stdout); explore the codebase read-only; weave in the SessionStart `recall` — same as step 3.

4. **Decided-mode probe — then the headless plan.**
   First probe whether the maintainer already triaged + reopened this bead with a recorded decision. Without this, an `--auto` relaunch re-defers on the exact question already answered (the triage→reopen→re-defer loop never converges):
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/triage.py decided --workspace-root . --key "$KEY"
   ```
   It always emits one JSON object `{"decided": bool, "answer": str|null, "is_hot": bool}` (never raises; a bd-read failure reads as `decided:false, is_hot:true`). When `decided` is true, INJECT the `answer` into the Plan subagent prompt as AUTHORITATIVE — a line like: "the maintainer has already decided this: <answer>; treat it as settled, do NOT raise it as a clarifying question." Carry the `decided` flag into step 5's branch.

   Read `${CLAUDE_SKILL_DIR}/references/stage-plan.md`, then spawn the `Plan` subagent embedding that protocol PLUS the output contract below.
   The subagent runs PRE-bootstrap, so `.flow/runs/<KEY>/ticket.json` and `.flow/tickets/<KEY>.md` do NOT exist yet (the `ticket` stage writes ticket.json; `flow_worktree.py create` writes tickets/<KEY>.md — both run later). Do NOT point the subagent at those files. Instead, INLINE the ticket JSON you already fetched in step 3 (`tracker_cli.py get`) into the embedded ticket-context block below, pasting it verbatim where the placeholder sits:
   ```
   Agent(
     subagent_type="Plan",
     description="plan (auto) for <KEY>",
     prompt="""
     Ticket: <KEY>
     You are the Plan subagent for the plan stage of /flow, running in --auto mode.

     Ticket context (fetched by the orchestrator in step 3 — this is your primary
     source of intent; the pre-bootstrap files do NOT exist yet):
     <the ticket JSON from step 3's tracker_cli.py get, pasted here verbatim>

     Per-stage protocol (from references/stage-plan.md):
     <contents of stage-plan.md>

     Produce the plan with its normal sections, THEN end your report with a
     machine-readable block, exactly one of:
       - the literal line `NONE` under a `## CLARIFYING QUESTIONS` heading when
         the ticket is unambiguous and you are confident the plan is approvable
         as-is;
       - a `## CLARIFYING QUESTIONS` heading followed by one `- <question>`
         bullet per genuinely open decision a human must settle before code is
         written (competing interpretations, an unconfirmed assumption, a missing
         input). Only raise a question if its answer would change the plan.
     If you cannot produce a plan at all (the embedded ticket context is empty, or
     zero usable intent), return a single line `BAIL: <reason>` instead of a plan.
     """
   )
   ```
   A `BAIL` line routes to the defer-and-exit branch in step 5 (the bail reason becomes the comment text).
   Capture the full response.
   Then get the same INDEPENDENT confidence rating as interactive step 4 — call `advisor()` (or a `general-purpose` `Agent` if advisor is absent) over the captured plan. Its score feeds the branch below.

5. **Branch on the returned block:**
   - **`NONE` (clean plan) AND the assessor rated >=90%** → auto-approve, no human gate.
     Derive `--planned-files` from the plan's "Files to change" list — which (per stage-plan.md) already includes any anticipated NEW test file paths the TDD implement will create, so the stamped `planned_files` covers them — and `--commit-type` + `--commit-summary` from the Goal.
     For `--e2e-recipe`, honor step 6's contract: when e2e is enabled (`workspace.toml [pipeline.handlers] e2e` is not `none`), pass the `--e2e-recipe "..."` value the user gave, else default it to `test-ci-only`; when the e2e handler is `none`, omit it.
     **Base off `--base @default`, NOT the current branch.** An autonomous run (the evolve `drain` loop fires `claude --bg "/flow <key> --auto"` from whatever branch the cockpit is on) must branch off the freshly-fetched default branch, never the launcher's HEAD — else the PR inherits the launcher's unmerged/stale commits and lands DIRTY. `@default` makes `flow_worktree.py` fetch origin and resolve `origin/<HEAD>`.
     Go straight to shared step 6 — there is no `ExitPlanMode` to call, because you never entered plan mode.
   - **Clarifying questions present, a sub-90% rating with any user-reachable gap, OR a `BAIL` line** (a residual wall) → the disposition depends on whether step 4's probe reported `decided`:
     - **NOT decided** → **defer-and-exit** (unchanged). An `--auto` run never parks for a human (the launcher walked away, so there is nobody to ask). Instead the run comments the open questions on the original ticket, sets its status to `deferred`, and exits cleanly. It does NOT `EnterPlanMode`, does NOT degrade to interactive, does NOT bootstrap a worktree, and does NOT mint a follow-up bead. A `deferred` ticket drops out of `bd ready`, so an autonomous relaunch loop (the evolve `drain` loop) stops re-launching it. Run exactly, in order:
       ```bash
       # 1. comment the open questions / bail reason on the original ticket (tracker-agnostic seam)
       python3 ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . comment \
         --key "$KEY" \
         --text "flow --auto could not self-approve: <clarifying questions, or the BAIL reason>. To unstick: answer here, reopen (status->open), and re-run WITHOUT --auto to plan interactively."
       # 2. defer the ticket in place so it leaves bd ready (beads-native; tracker_cli transition has no deferred target)
       bd update "$KEY" --status deferred
       ```
       Then emit a terse `deferred <KEY>: <reason>` line (so an attended `--auto` run shows why it stopped) and STOP. No `EnterPlanMode`, no bootstrap (`flow_worktree.py create`), no `EnterWorktree`, no do-loop, no follow-up bead.
       The behavior ("`--auto` never parks") is universal; the `bd update --status deferred` command is the beads instance (the autonomous relaunch loop this serves, the evolve `drain` loop, is beads/maintainer-only).
     - **Decided** → NO plain re-defer (the judgment question is already answered; re-deferring on it would just re-loop). The wall now is an *implementation* block, not a judgment one. Re-probe with the plan's planned-files to classify hotness:
       ```bash
       python3 ${CLAUDE_SKILL_DIR}/scripts/triage.py decided --workspace-root . --key "$KEY" --files "<plan's planned-files, comma-separated>"
       ```
       - **`is_hot` true** → **block** (never blind-ship a guard change). Comment the NEW residual wall — word it distinctly so it reads as a *post-decision implementation block*, not a re-ask of the answered question, but still CONTAIN the stem `flow --auto could not self-approve` so the `/flow triage` scan surfaces it — then set status to `blocked` (NOT `deferred`, NOT a `tracker_cli` transition):
         ```bash
         python3 ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . comment \
           --key "$KEY" \
           --text "flow --auto could not self-approve: post-decision implementation block on a hot change — <the residual wall>. The judgment is settled, this is an implementation/safety concern. To unstick: answer here, reopen (status->open), and re-run WITHOUT --auto."
         bd update "$KEY" --status blocked
         ```
         Then emit a terse `blocked <KEY>: <reason>` line and STOP. No bootstrap, no worktree, no PR. A `blocked` bead drops out of `bd ready` (no relaunch loop) and surfaces in `/flow triage`.
       - **`is_hot` false** (clean change) → **proceed best-effort**: self-approve the strongest plan and go to shared step 6 (bootstrap), exactly as the clean-and-≥90% branch above. A clean decided bead self-ships, CI-gated only — wrong-but-compiling can land for clean decided beads.

   The two outcomes: (a) **self-approve** → shared bootstrap + enter-worktree (steps 6-7), then the tail; or (b) **cannot self-approve** → defer-and-exit (no bootstrap, no worktree, no tail).
   `--auto`'s only effect on the self-approve branch is skipping the interactive plan gate; it does not change how the tail runs. As always, whether the tail runs unattended is the user's separate `/bg` choice (see step 7), independent of `--auto`.
