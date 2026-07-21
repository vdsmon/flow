# Plan

Planning is an attended conversation owned by the driver. Its durable output is one complete
Markdown plan approved by the human. Flow does not maintain plan versions, feedback ledgers,
assessment receipts, approval receipts, or a second planning state machine.

Vocabulary is precise throughout this contract:

- **driver**: the main agent/session that talks to the human and continues the workflow;
- **human**: the user or maintainer who approves the plan;
- **host**: the Claude Code, Codex, or generic adapter that supplies agent and input tools.

Keep `owner` for real resource ownership such as leases, repositories, branches, or content.

## 1. Ground the work

The driver reads the ticket, relevant repository files, and directly applicable project
instructions. Fetch the default branch and record its SHA. Resolve factual questions read-only.
If an answer, access grant, permission, or scope choice is needed, the driver asks the human
directly through the host adapter's user-input capability and waits. An assessor never relays
those questions.

Optional memory or history reads are useful only when they answer a concrete planning question.
Do not expand planning into a general repository audit.

## 2. Write one complete plan

The driver writes and revises one canonical plan containing:

- the problem and intended outcome;
- current behavior and the smallest proposed design;
- exact files expected to change;
- constraints and behavior that must remain intact;
- implementation steps in dependency order;
- proportionate verification, including an E2E recipe only when behavior requires one; and
- the default-branch SHA used for inspection.

Prefer deletion and reuse over new layers. A revision replaces this conversational plan text. Do
not create a version graph, feedback object, schema, receipt, or model-authored envelope.

## 3. Run the adversarial confidence loop

Every plan receives an independent review. Launch one fresh independent agent through the host;
it acts as assessor and did not author the plan. Continue that same assessor for every
reassessment so it can verify its previous findings against the complete revised plan.

The assessor tries to disprove the plan. It looks for contradicted assumptions, missed callers or
invariants, unnecessary machinery, unverifiable claims, hidden access requirements, and scope that
is incomplete or excessive. A blocker must name a concrete failure mode with repository evidence
or a specific counterexample; vague preferences are not blockers.

The assessor assigns whole-number scores using this rubric:

| Dimension | Weight |
|---|---:|
| Repository grounding | 25% |
| Design correctness | 25% |
| Scope completeness | 20% |
| Verification quality | 20% |
| Operational feasibility | 10% |

Compute the weighted result without rounding. Display it to one decimal place, but only the
unrounded value determines the gate. The assessor returns concise Markdown with a verdict, the
overall score, all five category scores and deductions, blocking findings, non-blocking
deductions, and findings resolved since the prior pass. A score increase cites the changed plan
text or new repository evidence that earned it.

If confidence is below 90.0 or any blocker remains, the driver updates the same complete plan or
supplies concrete counter-evidence, then asks the same assessor to re-evaluate it. One autonomous
round permits at most three completed assessments. A failed assessor invocation returning no
assessment does not consume a pass.

If pass three still misses the gate, stop and show the current plan, scores, unresolved findings,
and the exact human decision, access, or evidence needed. A substantive human clarification may
start one new bounded round; a request to ignore the score may not.

If the assessor context is lost, one disclosed replacement is allowed for the entire planning
effort. Give it the complete current plan and prior findings. It scores from scratch and does not
reset the pass count. If that replacement is also lost, stop visibly.

## 4. Recheck the base

Immediately before the human gate, fetch the default branch again.

- Unchanged: continue.
- Proven-disjoint movement: update the recorded base and continue.
- Movement in a planned or behaviorally relevant path, including ambiguous overlap: update the
  plan against the new base and begin a new bounded assessment round.

## 5. Human gate

The gate opens only when the unrounded confidence is at least 90.0 and no blocker remains. Show:

- the exact complete plan;
- the recorded base SHA;
- weighted confidence and all category scores;
- completed pass count and whether a replacement assessor was used;
- findings resolved during assessment; and
- residual non-blocking risks.

The human approves that exact plan and evidence. No branch, worktree, run state, ticket mutation,
or approval artifact exists before explicit approval. A fresh unattended invocation stops here;
it cannot cross the gate.

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

`--branch` must begin with `feat/<ticket>` even when the repository normally uses
`fix/`, `bugfix/`, `chore/`, or another type prefix. Flow's reap, drain, selection,
and revision discovery identify newly minted ticket worktrees through that stable
prefix; `--commit-type` carries the actual change type. Do not translate a bug-fix
commit into a non-`feat/` Flow branch.

Do not pass `--recover-spill` automatically; it is an explicit operator recovery action.

Bootstrap preserves the isolated ticket worktree, single-ticket claim, current-base resolution,
atomic run state, planned-file ownership, and spill protection. It writes the approved text to
`stages/plan.out` and marks `plan` complete so delivery resumes at implementation. Bind
`result.worktree` as the absolute run root for every later operation. That absolute path is the
binding, not the convenience switch a host may offer.

The review brief remains an optional reviewer-facing output later in the pipeline. It is not part
of planning authorization.
