# flow

An autonomous, self-evolving ticket→PR pipeline skill for [Claude Code](https://claude.com/claude-code).

```
ME                       MACHINE                          ME
spec ──→ ExitPlanMode ──→ worktree → implement → … → draft PR ──→ PR review
plan mode    the one gate    one session, background anytime (/bg)   the deliverable
```

`/flow <ticket>` plans the change WITH you in plan mode. `ExitPlanMode` is the single human gate. On approval, flow seeds a git worktree and runs the autonomous tail (implement → code_review → e2e → commit → draft PR) in the same session — `/bg` it anytime to run unattended. You spec the work and review the PR; the machine owns everything in between.

- **Multi-tracker.** Pluggable backend: Jira (REST) or [beads](https://github.com/steveyegge/beads) (`bd`), one active per workspace.
- **Deterministic engine.** A state-machine dispatcher owns `state.json`, a per-ticket run lease, and a canonical-snapshot TOCTOU guard; stdlib-only Python, atomic writes, quarantine recovery.
- **Compounding memory.** The reflect stage extracts durable knowledge per ticket; SessionStart recall feeds it back into the next plan (BM25).
- **Self-evolving.** When a run hits friction, the reflect stage repairs flow's *own* harness from inside the run (lens B → `machinery_edit` → version bump → commit). See `plugins/flow/skills/flow/references/self-evolution.md`.

## Install

```
/plugin marketplace add https://github.com/vdsmon/flow
/plugin install flow@vdsmon-flow
```

Then `/flow init` in a project, and `/flow <ticket>` to go.

## Layout

A marketplace-of-one: the repo root is the marketplace; the plugin lives at `plugins/flow/`.

```
.claude-plugin/marketplace.json   # lists the one plugin
plugins/flow/
  .claude-plugin/plugin.json
  hooks/                          # SessionStart recall hook
  skills/flow/
    SKILL.md                      # router + the one gate + the do-loop skeleton
    references/                   # per-verb + per-stage detail, loaded on demand
    scripts/                      # the engine (stdlib-only Python) + tests
```

The agent reads `SKILL.md` on trigger and loads `references/*.md` just-in-time; `scripts/MODULE.md` is the live map of the engine.

## Why a plugin, not a standalone CLI

flow is deliberately three layers, and only one of them is a program:

1. **A deterministic engine** — stdlib-only Python CLIs (the state-machine dispatcher, run lease, snapshot TOCTOU guard, tracker/forge adapters, gates). Everything that must be exact is code.
2. **A prose control loop** — `SKILL.md` + `references/`, executed by the Claude session. The dispatcher deliberately does not run handlers; it emits a descriptor and the model acts on it. Everything that needs judgment — planning, review interpretation, recovery, knowing when to deviate — is instructions, not code.
3. **The harness** — Claude Code supplies the loop driver: plan mode, worktrees, subagents, backgrounding, permissions and sandboxing, transcripts, and distribution via the plugin marketplace.

A standalone binary (its own agent loop on the API) would buy a coded do-loop — killing the prose-drift bug class that `seam_check` and trace-mining police today — plus versioned releases and CI-runnability. It would cost more than it buys:

- **Rebuilding the harness.** Permissions, sandboxing, worktree isolation, subagents, the plan-approval UX — Claude Code provides all of it and keeps improving underneath the plugin at zero cost to flow.
- **The economics.** Fleet-scale runs (`/flow queue drain` fanning out `claude --bg` workers) ride a flat-rate plan; an API-driven binary pays per token.
- **Self-evolution, the thesis** (see [VISION.md](VISION.md)). The reflect stage repairs flow's own harness from inside a run because the running agent has edit access to its own prose and scripts, gated by `machinery_edit`. Prose is the medium that makes self-repair cheap and reviewable; a binary editing its own installed package mid-run is a versioning nightmare.
- **Graceful ambiguity.** Prose executed by a model absorbs the weird states — half-dead CI, misfiled tickets, recovery from a killed session — that hard-coded orchestration would have to enumerate case by case.

The split is the design: deterministic invariants in code, judgment in prose, the loop delegated to a harness someone else maintains. Portability is hedged the cheap way instead — the engine scripts are already harness-agnostic, and `/flow init --agents-md` wires the skill for non-Claude harnesses (see `references/harness.md`). If flow ever had to run without a coding-agent harness entirely, the migration path exists (prose becomes system prompts, dispatch descriptors become tool schemas); nothing about that decision needs to be made before someone actually hits the wall. Headless already works today: `claude --bg "/flow <key> --auto"` is the CLI — the binary is `claude`.

## Develop

The engine is stdlib-only at runtime (just `python3`). Dev tooling is pinned via [mise](https://mise.jdx.dev/):

```
cd plugins/flow/skills/flow/scripts
mise run lint      # ruff + ty
mise run test      # pytest (scripts + hooks)
python3 seam_check.py   # validate prose↔CLI invocations against the real argparse surface
```

CI runs all three on every push.

MIT licensed.
