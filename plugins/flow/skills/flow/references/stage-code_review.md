# Stage: code_review

## Purpose

Have one fresh reviewer challenge the implementation before commit. The
reviewer is logically independent from the driver and implementer, but it reads the
same authoritative ticket worktree. Flow does not require a particular provider,
model, effort level, clone, or execution receipt.

Code review answers: is it built right. The plan assessment already answered whether
it was the right thing to build, and it cannot reach what only running code shows.

Four categories carry nearly every finding worth having. They were derived from what
review actually caught across five delivered tickets, not from taste,
and every high-value finding in that set fell into one of them:

1. **Tests that pass for the wrong reason.** A test asserting an outcome two code
   paths can produce; a test named for a scope its assertion does not have; a
   disjunct that is always true.
2. **Documented properties with no witness.** Any behavior the prose or a docstring
   asserts whose loss no test would catch.
3. **Fail-opens.** Anything reporting success while doing nothing, or turning a
   documented refusal into silence. Ask directly: what input makes this pass while
   accomplishing nothing?
4. **Prose claims the code does not make good on.** Commands emitted from string
   literals that do not exist or return nothing; a recipe naming a payload its own
   command cannot produce.

Prove a finding rather than argue it: reproduce it in a scratch copy, and fire a
positive control before trusting any green gate, because a gate that skipped its
input reports the same zero as a gate that passed.

## Inputs

- `<ticket-dir>/baseline.json` for `origin_sha`, `head_sha`, and `planned_files`,
  written by the implement stage's `records_diff_baseline` pre-hook.
- `<ticket-dir>/review.diff`, the payload step 1 builds from that baseline.
- `<ticket-dir>/stages/plan.out` when a plan exists.
- The ticket context.
- The implementation report and test evidence.

## Steps

1. Build the review payload as one pre-built diff:

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" diff capture-review-diff \
     --ticket <KEY> --ticket-dir <ticket-dir> --cwd .
   ```

   This writes `<ticket-dir>/review.diff`: a single text-only unified diff of the owned
   change set against the stable `origin_sha` captured when the baseline was first
   recorded. Every planned change is present, whether it was committed, staged, left
   unstaged, added, modified, or deleted. This remains true after a baseline re-record
   moves `head_sha`. A baseline
   written before `origin_sha` existed falls back to `head_sha`; a present but invalid
   `origin_sha` is refused. The capture stages intent-to-add for untracked planned files
   first. Binary content is elided to a `Binary files ... differ` line, which names the
   path without spending the payload on bytes no reviewer can read.

   Do NOT hand the reviewer a path list, and do not tell it to find the change itself.
   `git diff <started_at_sha>` reports tracked changes only, so on a landing-shaped run
   it cannot contain the new files the run is about, and a reviewer sent to discover
   them opens each one in a separate round trip. On flow-pcj6 that shape hit a 600s
   ceiling and returned no report; the same change at the same model and effort reviewed
   in 2m32s once the payload was one pre-built diff. The lever is payload shape, not
   model capability.

   Exit 1 has three causes, distinguished by stderr, and their remedies differ. A
   missing or malformed baseline is a repair (see Errors). `planned file(s) gitignored,
   cannot be committed` means a planned file is ignored: do NOT retry implement, which
   re-records the same baseline and fails identically. Fix the cause as `stage-commit.md`
   directs, then rerun this step. `baseline.json planned_files is empty` means the run
   owns no files, so the capture refuses rather than hand a reviewer a repo-wide diff.
   Retrying implement loops here too, for the same reason: the re-record writes the same
   empty planned set. Put the run's files into the ticket frontmatter `planned_files` (or
   pass them to `record-baseline --files ...`), then rerun this step.

   **An incomplete or empty `review.diff` is a stop, not a clean review.** The former
   `head_sha` anchor moved to live HEAD when the post-implementation ownership reconcile
   re-recorded the baseline. A fully committed implementation then produced a zero-byte
   payload. A partial commit was harder to detect: the remaining dirty edit kept the
   payload non-empty while every committed planned change was omitted. The stable
   `origin_sha` anchor prevents both forms, while `head_sha` remains the moving anchor for
   the commit payload. If capture still produces no payload for an implement stage that
   reported changes, fail the stage and surface the ticket. A reviewer handed nothing
   returns no findings, which is indistinguishable from a clean review.

2. Exactly one fresh reviewer challenges the implementation. Which reviewer depends on
   the configured handler, and nothing else about this step changes:

   - `inline`: launch one fresh host-native reviewer through the independent-agent
     contract in `references/delivery-loop.md`.
   - `subagent:flow:codex-reviewer`: the bundled agent owns this stage and its reviewer
     is the Codex CLI, run as one bounded foreground call under a read-only sandbox.
     The agent still performs the triage and fix passes below natively.

   The reviewer's hint role is `reviewer` (`model --stage code_review --role reviewer`).
   Give the reviewer the ticket, approved plan, implementation report, the
   `<ticket-dir>/review.diff` built in step 1, repository root, and this document. Hand
   over that file, not a list of paths to open. It may inspect surrounding code and run
   focused read-only checks; the diff is its starting evidence, not a sandbox. It must
   not edit files, stage changes, commit, or advance Flow state.

   Mutation is how tests that do not prove their claims are actually found, and it is
   not an exception to the line above: copy the engine to a scratch directory outside
   the worktree, break the property there, re-run the suite, and see what reds. The
   worktree stays untouched. Four times in flow's history an adversarial reader settled
   by mutation what reading alone could not, twice from this stage and twice from a
   plan assessor: three tests that could not fail, and one correct property with no
   test at all, where 15 tests stayed green while the behavior broke. Reading produces
   the candidate; mutation is what tells you whether it is real.

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
   same-context self-review. An external reviewer that exits non-zero, exceeds its
   timeout, or leaves no parseable report is a missing reviewer.

3. Triage the returned findings. Dismiss only demonstrably incorrect or duplicate
   observations and record why. Findings whose fix would leave `planned_files` are
   not silently expanded here.

4. Perform at most one fix pass. If there are confident, in-scope Critical or Major
   fixes, launch one fresh native fixer with only the accepted findings and the
   ownership boundary. The fixer's hint role is `fixer` (`model --stage code_review --role fixer`). The fixer edits directly in the authoritative ticket
   worktree, runs the checks affected by its edits once, and returns. Do not create a
   private clone, import a patch, retry with another model, or start a second fix
   pass. Minor findings remain for the human unless they are inseparable from an
   accepted fix.

5. Recapture, then re-read the resulting diff once and update the disposition report:

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" diff capture-review-diff \
     --ticket <KEY> --ticket-dir <ticket-dir> --cwd .
   ```

   The recapture is required, not tidiness. Step 1 writes a file once, so a fix pass in
   step 4 does not change it, and re-reading the stale copy would judge fixes against
   pre-fix bytes and let the unresolved-Critical check below run on evidence that
   predates the fix. Any unresolved Critical finding fails the stage.

   **Check this recapture's exit code before re-reading, for the same reason the recapture
   exists.** The artifact is written atomically, so a capture that fails writes nothing and
   leaves step 1's payload in place, and a driver that ignores the exit reads exactly the
   pre-fix bytes this step was added to avoid. Step 1's exit-1 causes and the empty-payload
   stop apply here unchanged, and a fix pass is one way to reach the gitignored case, by
   adding a file the repo ignores. Treat a failed recapture as a stage failure rather than
   re-reading the old payload.

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
     findings to the human, exactly like an unresolved Critical. Never complete the
     stage with an open decision.

   Undecided Minor nits that need no decision stay recorded in `no-op` with why; they
   do not create another loop.

   When an agent handler owns this stage, that agent has no human in its context, so it
   returns its report with `## ask-user` still populated and the driver resolves the
   section on the rules above before it advances. The stage is not finished while the
   section has entries.

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

- Missing implementation baseline or unreadable diff (exit 1, stderr names a missing or
  malformed `baseline.json`): run `FLOW workspace repair <KEY>`, then
  `retry --stage implement`.
- Gitignored planned file (exit 1, stderr `planned file(s) gitignored, cannot be
  committed: <files>`): the same exit code, the opposite remedy. Do NOT retry implement:
  it re-records the same baseline and the capture fails identically. Fix the cause as
  `stage-commit.md` directs, by adding the narrowest `.gitignore` negation for the named
  files (adding `.gitignore` to the plan via `record-baseline --files ...`), or by
  dropping them from `planned_files` and re-recording. Then rerun step 1.
- Empty planned set (exit 1, stderr `baseline.json planned_files is empty`): the run owns
  no files, so the capture refuses a repo-wide payload. Do NOT retry implement: the
  re-record writes the same empty planned set and the capture fails identically, which
  loops. Put the files the run edited into the ticket frontmatter `planned_files` (or pass
  them to `record-baseline --files ...`), then rerun step 1.
- Reviewer failure: fail visibly; do not silently self-review. An external reviewer
  reports through a file, so name the command and its stderr rather than the empty
  artifact.
- Unresolved Critical finding: fail and return the finding to the human.
- An `ask-user` finding with no human to answer (unattended run): fail and return the
  findings to the human.
- A requested fix needs files outside `planned_files`: leave it unresolved and report
  the required scope decision.

## Skip conditions

The dispatcher skips this document only when the handler is `none`, or replaces it
when the workspace configures a review skill handler.
