# evolve verb

`/flow evolve <sub-verb>`. Maintainer-only. Routed from SKILL.md's argument table. `evolve` is a **namespace**: the self-evolution loop's producers + consumers, one sub-verb each.

- **`/flow evolve audit`** — the cold-audit **producer** (§audit): scan flow's OWN codebase for evidence-backed improvements and file them as `audit` beads in flow's backlog. Read-then-file; it does not implement.
- **`/flow evolve propose`** — the multi-angle **proposal producer** (§propose): fan out one agent per generative angle (feature gaps, simplification, reorg, dead-weight, architecture, symmetry), adversarially verify, and file a ranked set. Provably-safe findings become auto-drainable `audit` beads; judgment findings become plain `proposal` beads (non-`evolve`) in the maintainer's own backlog, run via `/flow <key>`. Read-then-file; it does not implement.
- **`/flow evolve drain`** — the **consumer** (§drain): a single looping pass that drains the whole backlog. Each turn it reaps finished orphans (merge the green leaf PRs of runs that died before self-merging + teardown merged-and-exited worktrees, lease-gated), then fans out the next launchable batch as background `/flow <key> --auto` runs (each run self-merges its own green PR via the `merge` stage, post-Layer-2). It loops — launching, waiting while runs are live, reaping — until nothing is startable, draining hot beads sequentially. This is the nightly loop's consumer.

The producers are **Producer B** (cold-audit + generative). Producer A is the reflect sling (`references/stage-reflect.md`): lived friction during real runs. The cold-audit and provably-safe generative findings land in the same `evolve`-labelled backlog and dedup through the same `--dedup-key` seam; the consumer auto-ships them. The producers differ in disposition: judgment generative findings are instead filed as plain `proposal` beads (non-`evolve`) in the maintainer's own backlog — drain never sees them; the maintainer runs them via `/flow <key>` (the vision's auto-vs-propose line is now a backlog split, not a label filter).

## 0. Dispatch

Match the **second whitespace token** of the args against the sub-verb set by exact string equality:

- `audit` → §audit. `propose` → §propose. `drain` → §drain.
- **empty** (bare `/flow evolve`, no sub-verb) → print the sub-verb listing above and stop. Do NOT default to a sub-verb; the namespace is explicit.
- **anything else** (unknown sub-verb) → print the listing + "unknown evolve sub-verb: `<token>`" and stop.

**`--dry-run`** is a modifier on `drain`: run ONE turn's reap + select classification and print the plan (what it would reap + launch), act on nothing. It is ignored on the read-only producers (`audit` / `propose` already change no live state).

**`--include-proposals`** is a DANGEROUS modifier on `drain`: it widens the loop to auto-drain plain `proposal` beads too, bypassing the human spec-plan accept gate (§`--include-proposals` below). Off by default; also ignored on the producers.

Every sub-verb runs the **Gate** below first.

## Gate — maintainer only

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/maintainer.py --workspace-root .
```

- Exit 0 → prints the flow repo root; you are the maintainer, continue with the dispatched sub-verb. Run against that repo.
- Exit 1 → not a maintainer setup (no `[maintainer]` marker). Print: "`/flow evolve` is maintainer-only; this workspace is not the flow self-improvement target." Stop. Do NOT audit or drain a user's project.

---

## audit

The cold-audit producer. Mine flow's own codebase for evidence-backed improvements and file them as `audit` beads.

### 1. Fan out evidence miners (read-only)

Spawn parallel read-only audit agents (the `Agent` tool with `Explore` / `general-purpose`, or a `Workflow` fan-out when available), one per evidence source. Every finding MUST cite concrete evidence — a `file:line` or a reproduced command — or it is not a candidate. No "could be cleaner". Mine, at least:

- **quality gates** — run `mise run lint`, `mise run test`, `python3 seam_check.py` from `scripts/`; every real failure / warning / lint-suppression is a finding.
- **test gaps** — public functions / branches with no test (use `MODULE.md` to map script → test).
- **dead code & complexity** — unused defs (prove zero refs), very long / tangled functions.
- **doc drift** — `MODULE.md` / `inventory.md` / `SKILL.md` / `references/*.md` claims vs the actual code.
- **friction & history** — unaddressed `MACHINERY:` entries in `knowledge.jsonl`, `TODO`/`FIXME`, recent git-log pain.
- **robustness** — real gaps in the load-bearing machinery (run lease, snapshot TOCTOU, atomic writes, ownership gate, flock). Tighten, never erode.
- **architecture / seam** — SKILL.md thinness, registry↔reference-doc consistency, prose↔CLI seam risks.

### 2. Synthesize, rank, assign stable ids

Dedup the raw findings (merge ones about the same root issue), drop the vague / unevidenced. Rank by evidence strength × value × blast-radius-safety × reviewability, then score each survivor against the repo-root `VISION.md` (serves the thesis / on the right side of the auto-vs-propose line / does not erode the floor — a candidate that cannot be anchored there is slop: drop it or escalate it as a question). Prefer small, isolated, high-evidence items. Give each survivor a **stable identity anchored on its primary file path** plus a short symptom — `<primary-relfile>::<short-symptom>`, e.g. `scripts/diff_extract.py::quotepath-parsing`. Anchor on the file, NOT free wording: the file path is the invariant a re-run will rediscover, so it is what makes the same defect dedup across runs (the seam fingerprints it, so exact formatting does not matter). Keep the `::` separator: the file component (its basename) now also anchors a fuzzy same-file dedup pass, so a re-discovery phrased differently still converges. Flag `hot` if it touches `SKILL.md` / `stage-registry.toml` / `CLAUDE.md` / a wired handler, OR a safety-machinery guard file (`lease.py`, `snapshot.py`, `_atomicio.py`, `_locking.py`, `state.py`, `dispatch_stage.py`, `diff_extract.py`, `machinery_edit.py`, `flow_friction.py`): a guard change must ride the hot path so the guard-property review gates it (the in-run merge reviewer when the run self-merges, or the §drain reap guard-property-check for an orphan).

### 3. File each candidate (dedup through the seam)

For each candidate, file it into flow's beads. The `--dedup-key` is the stable `id`; it stops refiling open work AND re-proposing findings already closed or rejected, so the loop converges:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/flow_beads_create.py \
  --workspace-root . \
  --summary "<finding title>" \
  --description "<evidence (file:line / repro) + value + blast radius>" \
  --type <bug|chore|task> --labels evolve \
  --dedup-key "<primary-relfile>::<short-symptom>"
```

The `--dedup-key` is reduced to a deterministic `evid:` fingerprint, so re-runs that phrase the same defect differently still collide on the same key.
- Exit 0 → filed; prints the new bead key.
- Exit 5 → a bead for this fingerprint already exists (open or closed); prints that key. Skip — do NOT refile. This is the normal converged path on a re-run.
- Exit 4 → not maintainer (should not happen after the Gate). Exit 2 → bd error; report and continue.

### 4. Report

Summarise: candidates found, filed (with keys), skipped-as-duplicate, dropped-as-noise. Be honest if the audit found little — a quiet run as the easy wins drain is success (the loop is self-limiting), not failure. Do not manufacture findings to fill the report.

The user reviews the backlog (`bd ready --label evolve`) and ships from it — or runs `/flow evolve drain` to drain it autonomously.

---

## drain

The consumer. A single LOOP that drains the whole backlog: each turn reaps finished orphans, launches the next startable batch, then waits while runs are live — repeating until nothing is startable. Post-Layer-2 each launched run self-merges its own green PR in-session (the `merge` stage, `references/stage-merge.md`), so `drain` does not merge live work itself; its reap step is the orphan safety-net (runs that died before self-merging), and its launch step starts new runs. Hot beads drain **sequentially** (serialized by `hot_inflight`), one landing before the next starts. The Gate above already ran.

### The loop

Repeat the turn below until step **D** returns `done`. If the user invoked `/flow evolve drain --include-proposals` (the dangerous mode, §`--include-proposals` below), append `--include-proposals` to BOTH the `evolve_reap.py` (step **A**) and `evolve_drain.py` (step **B**) invocations every turn — the reap flag is not optional, it is what lets a proposal orphan reap (without it those PRs pile up unmerged).

**A. Reap — merge orphan green leaf PRs (safety-net), first each turn.** Reaping first frees backpressure (open-PR cap) and clears `hot_inflight` for a hot that just landed, so the launch step sees an honest picture.

A launched run self-merges its own green PR, so this only ever finds a green evolve PR whose run **died before self-merging**. Green LEAF evolve PRs merge to the default branch unattended (immediate on green). Non-green and conflicted PRs always wait as draft PRs for the human — the gate survives where the risk is. Hot PRs auto-merge ONLY under `[evolve] auto_merge_hot` (default off; on solely in this maintainer self-target repo) AND isolation: at most one hot PR merges per pass, and the fleet must be quiesced around the pass. Off / non-maintainer keeps today's behavior (hot → `skipped_hot`). Note: the code (`classify`) enforces only the one-hot-per-pass serialization; ensuring no other evolve run is active (quiescing the fleet) before an auto-merge pass is the operator's responsibility.

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/evolve_reap.py --workspace-root .
```

Returns JSON `{merge:[{pr,key,is_draft,is_hot}], not_green, skipped_hot, blocked}`. For each `merge` entry (skip all of this under `--dry-run`):

**Guard property-check — run FIRST for any entry with `is_hot: true`.** A hot entry touches the harness, possibly the safety machinery itself. Before merging it, review the PR diff (`gh pr diff <pr>`) against the guard-property checklist: does this DELETE or weaken a safety property — lease exclusivity (one run per ticket), snapshot drift-detection, atomic-write + corrupt-file quarantine, content-ownership refusal, or self-edit flock serialization? Guard *code* may be refactored, sped up, or improved freely; a guard *property* may only be replaced by one that provably still holds, never simply dropped. Green does NOT prove the property holds — most of these have no direct test — so this review is the enforcer, not CI. If the diff removes a protection without a provably-equivalent replacement → do NOT merge: leave the PR as a draft for the human (skip its `gh pr ready` + `gh pr merge`), and report it under `held_guard`. Only a property-preserving hot entry proceeds to the steps below; a non-hot entry (`is_hot: false`) skips straight to them.

```bash
# mark ready only if it was a draft, then squash-merge
gh pr ready <pr>        # only when is_draft is true
# squash-merge WITHOUT --delete-branch, then close the bead and delete the
# remote branch — both gated on the merge succeeding, neither on the other.
if gh pr merge <pr> --squash; then
  bd close <key> --reason "merged via PR #<pr>"
  git push origin --delete <branch> || true
fi
# reap owns the LOCAL worktree + local branch (lease-gated)
python3 ${CLAUDE_SKILL_DIR}/scripts/flow_worktree.py reap --ticket <key> --branch <branch> --main-root .
```

`<key>`, `<pr>`, and `<branch>` (== `headRefName`) all come from the `merge` entry. `--delete-branch` is dropped: gh's branch-delete step fails because the still-registered worktree under `flow.worktrees/` holds the local `feature/<key>-*` branch checked out, and that failure makes an otherwise-successful `gh pr merge` exit 1 — which short-circuited the old `&& bd close`, so the bead never closed and the remote branch was left undeleted. Now `gh pr merge --squash` alone exits 0 on a clean merge, so `bd close` runs; the remote branch is deleted explicitly with `git push origin --delete <branch>` (which also drops the local `refs/remotes/origin/<branch>` tracking ref that feeds `evolve_select._gather_refs`). Deleting the REMOTE ref is unaffected by the worktree holding the LOCAL branch. `bd close` and the remote delete are each gated on the merge succeeding and are independent of each other (separate statements inside the `if`, never chained behind one another), so a `bd close` hiccup never skips the remote delete. `gh pr merge` refuses a not-actually-mergeable PR, so it is a safe backstop if state changed since the classify; if it refuses, the `if` body is skipped and the bead stays open. Closing a bead whose PR never merged would mint the exact PR↔bead state-inconsistency this step exists to prevent. The `reap` step still owns the LOCAL worktree + local branch teardown. It is lease-gated: a worktree whose bg session is still running (typically the reflect stage, which runs after the PR is green) is SKIPPED and reaped on a later turn once the session ends.

`bd close` here autodiscovers `.beads/*.db` from cwd, and this sub-verb is maintainer-gated with no `cd` in the loop, so the close inherits the maintainer-repo cwd and hits flow's own DB. With the close wired in, reaping a PR also closes its bead, so the loop leaves no merged-but-open beads behind. Veto for the human: convert a PR to draft or close it before the next turn and the reap skips it.

**B. Decide the next action.**

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/evolve_drain.py --workspace-root .
```

This runs `evolve_select` (which is DAG-aware via `bd ready`, drops in-flight beads, enforces backpressure ≥ `cap` open PRs, partitions ≤1 hot per batch / no shared primary-file anchor) and annotates each in-flight bead with its run's lease liveness. It returns JSON `{action: "launch"|"wait"|"done", launch:[keys], parked:[keys], liveness:{}, select:{...}}`:

- **`launch`** (launch non-empty) → go to **C**.
- **`wait`** (launch empty, but a **blocking** in-flight run remains) → go to **D-wait**. A run blocks when its lease reads `live` OR `corrupt`: a live run will self-merge and free serialization/backpressure; a corrupt lease (run.lock unparseable, ownership unconfirmable) does NOT self-free — it blocks until a human runs `recover takeover`. Both route to **D-wait**.
- **`done`** (launch empty AND no in-flight run is blocking — none reads `live` or `corrupt` — backlog drained, or only parked-for-human work remains) → exit the loop, go to **Report**.

The termination is blocking-gated on purpose: a **withheld** hot bead (its in-run reviewer raised `held_guard`) leaves a ready PR + branch but its session has ended, so its lease is expired/absent (non-blocking) — it reads as `parked`, never `wait`, so the loop cannot spin on it. The other blocking state is `corrupt` (treated live-equivalent because an in-flight run that cannot be confirmed dead must never let the loop drain to `done`); a corrupt lease blocks until a human runs `recover takeover`. It terminates and hands the withheld bead (plus any hot beads stuck behind it in `held_hot`) to the human.

**C. Launch.** For each key in `launch` (under `--dry-run`, print the command instead of running it):

```bash
claude --bg "/flow <key> --auto"
```

Each spawns a detached run that auto-plans and either drives its PR to green-and-self-merged, or — when it cannot self-approve at ≥90% confidence — **defers** its bead in place (status → `deferred`, open questions commented) and exits. A deferred bead drops out of `bd ready`, so the loop stops relaunching it. Defer-and-exit is the intended unattended outcome, not a failure. Drain auto-picks decided beads (already triaged + reopened) via the recorded-decision marker — no command change; the `--auto` run self-detects the decision (verb-spec.md step 4) and ingests the answer instead of re-deferring on it. After launching, briefly wait (Monitor, short cap) until the new keys register a branch/PR so the next turn's select sees them as in-flight, then loop back to **A**.

**D-wait.** Nothing to launch yet, but a **blocking** run is in flight. Wait with the `Monitor` tool (foreground `sleep` is blocked) until a run settles — `open_pr_count` drops (a PR merged) OR a lease ceases to block (goes non-live, or a corrupt lease cleared by `recover takeover`) — capped at roughly a stage timeout; on the cap, loop back to **A** anyway (the next reap mops up a now-dead run). Then loop back to **A**.

### --dry-run

`/flow evolve drain --dry-run`: run ONE turn's **A** reap classification (`evolve_reap.py`, print the `merge`/`not_green`/`skipped_hot`/`blocked` sets, do NOT merge) + **B** (`evolve_drain.py`, print the action + would-launch keys + parked), then STOP. No merges, no launches, no loop.

### --include-proposals (dangerous)

`/flow evolve drain --include-proposals` widens the loop from the `evolve` backlog to **also auto-launch + reap plain `proposal` beads** — the judgment-side work (features, real refactors, reorgs) that §propose deliberately routes to the maintainer's own backlog so a human accepts it at the spec-plan gate. With this flag, each ready `proposal` bead is fanned out as a `/flow <key> --auto` run that self-plans and self-merges at ≥90% confidence, **bypassing that human accept**. This is the one place drain ships taste-and-fit work with no human in the loop; use it only when you genuinely want the proposal backlog drained autonomously.

Mechanically it threads through the whole turn: `evolve_select` pulls a second `bd ready -l proposal` candidate set (merged by id) and drops its proposal-exclusion guard; `evolve_drain.py --include-proposals` carries the flag into select and echoes `include_proposals: true` in its JSON; `evolve_reap.py --include-proposals` widens its label index so proposal **orphans** (runs that died before self-merging) reap too — pass it on the step **A** invocation or those PRs never merge. Hot proposals serialize on the same single hot slot as hot evolve beads. Composable with `--dry-run` to preview what the dangerous mode would launch. The Report (below) names `include_proposals: true` so a run that auto-drained judgment work says so.

### Report

When the loop exits (`done`), summarise the whole run: merged (keys) + worktrees torn down across all turns, launched (keys), deferred (keys), and everything **parked for the human** — `parked` in-flight beads (expired/absent (non-blocking) lease, including any `held_guard` hot PR you withheld because its diff removed a safety property — name the property), plus `not_green` / conflicted draft PRs. Tell the user how to follow along:

- Monitor live runs with `claude agents --json` (the plain `claude agents` needs a TTY).
- Review any **deferred** beads with `/flow triage` — it lists the whole deferred queue with each bead's open-question comment inline. `deferred` != done; to unstick one, `/flow triage <key> "<answer>"` posts the answer + reopens the bead (status → `open`), then re-run it interactively (WITHOUT `--auto`).

Expect defers, not all PRs: terse audit beads will sometimes score under 90% or raise questions. A high defer rate signals the audit evidence needs to be richer (a finding for the miners in §audit), not a consumer bug.

---

## propose

`/flow evolve propose`. Maintainer-gated (the Gate above ran). This is the generative half of Producer B. Where `audit` mines small, provably-safe fixes, `propose` goes after the judgment-side work a single run can never surface — net-new capability, real simplification, structural change — and files it as **ranked proposals for the maintainer**, not auto-drainable work. Read-then-file; it does not implement.

Everything here is scored against the repo-root `VISION.md`, the scoring anchor: serves the thesis / on the right side of the auto-vs-propose line / does not erode the floor. A candidate that cannot be anchored there is slop — drop it.

### A. Lenses — fan out one agent per angle (read-only, parallel)

Spawn parallel read-only agents (the `Agent` tool, or a `Workflow` fan-out when available), one per angle. Each reads flow's code + `VISION.md` and proposes candidates FROM ITS ANGLE ONLY, each with: concrete evidence (`file:line` or a named gap), a one-line rationale tied to the vision, a `BLAST RADIUS:` line, and an honest confidence (0-1). The angles:

- **feature / capability gaps** — what should flow DO that it doesn't, judged against the vision and the unfinished tracks (e.g. MCP-first Jira, attachment download into the ticket stage)? A gap is a candidate only if you can name the concrete workflow flow cannot serve today.
- **simplification / over-engineering** — an abstraction not earning its keep, speculative generality with one caller, N things that could be one. Cite the call sites.
- **reorg / navigability** — structure and altitude: a doc or module layout that makes the system harder to hold than it needs to be.
- **dead weight / deletion** — code or docs that are unused and removable. PROVE zero refs (a proven deletion is provably-safe — see C).
- **architecture coherence** — a property the prose asserts but the code does not enforce ("by convention, should be by construction"); a seam that works but is brittle.
- **symmetry / consistency** — X has a test, handler, or doc that its sibling Y lacks; the asymmetry is usually a latent gap or bug.

A quiet angle is success, not failure — do not manufacture candidates to fill a lens.

### B. Adversarially verify each candidate (parallel)

For each surviving candidate, spawn an independent skeptic prompted to REFUTE it: is the problem real, or an artifact of a narrow read? Does it serve the vision, or just add surface? Is it manufactured motion (the ouroboros failure mode the vision names)? Default to refuted when uncertain. Kill any candidate the skeptic refutes. This is the brake on a generative pass inventing work to justify itself.

### C. Synthesize, split by disposition, rank

Dedup across lenses (merge candidates about the same root issue). Then split each survivor by the auto-vs-propose line:

- **Provably-safe → `audit`** (auto-drainable). A mechanical, behavior-preserving change with hard evidence — a proven-dead-code deletion, a zero-behavior-change simplification. File it exactly as §audit step 3 (labels `evolve`); it joins the normal drain.
- **Judgment → `proposal`** (the maintainer's backlog). A feature, a real refactor, a reorg, an architecture challenge — anything whose merit is taste and fit, not a broke/works signal. File it as a plain `proposal` bead (label `proposal` only, NOT `evolve,proposal`). A plain `proposal` bead carries no `evolve` label, so drain never sees it — it lands in the maintainer's backlog and is run via `/flow <key>`, where the spec plan gate is the accept.

Rank by vision-alignment × value × evidence-strength × reviewability. Each `proposal` description MUST carry, beyond the evidence and blast-radius: your **confidence** and a **recommended default** (build / shelve / needs-discussion), so triaging it costs the maintainer seconds, not hours. Assign the same stable `<primary-relfile>::<short-symptom>` id and file through the §audit step 3 seam (the `--dedup-key` converges re-runs). Flag `hot` per §audit step 2 when it touches a hot or guard file.

### D. Report

Present the ranked proposal set: each proposal's title, disposition (`audit` auto-drains / `proposal` you run via `/flow <key>`), confidence, recommended default, and one-line rationale. Be honest when a pass found little — surfacing two real proposals and refuting the rest is success, not failure. The maintainer finds the `proposal` beads (`bd ready --label proposal`) and runs each via `/flow <key>`; the `audit` ones drain with §drain.
