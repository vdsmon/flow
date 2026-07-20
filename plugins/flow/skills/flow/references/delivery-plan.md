<!-- flow:activation-truth:begin -->
# Plan

Planning is an attended, host-native conversation. Its durable output is one complete Markdown
plan approved by the human. Flow does not maintain plan versions, feedback ledgers, assessment
receipts, approval receipts, or a second planning state machine.

## 1. Ground the work

Read the ticket, the relevant repository files, and any directly applicable project instructions.
Fetch the default branch and record its SHA before planning. If the ticket is ambiguous in a way
that changes scope or architecture, use the adapter's user-input capability and wait.

Optional memory or history reads are useful only when they answer a concrete question about the
ticket. Do not expand planning into a general repository audit.

## 2. Produce one plan

Use one fresh host-native planner context to inspect the repository and return one complete plan.
The plan must be understandable without hidden model state and include:

- the problem and intended outcome;
- the current behavior and the smallest proposed design;
- exact files expected to change;
- important constraints and preserved behavior;
- implementation steps in dependency order;
- proportionate verification, including an E2E recipe only when the behavior needs one;
- the base SHA used for the inspection.

Prefer deletion and reuse over new layers. A planning revision edits this same plan. Do not create a
version graph, replay feedback through another worker, or require a model-authored envelope.

## 3. Assess only when risk justifies it

For a hot, high-risk, or genuinely unclear change, ask one fresh independent agent to challenge the
plan. It returns concise findings to the owner; it does not author another canonical artifact. The
owner incorporates useful findings into the same Markdown plan and shows that plan to the human.

Ordinary bounded tickets do not require an assessor.

## 4. Human gate

Show the exact Markdown plan that will seed the run. Approval means approval of that text and its
scope. Requested changes update the same plan and return to this gate.

During stabilization, unattended planning stops here without creating a branch, worktree, run, or
ticket. It never bypasses the human gate.

## 5. Recheck the base

Immediately before bootstrap, fetch the default branch again.

- If the base is unchanged, continue.
- If it changed only in paths disjoint from the plan, update the recorded base and continue.
- If relevant paths changed, or overlap is ambiguous, return the same plan to the human with the
  new evidence. Do not manufacture a new plan version automatically.

## 6. Bootstrap the approved plan

Write the approved Markdown to a plan file and create the ticket worktree:

```bash
FLOW_HARNESS="<harness>" "<facade>" worktree create \
  --ticket "<ticket>" \
  --plan-from "<approved-plan.md>" \
  --base "<approved-base-sha>" \
  --branch "feat/<ticket-slug>" \
  --main-root "<workspace-root>" \
  --planned-files "<comma-separated-paths>" \
  --commit-type "<type>" \
  --commit-summary "<summary>" \
  --e2e-recipe "<recipe or skip: reason>"
```

Repeat `--route profile=harness,model,effort` only for deliberate post-plan worker overrides.
Do not pass `--recover-spill` automatically; it is an explicit operator recovery action.

The bootstrap keeps the safety boundaries that matter: isolated ticket worktree, single-ticket
claim, current-base resolution, atomic run state, frozen post-plan route snapshot, planned-file
ownership, and spill protection. It writes the approved text to `stages/plan.out` and marks `plan`
complete so delivery resumes at implementation.

Treat `result.worktree` as the absolute run root for all later operations. That absolute path is the
binding, not the convenience switch a host may offer for entering a worktree.

The review brief remains an optional, reviewer-facing output later in the pipeline. It is not part
of planning authorization and does not make the planning path heavier.
