# Self-evolution

Flow improves itself through the same ticket-to-PR lifecycle it applies to delivery
work. The self-target workspace enables the machinery-edit guard, immutable ship
events, and the machinery-bead filing route; delivery workspaces never inherit
self-target merge authority.

## Producers

- **Producer A (the reflect stage)** is the continuous recorder. It records durable
  knowledge and friction and applies safe machinery edits through
  `machinery_edit.py`; it files no beads. A `MACHINERY:` entry carrying its
  evidence and a candidate dedup anchor is where reflect stops
  (`stage-reflect.md` owns the entry shape).
- **The foreman** is the only bead producer. Its sweep reads the recorded entries
  and friction logs across every workspace and mints beads with the file-anchored
  dedup keys, only for findings with a delivery-workspace witness or a second
  real-cost witness (`foreman.md` §Friction handling). Friction witnessed only by
  flow running itself stays a ledger line until a real witness arrives.
- **Recurrence escalation** (`friction_escalate`, invoked from reflect) files a
  `recurrent`-labelled bead when a claimed machinery fix did not hold. Propose-only,
  never auto-gated; the foreman's triage applies the same witness bar.

A single minting seat plus file-anchored keys is what keeps one defect one bead. A
quiet run is a valid result.

## Consumer

The foreman is the bounded consumer (`foreman.md` §Pickup). Between runs it reads
`bd ready`, triages with veto power, groups or merges related beads, and hands each
chosen ticket back to the human to run directly: attended planning, human
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
