# spec verb

Full procedure for `/flow spec <ticket>` (and the bare `/flow <ticket>` default). SKILL.md keeps the narrative + the one gate; this is the step-by-step.

The read-only front half: fetch the ticket, design the plan WITH the user, then seed a worktree, enter it, and run the autonomous tail (`do` pipeline) in this same session.
This is the human/machine boundary — you own the spec and the eventual PR review; the machine owns everything between. Backgrounding that tail to run unattended (`/bg`) is your call at any point, not something spec does for you.

If `$ARGUMENTS` carries `--auto` (alias `--aa` / `--yolo`), follow the **Auto-approve path (`--auto`)** below instead of steps 1-7.
That path swaps the interactive plan + `ExitPlanMode` gate for a headless `Plan` subagent that self-approves ONLY when it has no clarifying questions — a shaky plan first gets one bounded close-the-holes revision round (step 4) — and otherwise defers the ticket in place and exits (it never parks for a human).
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
   **Verify any content/drift finding against the default base, not the working checkout.** General orientation reads stay on the working checkout via the Read tool (you do NOT need to `git show` every file). But the moment you would CITE a content/drift finding in the plan, or derive a `--planned-files` entry (step 6) BECAUSE OF a file's current content, re-read that specific file at the freshly-fetched default base first. The tail branches off `@default` (`origin/<default>`, fetched fresh) while this checkout can lag `origin/main`, so a drift seen here may already be fixed upstream and the planned fix would land as a no-op (flow-749). Resolve the base the way `flow_worktree.py create --base @default` does and read the base version:
   ```bash
   git fetch --quiet origin
   DEFAULT=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD)   # e.g. origin/main
   git show "$DEFAULT:<path>"   # the base version of the file you'd cite
   ```
   The `git fetch` is read-only by discipline (only remote-tracking refs / FETCH_HEAD, safe under plan mode, same as the `aws s3 cp` artefact step 5 lists). A content/drift finding is cited at plan time and may stamp `planned_files`, so it must be verified against the right base now and cannot be deferred to implement.

4. Iterate the implementation plan with the user: goal, files to change, approach, test strategy, risks.
   This is the same depth a `subagent:Plan` handler would produce — but interactive, so the user shapes it.
   **If the workspace opts into e2e** (`workspace.toml [pipeline.handlers] e2e` is not `none`), the plan MUST also settle the **e2e recipe** — this is the moment to decide it, while you (and any live tracker/AWS auth) are present.
   Elicit from the user: which suite/runner the e2e stage runs, the exact command + any env-prep it needs, the fixture (the concrete input — a sample id, account, dataset), and the expected pass signal.
   If this ticket has no meaningful e2e, settle that too — the recipe value becomes `skip: <reason>` or `test-ci-only`. The point is a conscious decision per ticket, never a silent omission.
   The bootstrap in step 6 **refuses** when e2e is enabled and no recipe is passed, so do not skip this.

   **Confidence rating (MUST, before step 5's gate) — assessed independently, not self-scored.** A plan's author is the worst judge of its confidence; optimism bias makes a self-reported score self-justifying. Hand it to a second mind. Preferred: the `advisor` tool — it auto-forwards the full transcript (the ticket, your exploration, the drafted plan), so in the SAME turn state what you want back — **Score (0-100%)**, **Proven** (bullets directly verified: code read, spec quoted, real data/DB inspected), **Inferred** (from convention / naming / a 1:1-chain argument), **What would raise it** (concrete reachable artefacts) — then call `advisor()`. **Fable short-circuit:** when the session model is Fable (`claude-fable-*`), `advisor` is absent by design and not needed — skip the `ToolSearch` probe and go straight to the fallback. For other models, fall back only after a `ToolSearch` for `advisor` returns nothing, or if `advisor` errors / is rate-limited. The fallback: spawn a `general-purpose` `Agent` instead, handing it the ticket context + the drafted plan text + that same rubric. Record the result as the plan's `## Confidence` section, attributed to the assessor. Library-API claims (a Polars/Pandas idiom, a framework hook, an SDK call) must be Context7-verified, never left under "Inferred".

5. **`ExitPlanMode`** with the plan = Gate 1, the one human gate.
   **Gate on the rating: < 90% → do NOT `ExitPlanMode` yet.** First exhaust every reachable read-only artefact (Read/Grep/Glob, an `Explore` agent, WebSearch/WebFetch, read-only MCPs, `aws s3 ls/cp`), then re-run the assessor. For a gap that needs user action (an SSO refresh, a bucket name, an owner's confirmation, an internal doc), ask via `AskUserQuestion` with specifics — never wave at it. Present only at >=90%, or when every reachable source is exhausted and the residual is documented as a risk the user can weigh. Anti-pattern this directly fixes: producing the confidence number only after the user asks for it — the rating is part of the plan, surfaced unprompted, every time.
   On approval you return to normal mode.

6. (Normal mode) Persist the approved plan and bootstrap the worktree.
   The tail branches off whatever `--base` you pass. Interactive: branch off your integration branch (the example uses the current branch) — stacking on a feature branch is a feature. Autonomous (`--auto`): pass `--base @default` AND `--auto`, so the run branches off the freshly-fetched default branch (never inheriting the launcher's HEAD) and the bootstrap code-enforces the hot hard-floor (see step 5's auto-approve branch).
   ```bash
   PLAN="${TMPDIR:-/tmp}/flow-plan-$KEY.md"   # write the approved plan text here (Write tool); bare /tmp is not sandbox-writable
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
   `--e2e-recipe` carries the recipe settled in step 4 (runner + command + env-prep + fixture + expected, or `skip: <reason>` / `test-ci-only`); pass it whenever e2e is enabled and omit it only when the handler is `none`.
   The bootstrap seeds state (plan pre-completed, ticket left pending), injects the plan, stamps `planned_files` + `commit_type` + `commit_summary` (+ `e2e_recipe` when given) into frontmatter (so the implement pre-hook, the commit stage, and the e2e stage never pause to ask the user — which is what lets the tail run unattended if you background it), points the worktree's memory store at this checkout's `.flow` (shared, so memory compounds across worktrees), copies gitignored config, and `mise trust`s the worktree.
   If e2e is enabled and you omit `--e2e-recipe`, create exits 2 (`_ConfigError`) — go back to step 4 and settle the recipe.
   **Hot hard-floor (code-enforced).** When the bootstrap is autonomous (`--auto`, or a `@default` base) on a beads tracker, create refuses (exit 2) if `--planned-files` trips `is_hot_change` (a guard/safety file, or a `hot`-labelled bead) and the bead carries no recorded `DECISION:`/`TRIAGE-DECISION:` comment. This is the floor's real enforcer: step 5's prose only carried it in the adjudication/decided sub-branches, so a clean re-plan could self-ship a hot change past it (flow-aen). On this refusal the auto path treats it as a hot block (defer-stem comment + `bd status blocked`, per step 5), exactly as if adjudication had blocked. Interactive runs are not gated here — `ExitPlanMode` is the human floor. `[evolve] adjudicate_hot = true` (maintainer self-target, default off) makes `create` skip this refusal, so a hot change bootstraps on an advisor proceed; the merge-time guard-property review remains the gate.
   **Duplicate-claim refusal (exit 4).** `create` transiently holds a canonical per-ticket bootstrap claim (a flock on the main checkout's `.flow/tickets/$KEY.claim`, released at bootstrap exit) and refuses with **exit 4** when a live sibling run already holds this ticket — a sibling worktree on the ticket's feature branch with a live (or corrupt) run lease, or a seeded non-terminal `state.json`. This is NOT the exit-2 hot block: the defer/block recipes above never apply to an exit 4.
   - Interactive: exit 4 → a live sibling run already holds this ticket (the message names its worktree + state). Surface it + the `/flow recover $KEY` hint; STOP. Do not retry, do not reap by hand.
   - `--auto`: exit 4 → a sibling run owns this ticket. Emit one terse `superseded <KEY>: sibling run live` line and STOP — exit silently-clean: NO `tracker_cli comment`, NO `bd update` (no defer, no block), NO friction entry, no follow-up bead. The sibling owns the bead and its status.
   **Terminal-bead refusal (exit 6).** `create` re-reads the bead's authoritative status at the bootstrap chokepoint (tracker-agnostic, before any git mutation) and refuses with **exit 6** when it is terminal (normalized `done`/`cancelled`). This catches the flow-d6gq case: a bead that was open at spec-fetch but closed during planning (e.g. a parent epic merged). Unconditional — interactive and `--auto` alike, since bootstrapping a closed bead is wrong either way. Fail-open is narrow: a genuine tracker read *exception* proceeds (a flaky read never strands a legit run), but a successful-but-incoherent status read also refuses. This is NOT the exit-2 hot block and NOT the exit-4 dup-claim: the bead is legitimately done, so there is nothing to defer or block.
   - Interactive: exit 6 → the bead is closed/done; surface "bead <KEY> is closed — nothing to bootstrap" + the reopen hint; STOP.
   - `--auto`: exit 6 → emit one terse `closed <KEY>: bead already terminal` line and STOP — exit silently-clean: NO `tracker_cli comment`, NO `bd update`, NO friction entry, no follow-up bead. The bead is done; its status is already correct.
   Surface any `WARN` lines (e.g. mise trust failures — the tail would die on the first `mise run`).

7. **Enter the worktree and continue the pipeline in this same session.**
   The bootstrap printed the worktree path (`result.worktree` in its stdout JSON). Switch this session into it:
   ```
   EnterWorktree(path="<worktree>")
   ```
   This moves the conversation's cwd into the seeded worktree, carrying the full planning context with it. It also pre-empts the harness's auto-worktree-on-first-edit (which is skipped once the session is already inside a linked worktree), so the pipeline runs in *this* base-controlled, config-copied worktree rather than a fresh one.

   **Backgrounded `--auto` fallback (cwd pinned at the repo root).** A `claude --bg /flow <key> --auto` run has its session cwd pinned at the repository root, so `EnterWorktree(path=<worktree>)` refuses ("current working directory is the repository root, not an isolated worktree — switching is only available to sessions whose working directory is inside a worktree"), and the harness bg-isolation guard rejects `Edit`/`Write` inside the linked `.flow/worktrees/<...>` worktree for both this session and any subagent it spawns (a subagent's cwd is pinned too). This is a Claude Code bg-harness cwd-pin interaction the run works around in prose, not a flow bug. Workaround (proven flow-ztfv): `cd` the persistent Bash cwd into the worktree FIRST — a single `cd "<worktree>"` in a Bash call moves the session cwd into the linked worktree. After that, still call `EnterWorktree(path="<worktree>")`: it reports "is the current working directory" — a harmless idempotent no-op here, and still correct for an attached run. Then drive the entire `do` loop via Bash with `--workspace-root .`, which now resolves against the worktree because the Bash cwd is inside it. Spawned subagents (implement, plan) must make their edits via Bash/Python string-replace against absolute worktree paths, since the guard blocks their `Edit`/`Write` and their cwd is the repo root.
   Then **continue straight into the `do` verb's orchestration loop** for `$KEY` (the do verb in SKILL.md, from its step 1). `do`'s `init` resumes idempotently under the `run_id` the bootstrap seeded (plan already `completed`, `ticket` pending), so it skips the done plan and lands on `implement`, which reads `plan.out`. The resume is driven entirely by `state.json` on disk, so it behaves identically whether spec flowed in or `do` was invoked standalone.

   **Running unattended is your call, not the pipeline's.** The pipeline is background-agnostic: it runs the same whether this session is attached or detached. At any point — before approving the plan, right after, or mid-implement — you (the user) can `/bg` (or press `←` on an empty prompt) to background this session; it continues the pipeline unattended and shows up in `claude agents` (attach to peek, answer a blocker that surfaces as needs-input, detach). The deliverable is a draft PR you review.
   See `references/background-pipeline.md`.

## Auto-approve path (`--auto`)

For tickets you already know are simple and whose body is descriptive: auto-approve the plan WITHOUT your intervention, but ONLY when the planner has no clarifying questions.
This is a conditional gate, not a blanket skip. It branches on the headless planner's output: a clean, high-confidence plan self-approves; a shaky one first spends exactly ONE close-the-holes revision round (step 4); only a wall that survives it — user-only information, or a change genuinely unsafe to auto-ship — defers/blocks the ticket in place and exits.
It replaces interactive steps 1-5. The self-approve branch then runs shared steps 6-7 (bootstrap + enter worktree) exactly as above; the defer-and-exit branch runs neither.

1. **Do NOT `EnterPlanMode`.**
   The headless path performs only reads until the intended bootstrap write — there is no interactive plan to gate, so the plan-mode lock is unnecessary.
   Keep the reads read-only by discipline; the first write is the bootstrap in shared step 6 (or, when the planner cannot self-approve, the defer-and-exit comment in step 5, the only non-bootstrap write).

2. Resolve the ticket key (positional `$ARGUMENTS` minus the flags, else `branch_ticket.py --workspace-root .`) — same as step 2.

3. Fetch ticket context into the conversation via `tracker_cli.py --workspace-root . get --key "$KEY"` (read the stdout); explore the codebase read-only; weave in the SessionStart `recall` — same as step 3.
   The drift-vs-`@default` rule (verify any cited content/drift finding against the freshly-fetched default base) lives in the `stage-plan.md` embedded into the Plan subagent in step 4; it is that subagent's plan, not this orchestrator's own explore, that derives `planned_files`, so the rule is enforced there.

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
   Then get the same INDEPENDENT confidence rating as interactive step 4 — call `advisor()` (or a `general-purpose` `Agent` if advisor is absent or errors / is rate-limited; on a Fable model skip the probe, advisor is absent by design) over the captured plan. Its score feeds the branch below.

   **Close-the-holes revision round (exactly one).** **Trigger**: the assessor rated sub-90%, OR the plan's clarifying questions include any answerable from the repo itself. Closeable holes are the flow-5fp witness classes: a missing test the implementer can write, an unmapped error/edge case it can handle, an unacknowledged-but-correct behavior change it can document, an unverified claim about code it can read. **Skip** the round on a `BAIL` (no plan to revise), on a plan whose only gaps are genuinely user-only, and on a decided bead (the decided sub-branch rules on hotness, not plan quality — a revision round there is a category error). Otherwise re-spawn the `Plan` subagent ONCE (same embedding as above), handing it the prior plan + the assessor's named gaps + every self-answerable question, instructing it to (a) fold each closeable hole into the plan as an explicit implementer commitment — the named failing test into Files to change/Test strategy, the error mapping into Approach, the behavior change documented in Approach/Risks; (b) raise confidence via read-only verification only (read the cited code paths, read-only probes, Context7 for library claims) — the path stays read-only until the bootstrap, so closing a hole here means committing the implement stage to close it, not editing now; (c) re-emit the full plan keeping only genuinely user-only questions in the `## CLARIFYING QUESTIONS` block. Re-score once with the same independent assessor. **Hard bound: ONE round per run** — never a second revision, never a loop. Rationale (maintainer policy, flow-5fp): this repo is low-stakes, merges revert cheaply, the merge-time gates are unchanged, false negatives are accepted — a closeable hole parked on a human is the costlier error.

   **Infra-failure branch (the spawn itself errors).** **Trigger**: any agent/advisor spawn in steps 4-5 — the Plan subagent, the independent assessor, the step-5 adjudication agent — errors environmentally: a spend/usage-limit error, an API/provider outage, the harness refusing to create the subagent. This is the exception to this section's "branches on the headless planner's output" framing — no planner ran at all, so there is no output to branch on. It is NOT a `BAIL`: `BAIL` is the planner's *output* (ticket-intrinsic, routed to step 5's defer-and-exit), and a planner that ran but returned something unusable also stays with the `BAIL`/defer machinery. **Disposition**: leave the bead **open and untouched** — NO `tracker_cli comment`, NO `bd update` (no defer, no block), NO friction entry, no follow-up bead, no bootstrap, no worktree. Emit one terse `failed <KEY>: <infra reason>` line and STOP. The wall is environmental, not ticket-intrinsic — `deferred`/`blocked` would drop the bead out of `bd ready` even after the limit resets, forcing a manual reopen, while an open bead relaunches cleanly from scratch (proven on flow-aod). When a drain loop fanned the run out, per-key backoff already exists: drain writes a `launch_ledger` marker at launch time, removed only when the run registers a lease/branch (TTL 1800s) — an infra-failed run never registers, so the key stays throttled until TTL expiry (~30 min). Accepted residual: a drain pass can still launch OTHER keys into the same global wall; each such launch also exits leave-open/no-writes, so the damage is wasted launches, not state corruption.

5. **Branch on the returned block.** When there IS no returned block because the spawn itself failed, this step never fires — route to step 4's infra-failure branch instead of forcing a defer. Whether a judgment fork is adjudicated or deferred depends on the `advisor_adjudicates` flag:
   ```bash
   ADJ=$(python3 ${CLAUDE_SKILL_DIR}/scripts/triage.py adjudicate-enabled --workspace-root .)
   ```
   `ADJ=true` (**the default** — on unless explicitly disabled) → skip to the **advisor-adjudication branch** below. `ADJ=false` (explicit opt-out via `[evolve] advisor_adjudicates = false`, restoring the old defer-on-fork behavior) → follow the **opt-out branch** directly here. The safety nets hold either way (the hot hard-floor, the broad-blast block, and the PR review/merge keystone are in both branches); the only difference is whether a judgment fork is ruled on or parked for the human.

   **Opt-out branch (`advisor_adjudicates = false`):**
   - **`NONE` (clean plan) AND the assessor rated >=90%** → auto-approve, no human gate.
     Derive `--planned-files` from the plan's "Files to change" list — which (per stage-plan.md) already includes any anticipated NEW test file paths the TDD implement will create, so the stamped `planned_files` covers them — and `--commit-type` + `--commit-summary` from the Goal.
     And mind drift: any `planned_files` entry the plan stamped because of a file's CURRENT content (a content/drift finding — "this row/line is stale, so touch this file") is advisory, since it was read pre-bootstrap from the launcher checkout, which can lag `origin/main`. Before stamping it, re-verify the finding against the base `--base @default` will resolve to — `git fetch origin`, then `git show origin/<default-branch>:<path>` to re-read the cited content there — and DROP the entry if that base already has it fixed. This keeps the drift-vs-base discipline even though the self-derive shortcut skips the `Plan` subagent (where the plan would otherwise be re-grounded).
     For `--e2e-recipe`, honor step 6's contract: when e2e is enabled (`workspace.toml [pipeline.handlers] e2e` is not `none`), pass the `--e2e-recipe "..."` value the user gave, else default it to `test-ci-only`; when the e2e handler is `none`, omit it.
     **Base off `--base @default`, NOT the current branch.** An autonomous run (the evolve `drain` loop fires `claude --bg "/flow <key> --auto"` from whatever branch the cockpit is on) must branch off the freshly-fetched default branch, never the launcher's HEAD — else the PR inherits the launcher's unmerged/stale commits and lands DIRTY. `@default` makes `flow_worktree.py` fetch origin and resolve `origin/<HEAD>`.
     Go straight to shared step 6 — there is no `ExitPlanMode` to call, because you never entered plan mode.
   - **Clarifying questions present, a sub-90% rating with any user-reachable gap, OR a `BAIL` line** (a residual wall — read against the POST-revision plan and score: step 4's single close-the-holes round is already spent where it applied, so reaching here means self-raising was exhausted, not skipped) → the disposition depends on whether step 4's probe reported `decided`:
     - **NOT decided** → **defer-and-exit** (unchanged). An `--auto` run never parks for a human (the launcher walked away, so there is nobody to ask). Instead the run comments the open questions on the original ticket, sets its status to `deferred`, and exits cleanly. It does NOT `EnterPlanMode`, does NOT degrade to interactive, does NOT bootstrap a worktree, and does NOT mint a follow-up bead. A `deferred` ticket drops out of `bd ready`, so an autonomous relaunch loop (the evolve `drain` loop) stops re-launching it. Run exactly, in order:
       ```bash
       # 1. comment the open questions / bail reason on the original ticket (tracker-agnostic seam)
       #    APPEND the structured [defer-reason: ...] tag — see the classification rule below.
       python3 ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . comment \
         --key "$KEY" \
         --text "flow --auto could not self-approve: <clarifying questions, or the BAIL reason>. To unstick: answer here, reopen (status->open), and re-run WITHOUT --auto to plan interactively. [defer-reason: <no-question|open-question>]"
       # 2. defer the ticket in place so it leaves bd ready (beads-native; tracker_cli transition has no deferred target)
       bd update "$KEY" --status deferred
       ```
       Then emit a terse `deferred <KEY>: <reason>` line (so an attended `--auto` run shows why it stopped) and STOP. No `EnterPlanMode`, no bootstrap (`flow_worktree.py create`), no `EnterWorktree`, no do-loop, no follow-up bead.
       The behavior ("`--auto` never parks") is universal; the `bd update --status deferred` command is the beads instance (the autonomous relaunch loop this serves, the evolve `drain` loop, is beads/maintainer-only).

       **Structured defer-reason (the `[defer-reason: ...]` tag).** The tag is a sub-field appended INSIDE the existing `flow --auto could not self-approve` triage-stem comment — NOT a competing stem (`/flow triage` still matches the stem, and the drain deferred-scan reads the tag from it). It distinguishes a defer a stronger model could clear from one that genuinely needs a human:
       - `[defer-reason: open-question]` — the defer carries a SUBSTANTIVE open question a human must answer: the plan's `## CLARIFYING QUESTIONS` block is non-empty, OR the wall is maintainer-only information, OR the `BAIL` reason is empty/zero-usable-intent ticket context (a stronger model cannot invent the missing intent). NOT escalatable — it needs an answer, not a bigger model.
       - `[defer-reason: no-question]` — the run gave up WITHOUT a substantive question: a planning-give-up `BAIL` (a plan a stronger model could plausibly produce), or a bare confidence shortfall (the assessor scored low but the plan raised no specific user-answerable gap). Escalatable — the evolve drain deferred-scan (verb-evolve.md §A3) reopens these and the sonnet→opus ladder retries once at opus.

       The two adjudication-branch defers below (the sub-70% floor and the advisor-`defer` verdict) append this SAME tag, each stamping the reason per this rule.
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

   **Advisor-adjudication branch (`advisor_adjudicates = true`, the default):**
   Same clean self-approve, but a judgment fork is RULED ON by a strong independent mind instead of parked, and the flat 90% number stops being a hard cliff — it folds into the ruling.
   - **`NONE` (clean plan) AND the assessor rated >=90%** → auto-approve exactly as the flag-off clean branch (derive `--planned-files` + `--commit-*`, re-verify any drift-stamped entry against `@default`, base off `--base @default`, go to shared step 6). No adjudication needed.
   - **Otherwise** (clarifying questions, a sub-90% rating, or a `BAIL` — all read against the POST-revision plan and score: step 4's single round is already spent where it applied, so reaching here means self-raising was exhausted, not skipped) → a judgment fork. The decided short-circuit still wins first: if step 4's probe reported `decided`, follow the flag-off **Decided** sub-branch above (re-probe hotness; `is_hot` true → block, clean → proceed) — a recorded maintainer decision outranks a fresh advisor ruling. Otherwise adjudicate:
     1. **Confidence floor.** If the assessor's score is below **70%**, defer immediately via the flag-off defer-and-exit recipe — the plan is too shaky to rule on; don't spend an adjudication call. Stamp `[defer-reason: no-question]` (a bare sub-70% shortfall is escalatable) UNLESS the plan raised a substantive `## CLARIFYING QUESTIONS` block, in which case stamp `[defer-reason: open-question]`. STOP.
     2. **Get a ship verdict from a strong, independent mind.** Prefer the `advisor` tool (on a Fable model skip the probe — advisor is absent by design — and run this same step, both axes and the blast-radius framing, via the strong-tier `Agent` fallback below). Hand it the drafted plan + the confidence score with its Proven/Inferred bullets, and ask for a verdict on **two SEPARATE axes** — "which option is right?" and, independently, "is it safe to auto-ship?" — returning one of `proceed` / `block` / `defer` plus a one-paragraph ruling. Frame "safe to auto-ship" explicitly on **blast-radius** (how many call sites / how broadly the touched code is used), **reversibility** (can a wrong landing be reverted cleanly), and **CI-coverage** (would CI catch a mistake). This is what keeps file-path hotness from standing in for blast-radius risk: a change can be non-hot yet broad-blast (e.g. a widely-imported helper), and the verdict must catch that. Before ruling, the verdict-giver must classify each weakness it would cite as **closeable in-run** (the implementer can write the missing test, map the error/edge case, document a correct behavior change, verify a claim by reading the code) vs **uncloseable** (needs user-only information, or true irreversibility). A closeable hole is NOT grounds for `block`/`defer` — it converts to a mandatory implementer commitment on a `proceed`. The refute-style default holds for uncloseable/unsafe walls only; witness flow-5fp, where an advisor block named three closeable holes and parked work the run could have finished itself. If `advisor` is not in this harness (a `ToolSearch` for it returns nothing), or if `advisor` errors / is rate-limited, spawn an independent strong-tier `Agent` instead — `model: opus`, fresh context, a refute-style rubric ("default to `block`/`defer` unless the call is clearly right AND clearly safe") — a fresh-context strong agent is genuinely independent, NOT the same-tier self-scoring the step-4 rubric warns against. If neither is reachable, defer (flag-off recipe). STOP.
     3. **`is_hot_change` hard floor.** Re-probe hotness with the plan's planned-files (`triage.py decided --workspace-root . --key "$KEY" --files "<...>"` reads `is_hot`). A hot change can NEVER `proceed` — downgrade any `proceed` on a hot change to `block`. Never blind-ship a guard/lease/safety change, regardless of the verdict. This downgrade is gated by `[evolve] adjudicate_hot` (read via `python3 ${CLAUDE_SKILL_DIR}/scripts/triage.py adjudicate-hot-enabled --workspace-root .`): when that flag is on (maintainer self-target, default off), a hot `proceed` is NOT downgraded — it ships like a non-hot one, gated instead by the merge-time guard-property review + CI. Default off → behavior unchanged.
     4. **Route the verdict:**
       - **`proceed`** → record the ruling as an authoritative decision the way a maintainer triage would, then self-approve and go to shared step 6:
         ```bash
         python3 ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . comment \
           --key "$KEY" \
           --text "DECISION: (advisor) <the ruling — which option, and why it is safe to auto-ship>"
         ```
         The `DECISION:` stem makes a relaunch idempotent (step 4's probe reads it as `decided`, so it never re-asks); the `(advisor)` marker lets `/flow triage` surface it for optional maintainer review. When the ruling carries mandatory implementer commitments (closeable holes converted in step 2), fold any not already in the revised plan into the derived `--planned-files` (including new test file paths) and record them in the `DECISION: (advisor)` comment text so they survive a relaunch. Then derive `--planned-files` + `--commit-*`, re-verify drift against `@default`, and base off `--base @default`, exactly as the clean branch.
       - **`block`** → rulable, but unsafe to auto-ship (broad blast radius / hard to reverse / hot). `block` is reserved for walls that survived step 2's closeability test — user-only information, true irreversibility, broad blast, hot. Comment with the DEFER-stem (NOT a `DECISION:` comment) and set status `blocked`:
         ```bash
         python3 ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . comment \
           --key "$KEY" \
           --text "flow --auto could not self-approve: advisor ruled <which option> but blocked auto-ship — <why unsafe: blast radius / irreversibility / hot>. Judgment settled, this is a safety hold. To unstick: answer here, reopen (status->open) and re-run WITHOUT --auto, or merge by hand."
         bd update "$KEY" --status blocked
         ```
         Then emit a terse `blocked <KEY>: <reason>` line and STOP. **Critical: a `block` MUST NOT write a `DECISION:` comment.** If it did, a relaunch's probe would read `decided` and — for a non-hot change — route straight to the Decided sub-branch's `proceed`, silently defeating the block. The whole reason the verdict is three-way (not "proceed unless hot") is to catch the non-hot-but-unsafe case (the broad-blast helper); writing a decision for it throws that away.
       - **`defer`** → needs maintainer-only information the advisor cannot supply, or the advisor itself is not confident. Defer via the flag-off defer-and-exit recipe. Stamp `[defer-reason: open-question]` when the advisor's ruling cites maintainer-only information (a human must answer); stamp `[defer-reason: no-question]` when it cites the advisor's OWN lack of confidence (escalatable — a stronger model may clear it). STOP.

   The two outcomes: (a) **self-approve** → shared bootstrap + enter-worktree (steps 6-7), then the tail; or (b) **cannot self-approve** → defer-and-exit (no bootstrap, no worktree, no tail).
   `--auto`'s only effect on the self-approve branch is skipping the interactive plan gate; it does not change how the tail runs. As always, whether the tail runs unattended is the user's separate `/bg` choice (see step 7), independent of `--auto`.
