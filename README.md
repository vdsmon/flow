# flow

An autonomous, self-evolving ticket-to-PR pipeline skill for [Claude Code](https://claude.com/claude-code).

```
ME                        MACHINE                             ME
spec ---> ExitPlanMode ---> worktree -> implement -> ... -> draft PR ---> PR review
plan mode    the one gate      one session, background anytime (/bg)     the deliverable
```

`/flow <ticket>` plans the change with you in plan mode, and `ExitPlanMode` is the single human gate. On approval, flow seeds a git worktree and runs the autonomous tail (implement -> code_review -> e2e -> commit -> draft PR) in the same session, and you can background it anytime with `/bg`. You spec the work and review the PR, and the machine owns everything in between.

- Multi-tracker: Jira (REST) or [beads](https://github.com/steveyegge/beads) (`bd`), one active per workspace.
- Deterministic engine: a state-machine dispatcher owns `state.json`, a per-ticket run lease, and a canonical-snapshot TOCTOU guard. Stdlib-only Python with atomic writes and quarantine recovery.
- Compounding memory: the reflect stage extracts durable knowledge per ticket, and plan-phase recall feeds it back into the next plan (BM25, optional semantic fusion).
- Self-evolving: when a run hits friction, the reflect stage repairs flow's *own* harness from inside the run (lens B -> `machinery_edit` -> version bump -> commit). See `plugins/flow/skills/flow/references/self-evolution.md`.

## Install

```
/plugin marketplace add https://github.com/vdsmon/flow
/plugin install flow@vdsmon-flow
```

Then `/flow init` in a project, and `/flow <ticket>` to go.

## Layout

A marketplace-of-one: the repo root is the marketplace, and the plugin lives at `plugins/flow/`.

```
.claude-plugin/marketplace.json   # lists the one plugin
docs/research/                    # experiment records (xqt counterfactual, cognitive-yield, novelty survey)
plugins/flow/
  .claude-plugin/plugin.json
  hooks/                          # SessionStart ops hook (evolve deadman/staleness)
  skills/flow/
    SKILL.md                      # router + the one gate + the do-loop skeleton
    references/                   # per-verb + per-stage detail, loaded on demand
    scripts/                      # the engine (stdlib-only Python) + tests
```

The agent reads `SKILL.md` on trigger and loads `references/*.md` just in time. `scripts/MODULE.md` is the live map of the engine (start with its Reader entry-point map). The experiments behind the design bounds are archived in [docs/research/](docs/research/).

## Why a plugin, not a standalone CLI

Fair question, since most tools in this space ship as their own binary. flow is deliberately three layers, and only one of them is a program:

1. **A deterministic engine:** stdlib-only Python CLIs (the state-machine dispatcher, run lease, snapshot TOCTOU guard, tracker and forge adapters, the gates). Everything that must be exact is code.
2. **A prose control loop:** `SKILL.md` plus `references/`, executed by the Claude session. The dispatcher deliberately does not run handlers, it emits a descriptor and the model acts on it. Everything that needs judgment (planning, review interpretation, recovery, knowing when to deviate) is instructions, not code.
3. **The harness:** Claude Code drives the loop and supplies plan mode, worktrees, subagents, backgrounding, permissions and sandboxing, transcripts, and the marketplace for distribution.

A standalone binary running its own agent loop on the API would buy real things: a coded do-loop (the prose-drift bug class that `seam_check` and trace-mining police today just disappears), versioned releases, and the option to run in CI. I still think it costs more than it buys.

- Rebuilding the harness. Permissions, sandboxing, worktree isolation, subagents, the plan-approval UX: Claude Code provides all of it and keeps improving underneath the plugin, for free.
- The economics. Fleet runs (`/flow queue drain` launching `claude --bg` workers) run on a flat-rate plan, while an API-driven binary pays per token.
- Self-evolution, which is the whole thesis (see [VISION.md](VISION.md)). The reflect stage can repair flow's own harness from inside a run because the running agent has edit access to its own prose and scripts, gated by `machinery_edit`. Prose is what makes self-repair cheap and reviewable, and a binary editing its own installed package mid-run is a versioning nightmare.
- Graceful ambiguity. Prose executed by a model absorbs the weird states (half-dead CI, misfiled tickets, recovery from a killed session) that hard-coded orchestration would have to enumerate case by case.

So the split is the design: what must be exact lives in code, what needs judgment lives in prose, and the loop belongs to a harness someone else maintains. Portability is hedged the cheap way instead. The engine scripts already run anywhere, and `/flow init --agents-md` writes the entry point for non-Claude harnesses (see `references/harness.md`). If flow ever had to run without a coding-agent harness at all there is a migration path (prose becomes system prompts, dispatch descriptors become tool schemas), but nobody has to place that bet before hitting the wall. Headless already works: `claude --bg "/flow <key> --auto"` is the CLI, the binary is just called `claude`.

## Develop

The engine is stdlib-only at runtime (just `python3`). Dev tooling is pinned via [mise](https://mise.jdx.dev/):

```
cd plugins/flow/skills/flow/scripts
mise run lint      # ruff + ty
mise run test      # pytest (scripts + hooks)
python3 seam_check.py   # validate prose->CLI invocations against the real argparse surface
```

CI runs all three on every push.

MIT licensed.
