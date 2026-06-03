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
