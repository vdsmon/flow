# review_loop stage (inline, forge-driven)

The post-PR wait loop: after `create_pr` opens the PR, this stage waits on CI and drives any fixes until green, then resolves the review bot's actionable threads. It is **integral**, not a nicety — flow's pipeline is not done at "draft PR opened", it is done at "PR is green and review-clean". The host calls go through the **forge seam** (`forge_cli.py`), so the same protocol serves GitHub (`gh`) and Bitbucket (`bkt`).

The bare plugin default handler is `none` (a no-op skip) so a workspace with no `[forge]` / no CI degrades cleanly; flow's dogfood wires `review_loop = "inline"`. The predecessor is `create_pr`. This stage reaches `completed` when **CI is green AND there are no unresolved Major+ review threads**.

## Revision mode

When `<ticket-dir>` contains `/revisions/` this is a revision sub-run (see `references/delivery-revision.md`): there is no `create_pr` predecessor — the SAME PR is updated in place, and the unresolved threads `review-threads` returns are the MAINTAINER's (the original run resolved the bot threads before delivery, so what remains unresolved is human).

**No `create_pr.out`** — skip the `## Inputs` read below; resolve the open PR already verified by the revision lifecycle from the branch instead, and use this `$PR_ID` for §1 / §3 / §4:
```bash
PR_ID=$(FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . detect-pr --branch "$(git rev-parse --abbrev-ref HEAD)" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("id","") if d else "")')
```

Deltas from the normal loop:

- **Explicit dispositions supersede the floor.** When `<ticket-dir>/dispositions.json` exists (an interactive `revise` opened the step-5a triage board — `references/revision-triage-board.md` carries the schema), the human's explicit dispositions SUPERSEDE inferred severity: the fix set is the **fix pile** (`threads[]` entries with `"disposition": "fix"`) regardless of severity, and `apply-floor` is NOT consulted. §5's terminal "zero unresolved Major+" check then evaluates over that fix pile, not the raw thread severities — a dismissed major must NOT deadlock terminal, and an explicit empty triage (file exists, fix pile empty: all defer/dismiss, or `"threads": []`) leaves the terminal check nothing to chase, no floor-bumped threads. While the board session is live, completion still waits on the user's end-session verdict (the board section's convergence rules) — a mid-session all-defer/dismiss batch does NOT complete the stage. Only when NO `dispositions.json` exists does the plain-comment floor below apply (the empty-vs-absent distinction).
- **Plain-comment floor.** Before the §4 address+resolve, fetch the threads capture-then-check (the §1 discipline: read `$?` first — piping `review-threads` straight into `apply-floor` swallows a non-zero exit, and `apply-floor` turns the empty stdin into `[]`, so a gh flake reads as ZERO maintainer threads, a false review-clean), then pipe the captured output through the floor so an unresolved `minor` (a plain human comment) is bumped to the configured severity:
  ```bash
  RAW=$(FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . review-threads --pr "$PR_ID"); rc=$?
  [ "$rc" -eq 0 ] && THREADS=$(printf '%s' "$RAW" | FLOW_HARNESS="<harness>" "<facade>" revise-config apply-floor --workspace-root .)
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

## 1. Wait for CI through the adapter

Keep the wait in the owning orchestration session. Claude Code may launch the
**Monitor** recipe below. Codex uses its session wait/poll mechanism or the bounded
foreground recipe; a generic adapter uses the bounded recipe. Do not hand continuation
to a child agent. Every probe uses explicit workdir `run_root` and the absolute
`facade`, per `references/harness.md`.

The poll reads the `ci-rollup` process exit code before parsing output, so an
intermittently-erroring forge trips a consecutive-error budget instead of spinning.
It short-circuits when the PR leaves OPEN state and always has an iteration cap. Bind
`$BRANCH` from the rooted worktree HEAD:

```bash
BRANCH=$(git rev-parse --abbrev-ref HEAD)   # the worktree is on the run's feature branch
```

```
Monitor(
  description="CI for PR #$PR_ID",
  command='budget=3; errs=0; n=0; cap=25; prev=""; nock=0; while :; do
      n=$((n+1)); [ "$n" -gt "$cap" ] && { echo "[$(date +%T)] cap $cap hit — leave for next pass"; break; }
      pr=$(FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . detect-pr --branch "$BRANCH"); drc=$?
      if [ "$drc" -eq 0 ] && [ "$(printf %s "$pr" | tr -d " \t\n")" = "null" ]; then echo "[$(date +%T)] PR #$PR_ID no longer open (merged/closed)"; break; fi
      out=$(FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . ci-rollup --pr "$PR_ID"); crc=$?
      s=""; nc=0; if [ "$crc" -eq 0 ]; then sc=$(printf %s "$out" | python3 -c "import sys,json;d=json.load(sys.stdin);print((d.get(\"status\",\"\") or \"\")+\"|\"+(\"1\" if not d.get(\"checks\") else \"0\"))" 2>/dev/null); [ -n "$sc" ] && { s=${sc%%|*}; nc=${sc##*|}; }; fi
      if [ "$crc" -ne 0 ] || [ -z "$s" ]; then errs=$((errs+1)); echo "[$(date +%T)] ci-rollup probe error ($errs/$budget)"; [ "$errs" -ge "$budget" ] && { echo "error budget exhausted — leave for next pass"; break; }; sleep 60; continue; fi
      errs=0; [ "$s" != "$prev" ] && { echo "[$(date +%T)] CI: $s"; prev=$s; }
      if [ "$s" = pending ] && [ "$nc" = 1 ]; then nock=$((nock+1)); [ "$nock" -ge 3 ] && { echo "[$(date +%T)] no checks registered x3 — probe mergeable (CONFLICTING?)"; break; }; else nock=0; fi
      case "$s" in green|failed) break;; esac
      sleep 60
    done',
  timeout_ms=1620000,
  persistent=false
)
```

Run exactly ONE CI Monitor at a time (stop the prior one before re-arming after a fix). Break on `green` or `failed` — the terminal `CI_STATUS` enum is `green` / `failed` (NOT `success` / `failure`, NOT `red`); `ci_rollup` folds the superseded `CANCELLED`/`STALE`/`NEUTRAL`/`SKIPPED` entries into `pending`, so those re-poll rather than trip a false `failed`. **Anti-pattern:** never `ci-rollup ... 2>/dev/null | python -c '...get("status","pending")'` — piping past the exit code makes an errored `gh` read as `pending` forever (a silent infinite spin); the probe reads `$?` first, which is the fix.

**Portable bounded foreground poll.** Use this on Codex, a generic adapter, or any
headless/turn-bounded Claude Code session where a Monitor/background task would die at
the turn boundary. Poll in one command call with an explicit iteration cap and the
host's bounded timeout:

```bash
i=0; errs=0; nock=0; while [ $i -lt 8 ]; do
  out=$(FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . ci-rollup --pr "$PR_ID"); crc=$?
  s=""; nc=0; if [ "$crc" -eq 0 ]; then sc=$(printf %s "$out" | python3 -c 'import sys,json;d=json.load(sys.stdin);print((d.get("status","") or "")+"|"+("1" if not d.get("checks") else "0"))' 2>/dev/null); [ -n "$sc" ] && { s=${sc%%|*}; nc=${sc##*|}; }; fi
  if [ "$crc" -ne 0 ] || [ -z "$s" ]; then
    errs=$((errs+1)); echo "[$(date +%T)] ci-rollup probe error ($errs/3)"
    [ "$errs" -ge 3 ] && { echo "error budget exhausted — leave for next pass"; break; }
    sleep 60; i=$((i+1)); continue
  fi
  errs=0; echo "[$(date +%T)] CI: $s"
  if [ "$s" = pending ] && [ "$nc" = 1 ]; then nock=$((nock+1)); [ "$nock" -ge 3 ] && { echo "[$(date +%T)] no checks registered x3 — probe mergeable (CONFLICTING?)"; break; }; else nock=0; fi
  if [ "$s" = "green" ] || [ "$s" = "failed" ]; then break; fi
  sleep 60; i=$((i+1))
done
```

8 × 60s = 480s keeps one call comfortably under the 600s Bash ceiling even with slow rollup calls. The probe reads `$?` before parsing, same as the Monitor — an erroring `gh` trips the 3-error budget instead of reading as `pending` forever (the §1 anti-pattern). If still `pending` at the cap, re-issue the same call — each call is one turn-safe unit. Break on `green`/`failed` exactly like the Monitor; the §2 fix-cycle cap is unchanged. This is a fallback, not a coequal default — attached/long-lived sessions keep using the Monitor.

**CONFLICTING short-circuit — no merge ref, checks can never register.** Both polls above break early with `no checks registered x3` when they see `pending` with an EMPTY `checks` array (`ci-rollup` `detail: "no checks registered yet"`) three times. That signal is GitHub-specific: a PR whose `mergeable` state is `CONFLICTING` has no merge ref, so `pull_request` workflows never start and `ci-rollup` reads `pending` forever, never `failed` (witnessed flow-09bg.2/PR#468: a mid-run merge to `main` conflicted the branch and the poll burned 24+ min). The empty-`checks` test is the precise discriminator — a slow-but-registered queue and the superseded verdicts (`CANCELLED`/`STALE`/`NEUTRAL`/`SKIPPED`, flow-5wr) both fold into `pending` with a NON-EMPTY `checks` array, so neither trips the counter. On that break, do NOT re-arm the poll blindly (it just re-burns the cap); probe mergeability and, on a real conflict, clear it with a base-merge:

```bash
mg=$(gh pr view "$PR_ID" --json mergeable -q .mergeable 2>/dev/null)   # GitHub-only; the forge PullRequest carries no mergeable field
case "$mg" in
  CONFLICTING)
    # base-merge fix cycle (counts as ONE of §2's 3 fix cycles)
    git fetch origin
    DEFAULT=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)
    [ -n "$DEFAULT" ] || DEFAULT=origin/main   # origin/HEAD may be unset in a fresh worktree
    if git merge --no-edit "$DEFAULT"; then    # PLAIN merge; never rebase, never force-push mid-run
      git push                                 # clean merge: plain push, so checks register on the new head
    else
      # merge left conflicts (the witnessed case): resolve them directly in the authoritative
      # worktree (NOT the §2 capsule path: a half-merged tree cannot be reproduced in a fresh
      # clone at source_sha), `git add`, commit the merge, and push. NEVER push the half-merged
      # tree, and never `git merge --abort`.
      :
    fi
    ;;                                         # then re-arm the CI poll (step 1)
  MERGEABLE)
    # not a conflict, just slow registration: re-arm the poll (step 1). Bound it: if a re-armed poll STILL sees
    # empty checks, CI is likely not wired for this PR, so stop after one re-arm and leave for the next pass.
    : ;;
  *)                                       # UNKNOWN (mergeability is recomputed async), or gh absent / errored
    # UNKNOWN: re-probe 2-3x a few seconds apart; if it resolves, branch on CONFLICTING/MERGEABLE above.
    # Still UNKNOWN, or gh unavailable (Bitbucket / any gh-less host): re-arm the poll unchanged (today's behavior).
    : ;;
esac
```

The base-merge commit legitimately pulls in `origin/<default>` content; it is a post-`commit`-stage push (like §2's CI-fix commits), so the content-ownership gate — which runs only at the `commit` stage (`references/stage-commit.md` §2b) — does NOT re-fire on it. Surface a one-line note in `review_loop.out` (e.g. `base-merged origin/<default> to clear CONFLICTING; CI re-armed`). This counts against §2's 3-fix-cycle cap. Bitbucket conflict detection (its own conflict signal) is a follow-up; the raw `gh` probe here is GitHub-specific, and the degrade path leaves gh-less hosts behaving exactly as before.

## 2. On CI failed — drive fixes (routed capsule writer, bounded)

Do NOT invent inline edit logic and do NOT hand the fix to a subagent that edits the
worktree directly. A fix routes through an activated importing writer: `review_fixer` for
ordinary CI or bot-review fixes, `revision_fixer` for a human-requested revision sub-run
(the two profiles keep pipeline remediation and a revision distinct). Both activate on an
exact CLI receipt in this increment.

Drive the fix through the cognitive-substep executor exactly as the `implement` stage's
importing writer (`references/delivery-loop.md`, "Activated cognitive substeps"): build the
fixer's closed facts — the failing-check logs / unresolved findings as `review_findings`,
or the human's revision as `revision_instruction` — plus its immutable input bundle, and
hand them to `cognitive-worker run-stage` for the sealed `review_fix` / `revision_fix`
substep. The writer edits and tests inside a private capsule seeded with the ticket's
uncommitted working state; Flow — not the model — captures the binary-aware patch and
compare-and-swap imports it into the authoritative worktree under a sole-writer claim. The
order's `allowed_mutation_paths` is sealed to the run's `planned_files`, so a touch outside
that set is an `ownership_violation` and nothing imports; there is no flag-and-widen escape
hatch (widen through the ownership reconcile before re-recording the baseline). The worker
returns only a typed report (`summary`, `evidence`, `source_sha`) and never serializes a
diff. After the import lands, commit with the existing commit machinery and `git push`,
then re-arm the adapter's CI wait (step 1). An exact-route failure stops the step visibly
rather than falling back to a native edit; legacy `[models]` retains its existing lane,
OFF, and fail-open behavior.

When this pass launches a fixer, only that substep's outcome is written; the sibling
conditional substep (`revision_fix` on a CI/bot fix, `review_fix` on a revision sub-run)
never ran and still needs a reasoned skip. §5 emits the skips for every un-run fixer
substep at the terminal advance, so the outcome fence is satisfied on this fix path and on
the green-first-poll path alike (`references/delivery-loop.md`, the per-substep
facts-or-skip contract).

**Hard cap: 3 fix cycles total** across CI + review combined (human-requested revision triage-board rounds do NOT count — a present human is the judgment the cap substitutes for, so the cap bounds unattended loops only; see `references/revision-triage-board.md`). If CI is still red after 3, set `STATUS=failed` and surface the last failing logs — do not loop forever.

## 3. Poll review threads

**First, wait for the review bot to finish (flow-arva).** CI green does NOT mean the bot has reviewed — CodeRabbit reviews asynchronously and routinely posts its findings *after* CI is green. Fetching threads once at CI-green races that review: an empty list reads as "clean" when the bot simply has not run yet, and a late Major+ finding would be merged past under a false "review-clean". Gate on the bot's completion signal before trusting the thread list:

```bash
FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . review-status --pr "$PR_ID"
```

- `{"supported": false}` → this host exposes no review-bot completion signal (e.g. the GitHub self-target runs no bot). **Do not wait** — go straight to the thread poll below. An empty list is legitimately clean here *only when no bot runs on this host at all*; when the org is known to run a review bot and the adapter merely lacks a completion probe, an empty list is the same ambiguity as the cap-expiry case below — record the not-reviewed caveat (flow-enr8) instead of asserting review-clean.
- `{"reviewed": true}` → the bot has finished; proceed to the thread poll.
- `{"reviewed": false}` → the bot has not finished. Read `.draft` for context (it shapes the cap-expiry wording below; the `2>/dev/null` guards keep a transient/empty `pr-info` from dumping a traceback — `$DRAFT` degrades to empty, not `True`):

```bash
DRAFT=$(FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . pr-info --pr "$PR_ID" 2>/dev/null \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("draft") if d else False)' 2>/dev/null)
```

  Then re-poll on a bounded wait until `reviewed` is `true` OR the cap is hit (turn-safe in one Bash call; an attached session MAY use a §1-style Monitor instead) — **on a draft too**. The old draft short-circuit (flow-uc8n) is retired (flow-enr8): it skipped this wait on the premise that CodeRabbit never reviews draft PRs, but whether CR reviews drafts is org configuration, not a host constant (witnessed on CO-226/PR#2939: CR reviews Bitbucket drafts there), so skipping the wait on a draft races past a review that lands a minute later. `reviewed:false` on a draft is ambiguous — still running, deferred-until-ready, or disabled org-wide — and only this bounded wait separates the first from the rest; a bot that genuinely defers drafts hits the cap and takes the not-reviewed path below, which is correct: that review really has not happened yet.

```bash
i=0; while [ $i -lt 10 ]; do
  r=$(FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . review-status --pr "$PR_ID" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("reviewed"))')
  echo "[$(date +%T)] review bot finished: $r"
  [ "$r" = "True" ] && break
  sleep 45; i=$((i+1))
done
```

10 × 45s = 450s (under the 600s Bash ceiling) covers the observed CR latency (it completed ~1min after CI-green on the witness PR). If still not finished at the cap, proceed to the thread poll but do NOT let an empty list read as review-clean (flow-enr8): a CR disabled org-wide keeps `reviewed:false` and `[]` threads *indefinitely* — the CO-226/PR#2939 pattern — and the empty list means "nothing reviewed", not "nothing found". Cap expired + threads empty → **record in the stage report AND surface to the user**: "review bot did not review this PR (likely disabled" — or, when `$DRAFT` is `True`, "likely deferred on draft or disabled" — "); proceeding on CI-green only — automated review did not happen". Resilience, not a block: the stage still proceeds to §5 on CI-green. Cap expired + threads NON-empty → handle them per §4 (a partial review beats none) and still record the incomplete-review caveat.

Then poll the threads:

```bash
FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . review-threads --pr "$PR_ID"
```

- Output `{"supported": false}` (a host with no review-bot/forge wired): **skip thread handling**, report "review threads not wired for this host", and proceed to the terminal check on CI-green alone.
- Otherwise: a JSON array of normalized threads, each with `severity` (`critical`/`major`/`minor`/`nit`/`unknown`), `file`, `line`, `title`, `id`.

## 4. Address + resolve (Major+ only)

Only an **unresolved Major+ finding (or CI red)** justifies another fix cycle; Minor/nit never trigger their own cycle. **Each cycle must strictly reduce the unresolved Major+ count** — if a cycle does not, stop and escalate to the user with both sides (the finding and why it resists), rather than burning the cap.

For each Major+ thread you addressed in a pushed commit:

```bash
FIX_SHA=$(git rev-parse --short HEAD)
FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . post-reply \
  --pr "$PR_ID" --thread "<CID>" --text "Fixed in $FIX_SHA. <one line: what changed and why>."
FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . resolve-thread \
  --pr "$PR_ID" --thread "<CID>"
```

`resolve-thread` returns `{"resolved": true}` only when the host VERIFIED the thread resolved (the bkt adapter re-reads the comment and checks `.resolution != null`). For a **reasoned-skip** thread (you disagree with the finding): `post-reply` with the reasoning, leave it open, and document it in the stage report — do not resolve it.

## 5. Terminal

`STATUS=completed` when **CI is green AND zero unresolved Major+ threads remain**, with the review-clean claim gated by §3: the bot-completion gate is satisfied when the review bot has finished, OR the host exposes no completion signal AND no bot runs there (`{"supported": false}`, first §3 bullet). When the gate is NOT satisfied at §3's cap (the bot never finished — disabled, or deferring a draft) and the thread list stayed empty, the stage still completes on CI-green but as **not-reviewed, never review-clean** (flow-enr8): the stage report AND the user-facing completion message carry §3's warning verbatim — "proceeding on CI-green only — automated review did not happen". An empty thread list only means "clean" once the gate passed; never terminate review-clean on an empty list the bot did not produce. Remaining Minor/nit threads are reported open with one-line reasons, not chased. Respect the 3-cycle cap. **Stop every Monitor on exit** (a leaked Monitor keeps the shell alive). The PR-ready notification fires exactly once at the normal do-loop step-e delivery point once this stage is `completed`; only when the handler is `none` does that notification fall back to firing at `create_pr` instead.

**Satisfy the cognitive outcome fence before `advance --status completed`.** Activating the
review-loop fixers seals `review_fix` and `revision_fix` as `pending` conditional substeps,
so completion requires an outcome-or-skip for each — on EVERY exit path, the green-first-poll
included (§1 broke on green, so §2 never ran and neither fixer launched). Emit a reasoned
skip for each fixer substep that did NOT launch a capsule this run through the same
`cognitive-worker run-stage` skip input as §2 (`references/delivery-loop.md`, "Activated
cognitive substeps"), then pass the executor's `cognitive_skips` as the advance skill output.
A fixer that DID run already wrote its outcome to disk (the fence reads that first and ignores
a skip for it), so only the un-run substeps skip: green-first-poll skips both; a CI/bot fix
pass skips `revision_fix`; a revision sub-run skips `review_fix`. Without this the terminal
advance fails closed — `activated cognitive substep 'review_fix' has no successful outcome or
valid skip`.

This stage MAY write a short report (cycles run, threads resolved/skipped, final CI state) to `$TICKET_DIR/stages/review_loop.out`; pass `--output-path` on `advance` if it does.

## 6. Continue to the reviewer companion

Record completion and advance without waiting for human review. The next configured
stage, `review_brief`, generates the read-only local companion and then advances to
`reflect` immediately. Human comments after delivery enter the revision workflow,
not an in-stage HTML feedback loop.
