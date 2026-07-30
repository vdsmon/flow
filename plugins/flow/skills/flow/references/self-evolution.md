# Self-evolution

Flow improves itself through the same ticket-to-PR lifecycle it applies to delivery
work. The self-target workspace enables the machinery-edit guard, immutable ship
events, and the machinery-bead filing route; delivery workspaces never inherit
self-target merge authority.

## Producers

- **Producer A — the reflect stage** is the continuous producer. It records durable
  knowledge and friction, applies safe machinery edits through `machinery_edit.py`,
  and files a `MACHINERY:` finding as a `machinery` bead when a change
  cannot safely land inside the current run (`stage-reflect.md` owns the filing
  recipe and its dedup keys).
- **The foreman** files independently from outside the run, with the same recipe
  and the same file-anchored dedup keys, for friction only the outside view can see:
  stalls, silent retries, cross-run patterns (`foreman.md` §Report and filing).
- **Recurrence escalation** (`friction_escalate`, invoked from reflect) files a
  `recurrent`-labelled bead when a claimed machinery fix did not hold. Propose-only,
  never auto-gated.

All producers deduplicate against open and closed tracker evidence, so two producers
observing the same defect converge on one bead. A quiet run is a valid result.

## Consumer

The foreman is the bounded consumer (`foreman.md` §Pickup). Between runs it reads
`bd ready`, triages with veto power, groups or merges related beads, and routes each
chosen ticket through an ordinary managed pipeline run — attended planning, human
plan approval, review, CI. On a met gate the foreman merges (`foreman.md` §Merging);
a hot change additionally passes the independent guard-property review before it
lands. Delivery workspaces and held changes remain human-merge.

Filing and triage are self-target-only. Confirm the route before consuming the
queue:

```bash
FLOW_HARNESS="<harness>" "<facade>" maintainer --workspace-root . --require-current
```

A refusal stops the pickup and names any configured target outside the invoking
repository — machinery beads are then not this workspace's to work.

## Guardrails

- `machinery_edit.py` is the only in-run self-edit path. It serializes edits and
  records ownership for commit review.
- A machinery fix never commits onto a protected branch: `machinery_edit.py`
  refuses a skill root on main/master/dev/develop, and the finding routes to
  propose-and-record instead.
- Never edit `stage-registry.toml` or a wired handler while the run snapshots it.
  File the change, or use the evidence-specific `FLOW workspace repair <target>`
  path and confirm a snapshot reload.
- Never advance or update the flow main checkout while any base or revision lease
  is live.
- Read-only discovery workers are accepted only when HEAD, index, tracked worktree,
  and untracked-worktree snapshots are unchanged.
- The review stages and the foreman's merge checklist independently check the
  resulting diff. The producer's confidence is not merge authority.
- Immutable ship events and friction records drive `FLOW measure` outcomes; tracker
  status alone is not delivery evidence.

The run-and-merge mechanics live in `foreman.md`.
