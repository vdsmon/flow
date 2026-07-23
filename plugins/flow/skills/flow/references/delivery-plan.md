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

Planning's first act mutates the ticket: transition it to `in_progress` in the tracker backend
(Atlassian MCP first when available; REST fallback):

```bash
FLOW_HARNESS="<harness>" "<facade>" tracker \
  --workspace-root . \
  transition --key <KEY> --to-state in_progress
```

The claim is best-effort and never blocks planning: exit 3 (already `in_progress`, or the
tracker has no such state) continues silently; any other failure logs one warning and
continues. The point is that the team sees the ticket claimed the moment work starts, not
after approval. This is the one sanctioned ticket mutation before the human gate.

The driver reads the ticket, relevant repository files, and directly applicable project
instructions. Fetch the default branch and record its SHA. Resolve factual questions read-only.
If an answer, access grant, permission, or scope choice is needed, the driver asks the human
directly through the host adapter's user-input capability and waits. Raise such a blocker as soon
as it is discovered; do not navigate around it toward an alternative path unless the detour is
very short and obviously equivalent. Working around a missing grant or decision wastes time and
tokens and drifts the plan toward a less precise result. An assessor never relays those
questions.

Optional memory or history reads are useful only when they answer a concrete planning question.
Do not expand planning into a general repository audit.

When the ticket names a concrete failing artifact — a generated file, a payload, a load id —
fetch and inspect the real artifact read-only during grounding. The actual bytes settle questions
code reading cannot, and they anchor the plan's verification to reality.

## 2. Write one complete plan

The driver writes and revises one canonical plan containing:

- the problem and intended outcome;
- current behavior and the smallest proposed design;
- exact files expected to change;
- constraints and behavior that must remain intact;
- implementation steps in dependency order;
- proportionate verification, including an E2E recipe only when behavior requires one; and
- the default-branch SHA used for inspection.

Write the plan in basic English: simple words, short sentences, as brief as completeness
allows, for a reader arriving with little context. Name files and behaviors explicitly, spell
out abbreviations on first use, and cut anything that does not change what gets built or
verified.

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

The assessor assigns each dimension a whole-number score from 0 to 100 using this rubric:

| Dimension | Weight |
|---|---:|
| Repository grounding | 25% |
| Design correctness | 25% |
| Scope completeness | 20% |
| Verification quality | 20% |
| Operational feasibility | 10% |

Compute the weighted result without rounding. Display it to one decimal place, but only the
unrounded value determines the gate. The assessor returns concise Markdown with a verdict, the
overall score, all five category scores each displayed out of 100, deductions, blocking findings,
non-blocking deductions, and findings resolved since the prior pass. A score increase cites the changed plan
text or new repository evidence that earned it.

If confidence is below 90.0 or any blocker remains, the driver updates the same complete plan or
supplies concrete counter-evidence, then asks the same assessor to re-evaluate it. One autonomous
round permits at most three completed assessments. A failed assessor invocation returning no
assessment does not consume a pass. An idle or acknowledgement signal without the full verdict is
the same failed invocation: prompt the same assessor once for the verdict.

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

This recheck and its re-assessment remedy run before presentation. The post-convergence recheck
in section 5 is settled with the human directly and never re-enters the assessment loop.

## 5. Human gate

The gate opens only when the unrounded confidence is at least 90.0 and no blocker remains. Show:

- the exact complete plan;
- the recorded base SHA;
- weighted confidence and all category scores;
- completed pass count and whether a replacement assessor was used;
- findings resolved during assessment; and
- residual non-blocking risks.

Present through the Lavish plan surface when its gate passes (`references/plan-surface.md`); on
a failed gate, fall back to this plain presentation plus one visible
`Lavish plan surface: skipped — <reason>` line, never silently. From presentation onward,
revision is strictly between the human and the driver: annotations revise the plan and the
surface re-renders, and nothing re-enters the assessment loop; the displayed evidence stays as
assessed. After the surface's end-session signal, fetch the default branch once more: unchanged
or proven-disjoint movement proceeds to approval; movement in a planned or behaviorally
relevant path is shown to the human as a plan delta and settled directly, without an assessor.

The human approves that exact plan and evidence. No branch, worktree, run state, or approval
artifact exists before explicit approval; the ticket status claim made when planning began is
the one prior mutation. A fresh unattended invocation stops here;
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
