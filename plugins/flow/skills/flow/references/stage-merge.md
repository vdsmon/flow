# merge stage (inline, evolve self-target only)

The terminal self-merge stage. Runs **after `reflect`**, so every output of the run is committed before the PR lands. Default handler is `none` (the generic pipeline and every user project skip it — the human keystone holds); flow's own self-target workspace wires `merge = "inline"`. This is **Layer 2** of the evolve restructure: an evolve run that reached green + review-clean (`review_loop`) merges its own PR instead of waiting for a deferred drain pass.

A `hot`/guard PR self-merges **only after an independent reviewer subagent clears the §2 guard-property check** — the run that wrote the diff is never the sole judge of whether it removed a safety property.

Merge itself remains a deterministic tool stage. The independent reviewer has no
write or merge authority.

The mechanical plumbing (eligibility probe, CI re-read, harness eval, main-CI health, the self-merge gate call, the §3 push-state guard, the squash merge, bead close + covers) lives in `scripts/stage_merge.py`, shelling `evolve_self_merge.py` / `main_ci_health.py` / `harness_eval.py` / `forge_cli.py` as subprocesses so the decision code stays byte-identical to what those scripts always did. This doc stays judgment-only: the two `stage_merge.py` calls, the branch on their verdict, and the §2 independent guard review that runs between them.

## 1. Probe

```bash
PROBE=$(FLOW_HARNESS="<harness>" "<facade>" merge probe \
  --workspace-root . --ticket-dir "$TICKET_DIR" --key "$KEY")
ALREADY_MERGED=$(printf '%s' "$PROBE" | python3 -c 'import sys,json;print(json.load(sys.stdin)["already_merged"])')
PR_ID=$(printf '%s' "$PROBE" | python3 -c 'import sys,json;print(json.load(sys.stdin)["pr_id"])')
ACTION=$(printf '%s' "$PROBE" | python3 -c 'import sys,json;print(json.load(sys.stdin)["action"])')
IS_HOT=$(printf '%s' "$PROBE" | python3 -c 'import sys,json;print(json.load(sys.stdin)["is_hot"])')
```

`$PROBE` is the verdict JSON: `{already_merged, pr_id, action, is_hot, reason, ci_status, eval_status, regressed_cases, changed_files, guard_diff_path, review_brief_status, review_brief_reason, review_brief_path}`.

`probe` does the whole read side internally, with no merge/close side effects: it parses `PR_ID` off `create_pr.out`'s `PR_URL=` line; checks the PR's actual merge state first (`already_merged: true` short-circuits everything below — a `hot` leaf PR can auto-merge via the evolve janitor before this run's own merge stage runs, and the eligibility gate does not itself read PR merge state, so this check keeps a merged PR from tripping the §3 push-state guard once origin has deleted the branch; scoped to `MERGED` only — a `CLOSED`-not-merged PR is NOT handled here, it falls through to the ordinary flow); verifies the configured review brief against local HEAD and the PR head before any other merge gate; re-reads CI through the forge seam (a non-green/non-zero read surfaces as `ci_status`, never silently read as pending forever); reads the PR's changed-file list once (feeding both the harness-eval trigger and the gate's observed-diff hotness input); replays the frozen decider corpus (`harness_eval.py score`) when the diff touches the engine's own scripts (the Self-Harness no-degradation rule — an `eval_status` other than `pass` blocks the merge; `stages/harness_eval.json` is written for the maintainer to inspect); probes main's own CI health (asymmetric: only `failed` pauses this turn, a probe `error` resumes); and asks `evolve_self_merge.py` for the merge/skip verdict. When the verdict is `is_hot && action == "merge"`, the full PR diff is written to `stages/merge_guard_diff.txt` (`guard_diff_path` in the verdict) for §2 below.

Branch on the verdict:

- **`already_merged` true** → nothing to do. Run `execute --already-merged` (§3 below) to close the bead + any covers, then `STATUS=completed`. STOP — skip everything else in this doc.
- **`action` `"refresh_review_brief"`** → do not merge and do not mark this stage complete. Surface `review_brief_reason`. If the reason says local and PR heads differ, use the existing authorized publish path first; never make the freshness probe push implicitly. Then repeat `references/stage-review_brief.md`'s evidence-authoring and render steps at the new SHA and rerun this probe. This refresh is mechanical continuation, not a wait for human approval. Cap it at one refresh per merge attempt; a second mismatch is `STATUS=failed` with both SHAs so workspace repair can retry deliberately.
- **`action` `"skip"`** → leave the PR as-is for the human (the normal outcome on a user project, and for a held hot ticket), `STATUS=completed`. Done. An unattended owner records this as a parked green PR in durable evidence; a later drain distinguishes it from an orphan without consulting host session state. On an eval-driven skip (`eval_status` is `regressed`/`error`), first post a PR comment naming `regressed_cases` from the verdict (mirrors §2's `held_guard` pattern) so the maintainer sees WHICH frozen cases moved.
- **`action` `"merge"`, `is_hot` false** → skip straight to §3 (Execute).
- **`action` `"merge"`, `is_hot` true** → run §2 first; only a clean review proceeds to §3.

## 2. Independent guard-property review (hot beads only)

A `hot` bead touches a guard or safety-machinery file. Spawn one fresh native reviewer
that is not the agent that wrote the change, using the rooted agent contract in
`references/delivery-loop.md`, and prompt it to refute:

> Review this PR diff for the evolve self-target. Question: does it DELETE or WEAKEN any safety property — lease exclusivity (one run per ticket), snapshot drift-detection, atomic-write + corrupt-file quarantine, content-ownership refusal, or self-edit flock serialization? Guard *code* may be refactored/sped up freely; a guard *property* may only be replaced by a provably-equivalent one, never dropped. Default to "property removed" when uncertain. Return a verdict: `{property_removed: bool, which: str, why: str}`. Diff:
> ```
> <the file at guard_diff_path from the probe verdict>
> ```

If the reviewer reports `property_removed: true` → **do NOT merge.** Post a PR comment naming the property, report it under `held_guard` in the stage output, and `STATUS=completed` (the PR stays ready for the maintainer). Only a clean review (`property_removed: false`) proceeds to §3.

## 3. Execute

```bash
FLOW_HARNESS="<harness>" "<facade>" merge execute \
  --workspace-root . --pr "$PR_ID" --key "$KEY"
```

(`--pr` is the `pr_id` read from the probe verdict in §1; add `--already-merged` for the already-merged branch instead.)

`execute` does the whole write side: rebuilds the §3 push-state guard (fetches the run's branch, then skips — leaving the PR for the drain reap / human — when a TRACKED file has an uncommitted change, there is an unpushed commit, or the remote branch was already deleted by the reap; **untracked files never count**, so the run's own scratch never blocks a self-merge); reads `mergeStateStatus` and leaves a `DIRTY` PR for the human (`gh pr merge` would refuse it — branches carry no version line, so a DIRTY here is a genuine code conflict); on `CLEAN`/`DRAFT`, marks the PR ready (if it was a draft) and squash-merges through the forge seam; and, ONLY once the merge tool itself reports success, closes the bead, closes any covers (see Cover-close below — `execute` runs this automatically, at both the already-merged branch and the merge branch), and deletes the **remote** branch. A merge-tool failure closes nothing and reports `STATUS=failed`; a post-merge close/cover/delete-branch hiccup is best-effort (warned, `STATUS=completed` still holds — the diff is already merged, the bead close is not what makes it safe).

The self-merge does NOT stamp the version. It merges like a human merge — touching no version files — and the server-side `version-stamp.yml` GitHub Action stamps `main` after the squash lands.

The **local** worktree + branch are NOT torn down by this stage — a run cannot remove the worktree it is standing in. Teardown is deferred to the drain reap step (`flow_worktree.py reap`, lease-gated), which reaps the worktree once this session exits.

`STATUS=completed` once the merge lands (or on a clean `skip`/`held_guard`/already-merged). Only a merge-tool failure → `STATUS=failed`.

## Cover-close (grouped runs only)

When this run grouped sibling tickets (`FLOW <KEY> <c1> <c2> --unattended --together`), the lead's self-merge must close the covers too — symmetric to the lead close, so a grouped cover does not re-surface in `bd ready` next drain turn. `execute` does this automatically, at both the already-merged branch and the main merge branch: it reads `covers` off `.flow/tickets/<KEY>.md` frontmatter and, per cover, comments + transitions it to `closed` through the `tracker_cli.py` seam (tracker-agnostic — the same call routes to Jira's done state) and drops the `bd dep` suppression edge. Best-effort, mirroring the lead close: a cover hiccup is a warning, never a stage failure — the lead is the source of truth, the diff is already merged. Absent/empty `covers` closes nothing extra.

## Serialization note

No merge-lease is needed: `evolve_select` launches at most one `hot` bead per batch and skips a hot bead while another hot PR/branch is in flight, so two hot runs never reach this stage concurrently.
