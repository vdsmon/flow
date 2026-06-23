# queue verb

`/flow queue [<sub-verb>] [--dry-run]`. Maintainer-only. `queue` is a **namespace**: the day-job backlog's surfaces, one sub-verb each. Day-job sibling of `/flow evolve` (`references/verb-evolve.md`) — the same loop shape over the project's OWN non-evolve backlog, with one structural difference: **nothing here ever merges a PR**. A day-job run's merge stage skips on a non-evolve bead, so every green PR parks as the maintainer's review queue.

- **bare `/flow queue`** (optionally `--dry-run`) — the read-only **status report** (§2-§4): ready beads, in-flight runs with lease liveness, queue-scoped backpressure. `--dry-run` additionally prints the exact batch a drain would launch — and still launches nothing.
- **`/flow queue drain`** — the day-job **consumer** (§drain): a single looping pass that drains the ready day-job backlog. Each turn it reaps merged-and-exited runs (close the bead, delete the remote branch, tear down the worktree — lease-gated), then fans out the next launchable batch as background `/flow <key> --auto` runs. It loops — launching, waiting while runs are live, reaping — until nothing is startable. Open PRs awaiting the maintainer's review+merge are this queue's **normal success terminal**, not leftovers.

**Read-only invariant for the status path (load-bearing):** bare `/flow queue` (with or without `--dry-run`) performs NO side effects, ever. No launches, no merges, no `bd` mutations, no launch-ledger marker pruning or removal. The `action` field in its JSON is **advisory** — what a drain WOULD do next — never an instruction to do it here. Only `/flow queue drain` mutates.

## 0. Dispatch

Match the **second whitespace token** of the args against the sub-verb set by exact string equality:

- `drain` → §drain.
- **empty** or `--dry-run` (bare `/flow queue`) → the status path: §2 gather, §3 render, plus §4 when `--dry-run` is present.
- **anything else** (unknown sub-verb) → print the listing above + "unknown queue sub-verb: `<token>`" and stop.

Namespace decision (mirrors `evolve`): there is no `scripts/queue.py` and there never will be — `queue` is a prose-level namespace, so `--dry-run` stays a prose-level modifier with no argparse home. The script cluster is `queue_select.py` (the select core) / `queue_status.py` (the status core) / `queue_drain.py` (the drain core).

Every sub-verb runs the **Gate** below first.

## 1. Gate — maintainer only

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/maintainer.py --workspace-root .
```

- Exit 0 → prints the flow repo root; you are the maintainer, continue with the dispatched sub-verb. Run against that repo.
- Exit 1 → not a maintainer setup (no `[maintainer]` marker). Print: "`/flow queue` is maintainer-only; this workspace is not the flow self-improvement target." Stop. Do NOT drain a user's project.

## 2. Gather — status path

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/queue_status.py --workspace-root .
```

Optional `--cap N` / `--concurrency N` override the `[queue]` section of `workspace.toml` (defaults cap=5, concurrency=3).

- Exit 0 → stdout is one JSON object: `{action, launch, parked, liveness, ready, select}`. Continue to §3.
- Exit 2 → a `bd`/`git`/`gh` call failed; surface stderr and stop.
- Exit 4 → not a maintainer setup (should not happen after the Gate); print the Gate's maintainer-only message and stop.

Field map: `ready` is the full day-job backlog (`bd ready` minus epics and minus `evolve`/`proposal`/`hot` labels), each `{id, priority, labels, title}`, sorted by (priority, id) — deeper than `launch`, which stops at the budget. `select` is the canonical `queue_select` partition (`launch`, `skipped_in_flight`, `held_backpressure`, `held_anchor`, `open_pr_count`, `open_pr_keys`, `live_runs`, `launched_pending`, `model_per_key`, `cap`, `concurrency`). `liveness` maps each in-flight key to its run's lease state (`live` / `corrupt` block a drain; everything else is settled). `action`/`parked` are the advisory drain decision.

## 3. Render — bare `/flow queue`

Present, in order:

1. **Ready** — a table of `ready`: id, priority, labels, title (id-only when title is absent).
2. **Cap usage** — `select.open_pr_count` of `select.cap` open day-job PRs (queue-scoped: open PRs belonging to active evolve beads do NOT count toward this cap). Note `held_backpressure: true` when the cap is full.
3. **In flight** — each `liveness` key with its lease state.
4. **Parked** — `parked` keys (in-flight but not live: orphaned PRs/branches a human should look at).
5. **Parked PRs with new human reviews (→ `/flow revise`)** — for each parked PR, whether it carries an unresolved Major+ review thread (a genuine new human CHANGES_REQUESTED). Gather every open-PR head ref (`gather_refs` returns slugged branch names, so reconstruct nothing) and feed the parked keys plus those refs to `queue_reviews.py`:

   ```bash
   PR_REFS=$(gh pr list --state open --json headRefName --limit 200 --jq '[.[].headRefName] | join(",")')
   python3 ${CLAUDE_SKILL_DIR}/scripts/queue_reviews.py --workspace-root . --keys <parked-keys-csv> --pr-refs "$PR_REFS"
   ```

   `--keys` is the `parked` list (csv); `--pr-refs` is EVERY open-PR head ref (the script joins each parked key to its exact slugged ref via `key_from_ref` and ignores the rest — passing a bare `feat/<key>` would not match the real `feat/<key>-<slug>` branch and silently flag nothing). Output is a JSON array `[{key, pr_id, pr_url, unresolved_major, threads}]` for parked PRs with `unresolved_major > 0`. Render a table of (key, PR# `pr_id`, unresolved Major+ count `unresolved_major`) and, per row, the exact command **`/flow revise <pr#>`** (bind `<pr#>` to `pr_id`). Empty array → omit the section. Read-only + advisory (the status path never mutates). Best-effort: silently skipped when the forge has no review-thread capability (the array stays empty). Surfaces **native Major+ only** — the `[revise] plain_comment_severity` floor is a revise-time knob (what the fix loop chases), NOT applied here, so leftover bot minors never produce false human-review flags.
6. **Launched pending** — `select.launched_pending`: keys fanned out by a drain that have not yet registered a branch/lease (the launch→init blind window).
7. The advisory `action` line: "a drain run now would: `<action>`".

## 4. Render — `--dry-run` addition

After §3, print the would-launch batch: for each key in `launch`, the exact command a drain would run, appending `--model <m>` only when `select.model_per_key[key]` exists. Hot is excluded upstream on this queue, so the only models reachable are `sonnet` for a `tier:trivial` OR `tier:light` bead and `[evolve] worker_model` for any other bead when that knob is set; an unset knob with no tier omits the flag and inherits the launcher default:

```
claude --bg [--model sonnet] "/flow <key> --auto"
```

Then close with the explicit line: **"printed only — nothing launched."** Empty `launch` → say so ("nothing launchable: backpressure / in-flight / empty backlog" per the select fields) and still print the closing line.

---

## drain

The day-job consumer. A single LOOP that drains the ready backlog: each turn it decides, reaps merged-and-exited runs, launches the next startable batch, then waits while runs are live — repeating until nothing is startable. The Gate above already ran. Unlike the evolve drain there is no reap-time merging, no hot serialization, and no proposal opt-in: launched runs park their green PRs for the human, and the only state this loop mutates is bead closes + worktree teardown for PRs the maintainer ALREADY merged.

### The loop

Repeat the turn below until step **C** returns `done`.

**A. Decide.** Ledger hygiene first, then the decision:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/launch_ledger.py prune --workspace-root .  # hygiene: drop expired launch markers
python3 ${CLAUDE_SKILL_DIR}/scripts/queue_drain.py --workspace-root .
```

`queue_drain.py` runs `queue_select` (day-job ready beads: unlabelled `bd ready` minus epics and minus `evolve`/`proposal`/`hot`, in-flight dropped, backpressure ≥ `[queue] cap` open day-job PRs), annotates each in-flight day-job run with its lease liveness, and classifies merged PRs for reaping. It returns JSON `{action: "launch"|"recover"|"wait"|"done", launch:[keys], parked:[keys], reap:[entries], stranded:[keys], stranded_pre_pr:[{key,branch,worktree}], liveness:{}, select:{...}}` (the top-level `stranded` key rides only the `recover` action; `stranded_pre_pr` is always present, empty when nothing is stranded). The wait gate is queue-scoped: the worktree pool and the launch ledger are shared with the evolve drain, so active-evolve keys are subtracted before liveness — this loop never blocks on (and never unmarks) a live evolve run. `select.launched_pending` lists the day-job keys still in the launch→init blind window; after `select` returns, the CLI physically removes a registered key's marker, an unregistered one blocks termination until it registers or its marker TTL-expires.

**B. Reap — tear down merged-and-exited runs, every turn.** Each `reap` entry `{key, branch, pr, bead_active, has_worktree}` is a merged flow PR whose key still has a registered worktree or sits in this turn's launch batch — `queue_drain.py` has already dropped any such key from `launch`, so a merged-but-unclosed bead diverts here and never relaunches. For each entry:

```bash
# close the bead FIRST when it is still active — the close is what removes the
# key from bd ready, so the teardown below is GATED on it succeeding.
bd close <key> --reason "merged via PR #<pr>"   # only when bead_active is true
git push origin --delete <branch> || true
python3 ${CLAUDE_SKILL_DIR}/scripts/flow_worktree.py reap --ticket <key> --branch <branch> --main-root .
```

When `bead_active` is true and the `bd close` FAILS, skip the branch delete + worktree reap for that entry and report it — tearing down without closing reopens the relaunch window. When `bead_active` is false (bead already closed, or `deferred` — a deferred bead stays the human's triage call, never auto-closed here) proceed straight to the teardown lines. The `reap` step is lease-gated + idempotent: a worktree whose bg session is still running is SKIPPED and reaped on a later turn once the session ends.

**C. Act on `action`.**

- **`launch`** → for each key in `launch`, read the per-key worker model from the step-**A** JSON (`result.select.model_per_key[key]`) and append `--model <model>` when present. Hot is excluded upstream here, so only `sonnet` (a `tier:trivial` OR `tier:light` bead) or `[evolve] worker_model` (any other bead when set) is reachable; absent → omit the flag and inherit the launcher default:

  ```bash
  # record the launch FIRST so the very next turn's select sees this key as in-flight
  # even before it registers a branch/lease (closes the re-launch window).
  python3 ${CLAUDE_SKILL_DIR}/scripts/launch_ledger.py add --key <key> --workspace-root .
  # shadow-register the launch in the fleet liveness ledger (epic flow-8by2; child-3 reads it).
  python3 ${CLAUDE_SKILL_DIR}/scripts/fleet.py register --key <key> --workspace-root .
  claude --bg [--model sonnet] "/flow <key> --auto"
  ```

  Each spawns a detached run that auto-plans and drives its PR to green — then **parks it** (ready for the maintainer's review; no self-merge) — or, when it cannot self-approve at ≥90% confidence, **defers** its bead in place (status → `deferred`, open questions commented) and exits. A deferred bead drops out of `bd ready`, so the loop stops relaunching it; defer-and-exit is the intended unattended outcome, not a failure. After launching, briefly wait (the `Monitor` tool, short cap; foreground `sleep` is blocked) until the new keys register a branch/PR, then loop back to **A**.

- **`recover`** (launch empty, `stranded_pre_pr` non-empty) → run the **§Recover stranded pre-PR runs** recipe below, then loop immediately back to **A** (no Monitor-wait — the recovery is local, the next turn's select sees the reopened beads). A stranded entry is a day-job `/flow <key> --auto` run that died PRE-PR: its bead sits in_progress with a dirty orphan worktree but no lease and no PR, so every other channel (reap is merged-PR-only, the deferred path needs a terminal bead) reads it as gone and the loop would false-positive to `done`. `recover` outranks `wait`: it only touches the stranded bead's own dead worktree (fleet-rechecked first), so a live run blocking elsewhere does not defer it.

- **`wait`** → a blocking run is in flight: a `live`/`corrupt` lease, or a `launched_pending` key still pre-lease. Wait with the `Monitor` tool until a run settles — a lease ceases to block, or a launched_pending run registers — capped at roughly a stage timeout; on the cap, loop back to **A** anyway (the next turn's reap + ledger TTL mop up a dead run). Then loop back to **A**.

- **`done`** → nothing startable, nothing blocking, nothing stranded. Exit the loop, go to **Report**.

**§Recover stranded pre-PR runs.** On a `recover` action, act on each entry in the step-**A** JSON's `stranded_pre_pr` (`{key, branch, worktree}`), then loop back to **A**. This is the day-job parity of the evolve drain's recovery (`references/verb-evolve.md` §Recover), identical in mechanism — only the detection scope differs: `queue_drain.py` classifies STRANDED over the DAY-JOB in_progress set (all in_progress beads minus epics minus `evolve`/`proposal`/`hot`, the inverse of evolve's per-label union), so the evolve drain's own in-flight runs are never touched. Each entry is in_progress, lease non-live, not in `launched_pending`, with NO PR open or merged. Recovery tears down the dirty worktree and reopens the bead so the NEXT turn's select relaunches it FRESH off `origin/main` — it NEVER do-resumes the dirty worktree (the do-resume re-dies at implement entry, witnessed flow-mmh3 attempt 2). The day-job loop never auto-merges, but stranded recovery is orthogonal to merging — a pre-PR death has no PR to merge. Skip ALL side effects under `--dry-run` (print the `stranded_pre_pr` set, run nothing). For each entry, run the SAME fleet-rechecked, reap-before-reopen/block, `STRANDED-RECOVERY:`-bounded recipe the evolve drain uses:

```bash
# fleet re-check FIRST (flow-8by2.3): classify ran lock-free; a bead that re-acquired a
# lease in the classify->recover gap must NOT have its worktree reaped from under a now-live
# run. is-live is lease-only, fail-safe (exit 0 = live = SKIP). fleet.py is tracker-agnostic
# (keys, not evolve labels), so it works for a day-job key.
if python3 ${CLAUDE_SKILL_DIR}/scripts/fleet.py is-live --key <key> --workspace-root .; then
  echo "fleet: <key> went live after classify — not recovering this turn"
else
  # ATTEMPT-N BOUND (bd-comment marker, distinct STRANDED-RECOVERY: stem; persists across
  # reopen->relaunch->re-strand). 3-state ladder, identical to verb-evolve.md §Recover.
  MARK=$(bd show <key> --include-comments --json \
    | python3 -c 'import sys,json,re;cs=json.load(sys.stdin);cs=cs[0] if isinstance(cs,list) else cs;t=[ (c.get("text") or "") for c in (cs.get("comments") or []) ];nums=[int(x) for s in t for x in re.findall(r"STRANDED-RECOVERY: attempt-(\d+)", s)];print(f"attempt-{max(nums)}" if nums else "")')
  if [ "$MARK" = "attempt-2" ]; then
    # two recovery relaunches both re-stranded -> give up to the human. Reap (cleanup),
    # do NOT reopen; block + triage stem (surfaces in /flow triage). REAP BEFORE BLOCK.
    python3 ${CLAUDE_SKILL_DIR}/scripts/flow_worktree.py reap --ticket <key> --main-root . \
      && { bd update <key> --status blocked
           python3 ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . comment --key <key> \
             --text "flow --auto could not self-approve: STRANDED-RECOVERY exhausted — <key> re-stranded pre-PR after two fresh relaunches (deterministic mid-pipeline crash). Needs a human: reopen (status->open) and run WITHOUT --auto, or fix the crash cause first."; }
  else
    # no marker (first strand) or attempt-1 (first recovery re-stranded) -> reap + reopen
    # so the next turn relaunches FRESH, and stamp the next rung. REAP BEFORE REOPEN.
    python3 ${CLAUDE_SKILL_DIR}/scripts/flow_worktree.py reap --ticket <key> --main-root . \
      && { bd update <key> --status open
           NEXT=$([ "$MARK" = "attempt-1" ] && echo attempt-2 || echo attempt-1)
           python3 ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . comment --key <key> \
             --text "STRANDED-RECOVERY: $NEXT"; }
  fi
fi
```

`<key>` comes from the entry. **Reap-then-reopen / reap-then-block order is load-bearing in EVERY branch** (a failed reap leaves the bead in_progress → re-qualifies next turn → idempotent self-heal; the marker write is `&&`-gated on a clean reap so it doesn't advance on a partial failure). **The `STRANDED-RECOVERY:` marker bounds the cycle at TWO recovery relaunches** (no marker → relaunch + `attempt-1`; `attempt-1` → relaunch + `attempt-2`; `attempt-2` → block + triage stem, terminal — a `blocked` bead leaves in_progress so it drops out of the detector). The recover branch loops straight back to **A** with no Monitor-wait; the loop-level iteration cap is its outer bound.

Optional session hygiene before reporting (same classification the evolve drain uses; it is key/intent-based, not evolve-label-gated, so finished day-job sessions classify too — follow `references/verb-evolve.md` step A2 for the stop + tombstone handling of each `stoppable` entry):

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/evolve_session_cleanup.py --workspace-root . --self-job "$(basename "$CLAUDE_JOB_DIR")"
```

### Report

When the loop exits (`done`), summarise the whole run:

- **launched** (keys) across all turns.
- **reaped** — beads closed + worktrees torn down (and any entry whose `bd close` failed, called out).
- **deferred** (keys) — review with `/flow triage`; to unstick one, `/flow triage <key> "<answer>"` posts the answer + reopens the bead, then re-run it interactively (WITHOUT `--auto`).
- **parked** — open PRs awaiting the maintainer's review+merge, plus any in-flight bead with an expired/absent lease. For this queue, parked open PRs are the **normal success terminal**: the loop's job is to produce them, the maintainer's job is to merge them. The next `drain` run reaps whatever got merged in between.

Expect defers, not all PRs: a terse bead will sometimes score under 90% or raise questions. A high defer rate signals the tickets need richer descriptions, not a consumer bug.
