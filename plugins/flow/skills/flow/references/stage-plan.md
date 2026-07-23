# Stage: plan

The inline plan stage records the one human-approved Markdown plan authored by the driver. It does
not launch a planner agent and owns no plan-version or assessment transaction protocol.

Before mutation, the driver must complete `references/delivery-plan.md`: claim the ticket
(`in_progress`, best-effort), investigate read-only,
write one plan, obtain the mandatory adversarial assessment, revise with the same assessor for no
more than three completed passes in the round, reach unrounded confidence of at least 90.0 with no
blockers, recheck the base, present the confidence evidence, and receive explicit human approval.

For ordinary delivery, `worktree create` writes the approved text to `stages/plan.out` and marks
this stage complete before the pipeline starts. If a workspace invokes the stage directly, the
driver follows the same gate and writes the approved plan containing the design, expected files,
ordered implementation steps, verification, and base SHA.

A fresh unattended invocation stops without creating a branch, worktree, run, ticket mutation, or
approval artifact.
