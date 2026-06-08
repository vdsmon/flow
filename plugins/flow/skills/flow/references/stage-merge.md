# merge stage (inline, evolve self-target only)

The terminal self-merge stage. Runs **after `reflect`**, so every output of the run is committed before the PR lands. Default handler is `none` (the generic pipeline and every user project skip it — the human keystone holds); flow's own self-target workspace wires `merge = "inline"`. This is **Layer 2** of the evolve restructure: an evolve run that reached green + review-clean (`review_loop`) merges its own PR instead of waiting for a deferred drain pass.

A `hot`/guard PR self-merges **only after an independent reviewer subagent clears the §6A guard-property check** — the run that wrote the diff is never the sole judge of whether it removed a safety property.

## 1. Inputs + eligibility gate

Read the PR opened by `create_pr`:

```bash
PR_URL=$(grep -oE '^PR_URL=.*' "$TICKET_DIR/stages/create_pr.out" | head -1 | cut -d= -f2-)
PR_ID=$(printf '%s' "$PR_URL" | grep -oE '[0-9]+$')
```

**Already-merged short-circuit.** Before re-reading CI or asking the gate, check the PR's actual merge state — a `hot` leaf PR can auto-merge (via the evolve janitor) before this run's own merge stage runs, and the eligibility gate below does NOT read PR merge state (it decides from CI-green + self-target + evolve-bead + hot-policy), so an already-MERGED PR with still-green CI would return `action: "merge"`, burn a §2 guard review on a merged PR, then trip §3's `@{u}` fatal once origin has deleted the branch. The check sits here, right after PR_ID is in hand, so it short-circuits all of that. Read the state with raw `gh` (the established precedent for this inline, self-target-only GitHub stage — §2 already uses `gh pr diff`; `forge_cli detect-pr` filters `--state open` and cannot see an already-MERGED known PR_ID):

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

Ask the pure gate whether this run may self-merge:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/evolve_self_merge.py \
  --workspace-root . --key "$KEY" --ci-status "$CI"
```

Returns `{"action": "merge"|"skip", "is_hot": bool, "reason": "..."}`. The gate skips when this is not the maintainer self-target, not an `evolve` bead, CI is not green, or a `hot` bead while `[evolve] auto_merge_hot` is off.

- **`action: "skip"`** → leave the PR as-is for the human (this is the normal outcome on a user project and for held hot beads), `STATUS=completed`. Done.
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
if [ -n "$(git status --porcelain --untracked-files=no)" ] || [ "$(git rev-parse HEAD)" != "$(git rev-parse @{u} 2>/dev/null)" ]; then
  echo "branch has uncommitted (tracked) or unpushed changes CI never validated — skipping self-merge"
  # STATUS=completed; the deferred drain reap (or the human) merges once state settles.
else
  python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . mark-ready --pr "$PR_ID"   # if it was a draft
  python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . merge --pr "$PR_ID" --squash
  bd close "$KEY" --reason "self-merged via PR #$PR_ID"
  python3 ${CLAUDE_SKILL_DIR}/scripts/forge_cli.py --workspace-root . delete-branch --branch "$BRANCH"
fi
```

The push-state check binds the merge to the CI'd SHA: `git rev-parse @{u}` is the last-pushed commit, so `HEAD == @{u}` proves every local commit was pushed and therefore CI'd. Close the bead and delete the **remote** branch only AFTER `merge` succeeds — a `bd close` on a PR that never merged would mint the exact PR↔bead inconsistency this guards against. The **local** worktree + branch are NOT torn down here: a run cannot remove the worktree it is standing in. Teardown is deferred to the drain reap step (`flow_worktree.py reap`, lease-gated), which reaps the worktree once this session exits.

`STATUS=completed` once the merge lands (or on a clean `skip`/`held_guard`). Only a tool failure on `merge` itself → `STATUS=failed`.

## Serialization note

No merge-lease is needed: `evolve_select` launches at most one `hot` bead per batch and skips a hot bead while another hot PR/branch is in flight, so two hot runs never reach this stage concurrently.
