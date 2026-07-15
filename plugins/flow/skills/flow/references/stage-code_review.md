# Stage: code_review

## Routed reviews and the fix writer

When the frozen route marks a reviewer pending, dispatch seals its logical invocation,
stage generation, source SHA, route digest, owner, and lease fence. Build one immutable
`flow.review-input-bundle/v1` from the authoritative tree. `code_reviewer` receives the
accepted plan and ticket; `diff_reviewer` receives only source identity, the bundle, and
its fixed plan-blind rubric. Both return typed findings ONLY: a reviewer that finds an
issue never fixes it and never gains write authority. `primary_review` is a non-conditional
reader, satisfied by a matching successful outcome; `plan_blind_review` is conditional so
the cheap lanes can reasoned-skip it. The routed readers run through `cognitive-worker
run-stage` like every other activated substep (`references/delivery-loop.md`, "Activated
cognitive substeps").

Any fix routes through the `review_fixer` capsule, a separate importing writer, NOT the
reviewer. When the classified findings carry an auto-fix, drive the `review_fix` substep
through `cognitive-worker run-stage` exactly as `review_loop` drives its fixer
(`references/stage-review_loop.md` §2): the writer edits inside a private capsule seeded
with the ticket's uncommitted working state, and Flow captures its binary-aware patch and
compare-and-swaps it into the authoritative worktree under a sole-writer claim. When no
finding is auto-fixable, `review_fix` is a reasoned skip. Review findings never grant write
authority or trigger a fallback review.

## Purpose

Review the implement-stage diff through the frozen exact reader routes when active.
Historical, generic, and legacy snapshots retain their recorded inline behavior.

The route snapshot records the primary pass as `code_reviewer`, the plan-blind pass as
`diff_reviewer`, and any mutation as the conditional `review_fixer` substep. `code_reviewer`,
`diff_reviewer`, and `review_fixer` all activate from a new exact snapshot. `primary_review`
returns a findings outcome; `plan_blind_review` returns a findings outcome on the full lane
and a reasoned skip on the cheap lanes; `review_fix` runs the `review_fixer` capsule when a
finding is auto-fixable, else a reasoned skip.

This is the lowest-cost gate against regressions.
The routed readers are isolated from the implementation context. A historical shadow
run may still use the inline compatibility path recorded in its snapshot.

## Inputs

- `<ticket-dir>/state.json` — `stages.implement.started_at_sha` for the diff range.
- The current working tree (uncommitted changes from the implement stage).

## Steps

1. Pull the implement-stage diff:
   ```bash
   FLOW_HARNESS="<harness>" "<facade>" diff since-stage \
     --stage implement \
     --ticket <KEY> \
     --ticket-dir <ticket-dir> \
     --cwd .
   ```
   - Exit 0 → JSON with `files_touched / insertions / deletions / binary`.
   - Exit 1 → no started_at_sha (implement didn't run).
     Abort with status=failed; `FLOW workspace repair <KEY>` → `retry --stage implement`.
   - Exit 2 → git error. Surface stderr.

   **Empty `files_touched` is expected, not "nothing to review".** `since-stage` diffs the committed range `started_at_sha..HEAD`, but implement leaves its work UNCOMMITTED (the commit stage runs later), so `started_at_sha == HEAD` and the committed range is empty. The real change is in the working tree. When `files_touched` is empty, get the actual file list from the working tree instead: `git diff HEAD --name-only` (or `git status --porcelain`). Only treat the stage as a genuine no-op if the working tree is also clean.

2. **Primary review.** For each file (from `files_touched`, or the working-tree list above when `since-stage` was empty), Read the file and read the diff via `git diff <started_at_sha> -- <path>` (no `..HEAD`, so it includes the uncommitted working tree). This is the rubric the `primary_review` (`code_reviewer`) pass carries; its findings outcome is required before completion.
   Assess for:
   - Obvious bugs (off-by-one, null-deref, missing await, etc.).
   - Regressions in nearby tests not updated by implement stage.
   - Style violations against existing file conventions.
   - Comment bloat: run `FLOW_HARNESS="<harness>" "<facade>" lint-comments --diff-base <started_at_sha>` over the reviewed files first (same sha as step 1's diff range). Each finding is at minimum a Minor auto-fix; then flag any comment that violates the code-comment bar in `references/stage-implement.md` Step 4 (self-document first; WHY-only plus the workaround / invariant / dense-expression tail; wrapped to the configured line length; no AI tells). That bar overrides local file precedent: a new comment that restates the code or narrates the diff is a violation even if it matches a comment already sitting in the file.
   - Security-sensitive patterns (eval, raw SQL, missing escape).

   **Fowler smell baseline (always carried).** This baseline of high-signal refactoring smells rides even when the repo documents no standards; each smell reads what-it-is then how-to-fix, matched against the diff only.
   - possible Mysterious Name — a name that hides intent; rename to reveal it.
   - possible Duplicated Code — the same structure in two-plus spots; extract a shared function or pull it up.
   - possible Feature Envy — a function using another module's data more than its own; move the function to the data (or extract, then move).
   - possible Data Clumps — the same group of fields or params always travelling together; extract a class or introduce a parameter object.
   - possible Primitive Obsession — a bare primitive standing in for a domain concept; replace it with a small type or value object.
   - possible Repeated Switches — the same conditional on a type code in several places; replace the conditional with polymorphism.
   - possible Shotgun Surgery — one conceptual change forcing many scattered edits; move or combine so it has a single home.
   - possible Divergent Change — one module edited for many unrelated reasons; split it along its change axes.
   - possible Speculative Generality — a hook, param, or abstraction for a need that is not here; inline and remove it.
   - possible Message Chains — long a.b().c().d() navigation; hide the delegate, or extract a function.
   - possible Middle Man — a unit that only forwards to a delegate; remove it and call the delegate directly.
   - possible Refused Bequest — a subclass ignoring most of what it inherits; push members down, or replace inheritance with delegation.

   Two binding rules govern the baseline:
   1. **The repo overrides.** A documented repo standard always wins. Where the repo's own conventions (CLAUDE.md, a style doc, an established in-file pattern) endorse a shape the baseline would flag, suppress the smell; the repo already made that call. flow's own CLAUDE.md documents structural choices the generic baseline would misread: the flat `scripts/` dir reads as Divergent Change or a rejected src-layout, `_libs` forwarding as Middle Man, the deliberate non-reorganization as either. A smell that contradicts a documented repo invariant is suppressed, not filed.
   2. **Always a judgement call.** Every entry is a labelled heuristic surfaced as `possible <smell>`, never a hard violation, and anything tooling already enforces (ruff, ty) is skipped.

   A smell is a judgement call, so it is Minor by default (rarely Major) and never Critical (it never touches the step-6 gate); its owner is normally ask-user or no-op, and auto-fix only in the trivially confident, `planned_files`-confined case (a pure rename), never an autonomous refactor from this same biased context.

3. **Classify each finding on two axes**, after the step-2 assessment:
   - **Severity** (unchanged) — **Critical** blocks the stage; **Major** should fix but not blocking; **Minor** nitpick / style.
   - **Decision owner** — who disposes of the finding:
     - **auto-fix** — routed through the `review_fixer` capsule (step 5), never an inline edit by the reviewer.
     - **no-op** — a deliberate non-fix; cite the verbatim `plan.out` line that makes it deliberate.
     - **ask-user** — the human's call; parked on the PR, never silently dropped.

   The three owners are exhaustive, and ask-user is the fallback: a finding you cannot confidently place is ask-user by definition (no-op demands a verbatim `plan.out` citation, auto-fix demands confidence — a finding qualifying for neither is the human's call). In the `.out` file the owners map to the section headers `## ask-user`, `## no-op`, and `## auto-fixed` (the one past-tense header: by write time the fix has been applied) — P2d and any other consumer keys on those headers, not on the label spellings here.

   A Critical's ONLY non-failing decision owner is auto-fix — never record a Critical as no-op or ask-user. A real bug is not the human's "your call" to make, and disposition is not a way to punt one.

   **code_review causes a pre-commit mutation here.** Today's review only flags; the auto-fix disposition means it now mutates the working tree before commit runs. The write is the `review_fixer` capsule's, not the reviewer's, and the human-facing Critical floor (step 6) is unchanged.

4. **Plan-blind reader pass (full lane only).** A second review by a fresh mind that has never seen the plan, closing the residual planner-bias window this same context cannot: a flawed plan faithfully implemented reads clean to the reviewer who shares the planner's assumptions. It is a DISTINCT single pass over the implement diff, NOT a re-review loop. It runs BEFORE the step-5 fix so its catches ride the same single fixer invocation.

   **Gate on the lane — full only.** Read the run's lane from frontmatter and SKIP this entire step on the cheap lanes (`express` / `light`), which already traded away this depth:
   ```bash
   LANE=$(FLOW_HARNESS="<harness>" "<facade>" frontmatter read .flow/tickets/<KEY>.md \
     | python3 -c "import json,sys; print(json.load(sys.stdin).get('lane') or 'full')")
   ```
   Run the reader only when `LANE` is `full` (absent frontmatter reads as `full`). On `express` / `light`, take a reasoned skip for `plan_blind_review` at the terminal (step 9) — the conditional substep exists for exactly this lane skip. Gate on the LANE, never on route activation: a full-lane run whose reader route is legacy, shadowed, or opted out still carries the planner-bias window. Every full-lane run gets one; execution provenance is a separate question.

   **The lane is sealed, not just prose-honored.** For an activated `plan_blind_review`, dispatch seals the run's resolved lane onto the substep at `_cognitive_substeps` (absent/unknown frontmatter seals `full`), and `_validate_cognitive_completion` refuses a reasoned skip for it on the `full` lane (flow-ijyh). So a full-lane skip of the reader wedges the stage deterministically, not only by this prose; the express/light skip still completes.

   **Route.** Resolve `diff_reviewer` from the frozen snapshot and follow the
   structured launch and attestation contract in `references/delivery-loop.md`:
   ```bash
   FLOW_HARNESS="<harness>" "<facade>" agent-route resolve \
     --snapshot "$TICKET_DIR/route-snapshot.json" --profile diff_reviewer
   ```
   On the full lane the `plan_blind_review` substep returns a findings outcome, plan-blind by
   construction: the `diff_reviewer` bundle carries source identity and the diff, never the
   plan or ticket, so the route itself is the plan-blindness. Follow the "Activated cognitive
   substeps" launch in `references/delivery-loop.md`.

   **Triage — advisory only, no blocking power.** The reader's observations are candidates, not findings. Classify each through step 3's two-axis taxonomy, folding its catches into the same auto-fix / no-op / ask-user sets the step-5 fixer draws from, plus one reader-only disposition:
   - **dismissed** — a hallucinated or irrelevant observation, or one the primary pass already recorded (any owner — do not render the same decision twice): drop it, or record it as a `## no-op` with a verbatim `plan.out` citation when it names a choice the plan made deliberately AND you have independently confirmed the choice is correct. Deliberate is not correct — the reader exists because plan-faithful can be plan-flawed, so a reader observation contradicting a deliberate plan choice that you can NOT independently confirm fails safe: ask-user for a Major/Minor, and for a Critical the step-6 gate (ask-user is banned for Criticals). A fourth disposition, NOT a new `.out` section.
   - a real catch routes exactly as a primary finding — **auto-fix** (confident and confined to `planned_files`: it joins the step-5 auto-fix set) or **ask-user** (uncertain, or the fix falls outside `planned_files`).
   - the reader has NO independent blocking power: a reader-surfaced Critical fails the stage (step 6) ONLY on independent orchestrator agreement, after which it routes like any Critical (auto-fix if confined, else left unresolved). Step 3's invariant holds — a Critical's only non-failing owner is auto-fix.

   **Fail-open.** A spawn or return failure never fails the stage; the reader is advisory. Log one line and proceed with the primary findings only.

5. **Route the auto-fix set through the `review_fixer` capsule, never an inline edit.** The reviewer is findings-only; the fix is a separate writer invocation. When the classified auto-fix set (primary pass plus any full-lane reader catches) is non-empty, drive the `review_fix` substep through `cognitive-worker run-stage` (`references/delivery-loop.md`, "Activated cognitive substeps"), exactly as `review_loop` §2 drives its fixer: build the fixer's closed facts — the auto-fix findings as `review_findings`, plus `ticket`, `source_sha`, `planned_files`, and the report contract — and its immutable input bundle, and hand both to the executor for the sealed `review_fix` substep. Flow — not the model — captures the writer's binary-aware patch and compare-and-swap imports it under a sole-writer claim; the worker returns only a typed report (`summary`, `evidence`, `source_sha`) and never serializes a diff. After the import lands, re-assess ONCE. This is a single verification pass, not an unbounded re-review loop — do not iterate past it. Because code_review is the same biased context that just wrote the code, only route findings that are confident/local/obvious; a Critical needing a design rethink is not auto-fixable and falls through to the gate below unresolved.

   **Pre-commit seed, own-delta capture.** code_review runs after implement and BEFORE commit, so the working tree carries implement's uncommitted changes. Dispatch seeds the `review_fixer` capsule with that working-state delta and captures the fixer's patch against the seeded baseline, so ONLY the fixer's own change imports — implement's edits are the seed, never re-imported (no double-count, no `patch_import_conflict`). This is the same pre-commit-seeded importing-writer path the E2E capsule uses.

   **Confinement to `planned_files` is sealed, not advisory.** The order's `allowed_mutation_paths` is sealed to the run's `planned_files` (from `baseline.json`, the same set the commit stage stages and the content-ownership gate re-scans), so a fix touching any path outside that set makes the whole capsule patch an `ownership_violation` and nothing imports. A finding whose fix would touch an out-of-set file is NOT auto-fixable: downgrade it to ask-user, or, if Critical, leave it unresolved (it fails the stage at the gate below — the rerun-implement escape hatch, not a `planned_files` widening here). There is no inline reviewer edit here, so the old worktree-absolute-path and bg-isolation edit discipline no longer applies; the capsule owns the write.

   **No auto-fix findings → a reasoned skip.** When the auto-fix set is empty, do NOT launch the fixer: record a reasoned skip for the `review_fix` substep through the same `cognitive-worker run-stage` skip input (`references/delivery-loop.md`). The terminal advance (step 9) carries that skip.

6. **Critical gate.** After the step-5 fix pass, any unresolved Critical finding (a primary or a reader-surfaced Critical the orchestrator has independently agreed is real) aborts the stage with status=failed. Surface the finding so the user can decide between rerunning implement vs overriding — unchanged from before.

7. **Record no-ops** — Major/Minor findings left as deliberate non-fixes, each with a verbatim citation of the `plan.out` line that justifies it.

8. **Record ask-user items** — Major/Minor findings that are the human's call. Never fire an `AskUserQuestion` for these, even in an attended run; they ride to the PR as flagged decisions, not a mid-run blocker.

9. **Write `code_review.out`** (see Outputs), keep reporting findings inline as today, then satisfy the cognitive outcome fence before `advance --status completed` (below).

   **Satisfy the cognitive outcome fence on EVERY path.** The frozen snapshot seals `primary_review` (non-conditional), `plan_blind_review` (conditional), and `review_fix` (conditional). `primary_review` is satisfied by its findings outcome; it cannot be skipped, so this stage does not complete without it. `plan_blind_review` returns an outcome on the full lane and needs a reasoned skip only on `express` / `light`; because its lane is sealed onto the substep, the fence refuses a full-lane skip (flow-ijyh), so a full-lane run cannot complete on a `plan_blind_review` skip. `review_fix` is satisfied by its capsule outcome when a fix imported (the fence reads that first and ignores a skip for it), and needs a reasoned skip whenever the auto-fix set was empty. Emit a reasoned skip for every conditional substep that did NOT launch a capsule this run through the `cognitive-worker run-stage` skip input, then pass the executor's `cognitive_skips` as the advance skill output (`--skill-output-from`). Without the no-fix `review_fix` skip the terminal advance fails closed — `activated cognitive substep 'review_fix' has no successful outcome or valid skip` — the green-first-poll wedge class `review_loop` §5 closes, checked here the same way. Then `status=completed` when no unresolved Critical remains.

## Outputs

- `$TICKET_DIR/stages/code_review.out` — the classified findings, one section per decision owner. Written via the same quoted-heredoc pattern as `pr_body.md` (sentinel `FLOW_OUT_SENTINEL_9f3a`, see `references/delivery-loop.md`), then `--output-path "$TICKET_DIR/stages/code_review.out"` is passed on `advance`. First line is the marker `<!-- flow:code_review-taxonomy v1 -->` (flow's `<!-- SYNC: ... -->` HTML-comment idiom) — the signal `create_pr` uses to distinguish this taxonomy from a `skill:<name>` handler's free-form `.out`.

  ```
  <!-- flow:code_review-taxonomy v1 -->
  # code_review findings — <KEY>

  ## ask-user
  - [Major] <finding> — <the decision the human must make> (<file>:<loc>)

  ## no-op
  - [Minor] <finding> — deliberate per plan: "<verbatim plan.out line>" (<file>:<loc>)

  ## auto-fixed
  - [Major] <finding> — fixed in <file>:<loc>
  ```

  Bullets are plain `- [Major] ...`, no `**bold:**` lead — `pr_body.py::scrub` flattens a bold bullet lead, so a bold render would be mangled when `create_pr` lifts these into the PR body. A section is omitted entirely when its finding list is empty, EXCEPT `## auto-fixed`, which is never optional when non-empty: it is the run's only durable ANNOTATION of a pre-commit mutation the `review_fixer` capsule imported on its behalf, a silently auto-fixed Critical most of all (the fixed code itself is reviewer-visible in the draft-PR diff; this out-file section is run-state for downstream consumers like P2d, not part of the PR body).

## Errors

- `diff_extract.py` exit 1 → implement stage never ran.
- `diff_extract.py` exit 2 → git environment broken; abort.

## Skip conditions

- Skipped entirely if `workspace.toml [pipeline.handlers] code_review =
  "none"`.
- Replaced if `workspace.toml [pipeline.handlers] code_review =
  "skill:<name>"` — dispatcher dispatches the skill instead.
