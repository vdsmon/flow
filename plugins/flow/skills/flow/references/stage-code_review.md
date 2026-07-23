# Stage: code_review

## Purpose

Have one fresh native reviewer challenge the implementation before commit. The
reviewer is logically independent from the driver and implementer, but it reads the
same authoritative ticket worktree. Flow does not require a particular provider,
model, effort level, clone, or execution receipt.

## Inputs

- `<ticket-dir>/state.json` for `stages.implement.started_at_sha`.
- `<ticket-dir>/stages/plan.out` when a plan exists.
- The ticket context and current uncommitted working-tree change.
- The implementation report and test evidence.

## Steps

1. Resolve the implementation baseline and capture the real working-tree diff:

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" diff since-stage \
     --stage implement --ticket <KEY> --ticket-dir <ticket-dir> --cwd .
   git diff <started_at_sha>
   ```

   `since-stage` can report an empty committed range because implementation is still
   uncommitted. `git diff <started_at_sha>` is the review payload in that case.

2. Launch exactly one fresh host-native reviewer. Give it the ticket, approved plan,
   implementation report, diff, repository root, and this document. It may inspect
   surrounding code and run focused read-only checks. It must not edit files, stage
   changes, commit, or advance Flow state.

   Ask it to look for correctness defects, missing behavior, regressions, unsafe
   boundaries, tests that do not prove their claims, needless complexity, and code
   that conflicts with established repository conventions. Require each finding to
   cite a path and location and classify it as:

   Treat common code smells as heuristics, not violations: for example, flag
   possible Feature Envy only when it creates a concrete maintenance or correctness
   cost. A documented repo standard always wins over a generic style preference.

   - `Critical`: unsafe or incorrect to ship;
   - `Major`: materially worth fixing;
   - `Minor`: optional improvement.

   A missing or failed reviewer is a visible stage failure. Do not replace it with
   same-context self-review.

3. Triage the returned findings. Dismiss only demonstrably incorrect or duplicate
   observations and record why. Findings whose fix would leave `planned_files` are
   not silently expanded here.

4. Perform at most one fix pass. If there are confident, in-scope Critical or Major
   fixes, launch one fresh native fixer with only the accepted findings and the
   ownership boundary. The fixer edits directly in the authoritative ticket
   worktree, runs the checks affected by its edits once, and returns. Do not create a
   private clone, import a patch, retry with another model, or start a second fix
   pass. Minor findings remain for the human unless they are inseparable from an
   accepted fix.

5. Re-read the resulting diff once and update the disposition report. Any unresolved
   Critical finding fails the stage.

6. Resolve every `ask-user` finding with the human before completing. These findings
   surface only now because the reviewer reads the implemented diff; the plan gate
   could not have seen them. They are the ticket owner's decisions, not the PR
   reviewer's, so they never ride into the PR:
   - Attended run: pose each finding in the conversation and wait for the decision.
     A decision that requires edits directs one fresh fixer pass carrying the
     human-accepted findings (this human-directed pass is separate from step 4's
     autonomous pass). A decision to accept as-is moves the finding to `no-op` with
     the human's rationale.
   - Unattended run (nobody to answer): fail the stage visibly and return the
     findings to the user, exactly like an unresolved Critical. Never complete the
     stage with an open decision.

   Undecided Minor nits that need no decision stay recorded in `no-op` with why; they
   do not create another loop.

7. Write `<ticket-dir>/stages/code_review.out` and complete the stage.

## Output

The first line is the stable format marker:

```text
<!-- flow:code_review-taxonomy v1 -->
# code_review findings — <KEY>

## ask-user
- [Major] <finding and decision needed> (<file>:<line>)

## no-op
- [Minor] <finding> — dismissed because <evidence> (<file>:<line>)

## auto-fixed
- [Major] <finding> — fixed in <file>:<line>; check: <command/result>
```

Omit empty sections. `## ask-user` holds decision-needed findings only while the
stage runs; step 6 resolves them all, so a completed stage's report never carries the
section. The report must name the reviewer's overall verdict, each fix pass that ran,
and any residual risk.

## Errors

- Missing implementation baseline or unreadable diff: run `FLOW workspace repair
  <KEY>`, then `retry --stage implement`.
- Reviewer failure: fail visibly; do not silently self-review.
- Unresolved Critical finding: fail and return the finding to the user.
- An `ask-user` finding with no human to answer (unattended run): fail and return the
  findings to the user.
- A requested fix needs files outside `planned_files`: leave it unresolved and report
  the required scope decision.

## Skip conditions

The dispatcher skips this document only when the handler is `none`, or replaces it
when the workspace configures a review skill handler.
