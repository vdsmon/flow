# Stage: plan

The inline plan stage records the one human-approved Markdown plan authored by the driver. It does
not launch a planner agent and owns no plan-version or assessment transaction protocol.

Before mutation, the driver must complete `references/delivery-plan.md` end to end — the
claim, the read-only investigation, the plan, the adversarial assessment loop, the
confidence gate, the base recheck, and explicit human approval. That file is the whole
contract; this stage doc adds nothing to it.

For ordinary delivery, `worktree create` writes the approved text to `stages/plan.out` and marks
this stage complete before the pipeline starts. If a workspace invokes the stage directly, the
driver follows the same gate and writes the approved plan containing the design, expected files,
ordered implementation steps, verification, and base SHA.

A fresh unattended invocation stops without creating a branch, worktree, run, ticket mutation, or
approval artifact.
