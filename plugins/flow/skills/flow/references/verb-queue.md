# queue verb

`/flow queue`. Maintainer-only. Routed from SKILL.md's argument table. A **read-only** report on the day-job queue — the non-evolve backlog the future queue-drain loop (flow-hw1.3) will consume: ready beads, in-flight runs with lease liveness, queue-scoped backpressure. `--dry-run` additionally prints the exact batch a drain would launch, mirroring `/flow evolve drain --dry-run`'s rendering — and still launches nothing.

**Read-only invariant (load-bearing):** this verb performs NO side effects, ever. No launches, no merges, no `bd` mutations, no launch-ledger marker pruning or removal. The `action` field in the JSON is **advisory** — what a drain WOULD do next — never an instruction to do it here.

## 0. Dispatch

`--dry-run` is the only modifier: bare `/flow queue` renders §3; with `--dry-run` also render §4. Any other token after `queue` → print "unknown queue argument: `<token>`" plus this verb's one-line usage, and stop.

Namespace decision (mirrors `evolve`): there is no `scripts/queue.py` and there never will be — `queue` is a prose-level namespace, so `--dry-run` stays a prose-level modifier with no argparse home. The script cluster is `queue_select.py` (the select core) / `queue_status.py` (this verb's core) / the future queue-drain.

## 1. Gate — maintainer only

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/maintainer.py --workspace-root .
```

- Exit 0 → prints the flow repo root; you are the maintainer, continue.
- Exit 1 → not a maintainer setup (no `[maintainer]` marker). Print: "`/flow queue` is maintainer-only; this workspace is not the flow self-improvement target." Stop.

## 2. Gather

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
5. **Launched pending** — `select.launched_pending`: keys fanned out by a drain that have not yet registered a branch/lease (the launch→init blind window).
6. The advisory `action` line: "a drain run now would: `<action>`".

## 4. Render — `--dry-run` addition

After §3, print the would-launch batch: for each key in `launch`, the exact command a drain would run, appending `--model <m>` only when `select.model_per_key[key]` exists (a `tier:trivial` downshift):

```
claude --bg [--model sonnet] "/flow <key> --auto"
```

Then close with the explicit line: **"printed only — nothing launched."** Empty `launch` → say so ("nothing launchable: backpressure / in-flight / empty backlog" per the select fields) and still print the closing line.
