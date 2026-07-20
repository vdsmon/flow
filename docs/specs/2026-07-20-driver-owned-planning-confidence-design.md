# Driver-owned planning confidence loop

## Motivation

Flow planning needs a live relationship with the human. Repository access may be
missing, a permission boundary may need approval, or a factual question may change
the scope. Giving plan ownership to a separate planner agent makes those ordinary
interactions indirect: the driver must relay questions and reconstruct context
that it already holds.

The main driver therefore owns planning. Independent challenge remains valuable, but
it is a review of the driver's plan rather than a second planning system. The goal is
to combine direct human collaboration with adversarial confidence evidence while
avoiding the plan versions, feedback ledger, receipts, schemas, provider routes, and
automatic replanning that previously made planning expensive and difficult to
understand.

## Responsibilities

### Driver

The driver is the main agent/session and the sole plan author and human cockpit. In
this design, **human** means the user or maintainer at the approval gate and **host**
means the Claude Code, Codex, or generic adapter. **Owner** remains available for real
resource ownership such as leases, repositories, branches, and content. The driver:

- reads the ticket, current repository, project instructions, and directly relevant
  history;
- asks every human question and requests required access or permission;
- writes and revises one canonical Markdown plan;
- launches and continues the independent assessor;
- responds to assessor findings with plan changes or concrete counter-evidence;
- rechecks the default-branch base before the gate; and
- presents the plan and confidence evidence for human approval.

The ordinary `plan` handler is `inline`. There is no planner subagent in the planning
path.

### Assessor

Every plan receives a review from one fresh host-native agent that is independent
from the driver. The same assessor context is continued across reassessments so it
can verify whether its findings were actually resolved without rereading the entire
problem as a stranger on every pass.

The assessor challenges the plan but does not author it, edit the repository, ask the
human questions, or create a second canonical artifact. Its reports remain
conversational evidence owned by the driver.

## Planning sequence

1. The driver investigates the ticket and repository read-only, resolving factual,
   access, permission, and scope questions with the human.
2. The driver writes one complete Markdown plan grounded at a recorded default-branch
   SHA.
3. The driver gives the plan and relevant repository context to one fresh independent
   assessor.
4. The assessor returns an adversarial assessment, category scores, weighted
   confidence, blockers, deductions, and the changes required to reach the gate.
5. If the plan is below the gate, the driver updates the same plan or supplies
   repository evidence that rejects an incorrect finding.
6. The same assessor re-evaluates the complete revised plan.
7. The driver and assessor repeat steps 5 and 6 for at most three completed assessor
   passes in one autonomous round.
8. When confidence is at least 90 percent and no blocking finding remains, the driver
   presents the plan and confidence evidence to the human.
9. Only explicit human approval permits bootstrap. The approved plan is then written
   to the existing `plan.out` path through the ordinary worktree creation flow.

There is one plan throughout. A revision replaces the conversational plan text; it
does not create a numbered version, feedback object, approval receipt, or durable
assessment record.

## Adversarial assessment

The assessor tries to disprove the plan rather than validate it politely. It searches
for:

- assumptions contradicted by current code or configuration;
- missing callers, files, downstream effects, invariants, or failure paths;
- a design that introduces unnecessary architecture or orchestration;
- verification that cannot prove the claimed behavior;
- hidden access, permission, tool, environment, or deployment requirements;
- scope that is either incomplete or larger than the requested outcome; and
- contradictions between the plan, repository evidence, and stated constraints.

A blocking finding names a concrete failure mode and cites repository evidence or a
specific counterexample. Generic preferences, speculative risks without an observed
seam, and requests for more testing that do not identify an unproven claim are not
blockers.

The driver may reject a finding with concrete evidence. The assessor, not the driver,
decides whether that evidence removes the deduction on the next pass.

## Confidence contract

The assessor assigns whole-number scores from 0 through 100. The overall confidence
is the weighted result. Report it to one decimal place, but evaluate the gate against
the unrounded value:

| Dimension | Weight | Question |
|---|---:|---|
| Repository grounding | 25% | Does the plan accurately describe current behavior and the affected seams? |
| Design correctness | 25% | Will the proposed behavior work while preserving required invariants? |
| Scope completeness | 20% | Are all necessary changes included without unrelated expansion? |
| Verification quality | 20% | Can the proposed checks prove the intended outcome proportionately? |
| Operational feasibility | 10% | Are access, permissions, tools, and execution conditions understood? |

The assessor owns the score. A score cannot increase merely because another pass
occurred; each increase cites the revised plan text or new repository evidence that
removed a deduction.

The plan reaches the human approval gate only when:

```text
weighted confidence >= 90.0
and unresolved blocking findings == 0
```

A high numerical score cannot override a blocker, and the human cannot waive the
confidence threshold without first changing the plan, scope, or available evidence.

## Assessor response

The assessor returns concise Markdown, not a machine-validated schema:

```text
Verdict: REVISE | GATE_READY
Confidence: <weighted score>%

Scores
- Repository grounding: <score>/100: <evidence and deduction>
- Design correctness: <score>/100: <evidence and deduction>
- Scope completeness: <score>/100: <evidence and deduction>
- Verification quality: <score>/100: <evidence and deduction>
- Operational feasibility: <score>/100: <evidence and deduction>

Blocking findings
- <finding, concrete failure mode, repository evidence>

Non-blocking deductions
- <deduction and what would restore confidence>

Resolved since the prior pass
- <finding and the plan/evidence that resolved it>
```

Empty finding sections say `None`. The format exists for human legibility and prompt
clarity; Flow does not parse, attest, persist, or hash it.

## Convergence and stopping

One autonomous planning round permits at most three completed assessor passes. If the
third pass remains below 90 percent or retains a blocker, the driver stops and
reports a planning block containing:

- the current complete plan;
- the latest overall and category scores;
- every unresolved blocker and deduction; and
- the exact human clarification, access, evidence, or scope decision needed.

A substantive human clarification, access grant, or scope change may start a new
bounded three-pass round. This is human-directed continuation, not an automatic
fourth pass. A simple instruction to ignore the score does not start a new round.

## Assessor loss

If the original assessor context becomes unavailable, the driver may launch at most
one replacement assessor across the entire planning effort. The replacement receives
the current complete plan and the prior findings, scores the plan independently from
scratch, and is disclosed at the human gate.

Replacement does not reset the current round's three-pass count. A failed invocation
that returns no assessment is not a completed pass. If the replacement is also lost,
planning stops visibly; there is no provider retry engine or self-assessment fallback.

## Base movement

Immediately before the human gate, the driver fetches the default branch again.
Proven-disjoint movement updates the recorded base without invalidating confidence.
Movement in a planned or behaviorally relevant path invalidates the assessment. The
driver updates the plan against the new base and begins a new bounded assessment
round. Ambiguous overlap is relevant and does not fail open.

## Human gate

The driver presents:

- the exact complete plan that will seed the run;
- the recorded base SHA;
- overall confidence and the five category scores;
- the number of assessment passes and whether a replacement assessor was used;
- the findings resolved during assessment; and
- any residual non-blocking risks.

The human approves that exact plan and evidence. No branch, worktree, run state,
ticket mutation, or approval artifact exists before approval.

## Implementation boundary

Implementation changes the surviving planning instructions and configuration, plus
the shared drain decision needed to enforce the same gate for fresh unattended work:

- make the stage-registry plan handler and this workspace's plan handler `inline`;
- update `SKILL.md`, `references/delivery-plan.md`, and
  `references/stage-plan.md` to give the driver plan ownership;
- encode the mandatory assessor loop, adversarial posture, confidence rubric,
  convergence cap, replacement rule, base recheck, and gate display; and
- replace the drains' fresh `launch` action with `plan_required`, after recovery and
  waiting for existing work, so unattended maintenance cannot bypass human approval;
- reconcile the simplification map with the final planning contract.

It does not add runtime state, assessor commands, schemas, model routes, session-id
persistence, plan-version files, receipts, compatibility handling, or another
planning module.

## Verification

Verification stays proportional to this prose-and-configuration change:

- validate the live workspace and a freshly rendered workspace configuration;
- run the public-command and prose-to-CLI seam checks;
- run focused registry, setup, and planning-contract tests;
- run the repository lint and existing full test suite once before publication; and
- rely on CI rather than adding a test-only E2E or tests that pin whole prose
  paragraphs.

The design is complete when ordinary planning is visibly driver-authored, every plan
receives one bounded adversarial assessor loop, the human gate reports confidence,
and no planner-worker or assessment transaction machinery is reintroduced.
