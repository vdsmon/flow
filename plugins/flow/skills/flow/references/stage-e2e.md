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
     anything. This is the one case that emits NO evidence block: the skip line is
     the whole report, with no sentinel (create_pr then draws `## Evidence` from
     the implement verify tail alone, omitting the section when that too is empty).
   - `test-ci-only` → run the project's no-frills CI/unit suite (the cheap gate
     the recipe names, e.g. a `mise`/`make`/`npm` test task) and report its
     result as a rung-1 evidence block (the sentinel + the run block of step 4,
     for the CI-gate run). Red = failed stage.
   - anything else → a real recipe; go to step 3.

3. Execute the recipe exactly as written. Run its env-prep first (the recipe
   spells out any auth refresh, container/service bring-up, or resource tuning
   it needs), then the command, against the fixture it names.
   If an env-prep step needs credentials that have expired, run the refresh
   command the recipe specifies. Only when a genuinely interactive step cannot
   complete unattended do you stop and report the blocker.

   **Chunking a heavy module.** When a single module cannot finish in one Bash
   call — it exceeds the ~600s ceiling (`timeout` <= 600000ms) or risks the ~360s
   idle watchdog (flow-rbr) — and must be split across calls, do NOT chunk it by
   `pytest -k <class-name>` substrings: `-k` matching is not a partition, so any
   class named in neither shard's `-k` is silently dropped with no error (FT-1363:
   an 8+7-class split ran only 49 of the module's 84 tests). Partition by node-id
   instead. Run `pytest <module> --collect-only -q` once, bare, and confirm it
   reports N > 0 collected before you trust the count — a broken collect prints 0,
   so a naive equality check passes falsely at 0 == 0 (flow-aod). Split the emitted
   node-id lines (drop the trailing "N tests collected" summary line pytest appends
   under `-q`) into shards, and run each shard by explicit nodeids (quote each — parametrized ids carry `[`, `]`, and spaces). A node-id
   partition is disjoint and exhaustive by construction, which is the actual fix.
   Run each shard as one foreground Bash call with an explicit `timeout`
   <= 600000ms, never `run_in_background` or `Monitor` (this stage is a spawned
   `subagent:general-purpose`, so a backgrounded command strands the turn — the
   FT-1328 rule from `references/stage-implement.md` Step 5); short shards also
   dodge the ~360s idle watchdog. Then backstop the partition: the summed per-shard
   **collected-item count** MUST equal the collect-only total N. Sum the "collected
   K items" each shard reports, not the passed count — a green run can legitimately
   skip or xfail, so passed < N is normal, but collected < N is under-coverage. If
   the sum is short, a shard is missing tests; widen the partition until they match,
   and fold N and the per-shard aggregate into the rung-1 evidence transcript
   (step 4).

4. Produce a **structured evidence report**. This report is your stage output and
   the create_pr stage machine-reads it (the `## Evidence` PR section), so its shape
   is a contract, not free prose.

   The FIRST LINE must be the sentinel, exactly:
   ```
   <!-- flow:e2e-evidence v1 -->
   ```
   Then the evidence rungs, cheapest first. Always emit rung 1; add rungs 2-4 ONLY
   when the recipe's **evidence note** asks for them (the note is the plan-time
   declaration of which rungs to keep, its shape is in `e2e-recipes.md`). The ladder
   is a menu the note picks from, never a checklist to fill.

   - **Rung 1, transcript (always).** A run block: the recipe (verbatim), the exact
     command you ran, the exit status, the wall-clock duration, and a one-line
     pass/fail summary; then the tail of the real output in a fenced block (~40
     lines, prefix a `… earlier output trimmed …` line inside the fence if you
     dropped any). On green keep the summary lines that prove the pass; on red keep
     the failure and enough context to see why.
   - **Rung 2, baseline delta (only if the note asks).** Run the baseline comparison
     the note names, then report how many lines differ, which section/block they
     fall in, a tiny fenced diff excerpt, and an expected-vs-unexpected read scoped
     to what the note says the change should touch. (brinta's parity/snapshot
     framework is one producer of this rung; where that tooling is not present the
     note will not ask for rung 2.)
   - **Rung 3, fingerprint + targeted excerpt (only if the note asks).** For the
     output artifact the note names, report filename, byte size, sha256
     (`shasum -a 256 <file>` or `sha256sum <file>`), line count, and the
     section/record counts the note specifies; then a short fenced excerpt of the
     exact lines the ticket targets (before/after when the note frames it that way).
     All textual and local, computed from the run's own output.
   - **Rung 4, external blob link (only when the note carries a destination).** If,
     and only if, the evidence note contains an explicit human-authored upload
     destination, upload the full artifact there and record the URL plus the sha256
     that pins it. Never invent a destination; an `--auto` planner never introduces
     one, and with no destination in the note there is no rung 4.

   Keep the report scrub-safe: no em-dashes in the prose lines (write
   `command: N passed, M failed (duration)`); fenced blocks pass through the
   create_pr scrub untouched, so raw transcript punctuation is safe inside them.

   A red run is still a red run: report what failed exactly, in the same rung-1
   block, and do NOT return success on red.

5. Return the structured report as your response.

## Outputs

- The structured evidence report (sentinel first line, then the rungs step 4
  produced), returned as your stage report. The do-loop captures it to
  `<ticket-dir>/stages/e2e.out` unchanged; you do not write that file yourself.
  A `skip: <reason>` run is the exception: its report is the skip line, no
  sentinel.

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
