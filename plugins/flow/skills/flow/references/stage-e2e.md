# Stage: e2e

## Purpose

Execute the **e2e recipe the plan declared** and surface any failure.
This stage runs BY DEFAULT (`stage-registry.toml` default handler is `subagent:general-purpose`): it is the ONE stage that observes the change actually behaving end-to-end, and it significantly improves end-to-end correctness — no other stage exercises the change running.
A workspace disables it only by explicitly setting `e2e = "none"` in `workspace.toml [pipeline.handlers]`; that is a deliberate opt-out, never the convenient default.
When it runs, the spec/plan gate requires an `e2e_recipe` frontmatter field (see `flow_worktree.py create --e2e-recipe`), so by the time you run there is a recipe to execute — you do NOT detect or guess a suite.

e2e sits AFTER `code_review` so cheap inline review catches obvious issues before a slow end-to-end run burns time.
By the time you run, the implement diff has already passed review.

The recipe is the project's contract for what e2e means on this ticket.
Project specifics (auth/login, container setup, memory tuning, which fixture) live IN the recipe — authored at plan time by someone who knows the repo.
Your job is to run it exactly, not to reinterpret it.

## Inputs

- `.flow/tickets/<KEY>.md` frontmatter — the `e2e_recipe` field. This is your
  primary input: it names the runner, the exact command, any env-prep, the
  fixture, and the expected pass signal.
- `.flow/runs/<KEY>/ticket.json` — ticket context, for understanding what the
  recipe is verifying.
- The current repository, including the implement-stage changes in the working
  tree.

## Steps

1. HARD GATE the recipe is present:
   ```bash
   ${CLAUDE_SKILL_DIR}/scripts/lint_ticket.py \
     --stage e2e \
     --ticket-path .flow/tickets/<KEY>.md
   ```
   Exit 0 → continue.
   Exit 1 → `e2e_recipe` is missing/empty. The bootstrap gate should have caught
   this; report it as a failed stage (e2e is running but the plan never settled a
   recipe) and stop. (If `CLAUDE_SKILL_DIR` is unset in your environment, read the
   `e2e_recipe` field directly from the frontmatter instead; same outcome — an
   absent/empty recipe is a failed stage.)

2. Read the `e2e_recipe` value. Handle the two sentinel forms first:
   - `skip: <reason>` → the plan consciously declared no e2e for this ticket.
     Report the skip + the reason and finish the stage **completed**. Do not run
     anything.
   - `test-ci-only` → run the project's no-frills CI/unit suite (the cheap gate
     the recipe names, e.g. a `mise`/`make`/`npm` test task) and report its
     result. Red = failed stage.
   - anything else → a real recipe; go to step 3.

3. Execute the recipe exactly as written. Run its env-prep first (the recipe
   spells out any auth refresh, container/service bring-up, or resource tuning
   it needs), then the command, against the fixture it names.
   If an env-prep step needs credentials that have expired, run the refresh
   command the recipe specifies. Only when a genuinely interactive step cannot
   complete unattended do you stop and report the blocker.

4. Surface the result:
   - All green → report the recipe, the command run, and the pass summary.
   - Failures → report what failed, the command, and the relevant failure
     output. A red run is a real regression; do NOT return success on red.

5. Return the run result as your response.

## Outputs

- The e2e run result (recipe, command, pass/fail summary, failure detail on
  red), returned as your stage report. The do-loop captures it to
  `<ticket-dir>/stages/e2e.out`; you do not write that file yourself.

## Errors

- Recipe runs and fails → report the failure and return with the stage
  unfinished. A failing e2e recipe is a failed stage.
- `e2e_recipe` missing/empty → workspace misconfiguration (e2e is running without a
  recipe; the bootstrap gate normally prevents this). Report it as failed so the
  user supplies a recipe or explicitly disables e2e (`e2e = "none"`).
- Env-prep needs a genuinely interactive step that cannot run unattended → stop
  and report the blocker (it surfaces as needs-input in `claude agents` when the
  session is backgrounded); recipes should specify a non-interactive refresh path
  to avoid this.

## Skip conditions

- Stage handler is `none`. This is NOT the default — a workspace must have
  explicitly disabled e2e (`e2e = "none"` in `workspace.toml [pipeline.handlers]`)
  for this to apply. The do-loop's `none` branch short-circuits the stage before
  this doc is ever read, and the `e2e_recipe` requirement never applies.
- A `skip: <reason>` recipe value is an in-stage skip (step 2): the stage runs,
  reads the conscious decision, and finishes completed without executing a suite.
  This is the exceptional, justified path (a genuinely no-runnable-surface ticket)
  — never the convenient way to dodge a real run.
