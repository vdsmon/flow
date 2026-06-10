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

A *bare* foreground `sleep` is blocked (`sleep` inside a single bounded Bash call is fine — that is the fallback's mechanism, below). Primary recipe: launch a **Monitor** that polls the one-shot rollup and emits only on state change (every emitted line is a notification; CI phases span minutes). It reads the `forge_cli ci-rollup` PROCESS EXIT CODE (not a piped status string), so an intermittently-erroring `gh` trips a consecutive-error budget and breaks instead of spinning; it short-circuits when the PR leaves the OPEN state (`detect-pr` returns `null`); and the iteration cap is the guaranteed terminal exit. The probe needs `$BRANCH` for the `detect-pr` short-circuit — bind it from HEAD (a flow worktree is always checked out on the run's feature branch):

```bash
BRANCH=$(git rev-parse --abbrev-ref HEAD)   # the worktree is on the run's feature branch
```

```
Monitor(
  description="CI for PR #$PR_ID",
  command='budget=3; errs=0; n=0; cap=25; prev=""; while :; do
      n=$((n+1)); [ "$n" -gt "$cap" ] && { echo "[$(date +%T)] cap $cap hit — leave for next pass"; break; }
      pr=$(python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . detect-pr --branch "$BRANCH"); drc=$?
      if [ "$drc" -eq 0 ] && [ "$(printf %s "$pr" | tr -d " \t\n")" = "null" ]; then echo "[$(date +%T)] PR #$PR_ID no longer open (merged/closed)"; break; fi
      out=$(python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . ci-rollup --pr "$PR_ID"); crc=$?
      s=""; [ "$crc" -eq 0 ] && s=$(printf %s "$out" | python3 -c "import sys,json;print(json.load(sys.stdin).get(\"status\",\"\"))" 2>/dev/null)
      if [ "$crc" -ne 0 ] || [ -z "$s" ]; then errs=$((errs+1)); echo "[$(date +%T)] ci-rollup probe error ($errs/$budget)"; [ "$errs" -ge "$budget" ] && { echo "error budget exhausted — leave for next pass"; break; }; sleep 60; continue; fi
      errs=0; [ "$s" != "$prev" ] && { echo "[$(date +%T)] CI: $s"; prev=$s; }
      case "$s" in green|failed) break;; esac
      sleep 60
    done',
  timeout_ms=1620000,
  persistent=false
)
```

Run exactly ONE CI Monitor at a time (stop the prior one before re-arming after a fix). Break on `green` or `failed` — the terminal `CI_STATUS` enum is `green` / `failed` (NOT `success` / `failure`, NOT `red`); `ci_rollup` folds the superseded `CANCELLED`/`STALE`/`NEUTRAL`/`SKIPPED` entries into `pending`, so those re-poll rather than trip a false `failed`. **Anti-pattern:** never `ci-rollup ... 2>/dev/null | python -c '...get("status","pending")'` — piping past the exit code makes an errored `gh` read as `pending` forever (a silent infinite spin); the probe reads `$?` first, which is the fix.

**Headless fallback — bounded foreground poll.** In a headless/turn-bounded session (a detached `--auto` run relaunched per turn, or a run interrupted at a turn boundary, e.g. by a rate limit) a Monitor or background task dies at turn end and its completion notification never arrives (observed in the flow-aod run: the bounded poll reached CI green in ~30s after the Monitor path silently died). There, poll in ONE Bash call with an explicit iteration cap and `timeout: 600000` (the Bash max):

```bash
i=0; while [ $i -lt 8 ]; do
  s=$(python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . ci-rollup --pr "$PR_ID" 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status","pending"))')
  echo "[$(date +%T)] CI: $s"
  if [ "$s" = "green" ] || [ "$s" = "failed" ]; then break; fi
  sleep 60; i=$((i+1))
done
```

8 × 60s = 480s keeps one call comfortably under the 600s Bash ceiling even with slow rollup calls. If still `pending` at the cap, re-issue the same call — each call is one turn-safe unit. Break on `green`/`failed` exactly like the Monitor; the §2 fix-cycle cap is unchanged. This is a fallback, not a coequal default — attached/long-lived sessions keep using the Monitor.

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
