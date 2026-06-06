# review_loop stage (inline, forge-driven)

The post-PR wait loop: after `create_pr` opens the PR, this stage waits on CI and drives any fixes until green, then resolves the review bot's actionable threads. It is **integral**, not a nicety — flow's pipeline is not done at "draft PR opened", it is done at "PR is green and review-clean". The host calls go through the **forge seam** (`forge_cli.py`), so the same protocol serves GitHub (`gh`) and Bitbucket (`bkt`).

The bare plugin default handler is `none` (a no-op skip) so a workspace with no `[forge]` / no CI degrades cleanly; flow's dogfood wires `review_loop = "inline"`. The predecessor is `create_pr`. This stage reaches `completed` when **CI is green AND there are no unresolved Major+ review threads**.

## Inputs

Read the PR from the predecessor's captured output:

```bash
PR_URL=$(grep -oE '^PR_URL=.*' "$TICKET_DIR/stages/create_pr.out" | head -1 | cut -d= -f2-)
PR_ID=$(printf '%s' "$PR_URL" | grep -oE '[0-9]+$')   # trailing number: gh /pull/N, bkt /pull-requests/N
```

`PR_ID` is the host handle both adapters accept (`forge_cli --pr "$PR_ID"`).

## 1. Wait for CI (Monitor, not a foreground sleep)

Foreground `sleep` is blocked, so launch a **Monitor** that polls the one-shot rollup and emits only on state change (every emitted line is a notification; CI phases span minutes):

```
Monitor(
  description="CI for PR #<PR_ID>",
  command='prev=""; while true; do s=$(python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . ci-rollup --pr "<PR_ID>" 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin).get(\"status\",\"pending\"))"); if [ "$s" != "$prev" ]; then echo "[$(date +%T)] CI: $s"; prev=$s; fi; if [ "$s" = "green" ] || [ "$s" = "failed" ]; then break; fi; sleep 60; done',
  timeout_ms=1500000, persistent=false
)
```

Run exactly ONE CI Monitor at a time (stop the prior one before re-arming after a fix). Break on `green` or `failed`.

## 2. On CI failed — drive fixes (delegated, bounded)

Do NOT invent inline edit logic. Delegate the fix to a subagent (the same way the `implement` stage uses `subagent:general-purpose`): give it the failing-check logs, have it apply the fix, commit with the existing commit machinery, and `git push`. Then re-arm the CI Monitor (step 1).

**Hard cap: 3 fix cycles total** across CI + review combined. If CI is still red after 3, set `STATUS=failed` and surface the last failing logs — do not loop forever.

## 3. Poll review threads

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . review-threads --pr "$PR_ID"
```

- Output `{"supported": false}` (e.g. GitHub today — no review-bot wired): **skip thread handling**, report "review threads not wired for this host", and proceed to the terminal check on CI-green alone.
- Otherwise: a JSON array of normalized threads, each with `severity` (`critical`/`major`/`minor`/`nit`/`unknown`), `file`, `line`, `title`, `id`.

## 4. Address + resolve (Major+ only)

Only an **unresolved Major+ finding (or CI red)** justifies another fix cycle; Minor/nit never trigger their own cycle. **Each cycle must strictly reduce the unresolved Major+ count** — if a cycle does not, stop and escalate to the user with both sides (the finding and why it resists), rather than burning the cap.

For each Major+ thread you addressed in a pushed commit:

```bash
FIX_SHA=$(git rev-parse --short HEAD)
python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . post-reply \
  --pr "$PR_ID" --thread "<CID>" --text "Fixed in $FIX_SHA. <one line: what changed and why>."
python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . resolve-thread \
  --pr "$PR_ID" --thread "<CID>"
```

`resolve-thread` returns `{"resolved": true}` only when the host VERIFIED the thread resolved (the bkt adapter re-reads the comment and checks `.resolution != null`). For a **reasoned-skip** thread (you disagree with the finding): `post-reply` with the reasoning, leave it open, and document it in the stage report — do not resolve it.

## 5. Terminal

`STATUS=completed` when **CI is green AND zero unresolved Major+ threads remain**. Remaining Minor/nit threads are reported open with one-line reasons, not chased. Respect the 3-cycle cap. **Stop every Monitor on exit** (a leaked Monitor keeps the shell alive). On `completed`, the PR-ready notification fires with the PR URL (see `references/verb-do.md`); only when the handler is `none` does that notification fall back to firing at `create_pr` instead.

This stage MAY write a short report (cycles run, threads resolved/skipped, final CI state) to `$TICKET_DIR/stages/review_loop.out`; pass `--output-path` on `advance` if it does.
