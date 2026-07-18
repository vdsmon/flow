# Observed stabilization run design

## Context

Flow is mechanically green, but its ticket-to-PR pipeline has become difficult for
the maintainer to understand. The next phase optimizes for legibility, deletion,
lower verification cost, and bounded machinery rather than new capability.

The stabilization sequence is:

1. `flow-yahm`: unify the two read-only Git receipt implementations while observing
   the current pipeline.
2. `flow-10nd`: use the observation evidence to collapse unnecessary receipt and
   attestation ceremony.
3. `flow-qpgd`: delete the validated set of 185 redundant tests after the receipt
   surfaces stop changing.

This document specifies the first run only.

## Goals

- Deliver `flow-yahm` as a subtractive, reviewable change.
- Set `[reflect].machinery = false` so the run cannot grow the machinery backlog.
- Measure the complete planning and delivery timeline without optimizing it mid-run.
- Separate useful verification from repeated ceremony.
- Produce evidence that can constrain `flow-10nd` without filing new tickets.
- Give the maintainer a plain-language explanation of what happened and why.

## Non-goals

- Add new routes, roles, guards, review surfaces, or self-evolution features.
- Repair ordinary slowness while the experiment is running.
- Weaken the four load-bearing safety properties described in `docs/STATE.md`.
- Create machinery beads from reflection or from the observation report.
- Start `flow-10nd` or `flow-qpgd` before the first report is reviewed.

## Roles and model routes

One Codex child agent owns the Flow lifecycle. The parent agent is a read-only
observer and remains the maintainer-facing cockpit.

The run preserves the current workspace routes so the baseline is not confounded:

- planner: Codex `gpt-5.6-sol`, `xhigh`;
- plan assessor: Claude Code `opus`, `high`;
- implementer: Codex `gpt-5.6-luna`, `high`;
- review readers: Codex `gpt-5.6-sol`, `high`;
- E2E: Codex `gpt-5.6-luna`, `medium`;
- review fixer, if required: Codex `gpt-5.6-luna`, `high`;
- reflector: Codex `gpt-5.6-sol`, `high`.

The review brief remains disabled. Machinery reflection is changed to off as an
explicit acceptance outcome of `flow-yahm`.

## Execution protocol

The driver invokes logical `FLOW` for `flow-yahm` with additional intent to set
`reflect.machinery` to false. Planning remains read-only and returns through the
observer for the single human approval gate. After approval, the driver continues
through the normal full verification lane.

The driver reports a timestamp at every planning version, worker attempt, assessor
verdict, stage transition, retry, recovery, and durable stop. The observer checks the
durable evidence and maintains a temporary observation log outside the repository so
context compaction cannot erase the timeline.

The observer does not edit the plan, tracker, worktree, or run state. It intervenes
only for scope expansion, weakened safety, contradictory evidence, or a failed
documented recovery. Ordinary delay and repetition remain visible as experiment data.

## Planning observation

The report distinguishes three counters that must not be collapsed into "generation":

- **plan version**: a complete plan revision such as v1 or v2;
- **physical attempt**: a provider retry for the same plan version;
- **capsule generation**: an internal execution identity used by the worker engine.

For each plan version, record:

- planner and assessor wall time;
- the event that required a new version;
- feedback added, repeated, incorporated, or rejected;
- changes to acceptance criteria, approach, planned files, tests, and risks;
- whether the change was material or only wording, evidence, or receipt churn;
- revalidation caused by movement of the default branch;
- facade and receipt operations required before approval.

A revision is valuable when it changes correctness, scope, implementation, or
verification. Repackaging the same plan, repeating resolved feedback, or regenerating
solely because of coordination machinery is overhead.

At plan version 3, the observer flags churn without interfering. At 45 minutes of
total planning wall time, the observer sends the maintainer a timeline update.

## Delivery observation

For every stage, record total wall time, worker-active time where available, human
wait time, model route, attempts, retries, and recovery. Count every repeated
verification command and its cumulative cost. Record manual driver improvisation and
every facade or receipt operation whose purpose is not apparent from the resulting
artifact.

The current full lane remains unchanged for this baseline: targeted tests, full lint
and tests, seam checks, disposable E2E, and normal code review. Repetition is measured,
not removed during the run.

## Success and stop conditions

The experiment succeeds when:

- one shared Git receipt authority replaces the duplicate implementations;
- existing read-only protections are preserved or strengthened;
- `reflect.machinery` is false;
- the PR is green and reviewable;
- no new machinery tickets were created; and
- the observation report accounts for planning churn and delivery time.

Before approval, stop on unapproved scope, a weakened guard, or contradictory
evidence. After approval, allow one documented Flow recovery for a correctness or
safety failure, then stop and preserve evidence if the run remains unhealthy. Do not
stop for ordinary slowness, because that is the subject of the experiment.

## Report

The final report is concise and maintainer-oriented. It includes:

- a stage timeline with machine and human time separated;
- a plan-version and assessor-loop history;
- repeated verification commands and cumulative cost;
- lines added, removed, and duplicate code eliminated;
- manual recovery and improvisation;
- a `keep`, `simplify`, `remove`, or `insufficient evidence` verdict for each ceremony;
- bounded recommendations for `flow-10nd`.

The report does not create new tracker work. The maintainer reviews it before the
second stabilization ticket begins.
