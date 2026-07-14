# Stage: code_review

## Purpose

Inline main-agent self-review of the implement-stage diff.
Bare workspace default; richer review is wired by installing a code-review skill via the init wizard.

This is the lowest-cost gate against regressions.
The main agent is the same context that just produced the implement-stage code, so the review is biased toward what it just wrote.
That bias is acceptable for personal-mode flow; work-mode users opt in to `skill:code-review` via init.

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

2. For each file (from `files_touched`, or the working-tree list above when `since-stage` was empty), Read the file and read the diff via `git diff <started_at_sha> -- <path>` (no `..HEAD`, so it includes the uncommitted working tree).
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
     - **auto-fix** — the agent fixes it in the working tree before commit.
     - **no-op** — a deliberate non-fix; cite the verbatim `plan.out` line that makes it deliberate.
     - **ask-user** — the human's call; parked on the PR, never silently dropped.

   The three owners are exhaustive, and ask-user is the fallback: a finding you cannot confidently place is ask-user by definition (no-op demands a verbatim `plan.out` citation, auto-fix demands confidence — a finding qualifying for neither is the human's call). In the `.out` file the owners map to the section headers `## ask-user`, `## no-op`, and `## auto-fixed` (the one past-tense header: by write time the fix has been applied) — P2d and any other consumer keys on those headers, not on the label spellings here.

   A Critical's ONLY non-failing decision owner is auto-fix — never record a Critical as no-op or ask-user. A real bug is not the human's "your call" to make, and disposition is not a way to punt one.

   **code_review becomes a writing stage here.** Today's review only flags; the auto-fix disposition below means it now mutates the working tree before commit runs. The human-facing Critical floor (step 6) is unchanged.

4. **Apply auto-fixes** to the working tree (inline `Edit`/`Write`), then re-assess ONCE. This is a single verification pass, not an unbounded re-review loop — do not iterate past it. Because code_review is the same biased context that just wrote the code, only fix what is confident/local/obvious; a Critical needing a design rethink is not auto-fixable and falls through to the gate below unresolved.

   **Auto-fix confinement to `planned_files`.** The commit stage stages only `planned_files` from `baseline.json`; a fix touching a file outside that set does not ride into the commit. A finding whose fix would touch an out-of-set file is NOT auto-fixable: downgrade it to ask-user, or, if Critical, leave it unresolved (it fails the stage at the gate below — the correct rerun-implement escape hatch, not a `planned_files` widening here).

   **Auto-fix edit-path discipline.** These edits follow the same "Inline-edit path discipline" as the review_loop fix edits (`references/delivery-loop.md`, flow-cjgy): a worktree-absolute (or worktree-relative) path only — a main-checkout-absolute path silently escapes the worktree and writes main. In a backgrounded unattended run the bg-isolation guard forces the heredoc/Bash string-replace fallback for these edits, same as any other inline write in that mode.

5. **Plan-blind reader pass (full lane only).** A second review by a fresh mind that has never seen the plan, closing the residual planner-bias window this same context cannot: a flawed plan faithfully implemented reads clean to the reviewer who shares the planner's assumptions. It is a DISTINCT single pass, NOT a re-entry of step 4's loop — step 4's "re-assess ONCE" guards the biased context from iterating on itself, while a plan-blind reader is categorically a different reviewer. One inline pass + one reader pass = two single passes, no loop.

   **Gate on the lane — full only.** Read the run's lane from frontmatter and SKIP this entire step on the cheap lanes (`express` / `light`), which already traded away this depth:
   ```bash
   LANE=$(FLOW_HARNESS="<harness>" "<facade>" frontmatter read .flow/tickets/<KEY>.md \
     | python3 -c "import json,sys; print(json.load(sys.stdin).get('lane') or 'full')")
   ```
   Run the reader only when `LANE` is `full` (absent frontmatter reads as `full`). Gate on the LANE, never on route activation: a full-lane run whose reader route is legacy, shadowed, or opted out still carries the planner-bias window. Every full-lane run gets one; execution provenance is a separate question.

   **Route.** Resolve `diff_reviewer` from the frozen snapshot and follow the
   structured launch and attestation contract in `references/delivery-loop.md`:
   ```bash
   FLOW_HARNESS="<harness>" "<facade>" agent-route resolve \
     --snapshot "$TICKET_DIR/route-snapshot.json" --profile diff_reviewer
   ```
   Claude Code may activate the exact desired route after native structured
   acceptance. Codex, generic, and legacy paths inherit as documented and never
   claim the desired route ran.

   **Spawn: the diff, and only the diff.** Capture the post-auto-fix working-tree diff (`git diff <started_at_sha>`, no `..HEAD`, so it includes the uncommitted implement work and any step-4 auto-fixes; this is the diff that will actually ship), then spawn ONE fresh independent agent with the compatible model behavior above. Include `Harness: <claude-code|codex|generic>` in its prompt, then carry ONLY that diff embedded verbatim plus the fixed question: *what does this change do; what looks wrong or surprising*. Instruct it to review ONLY the shown diff and NOT read any file, open the ticket or plan, or run any command; its value is that it is blind to the intent. If the protocol ever permits a Flow command later, the harness identity requires the same-call `FLOW_HARNESS=<Harness>` prefix. Embedding the diff rather than telling it to run `git` is load-bearing: a fresh subagent could otherwise wander into `.flow/tickets/` or `plan.out` and lose the plan-blindness that is the whole point.

   **Triage — advisory only, no blocking power.** The reader's observations are candidates, not findings. Classify each through step 3's two-axis taxonomy, plus one reader-only disposition:
   - **dismissed** — a hallucinated or irrelevant observation, one the inline pass already recorded (any owner — do not render the same decision twice), or one an auto-fix already resolved: drop it, or record it as a `## no-op` with a verbatim `plan.out` citation when it names a choice the plan made deliberately AND you have independently confirmed the choice is correct. Deliberate is not correct — the reader exists because plan-faithful can be plan-flawed, so a reader observation contradicting a deliberate plan choice that you can NOT independently confirm fails safe: ask-user for a Major/Minor, and for a Critical the step-6 gate (ask-user is banned for Criticals). A fourth disposition, NOT a new `.out` section.
   - a real catch routes exactly as an inline finding — **auto-fix** (confident and confined to `planned_files`: apply it in this pass's single auto-fix application, same confinement + edit-path discipline as step 4, no reader re-spawn) or **ask-user** (uncertain, or the fix falls outside `planned_files`).
   - the reader has NO independent blocking power: a reader-surfaced Critical fails the stage (step 6) ONLY on independent orchestrator agreement, after which it routes like any Critical (auto-fix if confined, else left unresolved). Step 3's invariant holds — a Critical's only non-failing owner is auto-fix.

   **Fail-open.** A spawn or return failure never fails the stage; the reader is advisory. Log one line and proceed to the gate with the inline findings only.

6. **Critical gate.** After the auto-fix pass, any unresolved Critical finding (inline, or a reader-surfaced Critical the orchestrator has independently agreed is real) aborts the stage with status=failed. Surface the finding so the user can decide between rerunning implement vs overriding — unchanged from before.

7. **Record no-ops** — Major/Minor findings left as deliberate non-fixes, each with a verbatim citation of the `plan.out` line that justifies it.

8. **Record ask-user items** — Major/Minor findings that are the human's call. Never fire an `AskUserQuestion` for these, even in an attended run; they ride to the PR as flagged decisions, not a mid-run blocker.

9. **Write `code_review.out`** (see Outputs), keep reporting findings inline as today, then `status=completed` when no unresolved Critical remains.

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

  Bullets are plain `- [Major] ...`, no `**bold:**` lead — `pr_body.py::scrub` flattens a bold bullet lead, so a bold render would be mangled when `create_pr` lifts these into the PR body. A section is omitted entirely when its finding list is empty, EXCEPT `## auto-fixed`, which is never optional when non-empty: it is the run's only durable ANNOTATION of a pre-commit mutation it made on its own, a silently auto-fixed Critical most of all (the fixed code itself is reviewer-visible in the draft-PR diff; this out-file section is run-state for downstream consumers like P2d, not part of the PR body).

## Errors

- `diff_extract.py` exit 1 → implement stage never ran.
- `diff_extract.py` exit 2 → git environment broken; abort.

## Skip conditions

- Skipped entirely if `workspace.toml [pipeline.handlers] code_review =
  "none"`.
- Replaced if `workspace.toml [pipeline.handlers] code_review =
  "skill:<name>"` — dispatcher dispatches the skill instead.
