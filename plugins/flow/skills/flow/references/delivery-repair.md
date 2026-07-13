# Delivery diagnosis and repair

Repairs are evidence-specific, target-specific, and confirmation-gated. Diagnose
without mutation first:

```bash
FLOW_HARNESS="<harness>" "<facade>" recover detect --ticket "<ticket>" --workspace-root .
```

The report includes base/revision lease classification, holder liveness when known,
failed stages, snapshot integrity, state backup availability, worktree health, and
ship-event attention. Process liveness is advisory: pids can be reused and remote
holders cannot be probed. Lease state remains authoritative.

## Applicable actions

### Expired or stale lease

Offer takeover only when the lease is expired or reboot-clearable. Show that takeover
clears the lock, rotates ownership, resets in-progress stages to pending, and refreshes
the snapshot. Confirm before applying. If the lease still classifies live, ordinary
takeover refuses.

A live-lease force is operator-explicit and available only here. Show holder identity,
age, and liveness evidence, ask the operator to assert the holder is dead, then confirm
the exact target. Never infer deadness automatically.

### Failed stage

Offer only:

- retry the named stage;
- skip it with an explicit receipt;
- abort the target run.

Confirm each. Skip is not a generic success and must remain visible in the run
receipt. Abort refuses a foreign live lease unless the operator performs the same
explicit dead-holder assertion.

### Snapshot or engine drift

Show changed components and whether they are committed, owned by planned files, or
foreign. Offer snapshot reload to accept current workspace machinery, or abort. Never
reload automatically over dirty or ambiguous engine changes.

### Corrupt state or lock

Preserve and quarantine corrupt files. Restore the newest valid state backup when one
exists, then re-verify non-idempotent external effects before replay. Replacing state
with no valid backup is an explicit destructive repair because it can replay delivery;
show that risk and require confirmation. A corrupt lock is quarantined only during a
confirmed takeover.

### Ship-event attention

Duplicate or corrupt ship events are not automatically reconciled. Name every file,
hash, and contradiction and stop. Do not delete the evidence or continue to a delivery
action until an operator establishes the authoritative receipt.

### Worktree loss or drift

A terminal run with an open PR may reseed its worktree from the PR branch for a
revision. A stranded pre-PR worktree is checkpointed to a rescue ref before any reap;
capture failure leaves it intact. Content-ownership drift never uses force.

## Continue after repair

After a successful write, run the read-only diagnosis and full target evidence probe
again. Feed the new evidence through the lifecycle reducer. Continue in the same
invocation when it returns `resume`, `revise`, or `show`; stop on `running`, remaining
`repair`, or `conflict`. Never chain two repairs based on stale pre-repair evidence.
