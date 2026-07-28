# /flow e2e recipe cheatsheet (flow engine repo)

Plan-time reference for authoring the `--e2e-recipe` string that `/flow spec` stamps into ticket frontmatter. The e2e stage is the one stage that observes the change actually *behaving* — not compiling, not passing review, behaving — and it significantly improves end-to-end correctness. Author a real recipe whenever the ticket has a runnable surface. `skip:` is the exceptional, justified path, never the convenient one.

## Precondition: no e2e stage is wired here right now

`[pipeline.handlers]` in `.flow/workspace.toml` carries no `e2e` key, so nothing executes the recipes below, and `worktree create` refuses a recipe that expects the stage to run. Until the handler is restored, settle `skip: <reason>` at the gate and run the matching row from the table below by hand, recording the result in the ticket.

The absence is drift rather than a decision: PR #427 wired the stage here, the 2026-07-18 stabilization freeze removed it alongside `reflect`, `merge` and `review_brief`, and the 2026-07-27 lift restored only four coupled parts and not this one. Restoring it is tracked separately, so treat the table below as the recipe you will run by hand today and the recipe the stage will run once it is back.

## Decide the recipe

| ticket touches | recipe |
|---|---|
| engine scripts (`plugins/flow/skills/flow/scripts/*.py`) | `test-ci-only` (definition below) |
| prose↔CLI seam only (`SKILL.md`, `references/*.md` naming flags/scripts) | seam gate: `python3 seam_check.py` from the scripts dir (add `mise run test` when scripts changed too) |
| hooks (`plugins/flow/hooks/`) | hooks suite: `mise exec python -- pytest ../../../hooks/tests` from the scripts dir (also covered inside `mise run test`) |
| live pipeline behavior (dispatch loop, bootstrap, stage wiring) | live-run smoke, settled per ticket: exercise the changed path for real (e.g. `flow_worktree.py create` against a scratch ticket, or a `dispatch_stage.py` cycle in a throwaway run dir) — the suite alone does not observe dispatch behavior |
| docs/meta only (README, dev-history, inventory prose) | `skip: docs-only, no runnable surface` |

While no e2e stage is wired here (see the precondition above), every row other than the docs/meta one is a recipe you settle as `skip: <reason>` and then run by hand, because bootstrap refuses a recipe that expects a stage to run.

## test-ci-only definition

From `plugins/flow/skills/flow/scripts/`:

```bash
mise run lint && mise run test && python3 seam_check.py
```

All green = pass. Any red = failed stage — a red run is a real regression; never return success on red.

## Pass signal

Exit codes are the signal: pytest summary green (0 failed) + `seam_check.py` exit 0 + ruff/ty clean. No `E2E_OK` token needed in this repo.

## Env-prep

None. Runtime is stdlib `python3`; the dev venv resolves via `mise` from the scripts dir (worktrees are `mise trust`ed at bootstrap). No credentials, no containers.

## Sentinels (deliberate, never silent)

`skip: <reason>` — the plan consciously declares no runnable e2e for this ticket, with the reason stated. Use it for docs/meta-only diffs, never to dodge a real run on engine behavior.

`test-ci-only` — the cheap gate above and nothing heavier. The floor for engine-script tickets and the `--auto` fallback when no richer recipe was settled.

`test-ci-only` is itself refused while no e2e stage is wired here: it expects the stage to run and emit its evidence block, so it is not a way to opt out. Settle `skip: <reason>` and run the gate by hand instead.
