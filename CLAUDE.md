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

## Invariants

- **Prose↔CLI seam.** `SKILL.md` + `references/*.md` invoke `${CLAUDE_SKILL_DIR}/scripts/*.py`. After editing any of them, run `seam_check.py` (also gated by `tests/test_seam_check.py::test_live_docs_are_green`). It catches prose naming a flag/subcommand a script lacks — unit tests bypass argparse and miss it.
- **SKILL.md stays thin.** Router + the one gate (ExitPlanMode + confidence) + the do-loop skeleton stay inline (hot path, run every iteration incl. backgrounded). Verbose detail lives in `references/`. Don't let SKILL.md grow back.
- **Self-evolution is the thesis.** The reflect stage repairs the harness from inside a run via `machinery_edit.py` (flock-serialized, snapshot-aware). See `references/self-evolution.md`. Never route machinery fixes through the raw Edit tool; never self-edit `stage-registry.toml` or a wired handler mid-run.
- **Version bumps.** On any change to the plugin, bump `plugins/flow/.claude-plugin/plugin.json` and the `.claude-plugin/marketplace.json` flow entry in sync.

## Robustness (do not erode)

Run lease, canonical-snapshot TOCTOU guard, atomic writes + quarantine, content-ownership commit gate, friction logging. These are load-bearing; simplify presentation, never the safety machinery.
