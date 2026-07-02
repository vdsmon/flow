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
   ${CLAUDE_SKILL_DIR}/scripts/diff_extract.py since-stage \
     --stage implement \
     --ticket <KEY> \
     --ticket-dir <ticket-dir> \
     --cwd .
   ```
   - Exit 0 → JSON with `files_touched / insertions / deletions / binary`.
   - Exit 1 → no started_at_sha (implement didn't run).
     Abort with status=failed; `/flow recover <KEY>` → `retry --stage implement`.
   - Exit 2 → git error. Surface stderr.

   **Empty `files_touched` is expected, not "nothing to review".** `since-stage` diffs the committed range `started_at_sha..HEAD`, but implement leaves its work UNCOMMITTED (the commit stage runs later), so `started_at_sha == HEAD` and the committed range is empty. The real change is in the working tree. When `files_touched` is empty, get the actual file list from the working tree instead: `git diff HEAD --name-only` (or `git status --porcelain`). Only treat the stage as a genuine no-op if the working tree is also clean.

2. For each file (from `files_touched`, or the working-tree list above when `since-stage` was empty), Read the file and read the diff via `git diff <started_at_sha> -- <path>` (no `..HEAD`, so it includes the uncommitted working tree).
   Assess for:
   - Obvious bugs (off-by-one, null-deref, missing await, etc.).
   - Regressions in nearby tests not updated by implement stage.
   - Style violations against existing file conventions.
   - Comment bloat: flag any comment that violates the code-comment bar in `references/stage-implement.md` Step 4 (self-document first; WHY-only plus the workaround / invariant / dense-expression tail; wrapped to the configured line length; no AI tells). That bar overrides local file precedent: a new comment that restates the code or narrates the diff is a violation even if it matches a comment already sitting in the file.
   - Security-sensitive patterns (eval, raw SQL, missing escape).

3. **Classify each finding on two axes**, after the step-2 assessment:
   - **Severity** (unchanged) — **Critical** blocks the stage; **Major** should fix but not blocking; **Minor** nitpick / style.
   - **Decision owner** — who disposes of the finding:
     - **auto-fix** — the agent fixes it in the working tree before commit.
     - **no-op** — a deliberate non-fix; cite the verbatim `plan.out` line that makes it deliberate.
     - **ask-user** — the human's call; parked on the PR, never silently dropped.

   The three owners are exhaustive, and ask-user is the fallback: a finding you cannot confidently place is ask-user by definition (no-op demands a verbatim `plan.out` citation, auto-fix demands confidence — a finding qualifying for neither is the human's call). In the `.out` file the owners map to the section headers `## ask-user`, `## no-op`, and `## auto-fixed` (the one past-tense header: by write time the fix has been applied) — P2d and any other consumer keys on those headers, not on the label spellings here.

   A Critical's ONLY non-failing decision owner is auto-fix — never record a Critical as no-op or ask-user. A real bug is not the human's "your call" to make, and disposition is not a way to punt one.

   **code_review becomes a writing stage here.** Today's review only flags; the auto-fix disposition below means it now mutates the working tree before commit runs. The human-facing Critical floor (step 5) is unchanged.

4. **Apply auto-fixes** to the working tree (inline `Edit`/`Write`), then re-assess ONCE. This is a single verification pass, not an unbounded re-review loop — do not iterate past it. Because code_review is the same biased context that just wrote the code, only fix what is confident/local/obvious; a Critical needing a design rethink is not auto-fixable and falls through to the gate below unresolved.

   **Auto-fix confinement to `planned_files`.** The commit stage stages only `planned_files` from `baseline.json`; a fix touching a file outside that set does not ride into the commit. A finding whose fix would touch an out-of-set file is NOT auto-fixable: downgrade it to ask-user, or, if Critical, leave it unresolved (it fails the stage at the gate below — the correct rerun-implement escape hatch, not a `planned_files` widening here).

   **Auto-fix edit-path discipline.** These edits follow the same "Inline-edit path discipline" as the review_loop fix edits (`references/verb-do.md`, flow-cjgy): a worktree-absolute (or worktree-relative) path only — a main-checkout-absolute path silently escapes the worktree and writes main. In a backgrounded `--auto` run the bg-isolation guard forces the heredoc/Bash string-replace fallback for these edits, same as any other inline write in that mode.

5. **Critical gate.** After the auto-fix pass, any unresolved Critical finding aborts the stage with status=failed. Surface the finding so the user can decide between rerunning implement vs overriding — unchanged from before.

6. **Record no-ops** — Major/Minor findings left as deliberate non-fixes, each with a verbatim citation of the `plan.out` line that justifies it.

7. **Record ask-user items** — Major/Minor findings that are the human's call. Never fire an `AskUserQuestion` for these, even in an attended run; they ride to the PR as flagged decisions, not a mid-run blocker.

8. **Write `code_review.out`** (see Outputs), keep reporting findings inline as today, then `status=completed` when no unresolved Critical remains.

## Outputs

- `$TICKET_DIR/stages/code_review.out` — the classified findings, one section per decision owner. Written via the same quoted-heredoc pattern as `pr_body.md` (sentinel `FLOW_OUT_SENTINEL_9f3a`, see `references/verb-do.md`), then `--output-path "$TICKET_DIR/stages/code_review.out"` is passed on `advance`. First line is the marker `<!-- flow:code_review-taxonomy v1 -->` (flow's `<!-- SYNC: ... -->` HTML-comment idiom) — the signal `create_pr` uses to distinguish this taxonomy from a `skill:<name>` handler's free-form `.out`.

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
