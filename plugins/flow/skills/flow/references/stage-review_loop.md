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

On a host with a native watch capability that notifies the same driver session (Claude
Code's Monitor tool), prefer one bounded watch on the `ci-rollup` command over blocking
sleeps: the driver stays free between events instead of pinning its one shell lane inside a
sleep loop, and the 2026-08-03 window showed exactly that split, one driver absorbing review
events while planning continued and another spending its wall clock inside `sleep 70`
iterations. The wait must still live and die with the driver session; a detached process
that outlives it is the topology this stage forbids.

Where no such capability exists, poll `ci-rollup` in bounded foreground calls. Read the
command exit code before parsing JSON; a probe error is not `pending`. Stop each call after
eight probes and return control to the driver before another call.

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
(CO-222, 2026-07-31). So after CI is green, probe for a sign of life for up to five minutes,
with bounded re-runs of the two probes above or a session-owned watch on them (section 1's
host-capability rule applies here unchanged): an in-progress or
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

Only unresolved Critical or Major threads are actionable: they are the only ones that can send work to the fixer (section 3), and the only ones whose unaddressed state fails this stage. Minor and nit findings do neither, and are listed in the report.

Actionability is not the thread lifecycle, and this is the sentence that gets misread as if it were (witnessed 2026-08-19, brinta PR 3221: a driver read "not actionable" as "leave the thread standing", left two Minor bot threads open, and answered them in a top-level pull request comment). Every thread this stage reads gets its reply IN the thread and reaches the end state section 4 sets for its author, whatever its severity: a Minor bot thread is still a bot thread, so its end state is resolved. Not actionable means no code change and no stage failure, never leave it open and move on.

For a revision run with no `dispositions.json` (triage never produced one;
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

Every thread gets its reply inside that thread, through `post-reply`. A top-level pull request comment is not a reply to a thread: it does not appear on the thread, and a review bot that reads only its own threads never sees it, so the finding stays unanswered however well the comment argued it.

For a thread you fixed, reply and resolve only after the fix commit is pushed:

```bash
FIX_SHA=$(git rev-parse --short HEAD)
FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . post-reply \
  --pr "$PR_ID" --thread "<CID>" --text "Fixed in $FIX_SHA. <what changed and why>."
FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . resolve-thread \
  --pr "$PR_ID" --thread "<CID>"
```

For a thread you did not fix (a disagreement, a deferral, or a finding the code already covers), the same `post-reply` call carries the reasoned reply, with no `FIX_SHA`. A run that changed no code still owes every thread its reply.

`resolve-thread` answering HTTP 409 on a bot thread usually means the bot resolved it first, seconds after reading the reply, which is the concession the disagreement path waits for. Confirm on the individual comment (`.../comments/<id>`), never on the comments collection, whose `resolution` is empty for every comment (`references/troubleshooting.md`). A populated `resolution.user` naming the bot is the concession, so treat that 409 as already settled rather than as a failure.

A disagreed finding gets a reasoned reply that names what was checked and what that showed; a reply that only asserts disagreement addresses nothing. That reply is what addresses the finding, and what happens to the thread afterwards depends on who authored it (section 4): a bot-authored thread is resolved only after the bot concedes, and a human reviewer's thread stays open for its author.

## 4. Complete

Addressed is not the same as resolved in the forge, and the resolution rule depends on the thread's author. A bot-authored thread is one authored by the review bot that section 2 identified by activity on this pull request; every other thread belongs to a human reviewer. The end state for bot-authored threads is zero open threads (human ruling, brinta PR 3119, 2026-07-31): a bot thread left open is a question still standing, so it is resolved the moment its ask is settled, and the only settled state that stays open is a disagreement the bot has not conceded. That end state is severity-independent: a Minor or nit bot thread reaches it too, because severity decides only what section 2 says it decides, whether a finding can reach the fixer and fail this stage. A Critical or Major thread is addressed when this stage did one of these:

- fixed it: the fix commit is pushed, the reply names it, and the thread is resolved (section 3);
- disagreed with it: a posted reply names what was checked and what that showed; on a bot-authored thread, keep polling for the bot's response in the same bounded five-minute shape as section 2's sign-of-life probe, resolve on a concession, and leave the thread open and reported on a push-back or on silence; a human reviewer's thread always stays open, because its author owns the concession;
- deferred it: a posted reply carries the reason and the filed ticket key; a bot-authored thread is then resolved, because the ticket is the durable receipt, and a human reviewer's thread stays open;
- carried a human's own `defer` or `dismiss` disposition from a revision sub-run: the recorded reason is posted as the reply and stands on its own, no ticket key needed; a bot-authored thread is then resolved and a human reviewer's thread stays open (`references/revision-triage-board.md`).

Anything else is unaddressed. You have no dismissal of your own: deciding that a
correct finding should never happen is a judgment about project priorities rather than
about the code, and it belongs to the human on the revision board.

A thread can carry more than one ask, and fixing part of it does not settle the rest. A bot-authored thread resolves only when every ask on it ended in a fix, a concession, or a filed ticket; a human reviewer's thread stays open when anything on it was disagreed with or deferred. A partially settled thread is reported with what remains, whatever else was fixed on it.

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

Completing is not a review-clean verdict. A human reviewer's disagreed or deferred thread stays open on the pull request, so it is still standing in front of whoever decides the merge; leaving that one open is the point, because resolving a human's finding you do not agree with hides it from the merge decision. A bot-authored thread has no such audience once its ask is settled, which is why it resolves on a fix, a concession, or a filed ticket. The one bot thread that stays open is a disagreement without a concession, and a finding you have measured to be wrong is never applied just to clear this gate.

Complete when CI is green and every Critical or Major thread is addressed. Write
`$TICKET_DIR/stages/review_loop.out` with:

- final CI state;
- whether the one fix pass ran and its commit;
- threads fixed; threads disagreed with, deferred, or carrying the human's own disposition, each with the reply's evidence, reason, or filed ticket key, and whether it ended resolved (fix, bot concession, filed ticket, carried disposition) or left open;
- whether automated review completed or remained unavailable/incomplete.

Stop on probe exhaustion, failed CI after the fix pass, or an unaddressed
Critical/Major finding. Do not exceed one fix pass.

## Hotfix merge

A hotfix-lane run (run frontmatter `hotfix = true`) does not park its green PR for a later merge decision: the human approved the expedited delivery at the plan gate, so reaching the completion bar above IS the merge moment. Two conditions still hold, in order. First, when the diff touches a guard file (a HOT change), run the guard-property review of `references/scrutinize.md` §Merging before merging and stop on any property regression; a hotfix never drops a safety property on green alone. Second, an open human thread carrying a disagreement blocks the merge exactly as it blocks a seat merge; incident pressure does not resolve it. When both hold, merge through the forge seam and record the merged state and PR id in `review_loop.out`:

```bash
FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . merge --pr "$PR_ID"
```

After the run completes, `FLOW ticket finalize <KEY>` closes out the merged delivery as usual. A non-hotfix run never merges here; its green PR parks for the human or the scrutinize seat.
