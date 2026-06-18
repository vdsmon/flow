# harness portability — running /flow off Claude Code

flow is **Claude-Code-first**. The Python engine (`scripts/*.py`) is harness-agnostic stdlib that shells out only to `git`/`bd`/`gh`/`bkt`/`mise`; it never invokes a Claude-Code tool. **Every** Claude-Code coupling lives in this prose layer plus a handful of environment variables. This file is the single source of truth for what each Claude-Code primitive does and how a harness that lacks it (Codex, Cursor, a bare SDK loop) degrades. Nothing here changes the Claude-Code path — each fallback is an additive branch taken only when the primitive is absent.

The self-evolution / maintainer machinery (`evolve`, `queue`, the SessionStart recall hook) stays Claude-Code-only and is out of scope here.

## Detecting what the current harness offers

Off Claude Code, the adapter author already knows the harness's toolset, so "detect" mostly means "wire the right fallback once." The probes below matter inside a Claude Code session running a different model (e.g. Fable lacks `advisor`):

- **Tool present?** `ToolSearch` for the tool name; an empty result means absent. `ToolSearch` is itself a Claude Code primitive — off-CC, just use what your harness exposes.
- **Model?** the session model string — Fable (`claude-fable-*`) has no `advisor` by design.
- **Env var present?** `${CLAUDE_SKILL_DIR}` set ⇒ Claude Code; `${CLAUDE_JOB_DIR}` set ⇒ a backgrounded job.

## Script path resolution — `${CLAUDE_SKILL_DIR}` (load-bearing)

Every call-site in SKILL.md + `references/*.md` runs `python3 ${CLAUDE_SKILL_DIR}/scripts/<x>.py`. Claude Code injects `${CLAUDE_SKILL_DIR}`; no other harness does, so off-CC every call-site breaks unless the var is set.

A non-CC harness exports the var once per session in its bootstrap. **Primary (prerequisite-free):** set it to your clone's skill dir directly — whoever wires the adapter already knows where they cloned the repo:

```bash
export CLAUDE_SKILL_DIR=/path/to/your-clone/plugins/flow/skills/flow
```

**Convenience when present:** `init` persists that absolute path to **`.flow/skill_dir`** (a gitignored, machine-local sibling — same pattern as `.flow/memory-root`; written in `init.py` `_write_skill_dir`), so on a freshly-initialized workspace you can read it instead of hardcoding:

```bash
export CLAUDE_SKILL_DIR="${CLAUDE_SKILL_DIR:-$(cat .flow/skill_dir 2>/dev/null)}"
```

Caveat: `.flow/skill_dir` only exists after a `/flow init` (or `--reconfigure` on an already-initialized workspace). On an existing checkout that predates this file the `$(cat …)` yields empty and every call-site breaks — so prefer the explicit form unless you know init wrote the file. The `:-` default is inert when Claude Code already set the var, so either line is safe to carry on every harness. It must be a pure-shell read, never "run a script to find the scripts dir" (the chicken-and-egg this avoids). Carry it in the off-CC entry point (below).

## Entry point — loading the skill without a plugin manifest

Claude Code discovers flow via `.claude-plugin/` and loads SKILL.md on the skill router. Off-CC there is no plugin manifest or `Skill` tool, so nothing loads the skill — that absence is the root cause of a non-CC run that freelances past the pipeline (it never read SKILL.md). The fix is **`AGENTS.md`**, the cross-harness convention Cursor, Windsurf, opencode and a bare loop all read from the repo root.

`/flow init --agents-md` writes (or append-only-extends) a marker-guarded stanza into `<repo>/AGENTS.md` that (a) carries the `CLAUDE_SKILL_DIR` export above, (b) tells the harness to read `SKILL.md` + this file as context on `/flow`, and (c) restates the approval-is-not-coding soft gate below. The flag is **opt-in**: Claude Code loads via the plugin and never reads AGENTS.md, so a default `init` writes no tracked file (zero change to the CC path). One artifact covers every harness — no per-harness adapter (`.cursor/rules`, a Windsurf rule, …) to maintain. A harness that wants a hard write-block on top can still add its own pre-edit hook; that is opt-in hardening, not a flow requirement.

## The one gate — `ExitPlanMode` + plan mode

The gate does two things: it **presents** the plan for approval, and it **enforces** no-edits-before-approval (the harness blocks writes while in plan mode).

- **Presenting** is universal: end the turn on the plan + the `## Confidence` rating, and treat the user's next affirmative as approval.
- **Enforcing** is Claude-Code-only. Off-CC nothing stops the model from proceeding early, so the fallback is a **soft gate (model self-restraint), not an equivalent**: after presenting the plan, STOP and wait for explicit user approval before seeding the worktree or making any edit. Treat it as a discipline, and a degradation — a careless run can break it where Claude Code could not.
  - **Engine backstop (no model discipline required).** Portable code cannot intercept a harness's own edit tool, so it cannot *prevent* a pre-bootstrap edit, but `flow_worktree.py create --recover-spill` *detects and recovers* one: an uncommitted planned file on the main checkout is carried into the seeded worktree (and reverted on main) at bootstrap. **The flag is the discriminator, not the symptom or the harness identity** — a dirty planned file on main is equally produced by a soft-gate spill *and* by a CC user's own pre-existing WIP, so flow cannot tell them apart from the file alone. Only the off-CC AGENTS.md entry point passes `--recover-spill`; SKILL.md's CC bootstrap omits it, so the CC path is byte-identical even when main is dirty (plan mode means any dirty planned file there is the user's WIP, which must not be moved). On the off-CC path this turns the soft-gate slip from "work lands on the wrong branch" into "work lands in the run, with a warning." Note `CLAUDE_SKILL_DIR`-set is no longer a CC signal once this stanza ships (it exports the var off-CC too), which is *why* the discriminator is an explicit flag, not env detection.

`EnterPlanMode` (entering the gate) is the same family: on CC, call it; off-CC, simply do the read-only front half and present the plan at the end of the turn.

This only matters for the interactive `spec` path. The `--auto` headless path never parks on the gate anyway (it defers-and-exits when it cannot self-approve), so it is unaffected.

## `EnterWorktree`

CC switches the session into the seeded worktree via `EnterWorktree(path=<worktree>)`. Off-CC (and on a backgrounded CC `--auto` run, whose cwd is pinned at the repo root) there is no such tool: `cd` into the worktree dir in the persistent shell before the `do` loop runs. That is all `EnterWorktree` does that the loop depends on.

## `advisor` — independent confidence rating

Canonical fallback chain (used by `spec` step 4's rating and the adjudication step):

1. **Fable** (`claude-fable-*`): `advisor` is absent by design — skip the `ToolSearch` probe, go straight to step 3.
2. Other models: prefer `advisor()` (it auto-forwards the transcript). Fall back only after a `ToolSearch` for `advisor` returns nothing, or if it errors / is rate-limited.
3. **Fallback:** spawn a `general-purpose` `Agent` (strong tier, fresh context) with the ticket context + drafted plan + the same rubric. Any harness with a sub-agent / second-pass call works; a bare loop can issue a second independent model call.

## `Agent` / subagent (`subagent:<type>` handlers)

Registry default handlers `subagent:Plan` and `subagent:general-purpose` spawn a sub-agent. Codex has sub-agent spawning; Cursor is weaker. Map `subagent:<type>` to whatever the harness offers; where no sub-agent exists, run the stage's reference-doc protocol inline in the main loop (the work is the same, only the isolation is lost).

## `Skill` tool (`skill:<name>` handlers)

**No default stage uses a `skill:` handler** — `stage-registry.toml` defaults are `inline` / `subagent` / `none`. `skill:` is opt-in only (e.g. a work repo wiring `ship-it` for `create_pr`/`review_loop`). A harness without a skill-loader therefore loses nothing on the default pipeline; only an explicitly-wired `skill:` handler is unavailable — replace it with an `inline` handler whose reference doc carries the equivalent steps, or a `subagent:` one.

## `PushNotification` — PR-ready ping

Canonical fallback (the `do` loop fires this when `review_loop` finishes `completed`): if `ToolSearch` for `PushNotification` returns nothing, do BOTH (a) surface the message in-thread and (b) post it durably as a PR comment via the workspace forge — GitHub `gh pr comment <url> --body "..."`, Bitbucket `bkt api ".../pullrequests/<id>/comments" -X POST ...`. The PR comment is what a detached run can see later; the in-thread echo alone is invisible to it. Best-effort always — `state.json` is the source of truth.

## `AskUserQuestion` — blockers and missing input

CC renders it natively (inline when attached, "needs input" in `claude agents` when backgrounded). Off-CC fallback: ask the user in plain text and wait for the reply — every interactive harness can pause for input. On a detached / `--auto` run there is no user to ask, so the run defers-and-exits with the open questions recorded (unchanged from the CC `--auto` behavior).

## `${CLAUDE_JOB_DIR}` — backgrounded self-teardown

The `--auto` self-teardown keys on `${CLAUDE_JOB_DIR}` (a CC backgrounded-job dir). Unset ⇒ the guard silently skips, which is correct for any foreground or non-CC run. No fallback needed.
