# /flow e2e recipe cheatsheet (flow engine repo)

Plan-time reference for authoring the `--e2e-recipe` string that `/flow spec` stamps into ticket frontmatter. The e2e stage is the one stage that observes the change actually *behaving* — not compiling, not passing review, behaving — and it significantly improves end-to-end correctness. Author a real recipe whenever the ticket has a runnable surface. `skip:` is the exceptional, justified path, never the convenient one.

## What e2e is for here, and what it is not

The e2e stage observes the change BEHAVING. It is not a third run of the gates implement already ran and CI runs again: `mise run lint`, `mise run test` and `seam_check.py` are the implement gate, they run in CI on every push, and repeating them under an e2e recipe buys a slower green rather than new evidence.

So in this repo most ticket classes settle `skip: <reason>`, and that is the honest answer rather than a dodge. The one class that earns a real recipe is live pipeline behavior, because the suite genuinely does not observe dispatch: a run's own wiring, bootstrap, and stage transitions are exercised only by driving them.

## Decide the recipe

| ticket touches | recipe |
|---|---|
| engine scripts (`plugins/flow/skills/flow/scripts/*.py`) | `skip: covered by the implement gate and CI (lint, suite, seam_check)` |
| prose↔CLI seam only (`SKILL.md`, `references/*.md` naming flags/scripts) | `skip: seam_check runs in the implement gate and in CI` |
| hooks (`plugins/flow/hooks/`) | `skip: hooks suite runs inside mise run test` |
| live pipeline behavior (dispatch loop, bootstrap, stage wiring) | live-run smoke, settled per ticket: exercise the changed path for real (`flow_worktree.py create` against a scratch ticket, or a `dispatch_stage.py` cycle in a throwaway run dir). This is the row the stage exists for, because the suite alone does not observe dispatch behavior |
| docs/meta only (README, dev-history, inventory prose) | `skip: docs-only, no runnable surface` |

A `skip:` here is a claim that the change's runnable surface is already observed somewhere named, not that nobody looked. State where in the reason, so the gate reads as a decision rather than an omission.

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

`test-ci-only` remains available for a ticket whose runnable surface genuinely is the gate and nothing heavier, and it is the `--auto` fallback when no richer recipe was settled. Prefer a named `skip:` when the gate already covers the change, so the ticket records which check did the observing.
