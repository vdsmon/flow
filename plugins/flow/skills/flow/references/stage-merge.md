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
  # close any covered beads this folded run co-delivered (see §Cover-close below)
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
  MERGE_STATE=$(gh pr view "$PR_ID" --json mergeStateStatus -q .mergeStateStatus)
  if [ "$MERGE_STATE" = "DIRTY" ]; then
    echo "PR #$PR_ID is DIRTY (merge conflict) — leaving for the human"
    # STATUS=completed; STOP. No self-merge: gh pr merge refuses a DIRTY PR.
    # Branches no longer carry a version line (server-side version-stamp.yml stamps
    # main post-merge), so a DIRTY here is a genuine code conflict. The drain reap
    # routes it to `blocked`.
  else
    # CLEAN / DRAFT: merge.
    python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . mark-ready --pr "$PR_ID"   # if it was a draft
    python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . merge --pr "$PR_ID" --squash
    bd close "$KEY" --reason "self-merged via PR #$PR_ID"
    # close any covered beads this folded run co-delivered (see §Cover-close below)
    python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . delete-branch --branch "$BRANCH"
  fi
fi
```

The self-merge does NOT stamp the version. It merges like a human merge — touching no version files — and the server-side `version-stamp.yml` GitHub Action stamps `main` after the squash lands. The Action is skip-guarded (it skips when the merged push already changed the two version files), so a flow self-merge, which never touches them, triggers the Action's stamp path. Branches carry no version line, so they never conflict on one: a DIRTY here is a genuine code conflict left for the human.

The push-state check binds the merge to the CI'd SHA: `git rev-parse origin/$BRANCH` (after `git fetch origin $BRANCH`) is the last-pushed commit, so `HEAD == origin/$BRANCH` proves every local commit was pushed and therefore CI'd. A flow worktree never records upstream tracking — the shared `.git/config` write is sandbox-blocked, the same root cause flow-wjfs fixed for the push commands — so `@{u}` is empty there and the remote-tracking ref is the reliable pushed-SHA source. The merge-state branch then splits CLEAN/DRAFT (merge) from DIRTY (leave for the human). Close the bead and delete the **remote** branch only AFTER `merge` succeeds — a `bd close` on a PR that never merged would mint the exact PR↔bead inconsistency this guards against. The **local** worktree + branch are NOT torn down here: a run cannot remove the worktree it is standing in. Teardown is deferred to the drain reap step (`flow_worktree.py reap`, lease-gated), which reaps the worktree once this session exits.

`STATUS=completed` once the merge lands (or on a clean `skip`/`held_guard`). Only a tool failure on `merge` itself → `STATUS=failed`.

## Cover-close (grouped runs only)

When this run folded sibling beads (`/flow <KEY> --auto --covers <c1,c2>`, the §drain group-fold in `verb-evolve.md`), the lead's self-merge must close the covers too — symmetric to the lead close, so a folded cover does not re-surface in `bd ready` next drain turn. Run this AT BOTH lead-close points above: the already-merged short-circuit (after `bd close "$KEY" ...`, §1) and the main merge path (after `bd close "$KEY" --reason "self-merged ..."`, §3). The run is standing in its own worktree, so read the covers from frontmatter:

```bash
COVERS=$(python3 ${CLAUDE_SKILL_DIR}/scripts/ticket_frontmatter.py read .flow/tickets/"$KEY".md \
  | python3 -c 'import sys,json;print("\n".join(json.load(sys.stdin).get("covers") or []))')
for COVER in $COVERS; do
  python3 ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . \
    comment --key "$COVER" --text "co-delivered by $KEY via PR #$PR_ID"
  python3 ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . \
    transition --key "$COVER" --to-state closed
  bd dep remove "$COVER" "$KEY" || true   # drop the §drain suppression dep (beads-only; harmless if absent)
done
```

Best-effort, mirroring the lead close: a cover comment/transition that hiccups is a warning, never a stage failure (the lead is the source of truth, the diff is already merged). The close goes through the `tracker_cli.py` seam (not raw `bd close`) so it is tracker-agnostic — `*→closed` is a valid bead transition and the same call routes to jira's done state. `ticket_frontmatter.py read` takes the ticket-file path positionally (NOT `--ticket`), matching `stage-commit.md`'s covers fan-out. Absent/empty `covers` → the loop runs zero times (a normal single-ticket run closes nothing extra).

## Serialization note

No merge-lease is needed: `evolve_select` launches at most one `hot` bead per batch and skips a hot bead while another hot PR/branch is in flight, so two hot runs never reach this stage concurrently.
