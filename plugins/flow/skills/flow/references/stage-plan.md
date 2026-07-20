<!-- flow:activation-truth:begin -->
# Stage: plan

The plan stage records the one human-approved Markdown plan produced by the host-native planning
conversation. It does not launch a routed planner or assessor and owns no plan-version protocol.

For ordinary delivery, `worktree create` writes the approved text to `stages/plan.out` and marks
this stage complete before the pipeline starts. If a workspace invokes the stage directly, the
owner must write one complete plan containing the intended design, expected files, ordered
implementation steps, verification, and base SHA, then wait for human approval before mutation.
Use the adapter's user-input capability for that approval.

Planning is attended during stabilization. An unattended invocation must stop without creating a
branch, worktree, run, ticket, or approval artifact.
