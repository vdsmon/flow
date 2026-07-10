# review_loop stage (inline, forge-driven)

The post-PR wait loop: after `create_pr` opens the PR, this stage waits on CI and drives any fixes until green, then resolves the review bot's actionable threads. It is **integral**, not a nicety — flow's pipeline is not done at "draft PR opened", it is done at "PR is green and review-clean". The host calls go through the **forge seam** (`forge_cli.py`), so the same protocol serves GitHub (`gh`) and Bitbucket (`bkt`).

The bare plugin default handler is `none` (a no-op skip) so a workspace with no `[forge]` / no CI degrades cleanly; flow's dogfood wires `review_loop = "inline"`. The predecessor is `create_pr`. This stage reaches `completed` when **CI is green AND there are no unresolved Major+ review threads**.

## Revision mode

When `<ticket-dir>` contains `/revisions/` this is a revision sub-run (see `references/verb-revise.md`): there is no `create_pr` predecessor — the SAME PR is updated in place, and the unresolved threads `review-threads` returns are the MAINTAINER's (the original run resolved the bot threads before delivery, so what remains unresolved is human).

**No `create_pr.out`** — skip the `## Inputs` read below; resolve the (OPEN, guaranteed by verb-revise's step-2 guard) PR from the branch instead, and use this `$PR_ID` for §1 / §3 / §4:
```bash
PR_ID=$(python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . detect-pr --branch "$(git rev-parse --abbrev-ref HEAD)" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("id","") if d else "")')
```

Deltas from the normal loop:

- **Explicit dispositions supersede the floor.** When `<ticket-dir>/dispositions.json` exists (an interactive `revise` opened the step-5a triage board — `references/review-packet.md`'s `## Revision triage board (/flow revise)` section carries the schema), the human's explicit dispositions SUPERSEDE inferred severity: the fix set is the **fix pile** (`threads[]` entries with `"disposition": "fix"`) regardless of severity, and `apply-floor` is NOT consulted. §5's terminal "zero unresolved Major+" check then evaluates over that fix pile, not the raw thread severities — a dismissed major must NOT deadlock terminal, and an explicit empty triage (file exists, fix pile empty: all defer/dismiss, or `"threads": []`) leaves the terminal check nothing to chase, no floor-bumped threads. While the board session is live, completion still waits on the user's end-session verdict (the board section's convergence rules) — a mid-session all-defer/dismiss batch does NOT complete the stage. Only when NO `dispositions.json` exists does the plain-comment floor below apply (the empty-vs-absent distinction).
- **Plain-comment floor.** Before the §4 address+resolve, fetch the threads capture-then-check (the §1 discipline: read `$?` first — piping `review-threads` straight into `apply-floor` swallows a non-zero exit, and `apply-floor` turns the empty stdin into `[]`, so a gh flake reads as ZERO maintainer threads, a false review-clean), then pipe the captured output through the floor so an unresolved `minor` (a plain human comment) is bumped to the configured severity:
  ```bash
  RAW=$(python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . review-threads --pr "$PR_ID"); rc=$?
  [ "$rc" -eq 0 ] && THREADS=$(printf '%s' "$RAW" | python3 ${CLAUDE_SKILL_DIR}/scripts/revise_config.py apply-floor --workspace-root .)
  ```
  On `rc != 0` that is a PROBE ERROR, not an empty thread list: retry on a bounded budget (§1's pattern), and if it persists set `STATUS=failed` surfacing the stderr — never proceed to §4/§5 as review-clean. A RAW of `{"supported": false}` (a host without thread support) is §3's degrade: skip the floor and thread handling. `apply-floor` reads the threads array on stdin and returns it with every unresolved `minor` bumped to `[revise] plain_comment_severity`. When that floor is `major`, an unresolved minor thread enters the Major+ fix set; the default `minor` leaves the set unchanged (today's behavior). The bump is loop-side only — the forge adapter stays pure of `[revise]` config. Use `$THREADS` (not the raw `review-threads` output) for the §4 Major+ selection.
- **Reply + resolve, or reply + leave open.** After a fix commit is pushed for a fixed thread, `post-reply` (with the rationale) then a host-verified `resolve-thread` exactly as §4 (the .1 capabilities; the bkt adapter re-reads `.resolution != null`). A deferred or dismissed thread — a `dispositions.json` defer/dismiss, or a reasoned-skip on the floor path — gets a `post-reply` carrying the human's reason and stays OPEN, documented. Reply-posting is independent of fix-pile emptiness: an all-defer/dismiss batch still posts every reason.

The 3-fix-cycle cap is PER-REVISION (the revision seeded its own `state.json`, fresh counter) — no change. An instruction-sourced revision (no threads) just re-greens CI.

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
i=0; errs=0; while [ $i -lt 8 ]; do
  out=$(python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . ci-rollup --pr "$PR_ID"); crc=$?
  s=""; [ "$crc" -eq 0 ] && s=$(printf %s "$out" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status",""))' 2>/dev/null)
  if [ "$crc" -ne 0 ] || [ -z "$s" ]; then
    errs=$((errs+1)); echo "[$(date +%T)] ci-rollup probe error ($errs/3)"
    [ "$errs" -ge 3 ] && { echo "error budget exhausted — leave for next pass"; break; }
    sleep 60; i=$((i+1)); continue
  fi
  errs=0; echo "[$(date +%T)] CI: $s"
  if [ "$s" = "green" ] || [ "$s" = "failed" ]; then break; fi
  sleep 60; i=$((i+1))
done
```

8 × 60s = 480s keeps one call comfortably under the 600s Bash ceiling even with slow rollup calls. The probe reads `$?` before parsing, same as the Monitor — an erroring `gh` trips the 3-error budget instead of reading as `pending` forever (the §1 anti-pattern). If still `pending` at the cap, re-issue the same call — each call is one turn-safe unit. Break on `green`/`failed` exactly like the Monitor; the §2 fix-cycle cap is unchanged. This is a fallback, not a coequal default — attached/long-lived sessions keep using the Monitor.

## 2. On CI failed — drive fixes (delegated, bounded)

Do NOT invent inline edit logic. Delegate the fix to a subagent (the same way the `implement` stage uses `subagent:general-purpose`): give it the failing-check logs, have it apply the fix, commit with the existing commit machinery, and `git push`. Then re-arm the CI Monitor (step 1). This fix subagent is a code-writing spawn on a model_routed stage, so pin it the same way the do-loop pins `implement`, passing this stage's name: `M=$(python3 ${CLAUDE_SKILL_DIR}/scripts/model_resolve.py --workspace-root . --ticket "$KEY" --stage review_loop)` and pass `model=$M` to the fix Agent when `$M` is non-empty (else omit — inherit the session).

**Hard cap: 3 fix cycles total** across CI + review combined (human-requested review-packet rounds AND revision triage-board rounds do NOT count — a present human is the judgment the cap substitutes for, so the cap bounds unattended loops only; see `references/review-packet.md`). If CI is still red after 3, set `STATUS=failed` and surface the last failing logs — do not loop forever.

## 3. Poll review threads

**First, wait for the review bot to finish (flow-arva).** CI green does NOT mean the bot has reviewed — CodeRabbit reviews asynchronously and routinely posts its findings *after* CI is green. Fetching threads once at CI-green races that review: an empty list reads as "clean" when the bot simply has not run yet, and a late Major+ finding would be merged past under a false "review-clean". Gate on the bot's completion signal before trusting the thread list:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . review-status --pr "$PR_ID"
```

- `{"supported": false}` → this host exposes no review-bot completion signal (e.g. the GitHub self-target runs no bot). **Do not wait** — go straight to the thread poll below. An empty list is legitimately clean here *only when no bot runs on this host at all*; when the org is known to run a review bot and the adapter merely lacks a completion probe, an empty list is the same ambiguity as the cap-expiry case below — record the not-reviewed caveat (flow-enr8) instead of asserting review-clean.
- `{"reviewed": true}` → the bot has finished; proceed to the thread poll.
- `{"reviewed": false}` → the bot has not finished. Read `.draft` for context (it shapes the cap-expiry wording below; the `2>/dev/null` guards keep a transient/empty `pr-info` from dumping a traceback — `$DRAFT` degrades to empty, not `True`):

```bash
DRAFT=$(python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . pr-info --pr "$PR_ID" 2>/dev/null \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("draft") if d else False)' 2>/dev/null)
```

  Then re-poll on a bounded wait until `reviewed` is `true` OR the cap is hit (turn-safe in one Bash call; an attached session MAY use a §1-style Monitor instead) — **on a draft too**. The old draft short-circuit (flow-uc8n) is retired (flow-enr8): it skipped this wait on the premise that CodeRabbit never reviews draft PRs, but whether CR reviews drafts is org configuration, not a host constant (witnessed on CO-226/PR#2939: CR reviews Bitbucket drafts there), so skipping the wait on a draft races past a review that lands a minute later. `reviewed:false` on a draft is ambiguous — still running, deferred-until-ready, or disabled org-wide — and only this bounded wait separates the first from the rest; a bot that genuinely defers drafts hits the cap and takes the not-reviewed path below, which is correct: that review really has not happened yet.

```bash
i=0; while [ $i -lt 10 ]; do
  r=$(python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . review-status --pr "$PR_ID" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("reviewed"))')
  echo "[$(date +%T)] review bot finished: $r"
  [ "$r" = "True" ] && break
  sleep 45; i=$((i+1))
done
```

10 × 45s = 450s (under the 600s Bash ceiling) covers the observed CR latency (it completed ~1min after CI-green on the witness PR). If still not finished at the cap, proceed to the thread poll but do NOT let an empty list read as review-clean (flow-enr8): a CR disabled org-wide keeps `reviewed:false` and `[]` threads *indefinitely* — the CO-226/PR#2939 pattern — and the empty list means "nothing reviewed", not "nothing found". Cap expired + threads empty → **record in the stage report AND surface to the user**: "review bot did not review this PR (likely disabled" — or, when `$DRAFT` is `True`, "likely deferred on draft or disabled" — "); proceeding on CI-green only — automated review did not happen". Resilience, not a block: the stage still proceeds to §5 on CI-green. Cap expired + threads NON-empty → handle them per §4 (a partial review beats none) and still record the incomplete-review caveat.

Then poll the threads:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . review-threads --pr "$PR_ID"
```

- Output `{"supported": false}` (a host with no review-bot/forge wired): **skip thread handling**, report "review threads not wired for this host", and proceed to the terminal check on CI-green alone.
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

`STATUS=completed` when **CI is green AND zero unresolved Major+ threads remain**, with the review-clean claim gated by §3: the bot-completion gate is satisfied when the review bot has finished, OR the host exposes no completion signal AND no bot runs there (`{"supported": false}`, first §3 bullet). When the gate is NOT satisfied at §3's cap (the bot never finished — disabled, or deferring a draft) and the thread list stayed empty, the stage still completes on CI-green but as **not-reviewed, never review-clean** (flow-enr8): the stage report AND the user-facing completion message (the PR-ready notification below) carry §3's warning verbatim — "proceeding on CI-green only — automated review did not happen". An empty thread list only means "clean" once the gate passed; never terminate review-clean on an empty list the bot did not produce. Remaining Minor/nit threads are reported open with one-line reasons, not chased. Respect the 3-cycle cap. **Stop every Monitor on exit** (a leaked Monitor keeps the shell alive). The PR-ready notification fires exactly once per run: on a packet-gated interactive run at packet-open (§6 below, before this terminal), otherwise at do-loop step e once this stage is `completed`, with the PR URL (see `references/verb-do.md`); only when the handler is `none` does that notification fall back to firing at `create_pr` instead.

This stage MAY write a short report (cycles run, threads resolved/skipped, final CI state) to `$TICKET_DIR/stages/review_loop.out`; pass `--output-path` on `advance` if it does.

## 6. Review packet (interactive, gate-2)

When §5's terminal condition is met (CI green AND zero unresolved Major+ threads, the bot-gate satisfied per §3) and BEFORE `STATUS=completed` is recorded via `advance`, an interactive run offers a local HTML review packet — the gate-2 analogue of the plan surface (`references/verb-spec.md` step 4). The full protocol (gate, data assembly, authoring, the fix-round loop, the lease heartbeat, the verdict, the degradation contract) lives in `references/review-packet.md`; this is the one-line handoff. The fix rounds ARE review_loop semantics — §2's delegated-fix recipe reused verbatim, human-requested rounds exempt from §2's 3-cycle cap — so the round log lands in this stage's `review_loop.out`, no orphan stage file.

A **revision sub-run** (`<ticket-dir>` contains `/revisions/`) does NOT attach this gate-2 packet — its interactive surface is the revision triage board, opened earlier from `references/verb-revise.md` step 5a and specified in `references/review-packet.md`'s `## Revision triage board (/flow revise)` section. When that board persisted a `dispositions.json`, this stage's fix set and terminal check follow the revision-mode deltas above.

The PR-ready notification fires exactly once per run. On a packet-gated run it fires at packet-open — this satisfies the do-loop step-e firing point (no duplicate ping), and the packet loop then runs inside `review_loop`'s tail (see `references/review-packet.md`). On a gate-failed run (the packet never opens) it fires at step e exactly as today. The packet never attaches at the `create_pr` fallback firing point (`review_loop` handler `none` → skip line, no packet).

Any packet failure degrades to today's delivery plus one visible line — `Lavish: skipped — <reason>` before the packet opens, `Lavish: degraded mid-loop — <reason>` after — then this stage records `STATUS=completed` and advances exactly as it would without the packet. The packet is an ADD-ON, never a precondition of terminal.
