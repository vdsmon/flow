# Ticket commands

This reference owns ticket authoring, grouping, and splitting. All tracker reads and
writes go through the tracker seam exposed by the absolute workspace facade. Do not
use a vendor-specific connector behind the seam.

## `FLOW ticket create [--request "<problem>"]`

Capture the problem, create a rich tracker ticket, then offer to deliver it. This
command does not design the solution; delivery planning belongs to the target path.

1. Read the available leaf types and parent tickets:

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" tracker --workspace-root . list-types
   FLOW_HARNESS="<harness>" "<facade>" tracker --workspace-root . list-epics
   ```

2. Use `--request` as the initial problem statement when supplied. Otherwise ask for
   a concise problem and the observed or desired outcome through the adapter's
   user-input capability. Gather summary,
   description, optional parent, and current-sprint preference. Infer a valid type
   from the returned vocabulary; never invent a backend type. Default assignee from
   workspace configuration and do not ask for labels or status.
3. Keep the body problem-focused. It may contain evidence and acceptance outcomes,
   but not a guessed implementation plan. If a text-humanizing skill is available,
   apply it before previewing.
4. Show the exact create payload and post-create operations. Confirm once. Editing
   returns to the preview; cancellation writes nothing.
5. Create through the seam, passing optional fields only when present:

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" tracker --workspace-root . create \
     --summary "<summary>" --description "<description>" --type "<type>" \
     [--parent "<parent>"] [--assignee "<account-id>"]
   ```

6. Best-effort, transition to the configured open state and add the active sprint
   when requested and supported. A backend that lacks sprint support degrades
   visibly but does not invalidate the created ticket.

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" tracker --workspace-root . list-sprints
   FLOW_HARNESS="<harness>" "<facade>" tracker --workspace-root . set-sprint \
     --key "<key>" --sprint-id "<sprint-id>"
   ```
7. Print `Created <KEY>: <summary>` and offer to run `FLOW <KEY>` immediately. A yes
   enters the ordinary target lifecycle in the same conversation; it does not bypass
   planning or approval.

On partial failure, report the created key and precisely which optional operation
failed. Never create a second ticket as retry compensation.

## `FLOW ticket group (<ticket>... | --mine) [--state open]`

Grouping proposes one run-level lead plus covered siblings. It is for tickets that
need one plan, one diff, and one PR. It is not a general project or label bucket.

1. Resolve exactly one selector: explicit tickets, or `--mine`. `--state open`
   constrains the assigned-ticket selector. Read candidates and duplicate hints:

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" group-candidates [<ticket> ...] [--mine] [--filter open] \
     --workspace-root .
   ```

2. Cluster only where there is concrete coupling: dependency edges, a shared parent
   plus shared implementation surface, or repository evidence that the tickets touch
   the same files or API. Labels and project membership alone are insufficient.
3. Verify file overlap by reading the bodies and inspecting the default-branch code.
   If overlap cannot be shown, keep those tickets separate.
4. Pick the lead by active work, existing branch, then substantive scope. The lead
   owns the lease, branch, run, and memory; covered tickets remain distinct closure
   obligations. A strict independently-landable order is a reason to split the work
   into sequential PRs, not to group it.
5. Confirm duplicate hints from body scope. Propose duplicate closure separately;
   never hide a duplicate in the covered set.
6. Render lead, covers, dependency/coupling evidence, solo tickets, and confirmed
   duplicates. Ask whether to run now, persist for later, or leave it as a read-only
   proposal.
7. For persistence, confirm and record the cover set as a marker comment on the
   lead; repeating the same set is a no-op, and a later plan derives it back:

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" group-persist persist \
     --lead "<lead>" --covers "<c1>,<c2>" --workspace-root .
   ```

8. For run now, enter `FLOW <lead> <key1> <key2> --together` in the same
   conversation. The target path revalidates freshness and groupability before the
   gate.

The proposal phase is read-only. Tracker comments, duplicate transitions, and run
bootstrap occur only after their explicit confirmation.

## `FLOW ticket split <ticket>`

Split a wide refactor into an expand, migrate, contract ladder whose children each
land green from the default branch once their declared blockers are merged. The
front half is read-only; child creation and dependency wiring occur after one
explicit approval.

1. Fetch the parent and relevant memory. Refuse a second split when the durable
   child marker already exists; show the recorded frontier instead.
2. Map every definition, call site, import edge, schema surface, and test affected by
   the change. Group migration sites into independently verifiable batches.
3. Design:

   - one additive expand child that makes old and new coexist;
   - one or more migrate children, each moving a coherent site batch;
   - one contract child that deletes the old surface after every migration.

4. Refuse the split when old and new cannot coexist, the batches cannot remain green
   independently, or the blast radius fits one reviewable PR. Explain the missing
   compatibility seam or coupling that causes the refusal.
5. Present a table of child scope, blocker edges, verification, and why each child is
   independently green. Claude Code uses its native plan boundary. Codex uses native
   Plan mode when active or a soft end-of-turn approval boundary otherwise.
6. After approval, create all children through the tracker seam, write the parent
   marker, then add dependency edges. This order makes an interrupted write
   discoverable and forward-resumable:

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" tracker --workspace-root . create \
     --summary "<rung>" --description "<scope and green rationale>" --type "<leaf-type>"
   FLOW_HARNESS="<harness>" "<facade>" tracker --workspace-root . comment \
     --key "<parent>" --text "flow-split children: <expand>, <migrate...>, <contract>"
   FLOW_HARNESS="<harness>" "<facade>" tracker --workspace-root . link \
     --from-key "<blocked-child>" --to-key "<blocker>"
   ```

7. Verify the resulting graph from the tracker and print the ready frontier as
   `FLOW <child>` invocations. Do not start a child automatically.

If interrupted between child creation and the parent marker, search the tracker by
the approved rung summaries, write the marker with the existing keys, and continue
forward. Never mint a duplicate ladder from scratch.

## `FLOW ticket finalize <ticket> [--dry-run]`

Close out one delivered ticket after its PR merged. A delivery workspace parks
a green PR for the human, and the human merges it on the forge; nothing on that path
transitions the ticket, freezes the ship event, deletes the remote branch, or reaps
the worktree (the janitor sweep requires an already-terminal tracker state, so a
merged-but-open ticket preserves its worktree forever). Finalize sequences those
existing primitives once, gated on merged-PR proof.

Run it from the primary checkout, never from inside the ticket's own worktree — the
reap removes that directory. A driver standing in the doomed worktree changes to the
primary checkout first and invokes the primary checkout's facade; the command refuses
(exit 4) when invoked from the worktree it would remove.

```bash
FLOW_HARNESS="<harness>" "<facade>" finalize --workspace-root . --key <KEY> [--dry-run]
```

The probe writes nothing: it locates the ticket's managed worktree (or its unique
local branch when the worktree is already gone) and requires a merged PR for that
branch through the forge seam. An open PR, or no PR, exits 3 with zero writes. A live
or corrupt run lease, or a worktree tip that does not match the merged PR head,
refuses with exit 4 and preserves everything.

On merged proof, the sequence mirrors the evolve-drain reap, every step idempotent
and best-effort once the probe passes:

1. transition the ticket to its done state through the tracker seam (skipped when
   already terminal);
2. freeze the ship event (`observe-at-close`) before the run state it reads is
   destroyed;
3. delete the remote branch through the forge seam (a forge that auto-deleted it at
   merge reports failed_or_absent, which is fine);
4. reap the local worktree and branch (lease-gated; dirty work is checkpointed to a
   `flow-rescue/*` ref before removal, and a failed checkpoint preserves the
   worktree).

A step whose outcome already holds is skipped, so re-running converges: a fully
finalized ticket exits 0 as a no-op. Exit 3 makes the merge watch host-owned and
daemon-free — the human (or a host scheduler the human configures) re-invokes
finalize until it exits 0. Flow itself never schedules, backgrounds, or polls; the
single-shot command is the whole contract.
