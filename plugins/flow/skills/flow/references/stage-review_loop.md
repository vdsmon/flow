# Stage: review_loop

## Purpose

Wait for the existing pull request's CI result, address actionable review findings,
and stop. This is one bounded tail, not an autonomous repair loop. Across CI and
review feedback combined, the stage permits at most one fix pass.

The forge adapter is the only host-specific seam. GitHub and Bitbucket use the same
`forge` facade commands.

## Resolve the pull request

For a normal run, read the URL from `create_pr.out`:

```bash
PR_URL=$(grep -oE '^PR_URL=.*' "$TICKET_DIR/stages/create_pr.out" | head -1 | cut -d= -f2-)
PR_ID=$(printf '%s' "$PR_URL" | grep -oE '[0-9]+$')
```

For a revision run, resolve the already-open PR from the branch:

```bash
out=$(FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . \
  detect-pr --branch "$(git rev-parse --abbrev-ref HEAD)"); rc=$?
[ "$rc" -ne 0 ] && echo "detect-pr failed: rc=$rc" >&2 && exit "$rc"
PR_ID=$(printf '%s' "$out" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("id","") if d else "")')
```

An empty PR id is a failed stage.

## 1. Wait for CI

Poll `ci-rollup` in bounded foreground calls. Read the command exit code before
parsing JSON; a probe error is not `pending`. Stop each call after eight probes and
return control to the driver before another call.

```bash
i=0; errors=0; while [ $i -lt 8 ]; do
  out=$(FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . ci-rollup --pr "$PR_ID"); rc=$?
  if [ "$rc" -ne 0 ]; then
    errors=$((errors+1)); [ "$errors" -ge 3 ] && break
  else
    ci_status=$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status", ""))')
    if [ "$ci_status" = green ] || [ "$ci_status" = failed ]; then break; fi
    errors=0
  fi
  i=$((i+1)); sleep 60
done
```

Three consecutive probe errors fail visibly. A still-pending result is not failure;
report it and let the driver resume the same stage later.

## 2. Inspect review feedback

After CI is green, fetch normalized threads:

```bash
FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . review-status --pr "$PR_ID"
FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . review-threads --pr "$PR_ID"
```

If review status is unsupported, say so and use the available thread list.

A review bot is judged by ACTIVITY on this pull request, never by posted output alone and
never by a workspace declaration (no config key declares one, and a repository can carry an
active bot no config mentions). Posted comments cannot distinguish a bot mid-review from a
bot that is deactivated or unconfigured, which is exactly the state a run got stuck against
(CO-222, 2026-07-31). So after CI is green, probe for a sign of life with bounded re-runs of
the two probes above for up to five minutes, never a background monitor: an in-progress or
placeholder marker, a bot-authored check, or its first thread on THIS pull request. Other
pull requests are not evidence.

No sign of life inside the window means the bot is deactivated or not configured for this
repository: record `no review-bot activity within 5m; treated as absent` and an empty thread
list on green CI is then review-clean carrying that one note, not an
`automated review incomplete` caveat.

A sign of life flips the obligation: the review is coming, so keep waiting in the same
bounded polls until the bot FINISHES, meaning its completion marker appears or its
in-progress marker clears. Only when a completion signal never arrives does the stage
continue with the explicit `automated review incomplete` caveat, and that state is never
called review-clean.

Only unresolved Critical or Major threads are actionable. Minor and nit findings stay
open and are listed in the report.

For a revision run with no `dispositions.json` (the interactive board never opened —
`references/revision-triage-board.md`), apply the configured plain-comment severity
floor before selecting actionable threads. Capture-then-check: piping
`review-threads` straight into the floor would swallow a probe error and read a
forge flake as zero human review threads.

```bash
RAW=$(FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . review-threads --pr "$PR_ID"); rc=$?
[ "$rc" -eq 0 ] && THREADS=$(printf '%s' "$RAW" | \
  FLOW_HARNESS="<harness>" "<facade>" revise-config apply-floor --workspace-root .)
```

On `rc != 0` retry within the bounded budget, then fail visibly — never proceed as
review-clean. `apply-floor` returns the array with every unresolved `minor` (a plain
human comment) bumped to `[revise] plain_comment_severity`; the default `minor`
leaves the set unchanged. Use `$THREADS` for the actionable selection. When
`dispositions.json` exists, the human's explicit dispositions supersede the floor
and `apply-floor` is not consulted.

## 3. Optional single fix pass

If CI failed or actionable threads exist, and no fix pass has run yet, launch one
fresh native fixer. Its hint role is `fixer`:

```bash
FLOW_HARNESS="<harness>" "<facade>" model --workspace-root . --stage review_loop --role fixer
```

An empty result means inherit the session model. Effort is not resolved here: this fixer is
always host-native, and a native launch has no effort lever, so a resolved value would be
dropped. Give it the failing logs and all accepted findings together. It
edits directly in the authoritative ticket worktree, runs only the checks affected by
its changes, creates one conventional follow-up commit, and pushes it. It must not
create a clone, export/import a patch, or retry under another model.

Re-run the bounded CI wait once and re-read threads once. There is no second fixer.
If CI is still red or a Critical/Major thread is unaddressed (section 4), fail the
stage and return the evidence to the human.

For each thread you fixed, reply and resolve only after the fix commit is pushed:

```bash
FIX_SHA=$(git rev-parse --short HEAD)
FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . post-reply \
  --pr "$PR_ID" --thread "<CID>" --text "Fixed in $FIX_SHA. <what changed and why>."
FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . resolve-thread \
  --pr "$PR_ID" --thread "<CID>"
```

A disagreed finding gets a reasoned reply and stays open. Do not claim it resolved.
That reply is what addresses the finding (section 4). A reply that only asserts
disagreement addresses nothing.

## 4. Complete

Addressed is not the same as resolved in the forge. A Critical or Major thread is
addressed when this stage did one of these:

- fixed it: the fix commit is pushed, the reply names it, and the thread is resolved
  (section 3);
- disagreed with it: a posted reply names what was checked and what that showed, and
  the thread stays open;
- deferred it: a posted reply carries the reason and the filed ticket key, and the
  thread stays open;
- carried a human's own `defer` or `dismiss` disposition from a revision sub-run: the
  recorded reason is posted as the reply and stands on its own, no ticket key needed,
  and the thread stays open (`references/revision-triage-board.md`).

Anything else is unaddressed. You have no dismissal of your own: deciding that a
correct finding should never happen is a judgment about project priorities rather than
about the code, and it belongs to the human on the revision board.

A thread can carry more than one ask. Fixing part of it does not settle the rest: if
anything on the thread is disagreed with or deferred, the thread stays open and is
reported that way, whatever else was fixed on it.

Disagreement is a judgment about the finding, never about the budget: a thread you did
not fix only because the one fix pass is already spent is unaddressed, and the stage
fails on it as before. Deferring that same thread is still available to you and is a
different act, because it files the work and posts the key on the thread. Relabelling
it a disagreement is the move this forbids.

The forge cannot tell you which is which. `review-threads` returns every thread the
forge has not marked resolved, and neither adapter surfaces your reply: the GitHub one
reads only each thread's first comment, and the Bitbucket one keeps only the reviewer's
own comments. Your reply is invisible in the re-fetch either way, so this stage's own
record of what it replied to is the authority.

Completing is not a review-clean verdict. A disagreed or deferred thread stays open
on the pull request, so it is still standing in front of whoever decides the merge.
Leaving it open is the point: resolving a finding you do not agree with hides it, and
a finding you have measured to be wrong is never applied just to clear this gate.

Complete when CI is green and every Critical or Major thread is addressed. Write
`$TICKET_DIR/stages/review_loop.out` with:

- final CI state;
- whether the one fix pass ran and its commit;
- threads fixed; threads disagreed with, deferred, or carrying the human's own
  disposition, each with the reply's evidence, reason, or filed ticket key; and
  threads left open;
- whether automated review completed or remained unavailable/incomplete.

Stop on probe exhaustion, failed CI after the fix pass, or an unaddressed
Critical/Major finding. Do not exceed one fix pass.
