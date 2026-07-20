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
   Critical finding fails the stage. Unresolved Major or Minor findings are recorded
   for the PR reviewer and do not create another loop.

6. Write `<ticket-dir>/stages/code_review.out` and complete the stage.

## Output

The first line is the stable marker used by PR-body rendering:

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

Omit empty sections. The report must name the reviewer's overall verdict, the single
fix pass if one ran, and any residual risk.

## Errors

- Missing implementation baseline or unreadable diff: run `FLOW workspace repair
  <KEY>`, then `retry --stage implement`.
- Reviewer failure: fail visibly; do not silently self-review.
- Unresolved Critical finding: fail and return the finding to the user.
- A requested fix needs files outside `planned_files`: leave it unresolved and report
  the required scope decision.

## Skip conditions

The dispatcher skips this document only when the handler is `none`, or replaces it
when the workspace configures a review skill handler.
