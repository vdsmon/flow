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
7. For persistence, confirm and invoke the internal group-persistence seam with the
   lead and complete covered-ticket set. It writes an idempotent marker on the lead;
   repeating the same set is a no-op.

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
