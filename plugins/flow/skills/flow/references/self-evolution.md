# Self-evolution

Flow improves itself through the same ticket-to-PR lifecycle it applies to user
work. The maintainer workspace enables the machinery-edit guard, immutable ship
events, and evolution policy; user workspaces never inherit maintainer merge
authority.

## Producers

- `FLOW maintain evolution audit` finds concrete correctness, robustness, test,
  documentation, and measurement defects. Read-only discovery workers produce
  evidence; the owner verifies and files bounded evolution fixes.
- `FLOW maintain evolution propose` turns observed friction and architectural gaps
  into attended proposal tickets. Judgment work is not silently mixed into the
  autonomous fix queue.
- `FLOW maintain evolution epic` works at initiative altitude and files only epic
  parents plus a lazy child preview. `FLOW maintain evolution expand <epic>` is the
  explicit, confirm-gated materialization step.
- The reflect stage is the continuous producer. It records durable knowledge and
  friction, applies safe machinery edits through `machinery_edit.py`, and files work
  when a change cannot safely land inside the current run.

All producers deduplicate against open and closed tracker evidence. A quiet audit is
a valid result.

## Consumer

`FLOW maintain evolution drain` is the bounded consumer. Its owner session
classifies durable fleet, run, lease, worktree, tracker, CI, and PR evidence; reaps
settled work; launches ready tickets as `FLOW <key> --unattended`; waits; and
reclassifies until done or its iteration bound is reached.

Workers are host-native collaboration agents. The owner uses the executable
`worker-pool` reducers to reserve one host slot, guard discovery reads, and classify
owner recovery. Handles are disposable; durable evidence is authoritative
after an owner disappears. Flow never launches a detached host CLI, scans a host job
directory, stops host sessions, or schedules self-teardown. A user may background
the owner conversation through the host without changing these rules.

Ordinary evolution fixes may self-merge only inside the configured maintainer
envelope after green CI and all guard checks. Hot changes serialize and require the
high-scrutiny lane; user projects and held changes remain human-merge.

## Guardrails

- `machinery_edit.py` is the only in-run self-edit path. It serializes edits and
  records ownership for commit review.
- Never edit `stage-registry.toml` or a wired handler while the run snapshots it.
  File the change, or use the evidence-specific `FLOW workspace repair <target>`
  path and confirm a snapshot reload.
- Never advance or update the maintainer checkout while any base or revision lease
  is live.
- Read-only discovery workers are accepted only when HEAD, index, tracked worktree,
  and untracked-worktree snapshots are unchanged.
- The review and merge stages independently check the resulting diff. The producer's
  confidence is not merge authority.
- Immutable ship events and friction records drive `FLOW measure` outcomes; tracker
  status alone is not delivery evidence.

The full command mechanics live in `command-maintain.md`. The feedback-loop model
lives in `loop-engineering.md`.
