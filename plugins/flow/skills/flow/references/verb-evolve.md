# evolve verb

`/flow evolve <sub-verb>`. Maintainer-only. Routed from SKILL.md's argument table. `evolve` is a **namespace**: the self-evolution loop's producers + consumers, one sub-verb each.

- **`/flow evolve audit`** — the cold-audit **producer** (§audit): scan flow's OWN codebase for evidence-backed improvements and file them as `audit` beads in flow's backlog. Read-then-file; it does not implement.
- **`/flow evolve propose`** — the multi-angle **proposal producer** (§propose): fan out one agent per generative angle (feature gaps, simplification, reorg, dead-weight, architecture, symmetry), adversarially verify, and file a ranked set. Provably-safe findings become auto-drainable `audit` beads; judgment findings become plain `proposal` beads (non-`evolve`) in the maintainer's own backlog, run via `/flow <key>`. Read-then-file; it does not implement.
- **`/flow evolve epic`** — the **high-altitude producer** (§epic): fan out web-reaching lenses for theme-scale work (capability tracks, architecture-era shifts, the meta-loop, unfinished tracks), gate on *conviction* not track-record (engage if grounded by a web cite / witness / bounded spike; refute only change-for-change's-sake), then file a parent `epic` bead + a tree of `proposal` children (the gearing into the per-ticket consumer). Maintainer-lane, weekly. Read-then-file; it does not implement.
- **`/flow evolve drain`** — the **consumer** (§drain): a single looping pass that drains the whole backlog. Each turn it reaps finished orphans (merge the green leaf PRs of runs that died before self-merging + teardown merged-and-exited worktrees, lease-gated), then fans out the next launchable batch as background `/flow <key> --auto` runs (each run self-merges its own green PR via the `merge` stage, post-Layer-2). It loops — launching, waiting while runs are live, reaping — until nothing is startable, draining hot beads sequentially. This is the nightly loop's consumer.

The sub-verbs here are **Producer B** (cold-audit + generative — `audit` mines defect-grain fixes, `propose` mines single-PR judgment work, `epic` mines theme-scale tracks); **Producer A** is the reflect sling (`references/stage-reflect.md`): lived friction during real runs. For the producer-A-vs-B framing and the auto-vs-propose backlog split (which findings auto-drain vs land in the maintainer's own backlog), see self-evolution.md §Producers.

## 0. Dispatch

Match the **second whitespace token** of the args against the sub-verb set by exact string equality:

- `audit` → §audit. `propose` → §propose. `epic` → §epic. `drain` → §drain.
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
- **test gaps** — public functions / branches with no test (use `MODULE.md` to map script → test). A claimed-missing test MUST be positively confirmed absent against the LIVE suite before it becomes a finding — a single grep-pattern miss can claim an already-tested path (flow-aod: both "missing" dispatch tests existed at the very test-count the evidence cited). Two independent probes, both required: (1) content-grep the whole `tests/` dir for the symbol AND its branch markers (function name, exit code, error string — a test may exercise the branch under a different name); (2) collect the live suite and grep node ids — from `scripts/`, run `mise exec python -- pytest tests/ --collect-only -q` once bare to confirm it reports N>0 tests collected, then piped `| grep -i <term>`. Grep finding nothing (empty output, exit 1) IS the clean-empty result; VOID means pytest itself errored or collected 0 — a VOID probe confirms nothing. Only both probes clean-and-empty support the claim, and the finding's evidence MUST stamp the exact probe commands + their empty results, so the plan stage can falsify the premise cheaply.
- **dead code & complexity** — unused defs (prove zero refs), very long / tangled functions.
- **doc drift** — `MODULE.md` / `inventory.md` / `SKILL.md` / `references/*.md` claims vs the actual code. For a PR-introduced *vocabulary/phrasing* drift (a renamed term, a reworded invariant, a changed concept name), the stale phrasing typically lives in EVERY reference describing that subsystem, not just the file the diff surfaced: grep the whole `references/*.md` + `SKILL.md` doc set for the old phrasing and enumerate ALL loci (every `file:line`) in the finding's description/evidence, so the one bead that fixes it names every locus — and when that bead is later spec'd its "Files to change" (and thus the stamped `planned_files`) covers them together. The finding's dedup identity still anchors on its single primary file (per §2); the multi-locus list belongs in the evidence, not the key.
- **friction & history** — unaddressed `MACHINERY:` entries in `knowledge.jsonl`, `TODO`/`FIXME`, recent git-log pain.
- **robustness** — real gaps in the load-bearing machinery (run lease, snapshot TOCTOU, atomic writes, ownership gate, flock). Tighten, never erode.
- **architecture / seam** — SKILL.md thinness, registry↔reference-doc consistency, prose↔CLI seam risks.

### 2. Synthesize, rank, assign stable ids

Dedup the raw findings (merge ones about the same root issue), drop the vague / unevidenced. Rank by evidence strength × value × blast-radius-safety × reviewability, then score each survivor against the repo-root `VISION.md` (serves the thesis / on the right side of the auto-vs-propose line / does not erode the floor — a candidate that cannot be anchored there is slop: drop it or escalate it as a question). Prefer small, isolated, high-evidence items. Give each survivor a **stable identity anchored on its primary file path** plus a short symptom — `<primary-relfile>::<short-symptom>`, e.g. `scripts/diff_extract.py::quotepath-parsing`. Anchor on the file, NOT free wording: the file path is the invariant a re-run will rediscover, so it is what makes the same defect dedup across runs (the seam fingerprints it, so exact formatting does not matter). Keep the `::` separator: the file component (its basename) now also anchors a fuzzy same-file dedup pass, so a re-discovery phrased differently still converges. Flag `hot` if it touches `SKILL.md` / `stage-registry.toml` / `CLAUDE.md` / a wired handler, OR a safety-machinery guard file (`lease.py`, `snapshot.py`, `_atomicio.py`, `_locking.py`, `state.py`, `dispatch_stage.py`, `diff_extract.py`, `machinery_edit.py`, `flow_friction.py`): a guard change must ride the hot path so the guard-property review gates it (the in-run merge reviewer when the run self-merges, or the §drain reap guard-property-check for an orphan). Parallel to `hot`, flag **`tier:trivial`** when the finding is mechanical, tightly bounded, behavior-preserving, and non-`hot` — work a capable cheaper model handles safely (a one-line doc-drift fix, a proven-dead-code deletion). `hot` and `tier:trivial` are mutually exclusive: a finding is either harness-risky (`hot`) or cheap-and-safe (`tier:trivial`), never both. A `tier:trivial` stamp lets drain run that bead's whole run at a cheaper worker model (§drain step C); leaving it unstamped defaults to the strong model.
<!-- SYNC: this 9-file hot guard list is duplicated by design in references/stage-reflect.md step 2b — keep both in sync (flow-837; not extracted to a constant per maintainer decision) -->

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

When the candidate was flagged `tier:trivial` (step 2), add it to the `--labels` value: `--labels "evolve,tier:trivial"`. No script change — `flow_beads_create.py` comma-splits `--labels`, so the extra label passes straight through onto the bead.

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
python3 ${CLAUDE_SKILL_DIR}/scripts/launch_ledger.py prune --workspace-root .  # hygiene: drop expired launch markers
python3 ${CLAUDE_SKILL_DIR}/scripts/evolve_reap.py --workspace-root .
```

Returns JSON `{merge:[{pr,key,is_draft,is_hot}], not_green, skipped_hot, version_recoverable, blocked}`. The `launch_ledger.py prune` on the first line is hygiene only (drops expired launch markers); SKIP it under `--dry-run` like every other side effect, since it deletes files. For each `merge` entry (skip all of this under `--dry-run`):

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

`<key>`, `<pr>`, and `<branch>` (== `headRefName`) all come from the `merge` entry. `--delete-branch` is dropped: gh's branch-delete step fails because the still-registered worktree under `.flow/worktrees/` holds the local `feature/<key>-*` branch checked out, and that failure makes an otherwise-successful `gh pr merge` exit 1 — which short-circuited the old `&& bd close`, so the bead never closed and the remote branch was left undeleted. Now `gh pr merge --squash` alone exits 0 on a clean merge, so `bd close` runs; the remote branch is deleted explicitly with `git push origin --delete <branch>` (which also drops the local `refs/remotes/origin/<branch>` tracking ref that feeds `evolve_select._gather_refs`). Deleting the REMOTE ref is unaffected by the worktree holding the LOCAL branch. `bd close` and the remote delete are each gated on the merge succeeding and are independent of each other (separate statements inside the `if`, never chained behind one another), so a `bd close` hiccup never skips the remote delete. `gh pr merge` refuses a not-actually-mergeable PR, so it is a safe backstop if state changed since the classify; if it refuses, the `if` body is skipped and the bead stays open. Closing a bead whose PR never merged would mint the exact PR↔bead state-inconsistency this step exists to prevent. The `reap` step still owns the LOCAL worktree + local branch teardown. It is lease-gated: a worktree whose bg session is still running (typically the reflect stage, which runs after the PR is green) is SKIPPED and reaped on a later turn once the session ends.

`bd close` here autodiscovers `.beads/*.db` from cwd, and this sub-verb is maintainer-gated with no `cd` in the loop, so the close inherits the maintainer-repo cwd and hits flow's own DB. With the close wired in, reaping a PR also closes its bead, so the loop leaves no merged-but-open beads behind. Veto for the human: convert a PR to draft or close it before the next turn and the reap skips it.

**Then handle the `version_recoverable` set — merge-time version-conflict recovery (Option B).** A green non-hot DIRTY PR lands here: the version is stamped at merge time (stage-merge §3), not per-PR, so siblings' merge-time stamps walk main's version forward and a later PR can go DIRTY on the version line ONLY if main merges during its post-stamp CI re-wait (its code merges clean). Process this set **SERIALLY** — each merge walks main's version forward, so each PR must re-fetch main AFTER the prior one merged (don't parallelize). For each `{pr, key, branch}` entry (skip under `--dry-run`):

```bash
# fetch the branch, check it out in a TEMP worktree (never the maintainer checkout)
git fetch origin "$BRANCH"
TMP=$(mktemp -d)
git worktree add "$TMP" "$BRANCH"
REMERGE=$(python3 ${CLAUDE_SKILL_DIR}/scripts/version_remerge.py recover \
  --branch "$BRANCH" --cwd "$TMP" --workspace-root .)
RC=$?
if [ "$RC" -eq 3 ]; then
  echo "non-version conflict on PR #$PR — leave for the human (report under blocked/held)"
elif [ "$RC" -eq 0 ]; then
  # the helper PUSHED a NEW SHA CI never validated. RE-WAIT CI (bounded) before merging —
  # mandatory: it preserves "merge ONLY the CI-validated SHA". Poll with the Monitor tool
  # (foreground sleep is blocked), capped at ~a stage timeout; on the cap, leave it for the
  # next pass, do NOT hang.
  # ... wait until forge_cli ci-rollup --pr "$PR" reports success ...
  gh pr ready "$PR"        # only if it was a draft
  if gh pr merge "$PR" --squash; then
    bd close "$KEY" --reason "merged via PR #$PR (version-remerged)"
    git push origin --delete "$BRANCH" || true
  fi
fi
git worktree remove --force "$TMP"
```

The `recover` call stays flagless here (an orphan's temp worktree has no ticket frontmatter to read a `commit_type` from): flagless recovery resolves the bump type itself by scanning the branch-only commits (`origin/<default>..HEAD --no-merges`) for a `feat` conventional prefix, so a feat orphan is never re-stamped as a patch — the branch HEAD is usually the `chore: stamp plugin version` commit, which is why a HEAD-only read would not do. The CI re-wait is **non-negotiable**: `version_remerge` pushed a brand-new merge commit, so the green that gated the original PR no longer applies — only a fresh green proves the auto-resolved merge is semantically correct, not just textually clean. **Non-blocking liveness caveat:** a non-hot orphan merging mid-wait can re-DIRTY a still-waiting PR (main moved again). That is fine — it converges across drain passes **as long as merges stay serial**: each pass re-fetches and re-resolves against the now-current main. Bound the wait, never hang; an un-converged PR is simply picked up again on the next turn. A HOT DIRTY PR never lands in `version_recoverable` (classify keeps it in `blocked`); hot conflicts are not auto-recovered.

**A2. Cleanup finished sessions — stop + tombstone the idle done ones.** A launched `claude --bg /flow <key> --auto` run does not exit when its work finishes: after the PR merges + the reflect stage runs, the session goes idle but lingers as a job dir under `~/.claude/jobs/<id>/`, so a multi-bead drain leaves a pile of idle sessions in the agents panel for the maintainer to `claude stop` + Ctrl+X by hand. This step clears them. It is read-only classification + reviewable prose side effects (mirrors step A reap).

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/evolve_session_cleanup.py --workspace-root . --self-job "$(basename "$CLAUDE_JOB_DIR")"
```

Enumeration + liveness are filesystem-only — the script scans `~/.claude/jobs/*/state.json` directly and NEVER calls `claude agents --json` (it blocks on a TTY and the drain can run headless). Flags: `--workspace-root` (required; non-maintainer → exit 4, skip this step), `--self-job` (the orchestrator's own `$CLAUDE_JOB_DIR` basename, skipped outright), `--idle-threshold-secs` (default 300; a transcript with a fresher mtime is treated as still writing → not stopped), `--stale-idle-threshold-secs` (default 600; the longer idle bar applied when `state` is not a clean terminal — see below). It returns JSON `{stoppable:[{session_id, job_id, key, cwd, job_dir, reason}], skipped:[{session_id, reason}]}` — the `job_id` (the 8-hex dir basename) is the `claude stop` handle, NOT the session UUID. The session→bead map is the job's `intent` (`/flow <key> --auto`), which also filters out foreign / non-flow jobs; the bg orchestrator records `cwd == repo root` (not the worktree), so a job is eligible only when its cwd is this repo's root. A session reaches `stoppable` only when its `<key>`'s bead is terminal (closed/blocked/deferred), `tempo == idle`, its run lease is non-live (`live`/`corrupt` → skipped, the same mid-reflect guard reap uses; an already-reaped worktree reads `absent` → non-live → proceeds), and its transcript mtime is idle — any busy or unprovable signal skips it (fail-safe toward NOT stopping). `state` is deliberately NOT gated: a finished bg run rests at `state == working` (or `blocked`) indefinitely — a `session_cron` keepalive task, or a daemon that never flips the field — so gating on `done`/`stopped` skipped the COMMON case and leaked every drained run as a zombie. Doneness rests on the three independent signals (lease ∧ transcript ∧ bead) instead; when `state` is not a clean terminal, the transcript must be idle past the longer `--stale-idle-threshold-secs` before the stale field is overridden.

For each `stoppable` entry (skip ALL of this under `--dry-run` — print the stoppable set and run nothing):

```bash
# validate tokens before interpolating (defensive on the destructive path)
timeout 90 claude stop <job_id> </dev/null || true       # the 8-hex JOB id is the stop handle; `claude stop <session_uuid>` fails "No job matching". stdin detached + bounded
rm -rf <job_dir>                                          # Ctrl+X-equivalent: drop the panel tombstone (the absolute path from the entry)
```

`<job_id>`, `<session_id>`, and `<job_dir>` come from the `stoppable` entry. **`claude stop` takes the `<job_id>` (the 8-hex dir basename), NOT the session UUID** — passing the UUID returns "No job matching" fast (the bug that left a whole drain's runs un-stopped; the follow-up `rm` was then silently re-materialized by the daemon, because the session was never actually stopped). Before the `claude stop`, validate `<job_id>` is 8-hex (`^[0-9a-f]{8}$`). Before the `rm -rf`, validate `<job_dir>` is under `~/.claude/jobs/` with an 8-hex basename (`^.*/\.claude/jobs/[0-9a-f]{8}$`) — the single destructive line, so guard the path it deletes. Use the entry's literal `<job_dir>`. **Order matters: the stop must land BEFORE the `rm`** — a still-registered job dir that is `rm`-ed gets re-created by the daemon, so only a stopped (or genuinely done) job stays removed. This is NON-DESTRUCTIVE to history: the transcript at `~/.claude/projects/<slug>/<session_id>.jsonl` is untouched, so the session stays resumable (`claude attach <session_id>`) after either stop or dir-removal. A daemon-sanctioned dismiss is the cleaner long-term path if a future CLI offers one (none in 2.1.169).

**B. Decide the next action.**

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/evolve_drain.py --workspace-root .
```

This runs `evolve_select` (which is DAG-aware via `bd ready`, drops in-flight beads, enforces backpressure ≥ `cap` open PRs, partitions ≤1 hot per batch / no shared primary-file anchor) and annotates each in-flight bead with its run's lease liveness. It returns JSON `{action: "launch"|"wait"|"done", launch:[keys], parked:[keys], liveness:{}, select:{...}}`. Inside `select`, `launched_pending` lists the keys held by the launch ledger — runs fanned out on a prior turn that have not yet registered a branch/lease (the launch→init window); the selector already counts them as in-flight, so they are neither re-launched nor allowed to break hot isolation.

- **`launch`** (launch non-empty) → go to **C**.
- **`wait`** (launch empty, but a **blocking** in-flight run remains) → go to **D-wait**. A run blocks when its lease reads `live` OR `corrupt`: a live run will self-merge and free serialization/backpressure; a corrupt lease (run.lock unparseable, ownership unconfirmable) does NOT self-free — it blocks until a human runs `recover takeover`. A non-empty `launched_pending` is a third blocking reason: a launched-but-pre-lease run (in the ledger, no branch/lease/PR registered yet) has no run dir to read, so it would otherwise look non-blocking — it blocks until it registers (then the lease/PR channels take over) or its marker TTL-expires. It is NOT parked. All route to **D-wait**.
- **`done`** (launch empty AND `launched_pending` empty AND no in-flight run is blocking — none reads `live` or `corrupt` — backlog drained, or only parked-for-human work remains) → exit the loop, go to **Report**.

The termination is blocking-gated on purpose: a **withheld** hot bead (its in-run reviewer raised `held_guard`) leaves a ready PR + branch but its session has ended, so its lease is expired/absent (non-blocking) — it reads as `parked`, never `wait`, so the loop cannot spin on it. The other blocking states are `corrupt` (treated live-equivalent because an in-flight run that cannot be confirmed dead must never let the loop drain to `done`; a corrupt lease blocks until a human runs `recover takeover`) and a `launched_pending` key (a just-launched run still in its pre-lease bootstrap window, which must not be abandoned with a hot bead held behind it). It terminates and hands the withheld bead (plus any hot beads stuck behind it in `held_hot`) to the human.

**C. Launch.** For each key in `launch` (under `--dry-run`, print the command instead of running it). Read the per-key worker model from the step-**B** JSON (`result.select.model_per_key[key]`, present in the same JSON you already consumed) and append `--model <model>` when the key is present (absent → omit the flag, the run inherits the strong default model):

```bash
# record the launch FIRST so the very next turn's select sees this key as in-flight
# even before it registers a branch/lease (closes the re-launch + 2nd-hot-isolation window).
python3 ${CLAUDE_SKILL_DIR}/scripts/launch_ledger.py add --key <key> --workspace-root .
claude --bg [--model sonnet] "/flow <key> --auto"
```

A producer-stamped `tier:trivial` non-`hot` bead maps to `sonnet`, so its whole run (orchestrator + plan/implement subagents) runs at the cheaper worker tier; every other bead omits the flag and runs at the default. Under `--dry-run`, the printed would-launch command shows the chosen `--model` so a downshift is visible before anything launches.

Downshifted runs stay **visible** for revert-correlation: the `tier:trivial` bead label persists after close, so `bd list --status closed --label tier:trivial` (closed-inclusive — a downshifted bead is closed by the time you would revert it) lists every downshifted bead, each matched to its `feature/<key>-*` branch / PR. That persistent label IS the record; a bad cheap-tier PR is found there, reverted, and the label removed to un-stamp the pattern.

Each spawns a detached run that auto-plans and either drives its PR to green-and-self-merged, or — when it cannot self-approve at ≥90% confidence — **defers** its bead in place (status → `deferred`, open questions commented) and exits. A deferred bead drops out of `bd ready`, so the loop stops relaunching it. Defer-and-exit is the intended unattended outcome, not a failure. Drain auto-picks decided beads (already triaged + reopened) via the recorded-decision marker — no command change; the `--auto` run self-detects the decision (verb-spec.md step 4) and ingests the answer instead of re-deferring on it. After launching, briefly wait (Monitor, short cap) until the new keys register a branch/PR so the next turn's select sees them as in-flight, then loop back to **A**.

**D-wait.** Nothing to launch yet, but a **blocking** run is in flight — either a `live`/`corrupt` lease, or a `launched_pending` key with no lease to event-wait on yet (still pre-lease in its launch→init window). Wait with the `Monitor` tool (foreground `sleep` is blocked) until a run settles — `open_pr_count` drops (a PR merged) OR a lease ceases to block (goes non-live, or a corrupt lease cleared by `recover takeover`) OR a launched_pending run registers a lease/PR (its marker is then removed) — capped at roughly a stage timeout; on the cap, loop back to **A** anyway (the next reap mops up a now-dead run, and a launched_pending key whose run never registered drops out once its marker TTL-expires). Then loop back to **A**.

### --dry-run

`/flow evolve drain --dry-run`: run ONE turn's **A** reap classification (`evolve_reap.py`, print the `merge`/`not_green`/`skipped_hot`/`blocked` sets, do NOT merge) + **A2** session-cleanup classification (`evolve_session_cleanup.py`, print the `stoppable` set, do NOT `claude stop` or `rm`) + **B** (`evolve_drain.py`, print the action + would-launch keys + parked), then STOP. No merges, no stops, no launches, no loop.

### --include-proposals (dangerous)

`/flow evolve drain --include-proposals` widens the loop from the `evolve` backlog to **also auto-launch + reap plain `proposal` beads** — the judgment-side work (features, real refactors, reorgs) that §propose deliberately routes to the maintainer's own backlog so a human accepts it at the spec-plan gate. With this flag, each ready `proposal` bead is fanned out as a `/flow <key> --auto` run that self-plans and self-merges at ≥90% confidence, **bypassing that human accept**. This is the one place drain ships taste-and-fit work with no human in the loop; use it only when you genuinely want the proposal backlog drained autonomously.

Mechanically it threads through the whole turn: `evolve_select` pulls a second `bd ready -l proposal` candidate set (merged by id) and drops its proposal-exclusion guard; `evolve_drain.py --include-proposals` carries the flag into select and echoes `include_proposals: true` in its JSON; `evolve_reap.py --include-proposals` widens its label index so proposal **orphans** (runs that died before self-merging) reap too — pass it on the step **A** invocation or those PRs never merge. Hot proposals serialize on the same single hot slot as hot evolve beads. Composable with `--dry-run` to preview what the dangerous mode would launch. The Report (below) names `include_proposals: true` so a run that auto-drained judgment work says so.

### Post-drain: advance the checkout (DEFAULT, always run)

When the loop exits (`done`), advance the maintainer checkout so the just-merged work goes live for the NEXT run. This is **default behaviour — always desirable, always run it** (the `vdsmon-flow` marketplace tracks the local `main` checkout, so an un-advanced checkout keeps serving the pre-drain code to every subsequent run). From the maintainer repo root, on `main`:

```bash
git fetch origin --quiet
# .beads/*.jsonl is a passive export (Dolt is truth) — discard its churn so ff-only is clean
git checkout -- .beads/ 2>/dev/null || true
if git merge --ff-only origin/main; then
  claude plugin marketplace update vdsmon-flow
else
  echo "ff-only skipped (checkout diverged/dirty) — advance by hand; NEVER force"
fi
```

Try-and-skip, **never force**: a diverged or dirty tree leaves the advance for the human, not a `--force`. Skip this step entirely under `--dry-run`. If the orchestrator is NOT on `main` (a detached/standalone `do`), skip — only the main checkout the marketplace tracks should advance.

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

- **Provably-safe → `audit`** (auto-drainable). A mechanical, behavior-preserving change with hard evidence — a proven-dead-code deletion, a zero-behavior-change simplification. File it exactly as §audit step 3 (labels `evolve`); it joins the normal drain. Such a disposition is also a natural `tier:trivial` (§audit step 2): stamp it `--labels "evolve,tier:trivial"` when it qualifies, so drain runs it at the cheaper worker model.
- **Judgment → `proposal`** (the maintainer's backlog). A feature, a real refactor, a reorg, an architecture challenge — anything whose merit is taste and fit, not a broke/works signal. File it as a plain `proposal` bead (label `proposal` only, NOT `evolve,proposal`). A plain `proposal` bead carries no `evolve` label, so drain never sees it — it lands in the maintainer's backlog and is run via `/flow <key>`, where the spec plan gate is the accept.

Rank by vision-alignment × value × evidence-strength × reviewability. Each `proposal` description MUST carry, beyond the evidence and blast-radius: your **confidence** and a **recommended default** (build / shelve / needs-discussion), so triaging it costs the maintainer seconds, not hours. Assign the same stable `<primary-relfile>::<short-symptom>` id and file through the §audit step 3 seam (the `--dedup-key` converges re-runs). Flag `hot` per §audit step 2 when it touches a hot or guard file.

### D. Report

Present the ranked proposal set: each proposal's title, disposition (`audit` auto-drains / `proposal` you run via `/flow <key>`), confidence, recommended default, and one-line rationale. Be honest when a pass found little — surfacing two real proposals and refuting the rest is success, not failure. The maintainer finds the `proposal` beads (`bd ready --label proposal`) and runs each via `/flow <key>`; the `audit` ones drain with §drain.

## epic

`/flow evolve epic`. Maintainer-gated (the Gate above ran). The **high-altitude producer**. Where `audit` mines defect-grain fixes and `propose` mines single-PR judgment work, `epic` goes after work no single PR can hold: net-new capability tracks, architecture-era shifts, cross-cutting initiatives, vision-tracks-not-yet-built — the kind of finding that becomes a parent epic with a tree of children. Read-then-file (with an optional bounded spike, §B); it does not implement and it does not auto-ship. Everything is scored against repo-root `VISION.md`.

This producer is **audacious by mandate** and runs **weekly, not nightly** — at theme altitude a daily cadence has weak signal (see `references/loop-engineering.md`). Its brake is a **conviction gate, not an evidence gate**: it does NOT require a proven track record or weeks of telemetry. Sometimes a change is clearly better and this producer exists to propose it. What it refuses is change for the sake of change — the ouroboros the vision names. The line between the two is *grounding* (§B), not caution.

### A. Lenses — fan out one read-only agent per angle (parallel, web-reaching)

Spawn parallel read-only agents, one per angle. Each reads flow's code + `MODULE.md` / `inventory.md` (the map) + `VISION.md` + the loop's own history (the friction log via `flow_friction.py` aggregates, `recall --metric` trends, `knowledge.jsonl` `MACHINERY:` entries, open epics via `bd list --type epic --json`) — it **compounds** on what the loop has lived rather than re-deriving from a cold stare. Each candidate carries: concrete grounding (a `file:line`, a named gap, a web citation, or a spike result), a one-line rationale tied to the vision, a `BLAST RADIUS:` line, a rough decomposition (the child tickets it would split into), and an honest confidence (0-1). The angles:

- **field-scan (the web angle)** — `WebSearch` / `WebFetch` the latest in agentic coding, loop engineering, Claude Code, LLM-harness design. Map each real advancement against what flow does today; propose epics that bring the good ones in. This is the audacious angle: the field moves weekly, and an advancement flow lacks is a real gap even with zero internal telemetry. Cite the source.
- **capability-track gaps vs vision** — a whole workflow the vision implies flow should serve but no track builds yet. A candidate only if you can name the concrete workflow flow cannot serve end-to-end today.
- **architecture-era shifts** — a structural premise that has aged at *system* scale: a layering that fights the grain now, an assumption (single-tracker, one-PR-per-ticket) a new reality outgrew. `propose` owns the single-seam version; `epic` owns the system-wide one.
- **the meta-loop** — flow's OWN self-evolution loop judged against loop-engineering canon (`references/loop-engineering.md`): is maker separated from checker everywhere, is memory compounding or re-derived, is there a real separate-verifier stop. A gap here is the highest-leverage epic class — it improves the engine that produces every other improvement.
- **unfinished tracks** — existing parent epics (e.g. flow-aut, flow-uo7) with stalled or never-built children: what closes the track? Lowest-ouroboros angle (the track was already judged worth starting), so weight it highest.

A quiet angle is success — do NOT manufacture an epic to fill a lens. The ouroboros risk peaks at this altitude; an empty pass beats a padded one.

### B. Ground each candidate — the conviction gate (adversarial, parallel)

For each surviving candidate, spawn an independent skeptic. Unlike `propose`'s default-refute brake, the epic skeptic's default is **engage if grounded and clearly-better; refute only the groundless or frivolous**. A candidate SURVIVES if it carries at least one *externalized* grounding — not raw assertion:

- a **web advancement** the field actually moved to (cited), OR
- a **witnessed signal** — a friction aggregate, a metric trend, an unfinished-track gap, OR
- a **spike result** (below).

Engineering judgment is how the skeptic *weighs* these — it is not itself a fourth grounding. "It's clearly better" with no cite, no witness, no spike is the pure-vibes hole and the ouroboros in disguise; refute it. But the grounding bar is **cheap and fast** (a citation, a ten-minute experiment), explicitly NOT the weeks-of-data bar `audit` / `propose` lean on. The skeptic asks, per candidate: (a) grounded at all? (b) serves the thesis, or builds an empire that adds surface? (c) manufactured motion to justify the loop's own existence? (d) **decomposable into do-loop-sized children** (§C — an epic that cannot be cut is escalated as a question, not filed)? Refute on a miss of (a)/(d) or a yes on (b)/(c).

**The spike (optional, bounded).** When a quick experiment would settle a candidate's worth better than more reading, the producer may run ONE throwaway spike: prototype the idea in a scratch worktree (or `$CLAUDE_JOB_DIR/tmp`), observe, discard it. Hard bounds — it **never touches `main` or the maintainer checkout**, it is time-boxed (a single spike ≤ ~15 min, at most a couple per run), and its only output is conviction captured into the epic's evidence. A spike substitutes for historical data: it lets the producer KNOW a change is better by trying it, not by waiting weeks to measure it. Spikes are optional; most candidates ground on a web cite or a witness without one.

The real stop condition is the **maintainer-accept gate** — a *separate* verifier, never self-graded. This producer ranks and hands off; it never ships.

### C. Decompose + file — the gearing (parallel)

The consumer (`drain`) is per-ticket → one PR; an epic is not one PR. The gearing is a **parent epic bead + a tree of do-loop-sized child beads**, reusing existing seams — no new code:

- **The parent is gated for free.** `evolve_select.py` filters `issue_type != "epic"` unconditionally (even `--include-proposals` cannot launch an epic-typed bead), so filing the parent `--type epic` means `drain` STRUCTURALLY never launches the whole epic as one run. Preserve that filter — it is load-bearing here, not decoration.
- **File the parent** through the §audit step 3 seam (`flow_beads_create.py`), `--type epic`, label `epic`, with a **dedup-key of the form `epic:<capability-track>`** (e.g. `epic:tracker-agnostic-frontdoor`). The `<relfile>::<symptom>` scheme breaks for cross-cutting epics; a stable capability-track slug is what makes a re-run *converge* instead of re-pitching the same empire, and — because the dedup fingerprint is checked across all statuses incl. closed — what keeps a *shelved* epic dead. Choose the slug from the capability, not the wording.
- **Children are plain `proposal` beads** (label `proposal` only, NOT `evolve`), filed `--type task --parent <epic-key>` through the same seam. Auto-shipping fragments of an UNACCEPTED epic is the ouroboros in its purest form; so children land in the maintainer's lane and run via `/flow <key>` at the spec-plan accept gate — the existing auto-vs-propose split, just rooted under an epic parent.
- **Lazy by default.** File ONLY the parent epic; carry the decomposition as an ordered PREVIEW in its description (each child: title + one-line rationale + rough blast radius). The preview is a CONTRACT that §E branches on: each child MUST be tagged net-new vs pre-existing-to-reparent — a NET-NEW child carries a `(NET-NEW ...)` marker plus its own `dedup epic:<track>::child-N-<symptom>` key, while a child mapping to an EXISTING bead carries the explicit tag `PRE-EXISTING CHILD to RE-PARENT (do NOT re-file): <key>` naming that bead's key. A bare bead key merely mentioned in a rationale does NOT count as a reparent target — only the explicit tagged-and-keyed form does. Materialize children only when the maintainer accepts the epic and runs the expand step (§E). This keeps the backlog clean of children belonging to epics that get shelved.

Rank survivors by **vision-leverage × ambition × decomposability × reviewability**. Vision-leverage dominates: an epic that closes an already-blessed unfinished track outranks a net-new empire (less ouroboros risk). Decomposability is FIRST a gate (§B.d), then a tie-breaker.

### D. Report

Present the ranked epic set. Each entry: title + capability-track slug, disposition (`epic` container; children run via `/flow <key>` after expand), the **decomposition preview** (the child tree, made visible before anything is built), confidence (0-1), a **recommended default** (build-now / shelve / needs-discussion), a one-line vision-leverage rationale, and the **grounding it cites** (which web advancement / witness / spike). The grounding is mandatory in the report — a maintainer must see at a glance this is not manufactured motion. Be honest when a pass found little: one real, well-grounded epic beats five padded ones. The maintainer finds filed epics with `bd list --type epic --json`, accepts one, and expands it (§E).

### E. Expand an accepted epic (maintainer-run)

When the maintainer accepts a lazily-filed epic, materialize its children: read the decomposition preview from the epic's description and process each child by its §C marker:
- **net-new child** (preview `(NET-NEW ...)`) → file fresh via `flow_beads_create.py --type task --parent <epic> --labels proposal --dedup-key "epic:<track>::child-<n>-<symptom>"`.
- **pre-existing child** (preview `PRE-EXISTING CHILD to RE-PARENT (do NOT re-file): <key>`) → reparent in place with a bare `bd update <key> --parent <epic>`. NEVER re-file it, and do NOT relabel it — the existing bead keeps its own identity, status, and labels.

The distinction matters because the `epic:<track>::child-N-<symptom>` dedup namespace is disjoint from a pre-existing bead's `<relfile>::<symptom>` key, so a naive re-file does NOT dedup-collide and WOULD mint a duplicate. Each child is then a normal ticket run via `/flow <key>` — do-loop-sized, gated at its own spec-plan accept. Children are deliberately NOT epic-aware; the altitude lives in the parent, the work lives in the leaves.
