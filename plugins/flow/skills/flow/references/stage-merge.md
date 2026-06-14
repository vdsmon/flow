# merge stage (inline, evolve self-target only)

The terminal self-merge stage. Runs **after `reflect`**, so every output of the run is committed before the PR lands. Default handler is `none` (the generic pipeline and every user project skip it — the human keystone holds); flow's own self-target workspace wires `merge = "inline"`. This is **Layer 2** of the evolve restructure: an evolve run that reached green + review-clean (`review_loop`) merges its own PR instead of waiting for a deferred drain pass.

A `hot`/guard PR self-merges **only after an independent reviewer subagent clears the §6A guard-property check** — the run that wrote the diff is never the sole judge of whether it removed a safety property.

## 1. Inputs + eligibility gate

Read the PR opened by `create_pr`:

```bash
PR_URL=$(grep -oE '^PR_URL=.*' "$TICKET_DIR/stages/create_pr.out" | head -1 | cut -d= -f2-)
PR_ID=$(printf '%s' "$PR_URL" | grep -oE '[0-9]+$')
```

**Already-merged short-circuit.** Before re-reading CI or asking the gate, check the PR's actual merge state — a `hot` leaf PR can auto-merge (via the evolve janitor) before this run's own merge stage runs, and the eligibility gate below does NOT read PR merge state (it decides from CI-green + self-target + evolve-bead + hot-policy), so an already-MERGED PR with still-green CI would return `action: "merge"`, burn a §2 guard review on a merged PR, then trip §3's push-state guard once origin has deleted the branch (its `git rev-parse origin/$BRANCH` cannot resolve the deleted ref). The check sits here, right after PR_ID is in hand, so it short-circuits all of that. Read the state with raw `gh` (the established precedent for this inline, self-target-only GitHub stage — §2 already uses `gh pr diff`; `forge_cli detect-pr` filters `--state open` and cannot see an already-MERGED known PR_ID):

```bash
PR_STATE=$(gh pr view "$PR_ID" --json state -q .state)
if [ "$PR_STATE" = "MERGED" ]; then
  echo "PR #$PR_ID already merged — nothing to do"
  bd close "$KEY" --reason "PR #$PR_ID already merged" || true   # may already be CLOSED by the auto-merge; must not fail the stage
  # STATUS=completed; STOP — skip the CI re-read, the eligibility gate, §2, and §3.
  # No delete-branch: origin already removed the branch; worktree teardown stays with the drain reap (§3's division of labor).
fi
```

On `MERGED`, set `STATUS=completed` and STOP here — do not fall through to the CI re-read, the eligibility gate, §2, or §3. Scope this short-circuit to `MERGED` ONLY. A `CLOSED`-not-merged (abandoned) PR is NOT handled here — it falls through to the §3 push-state guard unchanged.

Re-confirm CI is still green (it was `review_loop`'s terminal, but re-read defensively — nothing should have changed it):

```bash
CI=$(python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . ci-rollup --pr "$PR_ID" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')
```

**Harness non-regression eval (Self-Harness no-degradation rule).** When the PR touches the engine's own scripts, replay the frozen decider corpus against the candidate tree before asking the gate:

```bash
EVAL_STATUS=""
if gh pr diff "$PR_ID" --name-only | grep -qE '^plugins/flow/skills/flow/scripts/.*\.py$'; then
  python3 ${CLAUDE_SKILL_DIR}/scripts/harness_eval.py score \
    --candidate plugins/flow/skills/flow/scripts \
    > "$TICKET_DIR/stages/harness_eval.json"
  case $? in 0) EVAL_STATUS=pass ;; 3) EVAL_STATUS=regressed ;; *) EVAL_STATUS=error ;; esac
fi
```

Candidate = this run's own scripts tree (the self-edit branch); baseline + corpus = `harness_eval`'s defaults (the installed skill checkout it runs from). That means the eval does NOT see candidate-side edits to `harness_corpus.json` or `harness_eval.py` — it replays the installed copy against the baseline corpus; `tests/test_harness_corpus.py` (CI) is the real gate on corpus edits. An INTENTIONAL decider behavior change reads as `regressed` by design (the corpus is baseline-side); the human is the override. Non-scripts PRs skip the eval entirely (`EVAL_STATUS` stays empty → the gate sees no `--eval-status`).

**Probe main's own CI health (the per-drain-turn main-CI gate).** Before asking the gate, probe whether MAIN's CI is genuinely red — two concurrently-green PRs that semantically conflict land on main untested, and this run must not stack a self-merge onto an already-red main. The verdict is asymmetric: only `failed` pauses; `green`, `pending`, and a transient probe `error` (a gh 401 / network flake) all resume (the gate treats any non-`failed` value as a no-op).

```bash
MAIN_CI=$(python3 ${CLAUDE_SKILL_DIR}/scripts/main_ci_health.py probe --workspace-root . \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')
```

Ask the pure gate whether this run may self-merge:

```bash
_merge_args=(--workspace-root . --key "$KEY" --ci-status "$CI" --main-ci-status "$MAIN_CI")
[ -n "$EVAL_STATUS" ] && _merge_args+=(--eval-status "$EVAL_STATUS")
python3 ${CLAUDE_SKILL_DIR}/scripts/evolve_self_merge.py "${_merge_args[@]}"
```

Returns `{"action": "merge"|"skip", "is_hot": bool, "reason": "..."}`. The gate skips when this is not the maintainer self-target, not an `evolve` bead, CI is not green, **main's own CI is red** (`main CI red` — auto-merge paused this turn; a probe `error` resumes, it does not skip), the harness eval did not pass (`regressed` = the no-degradation rule; `error` = no non-regression evidence, blocked conservatively), or a `hot` bead while `[evolve] auto_merge_hot` is off.

- **`action: "skip"`** → leave the PR as-is for the human (this is the normal outcome on a user project and for held hot beads), `STATUS=completed`. Done. On an eval-driven skip (`regressed`/`error` reason), first post a PR comment naming the regressed case ids from `$TICKET_DIR/stages/harness_eval.json` (mirrors §2's `held_guard` pattern) so the maintainer sees WHICH frozen cases moved.
- **`action: "merge"`** → continue. If `is_hot` is true, run §2 FIRST; otherwise skip to §3.

## 2. Independent guard-property review (hot beads only)

A `hot` bead touches a guard / safety-machinery file. Before merging it, spawn a **fresh, independent reviewer** — NOT the agent that wrote the change — with the `Agent` tool (`subagent_type: general-purpose`), prompted to REFUTE:

> Review this PR diff for the evolve self-target. Question: does it DELETE or WEAKEN any safety property — lease exclusivity (one run per ticket), snapshot drift-detection, atomic-write + corrupt-file quarantine, content-ownership refusal, or self-edit flock serialization? Guard *code* may be refactored/sped up freely; a guard *property* may only be replaced by a provably-equivalent one, never dropped. Default to "property removed" when uncertain. Return a verdict: `{property_removed: bool, which: str, why: str}`. Diff:
> ```
> <output of: gh pr diff $PR_ID>
> ```

If the reviewer reports `property_removed: true` → **do NOT merge.** Post a PR comment naming the property, report it under `held_guard` in the stage output, and `STATUS=completed` (the PR stays ready for the maintainer). Only a clean review (`property_removed: false`) proceeds to §3.

## 3. Merge

**Merge ONLY the exact commit CI validated.** `review_loop`'s green verdict was for the branch HEAD it pushed; `reflect` does not commit to the run branch (it names repo-artifact gaps instead of adding files, and machinery self-edits land on a separate skill-checkout tree — `references/stage-reflect.md`). Guard against it anyway: if a TRACKED file has an uncommitted change, or there is an unpushed commit, CI never saw it, so do NOT self-merge — leave it for the drain reap / human. **Untracked files do not count** — the run's own scratch (`.flow/tickets/`, `.flow/runs/`) is never part of the PR, so `--untracked-files=no` is deliberate (a bare `git status --porcelain` would trip on that scratch and block every self-merge).

```bash
BRANCH=$(git rev-parse --abbrev-ref HEAD)   # the run's feature/<key>-* branch
git fetch --quiet origin "$BRANCH"   # refresh refs/remotes/origin/$BRANCH; a flow worktree records NO upstream tracking (@{u} empty), so prove the push via the remote-tracking ref
if [ -n "$(git status --porcelain --untracked-files=no)" ] || [ "$(git rev-parse HEAD)" != "$(git rev-parse "origin/$BRANCH" 2>/dev/null)" ]; then
  echo "branch has uncommitted (tracked) or unpushed changes CI never validated — skipping self-merge"
  # STATUS=completed; the deferred drain reap (or the human) merges once state settles.
else
  # version stamp (epic flow-6gx): the plugin version is NOT bumped per-PR anymore;
  # it is stamped here, at the actual merge point, computed from the current
  # origin/main. FETCH FIRST so origin/main is current — a stale ref makes every
  # concurrent drain run stamp the SAME number, forcing the DIRTY/version_remerge
  # serial recovery this design exists to reduce. version.py stamp then writes the
  # next version into both version files — MINOR on a feat commit type, PATCH
  # otherwise (the frontmatter commit_type feeds --commit-type; empty falls back
  # to the HEAD subject's conventional prefix, then patch) — and the new files
  # are committed + pushed as a NEW branch SHA.
  # Then RE-WAIT CI on that SHA — the stamp pushed a commit CI never saw, and the
  # "merge ONLY the CI-validated SHA" invariant the push-state guard upholds
  # requires CI to re-validate it (the same mandatory re-wait version_remerge does).
  git fetch --quiet origin
  COMMIT_TYPE=$(sed -n 's/^commit_type = "\(.*\)"$/\1/p' ".flow/tickets/$KEY.md" | head -1)
  python3 ${CLAUDE_SKILL_DIR}/scripts/version.py stamp --ref origin/main --cwd . \
    --commit-type "$COMMIT_TYPE"
  git commit -m "chore: stamp plugin version" -- \
    plugins/flow/.claude-plugin/plugin.json .claude-plugin/marketplace.json
  git push origin "$BRANCH"
  # ↓ run the **Hardened CI re-wait probe** (defined at the end of §3) on $PR_ID / $BRANCH;
  # proceed ONLY on a green break — every other terminal (failed / PR-merged-or-closed /
  # probe-error-budget / cap) leaves the PR for the drain reap / human (STATUS=completed),
  # do NOT hang. On EACH poll iteration, also heartbeat the run lease (below) so
  # this long re-wait does not let it go stale.
  # heartbeat: refresh the run lease each poll so a long CI re-wait does not let it
  # go stale (a stale lease lets a parallel drain reap merge this live PR + reap the
  # worktree -- flow-ztfv). $TICKET_DIR holds state.json (run_id) and run.lock.
  RUN_ID=$(python3 -c "import json;print(json.load(open('$TICKET_DIR/state.json'))['run_id'])")
  python3 ${CLAUDE_SKILL_DIR}/scripts/lease.py refresh \
    --ticket-dir "$TICKET_DIR" --run-id "$RUN_ID" --ttl-seconds 1800
  RC=$?
  # check the heartbeat exit: 7 = LeaseLost (lost / taken-over / nonce-rotated; a human
  # `recover takeover --force`, flow-5lg3, or a nonce rotation flipped ownership), 3 =
  # LeaseError. On either, the lease is no longer ours — a parallel drain reap may
  # already own this ticket — so STOP the ENTIRE §3 self-merge: do NOT run the
  # duplicate-stamp guard / MERGE_STATE read / merge / bd close / delete-branch below.
  # Leave the live PR for the drain reap / human. Do NOT release the lease (not ours to
  # release). This STRENGTHENS lease exclusivity (only the lease-holder merges/reaps) —
  # it removes no guard. Mirrors the version_remerge STOP further below (flow-tnfp).
  if [ "$RC" -ne 0 ]; then
    echo "run lease lost (exit $RC) — leaving PR #$PR_ID for the drain reap / human"   # STATUS=completed; STOP all of §3, no self-merge
  fi
  # duplicate-stamp guard (flow-5fp): a sibling drain run can walk main to the SAME
  # version this branch stamped while we re-waited CI. Identical content on both
  # sides of the version line merges CLEAN, so the DIRTY branch below never fires
  # and the PR would land with NO version walk. RE-FETCH FIRST — the stamp's fetch
  # above is stale after the CI re-wait. Equal => re-stamp from fresh origin/main,
  # commit, push, re-wait CI on the new SHA (the same mandatory re-wait as the
  # stamp), then re-run THIS guard. Bounded to ~a stage timeout like the re-waits;
  # on the cap, leave the PR for the drain reap / human (STATUS=completed). The
  # guard also fires on a branch that never stamped but whose tree sits at main's
  # version — intended; the restamp is idempotent-correct (it writes the next
  # version either way).
  git fetch --quiet origin
  BRANCH_VER=$(python3 -c 'import json;print(json.load(open("plugins/flow/.claude-plugin/plugin.json"))["version"])')
  MAIN_VER=$(git show origin/main:plugins/flow/.claude-plugin/plugin.json \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["version"])')
  if [ "$BRANCH_VER" = "$MAIN_VER" ]; then
    python3 ${CLAUDE_SKILL_DIR}/scripts/version.py stamp --ref origin/main --cwd . \
      --commit-type "$COMMIT_TYPE"
    git commit -m "chore: stamp plugin version" -- \
      plugins/flow/.claude-plugin/plugin.json .claude-plugin/marketplace.json
    git push origin "$BRANCH"
    # ... bounded CI re-wait on the new SHA (same Monitor-bounded pattern as above);
    # heartbeat the run lease on each poll too (same refresh as the first re-wait,
    # flow-ztfv) so the restamp's re-wait does not let the lease go stale, then
    # REPEAT this guard from the `git fetch` ...
    RUN_ID=$(python3 -c "import json;print(json.load(open('$TICKET_DIR/state.json'))['run_id'])")
    python3 ${CLAUDE_SKILL_DIR}/scripts/lease.py refresh \
      --ticket-dir "$TICKET_DIR" --run-id "$RUN_ID" --ttl-seconds 1800
    RC=$?
    # same heartbeat check as the first re-wait, but this locus is nested inside the
    # restamp `if` (itself inside the duplicate-stamp guard's "REPEAT from git fetch"
    # loop): exit 7 (LeaseLost) / 3 (LeaseError) means the lease is no longer ours, so
    # STOP the ENTIRE §3 self-merge — not merely this inner `if`/loop. Do NOT fall
    # through to the MERGE_STATE read / merge / bd close / delete-branch below; leave
    # the live PR for the drain reap / human; do NOT release the lease (not ours).
    # Strengthens lease exclusivity, removes no guard (flow-tnfp).
    if [ "$RC" -ne 0 ]; then
      echo "run lease lost (exit $RC) — leaving PR #$PR_ID for the drain reap / human"   # STATUS=completed; STOP all of §3, no self-merge
    fi
  fi
  MERGE_STATE=$(gh pr view "$PR_ID" --json mergeStateStatus -q .mergeStateStatus)
  if [ "$MERGE_STATE" = "DIRTY" ]; then
    # version-conflict recovery (Option B). A multi-bead drain walks main's version
    # forward, so a sibling that merged first leaves this PR DIRTY on the version
    # line ONLY. version_remerge re-merges main + auto-resolves IFF the conflict is
    # exactly the two version files; any other conflict → it aborts and exits 3.
    REMERGE=$(python3 ${CLAUDE_SKILL_DIR}/scripts/version_remerge.py recover \
      --branch "$BRANCH" --workspace-root . --commit-type "$COMMIT_TYPE")
    RC=$?
    if [ "$RC" -eq 3 ]; then
      echo "non-version conflict — leaving PR #$PR_ID for the human"   # STATUS=completed; STOP, no self-merge
    elif [ "$RC" -eq 0 ]; then
      # the helper PUSHED a NEW SHA CI has NOT validated. RE-WAIT CI on it before
      # merging — this is MANDATORY and non-negotiable: it preserves the "merge ONLY
      # the CI-validated SHA" invariant. A textually-clean but semantically-wrong
      # auto-resolve the conflict detector structurally cannot see is caught here.
      # ↓ run the **Hardened CI re-wait probe** (defined at the end of §3) on $PR_ID /
      # $BRANCH; proceed to mark-ready+merge ONLY on a green break — every other terminal
      # (failed / PR-merged-or-closed / probe-error-budget / cap) leaves the PR for the
      # drain reap / human, do NOT hang.
      python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . mark-ready --pr "$PR_ID"   # if it was a draft
      python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . merge --pr "$PR_ID" --squash
      bd close "$KEY" --reason "self-merged via PR #$PR_ID (version-remerged)"
      python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . delete-branch --branch "$BRANCH"
    else
      echo "version_remerge tool error (exit $RC) — leaving PR #$PR_ID for the human"   # STATUS=completed
    fi
  else
    # CLEAN / DRAFT: merge as today, no recovery needed.
    python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . mark-ready --pr "$PR_ID"   # if it was a draft
    python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . merge --pr "$PR_ID" --squash
    bd close "$KEY" --reason "self-merged via PR #$PR_ID"
    python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . delete-branch --branch "$BRANCH"
  fi
fi
```

**Hardened CI re-wait probe.** Both re-wait sites above run this as a single `Monitor` (foreground `sleep` is blocked). It reads the `forge_cli ci-rollup` PROCESS EXIT CODE (not a piped status string), so an intermittently-erroring `gh` trips a consecutive-error budget and breaks instead of spinning; it short-circuits when the PR leaves the OPEN state (`detect-pr` returns `null` → merged/closed); and the iteration cap is the guaranteed terminal exit. §3 already binds `BRANCH` and `PR_ID`. Proceed past the re-wait ONLY on a `green` break — every other terminal (`failed` / PR-merged-or-closed / probe-error-budget / cap) leaves the PR for the drain reap / human, do NOT hang. Heartbeat the run lease on each poll (the `lease.py refresh` shown at each call site) so the long re-wait does not let the lease go stale.

```
Monitor(
  description="merge CI re-wait on PR #$PR_ID",
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

The terminal `CI_STATUS` enum is `green` / `failed` (NOT `success` / `failure`, NOT `red`); `ci_rollup` folds the superseded `CANCELLED`/`STALE`/`NEUTRAL`/`SKIPPED` entries into `pending`, so those re-poll rather than trip a false `failed`. **Anti-pattern:** never `ci-rollup ... 2>/dev/null | python -c '...get("status","pending")'` — piping past the exit code makes an errored `gh` read as `pending` forever (a silent infinite spin); the probe reads `$?` first, which is the fix.

The version stamp runs ONCE, at the top of the merge branch (after the push-state guard, before the merge-state read), because the merge point is the only place the version is well-timed: it is computed from the current `origin/main`, so the stamped number is correct relative to whatever siblings already merged. The bump is semantic, not always-patch: a `feat` commit type bumps MINOR (`X.(Y+1).0`), anything else bumps PATCH — the type comes from the ticket frontmatter's `commit_type` via `--commit-type`, with a HEAD-subject conventional-prefix fallback when that is empty. Only which field increments varies; the next-from-fresh-`origin/main` concurrency design is unchanged. The stamp pushes a new SHA, hence the bounded CI re-wait before reading `MERGE_STATE` — same invariant, same Monitor-bounded pattern as the `version_remerge` re-wait below; on the cap, leave the PR (`STATUS=completed`) rather than hang. `version_remerge` is RETAINED, not replaced: the stamp puts a version line back on the branch, so if main moves again during the re-wait the PR can still go DIRTY on the version line and the DIRTY branch's `version_remerge` recovery resolves it. The duplicate-stamp guard covers the OTHER way main can move during the re-wait: a sibling walking main to the SAME version this branch stamped merges CLEAN — identical content on both sides of the version line — so it is invisible to the `MERGE_STATE`-DIRTY recovery and would land a PR with no version walk (the live flow-5ba/PR#213 incident). The guard re-fetches and compares the branch's stamped version against fresh `origin/main` BEFORE `MERGE_STATE` is read; equal → re-stamp + push + the same mandatory CI re-wait, then the guard repeats. `version_remerge recover` carries the mirror-image check on its clean-merge path: after a clean re-merge it compares the working tree's version against main's and restamps on equality (`restamped` instead of `remerged_clean`); the mandatory exit-0 CI re-wait below covers `restamped` exactly like `remerged`.

The push-state check binds the merge to the CI'd SHA: `git rev-parse origin/$BRANCH` (after `git fetch origin $BRANCH`) is the last-pushed commit, so `HEAD == origin/$BRANCH` proves every local commit was pushed and therefore CI'd. A flow worktree never records upstream tracking — the shared `.git/config` write is sandbox-blocked, the same root cause flow-wjfs fixed for the push commands — so `@{u}` is empty there and the remote-tracking ref is the reliable pushed-SHA source. The merge-state branch then splits CLEAN/DRAFT (merge as today) from DIRTY (run version-conflict recovery). **The CI re-wait after a successful remerge is mandatory and non-negotiable:** `version_remerge` pushed a brand-new merge commit that CI never validated, so merging it without re-waiting would break the "merge ONLY the CI-validated SHA" invariant the push-state guard upholds. The conflict detector is structural (it checks the conflicting *paths*); only a green CI proves the auto-resolved merge is also semantically correct. On exit 3 (a non-version conflict) the helper already ran `git merge --abort`, so the working tree is clean and the PR stays ready for the human. The hot §2 guard-property review still runs FIRST for a hot bead (a hot bead reaches §3 only after a clean §2 review); recovery sits entirely within §3 and does not reorder that. The §2 review cleared the branch diff D; `version_remerge` then pushes D′ = D + main's content for the two version files, and D′ is merged WITHOUT a re-review. This is safe and needs no second §2 pass: the strict detector proves ONLY the two version files conflicted (any other conflicting path → abort), so D′ adds nothing to the guard surface beyond main's already-reviewed version bump — the guard-relevant diff is unchanged from what §2 saw. The CI re-wait does NOT substitute for this argument (guard properties have no CI test); the structural detector is what makes the skip sound. Close the bead and delete the **remote** branch only AFTER `merge` succeeds — a `bd close` on a PR that never merged would mint the exact PR↔bead inconsistency this guards against. The **local** worktree + branch are NOT torn down here: a run cannot remove the worktree it is standing in. Teardown is deferred to the drain reap step (`flow_worktree.py reap`, lease-gated), which reaps the worktree once this session exits.

`STATUS=completed` once the merge lands (or on a clean `skip`/`held_guard`). Only a tool failure on `merge` itself → `STATUS=failed`.

## Serialization note

No merge-lease is needed: `evolve_select` launches at most one `hot` bead per batch and skips a hot bead while another hot PR/branch is in flight, so two hot runs never reach this stage concurrently.
