# AGENTS.md

Guide for Claude Code or Codex working in the `flow` repo — the standalone home of `flow`, an autonomous, self-evolving ticket→PR pipeline skill for both hosts. Claude Code loads this file through the one-line `CLAUDE.md` shim.

Flow uses one attended planning conversation, one human-approved Markdown plan, and one authoritative ticket worktree. Fresh host-native agents provide logical role separation for implementation and review. Keep the driver as the single human cockpit; do not add provider proof, execution capsules, or patch-import transactions around those roles.

The repo is a marketplace-of-one. The `plugins/flow/` nesting is load-bearing, not taste: both `marketplace.json` files point at `./plugins/flow`, `flow_launcher` resolves the installed engine through that source path, and the reflect self-edit path (`plugins/flow/.claude-plugin/plugin.json`) is written against it. `plugins/flow/skills/flow/scripts/MODULE.md` is the live map of the engine — read it to find a script, and its §Reference docs index to find a prose doc.

`docs/specs/` holds dated design records and `docs/research/` the experiment write-ups. Both are history, not contract: nothing under `docs/` is authoritative unless SKILL.md, a `references/` doc, or MODULE.md cites it.

## Dev commands

Run from `plugins/flow/skills/flow/scripts/` (mise finds `mise.toml` there). Use `rtk proxy` in front of pytest if output looks compressed/mangled.

```
mise run lint             # ruff check + ruff format --check + ty check
mise run test             # pytest scripts/tests
mise run check:commands   # public-commands registry vs router/help/trigger drift
python3 seam_check.py     # prose↔CLI seam checker
```

CI runs all four on every push. Runtime is stdlib-only (`python3`); the venv/mise is dev tooling only.

**Fail-fast hooks (prek).** `.pre-commit-config.yaml` at the repo root wires the CI checks as [prek](https://github.com/j178/prek) hooks so commits fail before CI. prek is pinned in the repo-root `mise.toml`; the hooks stay inert until `mise install && prek install` has run once in the checkout (from repo root). Stage split and the check-only rule: see the invariant below.

## Working here (gotchas)

- **Branch off `origin/main`, never local `main` (lags) or current HEAD.** This repo churns with many worktrees; cutting a feature branch off a stale/feature HEAD pollutes the PR with already-merged commits (→ DIRTY). Unattended Flow runs resolve the remote default branch before creating their worktree; do the same by hand.
- **Live-testing plugin changes:** the `vdsmon-flow` marketplace tracks the **local main checkout** (`~/repos/personal/flow`), not `origin`. A launched `/flow` run loads that checkout's code. To exercise merged changes: advance the checkout to `origin/main`, then `claude plugin marketplace update vdsmon-flow` (`claude plugin details flow` shows the version).
- **A run is sealed to the engine installed at its start, not the one its own PR changes.** The run resolves its engine through the install path pinned in `.flow/runtime/skill-root` at workspace setup, so a PR's own engine edits do not change the running pipeline. Only a run started after installation of the merged change picks up the new contract.
- **Never run `uv run` inside a worktree.** `uv run pytest` creates `plugins/flow/skills/flow/scripts/uv.lock`; the content-ownership commit gate treats it as unowned drift and exits 3. Use the repository's configured `mise` tasks instead. If a stray `uv.lock` already landed, remove it before committing.
- **`gh pr merge` needs a real branch** — a detached HEAD fails with "could not determine current branch"; merge from a throwaway branch off `origin/main`.
- **`stage-registry.toml` lives at the skill root** (`plugins/flow/skills/flow/`), never under `scripts/`. A `scripts/stage-registry.toml` entry in `planned_files` reads as unowned drift and aborts the run.
- **Deleting tests?** Read `plugins/flow/skills/flow/scripts/tests/AUDIT.md` first — 285 tests are protected witnesses cited in landed deletion evidence, and every previously refuted deletion is recorded there with its distinguishing mutation.
- **Env/CLI quirks** (gh keyring 401, GraphQL `{owner}`/`{repo}`, mise shim heal, zsh word-split, ty ignore syntax): `plugins/flow/skills/flow/references/troubleshooting.md`.

## Invariants

- **Public grammar is generated.** `public-commands.toml` is the authored source; `public_commands_check.py` reports what is stale. Removed public forms must fail normally; never add aliases or migration redirects. Likewise MODULE.md's §Derived surfaces (import graph + subcommand names) is generated: after adding, removing, or re-importing a script, run `python3 module_map.py write`; `seam_check.py` fails while it is stale.
- **Prose↔CLI seam.** `SKILL.md` + `references/*.md` invoke the installed `.flow/runtime/flow` facade. Run `seam_check.py` after editing them (also gated by `tests/test_seam_check.py::test_live_docs_are_green`): it catches prose naming a flag or subcommand a script lacks, which unit tests bypass argparse and miss.
- **SKILL.md stays thin.** Router + the one gate (ExitPlanMode + confidence) + the do-loop skeleton stay inline (hot path, run every iteration incl. backgrounded). Verbose detail lives in `references/`. Don't let SKILL.md grow back.
- **Self-evolution is the thesis.** The reflect stage repairs the harness from inside a run via `machinery_edit.py` (flock-serialized, snapshot-aware). See `references/self-evolution.md`. Never route machinery fixes through the raw Edit tool; never self-edit `stage-registry.toml` or a wired handler mid-run.
- **Hot auto-merge is maintainer-only.** A HOT leaf PR may auto-merge (in-run via the `merge` stage, or via the evolve janitor for an orphan) ONLY in this maintainer self-target repo, gated by `[evolve] auto_merge_hot` + isolation (one hot at a time) + CI-green + agent diff review. For user projects the flag stays off and the human-merge keystone holds.
- **Version bumps.** `plugins/flow/.claude-plugin/plugin.json` and the `.claude-plugin/marketplace.json` flow entry stay in sync. The sync happens post-merge on `main` via the server-side `version-stamp.yml` Action (it runs `version.py stamp`), not via a per-PR inline bump.
- **Fail-fast hooks are CHECK-ONLY.** The prek hooks never mutate files. Unattended Flow runs commit through the engine inside worktrees that share the main checkout's `.git`, so any installed hook fires during those commits too; a mutating hook (`ruff --fix`, a formatter writing) would create unowned drift against the content-ownership commit gate. The stage split lives in `.pre-commit-config.yaml`, which shells out to the repository checks so no rule set is redeclared against CI.
- **`scripts/` stays flat.** The engine is a flat dir of stdlib-only, single-purpose scripts, not an importable package. A filename is simultaneously the import name (`import state`), an internal facade mapping, and a `seam_check` entry, so a directory reorganization ripples through prose, the seam checker, and the import graph. Logical grouping belongs in `scripts/MODULE.md`, not the filesystem.
- **Dry-run gates every side effect.** `--dry-run` on any Flow command means no tracker write, no merge, no fleet registration, no worktree reaping, no worker launch — with no exception for a write that happens to live inside a read-side function (a `bd create` reached through a read path is still a tracker write). A read that exists only to guard a write (a dedup scan) is part of the write and is gated with it; the dry-run path reports a `would_*` action record instead. `senses_deadman.deadman` and `evolve_reap.reap` are the reference shape. (PR #514 carved the exception, PR #517 reversed it the same day; this line is the rule that would have settled it at review.)

## Robustness (do not erode)

Four correctness guards — run lease, canonical-snapshot TOCTOU guard, atomic writes + quarantine, content-ownership commit gate — on the flock substrate (`_locking.py`), plus friction logging as the self-evolution feedstock. These are load-bearing; simplify presentation, never the safety machinery. Threat → file → witnessed failure per mechanism: `plugins/flow/skills/flow/references/robustness.md`.

## Issue tracking (bd / beads)

This project uses **bd (beads)**. Run `bd prime` for workflow context and the full command reference.

- Use `bd` for ALL task tracking — never TodoWrite, TaskCreate, or markdown TODO lists.
- Issues live in a local Dolt DB; sync uses `refs/dolt/data` on the git remote; `.beads/issues.jsonl` is a passive export. Docs: https://github.com/gastownhall/beads
- Remote-durable bead state: `bd export -o .beads/issues.jsonl`, then commit it on a branch or PR. Never push `main`.
