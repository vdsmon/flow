# Background execution and rooted execution

Backgrounding is a host operation applied to the driver conversation. It does not
change Flow's lifecycle, evidence, or rooted-execution contract.

## Driver contract

The driver that crosses the plan gate retains continuation. It binds the returned
absolute worktree as `run_root`, uses `<run_root>/.flow/runtime/flow`, and roots every
command, read, edit, artifact, and worker prompt there. A host convenience such as a
native workspace switch never replaces the absolute binding.

If the human backgrounds that conversation, the same driver keeps driving dispatcher
descriptors and refreshing the lease. Flow does not spawn a detached `claude` or
`codex` CLI, inspect host job directories, poll transcripts, stop a host session, or
delete session files. A foreground driver and a backgrounded driver are the same
lifecycle state.

## Worker contract

Maintenance driver sessions create workers through host-native collaboration tools and
call the `worker-pool` facade for capacity, git-guard, and recovery decisions. Worker
handles are scoped to that driver and may disappear with it.
Durable run, fleet, lease, worktree, tracker, and PR evidence decides whether a later
driver monitors, relaunches, repairs, or reports settled work.

Read-only workers receive absolute roots and are guarded by pre/post git snapshots.
Any mutation invalidates their result and stops the batch before filing or applying
work.

## Worktree-local and shared state

Runtime layout v2 separates executable metadata and memory:

```text
.flow/runtime/{flow,skill-root,memory-root,layout-version}
.flow/memory/<namespace>/
```

Each worktree has local run state under `.flow/runs/<ticket>/` and points its
`runtime/memory-root` at the main workspace's shared `.flow/memory` base. Knowledge,
friction, usage, fleet, and ship-event evidence therefore survives worktree teardown.
The workspace configuration remains byte-identical; machine-local absolute pointers
live only in gitignored runtime metadata.

## Attended and unattended stops

An attended driver may ask the human a live question. A fresh unattended target stops
before bootstrap. During already-approved unattended delivery, a new question is
recorded durably and the ticket defers or blocks according to policy instead of
parking for live input. Infrastructure failure does not manufacture a product
decision. Both modes release acquired leases on exit and leave enough durable
evidence for the next `FLOW <target>` invocation to choose the safe action.
