# Self-evolution: flow improves and heals its own harness

This is the thesis, not a footnote. `/flow` is a self-evolving harness: it audits itself, files improvement tickets into its own backlog, works them autonomously to PRs, and auto-merges the safe ones — so the maintainer wakes to merged improvements. The same machinery also heals friction in-flight, while the context that produced it is still live.

Two halves: **producers** put evidence-backed work into one backlog; the **consumer** (`drain`) takes it from backlog to merged PR. Post-Layer-2 each launched run **self-merges its own green PR** in-session (the `merge` stage); `drain`'s reap step is the orphan safety-net for runs that died before self-merging. A human (or a green-CI gate + the in-run guard reviewer) keeps the keystone: what lands on `main`.

## The loop at a glance

```
   ┌─ Producer A: reflect sling ─┐
   │  (lived friction, in a run) │
   │                             ├─→  flow's OWN beads backlog  ─→  drain loop  ─→  (run self-merges)
   └─ Producer B: evolve audit ──┘     (evolve-labelled)        (evolve drain)         │
      (cold scan, on demand)                                          │          green + leaf
                                                              claude --bg     →  → self-merge in-run
                                                              /flow --auto        else → human merge
                                                              → create_pr → review_loop → merge
                                                                                  │
                                                                            drain reaps orphans
                                                                            (dead runs) + loops
```

This diagram is the **middle** loop. A `launchd` scheduler fires these producers + the `drain` consumer on a cadence, unattended — the nightly defect loop and the weekly epic loop. That outer loop and its runner live in `references/loop-engineering.md` (+ `ops/`); read it for how the loop runs on a clock rather than on demand.

Everything below is **maintainer-gated** (`maintainer.py`: the `[maintainer] self_target = true` marker in `.flow/workspace.toml`). A stranger running flow neither wants flow editing its own source nor cares about flow-internal findings — for them the whole loop is dormant.

## Producers — fill the backlog

**Producer A — the reflect sling (lived friction).** A run that hits a snag is the highest-fidelity judge of the harness that will ever exist for that snag. The do-loop logs friction in-flight (`flow_friction.py`); reflect lens-B reads the bundle (`reflect_inputs.py`), points the lens UP at the harness, and diagnoses at `file:line`. Two outlets:
- **In-place self-heal (fast path).** Surgical, high-confidence, strictly-correct fixes to flow's OWN `scripts/*.py` / `references/*.md` apply on the spot via `machinery_edit.py apply` (NOT the raw Edit tool — it flock-serializes read→replace→atomic-write so a fleet of concurrent reflect agents is safe, and refuses out-of-tree / snapshot-pinned paths). The apply edits the skill checkout's own working tree, so the fast path is live ONLY when that checkout is on a feature branch: bump the plugin version, commit the touched skill files on that branch, record the commit sha in a `MACHINERY:` knowledge entry. The churn-free premise (skill checkout is a separate tree from the run's worktree) holds in the normal marketplace-tracks-main setup and standalone `do`; in the self-target dogfood case where `${CLAUDE_SKILL_DIR}` coincides with the run's own worktree (`skill_root==worktree`, flow-alz/PR#111) the edit lands in the open PR and re-triggers CI + review, so it is not churn-free there (the snapshot caveat still keeps it drift-safe). NEVER commit a machinery fix to `main` — now enforced by code, not just prose: `machinery_edit.py` refuses (exit 2) any apply when skill-root is on a protected branch (main/master/dev/develop). In the normal marketplace-tracks-main setup the apply refuses and the finding routes to the bead → consumer (`drain`) → reviewed PR.
- **Sling to the backlog.** Anything too big / structural / not certain → `flow_beads_create.py` files a deduped `evolve` bead instead of editing. See `references/stage-reflect.md` (step 2b).

**Producer B — the evolve cold audit (on demand).** `/flow evolve audit` fans out read-only evidence miners over flow's own code (quality gates, test gaps, dead code, doc drift, friction history, robustness, seam), synthesizes findings with stable file-anchored ids, and files each as a deduped `evolve` bead. Read-then-file; it does not implement. See `references/verb-evolve.md` (§audit). The generative half, `/flow evolve propose`, mines the judgment-side work (features, real refactors) and files it as plain `proposal` beads (non-`evolve`); the consumer never sees them — the maintainer runs them via `/flow <key>`.

**The high-altitude generative producer — `/flow evolve epic` (weekly).** Above audit and propose sits the theme-altitude producer: web-reaching lenses surface work no single PR can hold (capability tracks, architecture-era shifts, the meta-loop itself), gated on *conviction* not track-record (engage if grounded by a web cite / witness / bounded spike; refute only change-for-change's-sake), then filed as a parent `epic` bead + a tree of `proposal` children — the gearing that feeds the per-ticket consumer at theme scale. Like propose's judgment half, it is **maintainer-lane**, not an auto-drain producer: the `epic`-typed parent is filtered out of `drain` for free (`evolve_select.py`'s `issue_type != "epic"`), so it never auto-launches, and the children run via `/flow <key>` at the maintainer's accept gate. See `references/verb-evolve.md` (§epic) and `references/loop-engineering.md` (the altitude axis + the weekly scheduler).

Audit findings and reflect-sling friction land in the **same backlog** (flow's OWN beads, `evolve`-labelled) and dedup through the same `--dedup-key` → `evid:<fingerprint>` seam, so a re-run never refiles open work nor re-proposes a closed/rejected finding. The generative-judgment half of propose is the exception: it lands in the plain (non-`evolve`) `proposal` backlog the maintainer runs by hand.

## Consumer — drain: backlog to merged, in one looping run (`/flow evolve drain`)

`evolve drain` is a single LOOP that drains the whole backlog. Each turn: (1) **reap** orphan green leaf PRs via `evolve_reap.py` (a healthy run self-merges, so this only catches runs that **died before self-merging** — green + leaf + mergeable → merge to `main` with `gh pr merge --squash`; hot / not-green / conflicted → left for the human) + tear down merged-and-exited worktrees (lease-gated); (2) **decide** via `evolve_drain.py`, which runs `evolve_select.py` (drops in-flight beads, enforces backpressure ≥ CAP open PRs, partitions best-effort coarse — ≤1 `hot` per batch, no two sharing a primary-file anchor) and annotates each in-flight bead with its run's **lease liveness**; (3) **act** — launch the batch (`claude --bg "/flow <key> --auto"`, each detached run branches off `--base @default`, implements, opens a PR, drives it green via `review_loop`, and **self-merges** via the `merge` stage), or **wait** while a blocking run (its lease reads `live` OR `corrupt`) settles, or — when nothing is startable and no run is blocking (none reads `live` or `corrupt`) — finish.

The termination is **blocking-gated**: a withheld hot bead (its in-run reviewer raised `held_guard`) leaves a ready PR but an expired/absent (non-blocking) lease, so it reads as `parked`, never `wait` — the loop cannot spin on it; it terminates and hands it to the human. The other blocking state is `corrupt` (run.lock unparseable, ownership unconfirmable), treated live-equivalent because an in-flight run that cannot be confirmed dead must never let the loop drain to done; a corrupt lease blocks until a human runs `recover takeover`. Hot beads drain **sequentially** (a live hot → wait → it self-merges → the next reap clears `hot_inflight` → the next hot launches). A bead that can't auto-plan at ≥90% confidence **defers** in place rather than guessing — intended.

Partition is best-effort, not a disjointness guarantee: planning is post-launch, so the selector never knows a run's real file set. Residual cross-run overlap surfaces as a merge conflict at review — each run is worktree/lease-isolated, so it's friction, never corruption. Keep CONCURRENCY low.

The keystone gate survives exactly where the risk is: leaf fixes flow through unattended (the run self-merges, or the janitor mops up an orphan); hot machinery, failing CI, and conflicts always wait for a human. Veto any auto-merge by converting its PR to draft (or closing it) before the next janitor pass.

## Inputs that feed the loop

- The **friction log** is Producer A's primary feedstock.
- The **prose↔CLI seam checker** (`scripts/seam_check.py`) is a self-heal input: a drift it flags (prose naming a flag/subcommand a script lacks) is exactly the class lens-B fixes.
- **CI** (ruff + ty + pytest + seam_check) on every PR the loop produces is the safety net that makes unattended auto-merge trustworthy — a bad change is caught before the run's self-merge (or the janitor) sees green.

## Guardrails (load-bearing — preserve exactly)

- **Maintainer-gated.** The entire loop is dormant outside the flow self-improvement target (the `[maintainer]` marker). User-mode runs never capture machinery friction nor enable `/flow evolve`.
- **machinery_edit flock + atomic write.** Cross-process serialization keeps a fleet safe. Do not route machinery fixes through the raw Edit tool.
- **Snapshot caveat.** Never self-edit `stage-registry.toml` or a WIRED handler skill mid-run — they are in the run's canonical snapshot, so editing them trips the drift guard and aborts the very run making the fix. Those go PROPOSE + RECORD, or apply then `/flow recover reload-snapshot`.
- **Reference-doc edits validate next run, not this one.** A stage editing its OWN `reference_doc` (the explicit self-modifying-stage case) is NOT drift-guarded — the edit applies cleanly to the worktree copy, commits, and merges. But the do-loop reads each inline/subagent stage's `reference_doc` from `${CLAUDE_SKILL_DIR}` (the installed checkout), so the run that authors the fix still executes the OLD prose; the fix only takes effect on the NEXT run after merge. Don't expect a mid-run prose fix to validate on the run that wrote it — it's deferred, not lost.
- **Never commit machinery to `main`.** In-flight self-edits commit on the run's own branch; everything else flows through a bead → consumer (`launch`) → PR → merge. A background process landing straight on `main` bypasses the keystone gate.
- **Human-merge keystone.** Only green + leaf PRs auto-merge. Hot / non-green / conflicted always wait for a human. The auto-merge envelope is deliberately narrow — widen it only with eyes open.
- **Fresh base for autonomous runs.** `--base @default`, never the launcher's branch (see §drain).

## Where the mechanics live

- `references/verb-evolve.md` — the `evolve` namespace: producers (§audit, §propose) + consumer (§drain).
- `references/stage-reflect.md` — reflect lens-B protocol + the sling.
- `MODULE.md` — `evolve_select.py`, `evolve_reap.py`, `create_pr.py`, `flow_beads_create.py`, `machinery_edit.py`, `maintainer.py`.
