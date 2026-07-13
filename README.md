# flow

An autonomous, state-aware ticket-to-PR pipeline for Claude Code and OpenAI
Codex.

```
ME                          MACHINE                              ME
target ---> plan approval ---> worktree -> implement -> ... -> draft PR ---> PR review
               one gate            one rooted session                 the deliverable
```

`/flow <target>` in Claude Code or `$flow:flow <target>` in Codex reads the
ticket, run, lease, and pull-request evidence, then does the safe next thing. A
fresh ticket is planned with you; explicit plan approval is the single human
gate. An incomplete run resumes, a deferred decision presents its saved
question, a broken run offers only applicable repairs, and an open PR with new
feedback enters a revision run. Bare Flow is a cockpit for everything that
currently needs attention.

After approval, Flow seeds a git worktree, binds every command, edit, and worker
to its absolute root, and runs the autonomous tail (implement -> code review ->
e2e -> commit -> draft PR) in the same owner session. You shape the work and
review the PR; the machine owns everything in between.

- Multi-tracker: Jira (REST) or [beads](https://github.com/steveyegge/beads) (`bd`), one active per workspace.
- Deterministic engine: a state-machine dispatcher owns `state.json`, a per-ticket run lease, and a canonical-snapshot TOCTOU guard. Stdlib-only Python with atomic writes and quarantine recovery.
- Compounding memory: the reflect stage extracts durable knowledge per ticket, and plan-phase recall feeds it back into the next plan (BM25, optional semantic fusion).
- Harness-neutral maintenance: Claude Code and Codex use their native collaboration workers behind the same bounded owner-session pool. Durable run, fleet, lease, and PR evidence remains authoritative if a worker handle disappears.
- Self-evolving: reflect turns observed friction into guarded machinery fixes (`machinery_edit` -> reviewed commit; version stamped after merge). See `plugins/flow/skills/flow/references/self-evolution.md`.

## Install with Claude Code

```
/plugin marketplace add https://github.com/vdsmon/flow
/plugin install flow@vdsmon-flow
```

Then run `/flow workspace setup` once in a new project and `/flow <ticket>` to
go. Existing Flow workspaces migrate their runtime layout automatically; do not
run setup again just for this release.

## Install with Codex

```bash
codex plugin marketplace add vdsmon/flow
codex plugin add flow@vdsmon-flow
```

Start a new Codex thread after installation. Run `$flow:flow workspace setup`
once in a new project, then `$flow:flow <ticket>`. Existing Flow workspaces
migrate automatically on first use. Codex uses its native skill/plugin
discovery; `AGENTS.md` is optional durable guidance, not the primary loader.

Use bare `/flow` or `$flow:flow` for the cockpit, and `FLOW help` (rendered with
the host's trigger) for the complete command tree.

## Layout

A marketplace-of-one for both hosts: the repo root is the marketplace, and both
manifests expose the same `plugins/flow/skills/` tree.

```
.claude-plugin/marketplace.json   # lists the one plugin
.agents/plugins/marketplace.json # Codex marketplace
docs/research/                    # experiment records (xqt counterfactual, cognitive-yield, novelty survey)
plugins/flow/
  .claude-plugin/plugin.json
  .codex-plugin/plugin.json
  skills/flow/
    SKILL.md                      # generated router + the one gate + do-loop skeleton
    public-commands.toml          # authored public grammar and effect metadata
    references/                   # per-domain commands + delivery stages, loaded on demand
    scripts/                      # the engine (stdlib-only Python) + tests
```

The agent reads `SKILL.md` on trigger and loads `references/*.md` just in time. `scripts/MODULE.md` is the live map of the engine (start with its Reader entry-point map). The experiments behind the design bounds are archived in [docs/research/](docs/research/).

## Why a plugin, not a standalone CLI

Fair question, since most tools in this space ship as their own binary. flow is deliberately three layers, and only one of them is a program:

1. **A deterministic engine:** stdlib-only Python CLIs (the state-machine dispatcher, run lease, snapshot TOCTOU guard, tracker and forge adapters, the gates). Everything that must be exact is code.
2. **A prose control loop:** `SKILL.md` plus `references/`, executed by the active agent session. The dispatcher deliberately does not run handlers: it emits a descriptor and the model acts on it. Everything that needs judgment (planning, review interpretation, recovery, knowing when to deviate) is instructions, not code.
3. **Harness adapters:** Claude Code and Codex map native plan gates, worktree behavior, subagents, file writes, waits, notifications, and backgrounding onto one rooted execution contract. A generic fallback states its degradations explicitly.

A standalone binary running its own agent loop on the API would buy real things: a coded do-loop (the prose-drift bug class that `seam_check` and trace-mining police today just disappears), versioned releases, and the option to run in CI. I still think it costs more than it buys.

- Rebuilding the harness. Permissions, sandboxing, worktree isolation, subagents, and plan-approval UX are supplied by Claude Code or Codex and keep improving underneath the plugin.
- Native workers. Maintenance drains use the active harness's collaboration agents. The owner session reserves one slot, bounds concurrency by host capacity, and can itself be backgrounded by the user without inventing a second job system.
- Self-evolution, which is the whole thesis (see [VISION.md](VISION.md)). The reflect stage can repair flow's own harness from inside a run because the running agent has edit access to its own prose and scripts, gated by `machinery_edit`. Prose is what makes self-repair cheap and reviewable, and a binary editing its own installed package mid-run is a versioning nightmare.
- Graceful ambiguity. Prose executed by a model absorbs the weird states (half-dead CI, misfiled tickets, recovery from a killed session) that hard-coded orchestration would have to enumerate case by case.

So the split is the design: exact behavior lives in code, judgment lives in prose,
and host differences live at the adapter boundary. `.flow/runtime/flow` is the
single post-setup command seam, and cwd is never hidden state. Claude Code and
Codex have native plugin manifests; `FLOW workspace setup --guidance` can write
durable repo guidance for either host. Maintenance, memory, recovery, and
delivery commands are all part of the same portable interface.

## Develop

The engine is stdlib-only at runtime (just `python3`). Dev tooling is pinned via [mise](https://mise.jdx.dev/):

```
cd plugins/flow/skills/flow/scripts
mise run lint      # ruff + ty
mise run test      # pytest (scripts)
python3 seam_check.py   # validate prose->CLI invocations against the real argparse surface
```

CI runs all three on every push.

MIT licensed.
