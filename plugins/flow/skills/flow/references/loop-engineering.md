# Loop engineering

Flow has three nested feedback loops:

1. The inner loop is `FLOW <target>`: plan, approve, deliver, review, and record a
   durable receipt.
2. The middle loop is `FLOW maintain backlog drain` or
   `FLOW maintain evolution drain`: an owner session repeatedly classifies durable
   evidence and runs a bounded worker fleet.
3. The outer loop is an optional scheduler that invokes producer and consumer owner
   sessions on a cadence. It is operational infrastructure, not another worker or
   job system.

Each loop has a distinct done signal. A ticket loop ends in a ready PR, terminal
receipt, durable decision, or evidence-specific block. A drain ends when nothing is
launchable or blocking within its bounded turn. A scheduled fire records explicit
start/end/outcome evidence. No loop infers completion from a vanished process handle.

## Worker ownership

The active Claude Code or Codex conversation is the owner. Native collaboration
agents are bounded by `min(configured, host_capacity - 1)`, leaving one slot for the
owner. The owner registers durable fleet intent before launch, waits through the host
adapter, and cancels its own handles on ordinary exit. After owner loss, a new owner
reconstructs the correct action from run state, lease, fleet, worktree, tracker, and
PR evidence.

Discovery workers are read-only. Their result is discarded before any ticket filing
when their pre/post git snapshot differs. This turns “read-only” from a prompt claim
into a checked property.

## Feedback and measurement

Reflection records friction and reusable knowledge at delivery time. Producers turn
recurrent evidence into bounded work. Drains deliver accepted work. Immutable ship
events close the loop, and `FLOW measure throughput|lead-time|friction|reverts|trend`
tests whether the machinery improved outcomes rather than merely producing activity.

The optional scheduler templates in `ops/` invoke synchronous owner sessions for
the logical maintain commands. They do not use detached host CLIs, transcript polling,
host job directories, or self-teardown.
