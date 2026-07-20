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
PR_ID=$(FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . \
  detect-pr --branch "$(git rev-parse --abbrev-ref HEAD)" | \
  python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("id","") if d else "")')
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
    status=$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status", ""))')
    [ "$status" = green ] || [ "$status" = failed ] && break
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

If review status is unsupported, say so and use the available thread list. If a known
review bot has not finished, wait once for a short bounded interval and retry the two
probes. Do not start a background monitor or an unbounded wait. If it still has not
finished, continue with an explicit `automated review incomplete` caveat; never call
that state review-clean.

Only unresolved Critical or Major threads are actionable. Minor and nit findings stay
open and are listed in the report.

## 3. Optional single fix pass

If CI failed or actionable threads exist, and no fix pass has run yet, launch one
fresh native fixer. Give it the failing logs and all accepted findings together. It
edits directly in the authoritative ticket worktree, runs only the checks affected by
its changes, creates one conventional follow-up commit, and pushes it. It must not
create a clone, export/import a patch, or retry under another model.

Re-run the bounded CI wait once and re-read threads once. There is no second fixer.
If CI is still red or a Critical/Major thread remains, fail the stage and return the
evidence to the user.

For each addressed thread, reply and resolve only after the fix commit is pushed:

```bash
FIX_SHA=$(git rev-parse --short HEAD)
FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . post-reply \
  --pr "$PR_ID" --thread "<CID>" --text "Fixed in $FIX_SHA. <what changed and why>."
FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . resolve-thread \
  --pr "$PR_ID" --thread "<CID>"
```

A disagreed finding gets a reasoned reply and stays open. Do not claim it resolved.

## 4. Complete

Complete when CI is green and no unresolved Critical or Major thread remains. Write
`$TICKET_DIR/stages/review_loop.out` with:

- final CI state;
- whether the one fix pass ran and its commit;
- threads fixed, disagreed with, or left open;
- whether automated review completed or remained unavailable/incomplete.

Stop on probe exhaustion, failed CI after the fix pass, or remaining Critical/Major
feedback. Do not exceed one fix pass.
