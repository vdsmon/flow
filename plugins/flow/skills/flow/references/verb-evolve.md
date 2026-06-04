# evolve verb

`/flow evolve`. Maintainer-only. Routed from SKILL.md's argument table. Two halves of the self-evolution loop:

- **`/flow evolve`** (no flag) — the cold-audit **producer**: scan flow's OWN codebase for evidence-backed improvements and file them as beads in flow's backlog (sections 1-5). Read-then-file; it does not implement.
- **`/flow evolve --ship`** — the **drainer** (consumer, section 6): auto-merge green leaf PRs from prior launches, then fan out the next batch of beads as background `/flow <key> --auto` runs. `--reap` does the merge half only; `--dry-run` prints both plans and acts on neither.

This is **Producer B**. Producer A is the reflect sling (`references/stage-reflect.md`): lived friction during real runs. Both land in the same `evolve`-labelled backlog, both dedup through the same `--dedup-key` seam.

## 1. Gate — maintainer only

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/maintainer.py --workspace-root .
```

- Exit 0 → prints the flow repo root; you are the maintainer, continue. Run the audit against that repo.
- Exit 1 → not a maintainer setup (no `[maintainer]` marker). Print: "`/flow evolve` is maintainer-only; this workspace is not the flow self-improvement target." Stop. Do NOT audit a user's project.

## 2. Audit — fan out evidence miners (read-only)

Spawn parallel read-only audit agents (the `Agent` tool with `Explore` / `general-purpose`, or a `Workflow` fan-out when available), one per evidence source. Every finding MUST cite concrete evidence — a `file:line` or a reproduced command — or it is not a candidate. No "could be cleaner". Mine, at least:

- **quality gates** — run `mise run lint`, `mise run test`, `python3 seam_check.py` from `scripts/`; every real failure / warning / lint-suppression is a finding.
- **test gaps** — public functions / branches with no test (use `MODULE.md` to map script → test).
- **dead code & complexity** — unused defs (prove zero refs), very long / tangled functions.
- **doc drift** — `MODULE.md` / `inventory.md` / `SKILL.md` / `references/*.md` claims vs the actual code.
- **friction & history** — unaddressed `MACHINERY:` entries in `knowledge.jsonl`, `TODO`/`FIXME`, recent git-log pain.
- **robustness** — real gaps in the load-bearing machinery (run lease, snapshot TOCTOU, atomic writes, ownership gate, flock). Tighten, never erode.
- **architecture / seam** — SKILL.md thinness, registry↔reference-doc consistency, prose↔CLI seam risks.

## 3. Synthesize, rank, assign stable ids

Dedup the raw findings (merge ones about the same root issue), drop the vague / unevidenced. Rank by evidence strength × value × blast-radius-safety × reviewability — prefer small, isolated, high-evidence items. Give each survivor a **stable identity anchored on its primary file path** plus a short symptom — `<primary-relfile>::<short-symptom>`, e.g. `scripts/diff_extract.py::quotepath-parsing`. Anchor on the file, NOT free wording: the file path is the invariant a re-run will rediscover, so it is what makes the same defect dedup across runs (the seam fingerprints it, so exact formatting does not matter). Keep the `::` separator: the file component (its basename) now also anchors a fuzzy same-file dedup pass, so a re-discovery phrased differently still converges. Flag `hot` if it touches `SKILL.md` / `stage-registry.toml` / `CLAUDE.md` / a wired handler.

## 4. File each candidate (dedup through the seam)

For each candidate, file it into flow's beads. The `--dedup-key` is the stable `id`; it stops refiling open work AND re-proposing findings already closed or rejected, so the loop converges:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/flow_beads_create.py \
  --workspace-root . \
  --summary "<finding title>" \
  --description "<evidence (file:line / repro) + value + blast radius>" \
  --type <bug|chore|task> --labels evolve,audit \
  --dedup-key "<primary-relfile>::<short-symptom>"
```

The `--dedup-key` is reduced to a deterministic `evid:` fingerprint, so re-runs that phrase the same defect differently still collide on the same key.
- Exit 0 → filed; prints the new bead key.
- Exit 5 → a bead for this fingerprint already exists (open or closed); prints that key. Skip — do NOT refile. This is the normal converged path on a re-run.
- Exit 4 → not maintainer (should not happen after step 1's gate). Exit 2 → bd error; report and continue.

## 5. Report

Summarise: candidates found, filed (with keys), skipped-as-duplicate, dropped-as-noise. Be honest if the audit found little — a quiet run as the easy wins drain is success (the loop is self-limiting), not failure. Do not manufacture findings to fill the report.

The user reviews the backlog (`bd ready --label evolve`) and ships from it — or runs `/flow evolve --ship` (section 6) to drain it autonomously.

## 6. `--ship` / `--reap`: drain the backlog (the consumer)

Maintainer-gated like the rest (section 1 already ran). The drainer reaps first (merge prior-launch green leaves), then launches the next batch — so repeated `--ship` calls self-pace: each pass clears finished work and starts more. `--reap` runs only the reap half; `--dry-run` prints both plans and changes nothing.

### A. Reap — auto-merge green leaf PRs

Green LEAF evolve PRs merge to the default branch unattended (immediate on green). Non-green and conflicted PRs always wait as draft PRs for the human — the gate survives where the risk is. Hot PRs auto-merge ONLY under `[evolve] auto_merge_hot` (default off; on solely in this maintainer self-target repo) AND isolation: at most one hot PR merges per pass, and the fleet must be quiesced around the pass. Off / non-maintainer keeps today's behavior (hot → `skipped_hot`). Note: the code (`classify`) enforces only the one-hot-per-pass serialization; ensuring no other evolve run is active (quiescing the fleet) before an auto-merge pass is the operator's responsibility.

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/evolve_reap.py --workspace-root .
```

Returns JSON `{merge:[{pr,key,is_draft}], not_green, skipped_hot, blocked}`. For each `merge` entry (skip all of this under `--dry-run`):

```bash
# mark ready only if it was a draft, then squash-merge and delete the branch
gh pr ready <pr>        # only when is_draft is true
# close the bead ONLY if the merge succeeds (chained with && so a refused merge leaves the bead open)
gh pr merge <pr> --squash --delete-branch && bd close <key> --reason "merged via PR #<pr>"
# only after a successful merge, reap the leaked local branch + worktree
python3 ${CLAUDE_SKILL_DIR}/scripts/flow_worktree.py reap --ticket <key> --branch <branch> --main-root .
```

`<key>`, `<pr>`, and `<branch>` (== `headRefName`) all come from the `merge` entry. `--delete-branch` deletes only the REMOTE branch; the local `feature/<key>-*` branch and its registered worktree under `flow.worktrees/` leak (the branch is "checked out" by the still-registered worktree), so `gh pr merge` even prints its own `failed to delete local branch` warning (cosmetic — it tries the local delete before this cleanup runs). The `reap` step removes both. It is lease-gated: a worktree whose bg session is still running (typically the reflect stage, which runs after the PR is green) is SKIPPED and reaped on a later `--ship` / `--reap` pass once the session ends. Run reap only when the merge succeeded — keep it after the `&&`-gated merge line (a refused merge leaves the worktree in use). The `bd close` runs ONLY when `gh pr merge` exits clean — gate it with `&&` (or an `if`), never as an unconditional third line. `gh pr merge` refuses a not-actually-mergeable PR, so it is a safe backstop if state changed since the classify; if it refuses, the `&&` short-circuits and the bead stays open. Closing a bead whose PR never merged would mint the exact PR↔bead state-inconsistency this step exists to prevent.

`bd close` here autodiscovers `.beads/*.db` from cwd, and section 6 is maintainer-gated with no `cd` in the loop, so the close inherits the maintainer-repo cwd and hits flow's own DB. With the close wired in, reaping a PR also closes its bead, so `--ship` / `--reap` leaves no merged-but-open beads behind. Veto for the human: convert a PR to draft or close it before the next pass and the reaper skips it.

### B. Select + launch — fan out the next batch

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/evolve_select.py --workspace-root .
```

Returns JSON `{launch:[keys], skipped_in_flight, held_backpressure, held_hot, held_anchor, cap, concurrency, open_pr_count}`. Selection is already DAG-aware (`bd ready` excludes blocked beads), drops in-flight beads (open branch/PR), enforces backpressure (≥ `cap` open PRs → empty launch), and partitions coarsely (≤1 hot per batch; no two beads sharing a primary-file anchor). For each `launch` key (under `--dry-run`, print the command instead of running it):

```bash
claude --bg "/flow <key> --auto"
```

Each spawns a detached run that auto-plans and either opens a draft PR or, when it cannot self-approve at ≥90% confidence, **defers** its bead in place (status → `deferred`, open questions commented) and exits. A deferred bead drops out of `bd ready`, so the drainer stops relaunching it. Defer-and-exit is the intended unattended outcome, not a failure.

### C. Report

Summarise: merged (keys), launched (keys), and everything held — `skipped_in_flight`, `held_backpressure`, `held_hot`, `held_anchor`, `not_green`, `blocked`, `skipped_hot`. Tell the user how to follow along:

- Monitor with `claude agents --json` (the plain `claude agents` needs a TTY).
- Review any **deferred** beads: the run commented its open questions before exiting. `deferred` != done, so to unstick, answer the comment, reopen the bead (status → `open`), and re-run it interactively (WITHOUT `--auto`).
- Remaining draft PRs (non-green / conflicted, plus any `skipped_hot` not auto-merged this pass) are theirs to review and merge.

Expect defers, not all PRs: terse audit beads will sometimes score under 90% or raise questions. A high defer rate is a signal the audit evidence needs to be richer (a finding for the miners in section 2), not a drainer bug.
