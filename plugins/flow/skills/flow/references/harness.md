# harness portability — running /flow off Claude Code

flow is **Claude-Code-first**. The Python engine (`scripts/*.py`) is harness-agnostic stdlib that shells out only to `git`/`bd`/`gh`/`bkt`/`mise`; it never invokes a Claude-Code tool. **Every** Claude-Code coupling lives in this prose layer plus a handful of environment variables. This file is the single source of truth for what each Claude-Code primitive does and how a harness that lacks it (Codex, Cursor, a bare SDK loop) degrades. Nothing here changes the Claude-Code path — each fallback is an additive branch taken only when the primitive is absent.

The self-evolution / maintainer machinery (`evolve`, `queue`) stays Claude-Code-only and is out of scope here. Recall, by contrast, now lives in the plan-phase skill prose (`verb-spec.md` / `stage-plan.md` run `recall.py --query-file ...`), NOT a SessionStart hook — so it ports cleanly to any agent that runs the skill (the hook was Claude-Code-only; the de-hooking removed that coupling). The SessionStart hook that remains is the evolve-loop deadman only.

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

Claude Code discovers flow via `.claude-plugin/` and loads SKILL.md on the skill router. Off-CC there is no plugin manifest or `Skill` tool: load `SKILL.md` + the `references/*.md` it points to as plain markdown context — Codex via `AGENTS.md` (import or inline), Cursor via a project rule. Put the `CLAUDE_SKILL_DIR` export line above in that same bootstrap so the call-sites resolve.

## The one gate — `ExitPlanMode` + plan mode

The gate does two things: it **presents** the plan for approval, and it **enforces** no-edits-before-approval (the harness blocks writes while in plan mode).

- **Presenting** is universal: end the turn on the plan + the `## Confidence` rating, and treat the user's next affirmative as approval.
- **Enforcing** is Claude-Code-only. Off-CC nothing stops the model from proceeding early, so the fallback is a **soft gate (model self-restraint), not an equivalent**: after presenting the plan, STOP and wait for explicit user approval before seeding the worktree or making any edit. Treat it as a discipline, and a degradation — a careless run can break it where Claude Code could not.

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
