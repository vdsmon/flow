<!-- flow:activation-truth:begin -->
# CLAUDE.md

Flow's cognitive route catalog is authoritative for model selection. New exact route
snapshots may launch read-only planning, assessment, review, review-brief authorship,
and reflection through either Claude Code or Codex capsules. E2E is also activated, as a
disposable-capsule writer: it clones the sealed `source_sha`, is seeded with the ticket's
uncommitted working state, runs the recipe there, captures the recipe's mutations as
evidence, imports nothing, and discards the capsule. The importing writers (implementer,
review_fixer, revision_fixer) also launch as capsule writers whose validated binary-aware
patch is compare-and-swap imported under a sole-writer claim, then disposed. The
machinery_fixer also launches, as a read-only capsule: it derives a report of anchored
`{file, old, new}` edits and reflect applies each through the untouched `machinery_edit`
guard (never the CAS import path). No exact post-plan route remains shadowed. Keep the
owner as the single human cockpit, pass exact typed outcomes back to dispatch, and never
treat an environment-only harness label as cross-harness execution proof.

Guide for Claude Code or Codex working in the `flow` repo.

## What this is

The standalone home of `flow` — an autonomous, self-evolving ticket→PR pipeline skill for Claude Code and Codex. This is a real software project: a stdlib-only Python engine, prose orchestration, and a large pytest suite with CI.

## Layout (marketplace-of-one)

```
.claude-plugin/marketplace.json   # the marketplace, lists the one plugin (source ./plugins/flow)
plugins/flow/
  .claude-plugin/plugin.json      # plugin manifest (name=flow, version)
  skills/flow/
    SKILL.md                      # generated router + the one gate + do-loop skeleton
    public-commands.toml          # authored public grammar/effect/harness registry
    references/                   # command-*.md + delivery/stage internals, loaded on demand
    scripts/                      # the engine + tests + mise.toml + pyproject.toml
      MODULE.md                   # live map of the engine (read this to find a script)
      inventory.md                # API/contract tables + archived build log
      dev-history.md              # archived build status
```

Keeping the `plugins/flow/` nesting (option a) means the marketplace can later hold companion bundles, and the reflect self-edit path (`plugins/flow/.claude-plugin/plugin.json`) stays valid.

## Dev commands

Run from `plugins/flow/skills/flow/scripts/` (mise finds `mise.toml` there). Use `rtk proxy` in front of pytest if output looks compressed/mangled.

```
mise run lint              # ruff check + ruff format --check + ty check
mise run lint:ruff         # ruff check only        (prek pre-commit)
mise run lint:format       # ruff format --check     (prek pre-commit)
mise run lint:ty           # ty check only           (prek pre-push)
mise run test              # pytest scripts/tests
python3 seam_check.py      # prose↔CLI seam checker
python3 public_commands_check.py  # registry↔router/help/trigger drift check
```

Runtime is stdlib-only (`python3`); the venv/mise is dev tooling only.

**Fail-fast hooks (prek, opt-in).** `.pre-commit-config.yaml` at the repo root wires the CI checks as [prek](https://github.com/j178/prek) hooks so commits fail before CI: pre-commit stage = ruff check + ruff format --check + seam_check (sub-second); pre-push stage = ty (seconds). prek is pinned in the repo-root `mise.toml`; install once per clone (from repo root): `mise install && prek install`. Hooks are `repo: local`, shell out to the `lint:*` mise sub-tasks (one source of truth with CI), and are **check-only** — see the invariant below.

## Working here (gotchas)

- **Branch off `origin/main`, never local `main` (lags) or current HEAD.** This repo churns with many worktrees; cutting a feature branch off a stale/feature HEAD pollutes the PR with already-merged commits (→ DIRTY). Unattended Flow runs resolve the remote default branch before creating their worktree; do the same by hand.
- **Live-testing plugin changes:** the `vdsmon-flow` marketplace tracks the **local main checkout** (`~/repos/personal/flow`), not `origin`. A launched `/flow` run loads that checkout's code. To exercise merged changes: advance the checkout to `origin/main`, then `claude plugin marketplace update vdsmon-flow` (`claude plugin details flow` shows the version).
- **`gh pr merge` needs a real branch** — a detached HEAD fails with "could not determine current branch"; merge from a throwaway branch off `origin/main`.
- **`stage-registry.toml` lives at the skill root** (`plugins/flow/skills/flow/`), never under `scripts/`. A `scripts/stage-registry.toml` entry in `planned_files` reads as unowned drift and aborts the run.
- **Env/CLI quirks** (gh keyring 401, GraphQL `{owner}`/`{repo}`, mise shim heal, zsh word-split, ty ignore syntax): `plugins/flow/skills/flow/references/troubleshooting.md`.

## Invariants

- **Public grammar is generated.** `public-commands.toml` is the authored source for command paths, options, effects, workspace requirements, help, harness parity, and reference routing. `public_commands_check.py` is check-only and fails when managed router/help/trigger content is stale. Removed public forms must fail normally; never add aliases or migration redirects.
- **Prose↔CLI seam.** `SKILL.md` + `references/*.md` invoke the installed `.flow/runtime/flow` facade. After editing them, run `seam_check.py` (also gated by `tests/test_seam_check.py::test_live_docs_are_green`). It catches prose naming a flag/subcommand a script lacks — unit tests bypass argparse and miss it.
- **SKILL.md stays thin.** Router + the one gate (ExitPlanMode + confidence) + the do-loop skeleton stay inline (hot path, run every iteration incl. backgrounded). Verbose detail lives in `references/`. Don't let SKILL.md grow back.
- **Self-evolution is the thesis.** The reflect stage repairs the harness from inside a run via `machinery_edit.py` (flock-serialized, snapshot-aware). See `references/self-evolution.md`. Never route machinery fixes through the raw Edit tool; never self-edit `stage-registry.toml` or a wired handler mid-run.
- **Hot auto-merge is maintainer-only.** A HOT leaf PR may auto-merge (in-run via the `merge` stage, or via the evolve janitor for an orphan) ONLY in this maintainer self-target repo, gated by `[evolve] auto_merge_hot` + isolation (one hot at a time) + CI-green + agent diff review. For user projects the flag stays off and the human-merge keystone holds.
- **Version bumps.** `plugins/flow/.claude-plugin/plugin.json` and the `.claude-plugin/marketplace.json` flow entry stay in sync. The sync happens post-merge on `main` via the server-side `version-stamp.yml` Action (it runs `version.py stamp`), not via a per-PR inline bump.
- **Fail-fast hooks are CHECK-ONLY.** The prek hooks (`.pre-commit-config.yaml`) never mutate files. Unattended Flow runs commit through the engine inside worktrees that share the main checkout's `.git`, so any installed hook fires during those commits too; a mutating hook (`ruff --fix`, a formatter writing) would create unowned drift against the content-ownership commit gate. Split by latency: pre-commit = ruff check + ruff format --check + command-registry check + seam_check; pre-push = ty. Hooks stay `repo: local` and shell out to the repository checks, so no rule set is redeclared against CI.
- **`scripts/` stays flat.** The engine is a flat dir of stdlib-only, single-purpose scripts, not an importable package. A filename is simultaneously the import name (`import state`), an internal facade mapping, and a `seam_check` entry, so a directory reorganization ripples through prose, the seam checker, and the import graph. Logical grouping belongs in `scripts/MODULE.md`, not the filesystem.

## Robustness (do not erode)

Four correctness guards — run lease, canonical-snapshot TOCTOU guard, atomic writes + quarantine, content-ownership commit gate — on the flock substrate (`_locking.py`), plus friction logging as the self-evolution feedstock. These are load-bearing; simplify presentation, never the safety machinery. Threat → file → witnessed failure per mechanism: `plugins/flow/skills/flow/references/robustness.md`.


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
