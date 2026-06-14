# CLAUDE.md

Guide for Claude Code working in the `flow` repo.

## What this is

The standalone home of `flow` — an autonomous, self-evolving ticket→PR pipeline skill for Claude Code (extracted from `vdsmon/claude-skills`). Unlike that pure-content marketplace, this repo is a real software project: ~24k LOC of stdlib-only Python engine + ~23k LOC of pytest, with CI.

## Layout (marketplace-of-one)

```
.claude-plugin/marketplace.json   # the marketplace, lists the one plugin (source ./plugins/flow)
plugins/flow/
  .claude-plugin/plugin.json      # plugin manifest (name=flow, version)
  hooks/                          # SessionStart recall hook + tests
  skills/flow/
    SKILL.md                      # router + the one gate + the do-loop skeleton (keep lean; ~200 lines)
    references/                   # verb-*.md + stage-*.md + self-evolution.md, loaded on demand
    scripts/                      # the engine + tests + mise.toml + pyproject.toml
      MODULE.md                   # live map of the engine (read this to find a script)
      inventory.md                # API/contract tables + archived build log
      dev-history.md              # archived build status
```

Keeping the `plugins/flow/` nesting (option a) means the marketplace can later hold companion bundles, and the reflect self-edit path (`plugins/flow/.claude-plugin/plugin.json`) stays valid.

## Dev commands

Run from `plugins/flow/skills/flow/scripts/` (mise finds `mise.toml` there). Use `rtk proxy` in front of pytest if output looks compressed/mangled.

```
mise run lint              # ruff + ty
mise run test              # pytest scripts/tests + hooks/tests (run separately; they have distinct rootdirs)
python3 seam_check.py      # prose↔CLI seam checker
```

Runtime is stdlib-only (`python3`); the venv/mise is dev tooling only.

## Working here (gotchas)

- **Branch off `origin/main`, never local `main` (lags) or current HEAD.** This repo churns with many `.claude/worktrees`; cutting a feature branch off a stale/feature HEAD pollutes the PR with already-merged commits (→ DIRTY). Autonomous `/flow --auto` runs use `flow_worktree.py --base @default` (fetch + resolve default branch); do the same by hand.
- **Live-testing plugin changes:** the `vdsmon-flow` marketplace tracks the **local main checkout** (`~/repos/personal/flow`), not `origin`. A launched `/flow` run loads that checkout's code. To exercise merged changes: advance the checkout to `origin/main`, then `claude plugin marketplace update vdsmon-flow` (`claude plugin details flow` shows the version).
- **`gh pr merge` needs a real branch** — a detached HEAD fails with "could not determine current branch"; merge from a throwaway branch off `origin/main`.

## Invariants

- **Prose↔CLI seam.** `SKILL.md` + `references/*.md` invoke `${CLAUDE_SKILL_DIR}/scripts/*.py`. After editing any of them, run `seam_check.py` (also gated by `tests/test_seam_check.py::test_live_docs_are_green`). It catches prose naming a flag/subcommand a script lacks — unit tests bypass argparse and miss it.
- **SKILL.md stays thin.** Router + the one gate (ExitPlanMode + confidence) + the do-loop skeleton stay inline (hot path, run every iteration incl. backgrounded). Verbose detail lives in `references/`. Don't let SKILL.md grow back.
- **Self-evolution is the thesis.** The reflect stage repairs the harness from inside a run via `machinery_edit.py` (flock-serialized, snapshot-aware). See `references/self-evolution.md`. Never route machinery fixes through the raw Edit tool; never self-edit `stage-registry.toml` or a wired handler mid-run.
- **Hot auto-merge is maintainer-only.** A HOT leaf PR may auto-merge (in-run via the `merge` stage, or via the evolve janitor for an orphan) ONLY in this maintainer self-target repo, gated by `[evolve] auto_merge_hot` + isolation (one hot at a time) + CI-green + agent diff review. For user projects the flag stays off and the human-merge keystone holds.
- **Version bumps.** `plugins/flow/.claude-plugin/plugin.json` and the `.claude-plugin/marketplace.json` flow entry stay in sync. The sync happens post-merge on `main` via the server-side `version-stamp.yml` Action (it runs `version.py stamp`), not via a per-PR inline bump.
- **`scripts/` stays flat.** The engine is a flat dir of stdlib-only, single-purpose scripts — not an importable package. A filename is simultaneously the import name (`import state`), the public CLI path (`${CLAUDE_SKILL_DIR}/scripts/state.py`), and a `seam_check` entry, so any move or rename (a `src/` layout, `tracker/`+`forge/` subdirs, or a cluster-prefix rename) ripples through ~84 prose call-sites + the seam checker + the import graph, and breaks the dual import-and-CLI scripts that work only because the dir is on `sys.path`. Don't reorganize it to make `ls` shorter. Logical grouping belongs in `scripts/MODULE.md` (the enforced cluster map), not the filesystem.

## Robustness (do not erode)

Run lease, canonical-snapshot TOCTOU guard, atomic writes + quarantine, content-ownership commit gate, friction logging. These are load-bearing; simplify presentation, never the safety machinery.


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:7510c1e2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

Remote-durable bead state: `bd export -o .beads/issues.jsonl` then commit it on a branch/PR (never push `main`). The shared Dolt DB is local truth; the jsonl is the git-portable export.

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->
