# Flow simplification map

## Purpose

Give the maintainer a durable, plain-language map of the architecture added after
`eb1e2d9`, identify what is core, what is valuable but ancillary, and what is
orchestration machinery, then define a safe deletion order. This document is an
understanding aid and the design for the first deletion slice. It does not authorize
later slices automatically.

## Current posture

The freeze from PR #539 is active on `main`. This repository no longer targets itself
for maintenance, automatically merges hot work, compounds memory, creates machinery
tickets through reflection, or runs `e2e`, `review_brief`, `reflect`, and `merge` in
its own pipeline. The corresponding plugin features still exist in the runtime.

The incomplete `flow-yahm` work is preserved on
`archive/flow-yahm-2026-07-18` and in a checksummed binary patch. It is evidence, not
work to finish. `flow-10nd` and `flow-qpgd` remain stopped.

No history rewrite or hard revert is planned. `eb1e2d9` is a conceptual target: it
retains native Codex support and the state-aware command while preceding the route,
planning-transaction, review-brief, and capsule expansion. Simplification proceeds
through forward deletion.

## Original intent and target

The useful idea from `PiLastDigit/TRIP-workflow` was logical role separation: a
planner concentrates on the plan, an implementer writes, and a fresh reviewer
challenges the result while one owner remains the human cockpit. That idea does not
require a swarm, private repository clones, exact provider proof, or a distributed
transaction protocol.

The target is:

```text
ticket
  -> one fresh native planner and one saved plan
  -> human approval
  -> one ticket worktree
  -> direct implementation in that worktree
  -> one authoritative deterministic check
  -> one fresh reviewer
  -> at most one fix pass
  -> commit, PR, CI, human merge
```

Roles remain logically separate. Claude Code and Codex remain first-class outer
harness adapters. A role may use the owning harness's native fresh-agent mechanism.
Exact cross-provider model execution is not a correctness property and is removed
from the hot path.

## Scale of the expansion

From `eb1e2d9` through the current frozen `main`, the repository gained 28,942 lines
and removed 1,035 across 135 files. The primary clusters account for most of that
growth:

| Cluster | Diff attribution | What its interface asks callers to understand |
|---|---:|---|
| Capsule execution | +10,439 lines, 9 paths | work orders, role catalog, private clones, claims, generations, process supervision, typed outcomes, receipts, patch capture/import, quarantine, recovery |
| Planning transaction | +4,498 lines, 8 paths | plan versions, feedback ledger, assessor identity, route digest, revalidation, gate tuple, approval receipt, bootstrap journal |
| Review brief | +3,704 lines, 18 paths | optional reviewer-facing report and its visual renderer/test surface |
| Exact route contracts | +1,816 lines, 2 primary paths | twelve profiles, owner-relative routes, model/effort proof, snapshots, attestations |
| Reflect/merge reporting additions | +667/-2 lines, 6 primary paths | run timing, reflect inputs, self-merge plumbing |
| Shared integration and other changes | +7,818/-1,033 lines, 92 paths | dispatcher, bootstrap, validation, prose, seams, bug fixes, and generated surfaces |

Size is inventory, not a deletion verdict. In particular, the review brief is an
intentional ancillary feature and is retained.

The shared bucket must not be bulk-reverted. It contains both machinery wiring and
independent fixes. Each deletion slice removes only wiring owned by the cluster being
deleted.

## Three classifications

### 1. Load-bearing core: keep

These modules provide leverage through small interfaces. Deleting them would spread
their safety obligations back across callers.

| Property | Primary seam | Why it stays |
|---|---|---|
| Ticket worktree isolation | `flow_worktree.py` | Keeps product edits away from the maintainer checkout and gives one obvious authoritative tree |
| One live owner | `lease.py` and `_locking.py` | Prevents concurrent writers from driving the same run |
| Base and engine drift detection | `snapshot.py` plus bootstrap base verification | Stops work based on silently changed inputs |
| Atomic run state and corrupt-state quarantine | `state.py`, `_atomicio.py`, `_locking.py` | Concentrates durable state recovery behind one interface |
| Planned-file ownership | `diff_extract.py` | Prevents unrelated files from entering the commit |
| Tracker and forge adapters | tracker and forge seams | Two real external backends justify these seams |
| Authoritative deterministic checks | repository lint, tests, and CI | Model claims are evidence, not proof that checks passed |

Keeping a property does not preserve every current field or test. Cognitive substep
state, receipt fields, and tests that reach past a surviving interface are removed
with their owning machinery.

### 2. Ancillary capability: keep, but decouple

Ancillary modules may be useful without participating in the delivery hot path.

#### Review brief

The review brief was deliberately built and remains a valued product capability. Its
being disabled for unattended Flow-on-Flow runs is not evidence that the capability
is unnecessary. Keep its renderer, stage, product and visual tests, assets, and visual
CI.

The simplification rule is narrower: the review brief must not force the core
dispatcher, planner, implementer, or reviewer to retain capsule and route machinery.
When the cognitive executor is simplified, the review brief keeps a small optional
interface and uses the same ordinary native-agent seam as other cognitive roles.
Capsule-specific integration tests may disappear with the capsule executor, but the
review brief's behavior and independent verification remain.

#### Optional behavioral E2E

Keep E2E only for a real behavioral recipe. Remove its disposable-capsule writer when
capsules are deleted. A test-only recipe is not E2E and must not cause a third copy of
the test suite to run. Whether the remaining direct E2E stage earns its keep is judged
after the capsule slice, not assumed now.

#### Worktree janitor

The worktree janitor predates the expansion boundary and solves a real cleanup
problem. Keep its ordinary stale-worktree sweep. Delete only capsule-quarantine
cleanup and its `cognitive_workers` dependency when capsules disappear.

### 3. Orchestration machinery: collapse or delete

#### Planning transaction

`planning_attempt.py`, `planner_worker.py`, `plan_review.py`, and
`bootstrap_journal.py` add 4,498 primary lines. Their interface exposes seven planning
record types and six schemas before a worktree exists. The current prose requires
route snapshots, strict provider schemas, attempt ids, versions, feedback ids,
assessor receipts, revalidation receipts, optimistic gate digests, approval receipts,
and a five-phase bootstrap journal.

The `flow-yahm` plan was approved at v1 with zero feedback, zero revisions, and zero
model retries. The machinery still spent about 26 cognitive minutes on planning and
assessment and required two route-shape rerenders. Preserve the useful outcome, not
the transaction protocol: one grounded plan, an optional independent challenge, one
human approval, the approved base SHA, and a bootstrap drift check.

Verdict: collapse first.

#### Cognitive capsules and patch import

`cognitive_workers.py` and its support/tests contribute 10,439 primary lines. Writer
capsules duplicate the isolation already supplied by the ticket worktree, then add a
second source tree, index state, mutation claim, patch capture, compare-and-swap
import, journal, and quarantine lifecycle. `flow-yahm` demonstrated a contradiction
between the authoritative preflight's unstaged-index expectation and the seeded
fixer importer's staged-index expectation. That contradiction regenerated a
successful fix and still ended on an authoritative one-line type error.

Verdict: delete after planning no longer depends on the capsule executor. Execute
implementers and fixers directly in the ticket worktree. Reviewers remain read-only
by instruction and ownership checks, not by cloning the repository again.

#### Exact route contracts

`agent_routes.py` and its primary tests add 1,816 lines, with further wiring in setup,
validation, dispatch, bootstrap, public commands, and seam checks. Exact route proof
was useful only if every role had to prove a provider, model, and effort level. That
is not required for logical role separation.

Verdict: collapse after its planning and capsule consumers are gone. Keep
`FLOW_HARNESS` selection and simple optional model hints accepted by the active host.
Do not keep route snapshots, attestations, owner-relative matrices, or exactness as a
delivery gate.

#### Automatic self-evolution

Reflection-backed machinery tickets and maintainer-only automatic merge created a
positive feedback loop: machinery failures created tickets, fixes expanded machinery,
and the expanded machinery created more interactions to repair. The freeze has
already stopped the loop for this repository.

Verdict: remove automatic backlog creation and Flow-on-Flow auto-merge after the
post-boundary execution machinery is gone. Explicit memory commands may remain;
promotion of an observation into a ticket becomes a maintainer action.

## Dependency shape

The main deletion dependencies are concentrated:

```text
planner_worker -----> planning_attempt
       |
       +------------> cognitive_workers -----> planning_attempt
                                |             -> agent_routes
                                |             -> review_brief
                                +------------> state

flow_worktree ------> planning_attempt, bootstrap_journal, agent_routes, state
dispatch_stage -----> cognitive_workers, agent_routes, state
worktree_janitor ---> cognitive_workers
```

Arrows point from caller to dependency. `state` survives. `review_brief` survives and
is decoupled. A slice must remove or redirect callers before deleting a dependency.

## Approaches considered

### A. Dependency-ordered forward deletion - recommended

Remove one vertical capability at a time, restore the simpler interface in the same
slice, delete tests that belong to the removed interface, and let CI verify the
remaining product. This produces understandable diffs and preserves independent
post-boundary fixes.

### B. Revert the full range

This is mechanically faster but crosses native Codex and runtime-layout changes,
discards independent fixes, and makes current worktrees and installed layouts harder
to reason about. The archives already provide rollback evidence without rewriting
`main`. Rejected.

### C. Keep the machinery and add a light lane

This hides cost behind configuration while leaving the same interfaces, tests,
failure modes, and maintenance burden in the repository. The frozen config already
proves that disabling stages is not the same as understanding or removing their
machinery. Rejected.

## Ordered slices

1. **Collapse planning transactions.** Restore a single native planner result and
   human gate. Remove plan versions, feedback CAS, assessor receipts, route-bound gate
   receipts, and the approval bootstrap journal. Retain one optional independent
   assessor for explicitly high-risk work.
2. **Delete capsule execution and patch import.** Restore direct native agents in the
   ticket worktree. Remove work orders, private clones, claims, generations, patch
   CAS, capsule quarantine, and their tests. Decouple the retained review brief and
   janitor.
3. **Collapse exact routes.** Remove route profiles, snapshots, attestations,
   overrides, generated route surfaces, and validator/seam obligations. Keep outer
   harness selection and optional host-native model hints.
4. **Remove the automatic self-evolution loop.** Delete machinery-ticket creation and
   maintainer auto-merge. Keep explicit memory operations only if their direct
   interface remains useful.
5. **Reconcile tests, docs, and configuration.** Rewrite the opening of `CLAUDE.md`,
   delete dead configuration and historical operational instructions, and test only
   through surviving interfaces. Do not execute `flow-qpgd`; most of its proposed
   test deletion will happen naturally with the removed modules.
6. **Reconcile backlog and Git debris.** Mark tickets tied exclusively to deleted
   machinery obsolete, then clean merged branches and stale worktrees with explicit
   preservation checks. Tracker mutation waits until the corresponding code is gone.
7. **Run one measured delivery.** Use at most three cognitive calls, no automatic
   tickets, no test-only E2E, one authoritative verification pass, under 30 minutes
   active wall time, and fewer than 4.46 million reported input tokens, an 80 percent
   reduction from `flow-yahm`.

## First slice design: collapse planning transactions

### Behavior

- A fresh plan uses one host-native planner context. It returns one complete Markdown
  plan grounded against the fetched default branch.
- The owner may request one fresh assessor only for hot, high-risk, or unclear work.
  Ordinary bounded tickets have no mandatory assessor.
- The maintainer sees the plan and explicitly approves it. Requested changes edit the
  same plan; Flow does not create a version graph or feedback ledger.
- During stabilization, planning is attended. Unattended planning does not bypass the
  human gate and is not reimplemented inside this slice. It may be reconsidered only
  after the measured simple delivery.
- Approval records the plan file and base SHA. Bootstrap fetches the default branch
  again. If the base moved, the owner performs one bounded changed-path check.
  Proven-disjoint movement continues; relevant or ambiguous movement returns the same
  plan to the human gate. It never automatically generates another plan.
- Bootstrap creates the existing ticket worktree and seeds the existing run state.
  The run lease, atomic state, snapshot, and planned-file ownership gate remain.
- Review brief behavior does not change.

### Removed surfaces

- `planning_attempt.py`, `planner_worker.py`, `plan_review.py`,
  `bootstrap_journal.py`, and tests whose only subject is those modules;
- their facade commands, schemas, receipts, attempt directories, and reference prose;
- approval-receipt, route-digest, and journal coupling in `flow_worktree.py`;
- planning-specific cognitive roles and result contracts that have no remaining
  caller.

Exact route machinery used by post-plan roles remains temporarily and is removed in
its own later slice. No replacement planning framework or compatibility alias is
added.

### Failure handling

- Planner/provider failure is reported directly and leaves the repository untouched.
- A user question is presented directly, not stored in a feedback ledger.
- Relevant or ambiguous base movement before bootstrap stops with the old and new
  SHAs and the changed paths. Proven-disjoint movement continues without another
  model call.
- Worktree bootstrap retains its existing claim, collision checks, and cleanup. The
  special approval journal is removed; ordinary bootstrap recovery remains.

### Verification budget

- During editing, run targeted tests for the surviving planning, worktree, dispatcher,
  validator, and public-command seams.
- Run lint/type and prose-to-CLI checks once on the final local diff.
- Let CI perform the single authoritative full pytest run. Do not run a test-only E2E
  stage or a second model-driven verification suite.
- Use one independent code review after the diff exists and at most one fix pass.
- Delete tests for removed interfaces. Do not rewrite them against implementation
  details of the simpler path.
- Stop and rescope if the slice requires a new durable state machine, compatibility
  layer, or more new runtime code than is needed to join the surviving seams.

### Completion evidence

The slice is complete when the public planning instructions describe only the simple
path, removed facade commands fail as unknown, worktree bootstrap accepts the simple
approved plan without route or approval receipts, retained safety tests pass, CI is
green, and the PR reports lines and tests deleted plus wall time. No Flow driver,
reflection, machinery ticket, or automatic merge is used to deliver it.
