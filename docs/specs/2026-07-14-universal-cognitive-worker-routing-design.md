# Universal cognitive-worker routing

## Status and relationship to the first routing epic

This design is the second phase of
`2026-07-13-modular-agent-routing-design.md`. The first phase made routes explicit,
activated the cross-harness planner, and deliberately left post-plan roles in
shadow mode. This phase removes that temporary boundary.

The end state is that every cognitive role has an explicit, independently
resolvable `harness`, `model`, and `effort` route. Deterministic operations remain
model-free. A routed worker may use Claude Code or Codex regardless of the harness
that owns the Flow conversation, but the owner conversation remains the single
human cockpit and the dispatcher remains the sole pipeline authority.

The work ships as three ordered increments:

1. close the route/provenance model, including the missing `review_brief` stage;
2. generalize the planner worker into a safe execution-capsule module and activate
   read-only roles;
3. activate E2E and sole-writer roles, then pass deterministic fault injection and
   a real cross-harness self-dogfood delivery.

## Why the current state is incomplete

The current universal route snapshot is useful but not yet universal:

- `review_brief` is a registered delivery stage but is absent from the route
  snapshot's stage-execution map;
- `plan_assessor`, `diff_reviewer`, and `guard_reviewer` have desired routes but
  remain shadowed;
- post-plan Codex routes inherit the owner model instead of proving the configured
  model and effort;
- implementation, E2E, fixing, review-brief authorship, and reflection still rely
  on stage-specific host behavior;
- the safe process lifecycle implemented for planning is not available as a common
  worker interface;
- write-capable agents would currently have to edit the authoritative ticket
  worktree directly, making ambiguous termination and partial mutation hard to
  recover safely.

The goal is not to create more agents. It is to make every existing cognitive
decision point explicit, replaceable, observable, and safely executable.

## Chosen architecture

### One deep worker module

Flow adds a `CognitiveWorkers` module at the seam between planning/dispatch state
machines and provider execution:

```python
class CognitiveWorkers:
    def run(self, order: WorkOrder[T], owner: OwnerProof) -> WorkOutcome[T]: ...
    def cancel(
        self,
        invocation: InvocationRef,
        owner: OwnerProof,
        reason: str,
    ) -> CancellationReceipt: ...
```

`run` is idempotent by logical invocation ID. Repeating it recovers the existing
invocation or returns its durable result; it never launches a competing physical
worker. `cancel` is also idempotent.

The caller supplies only facts it owns: the planning attempt or dispatch descriptor,
owner proof, immutable input bundle, and expected stage state. The module derives
the profile, frozen route, permission policy, result schema, prompt, deadlines,
retry policy, artifact paths, capsule mode, and ownership rules from a closed role
catalog. Callers cannot weaken permissions or rewrite a route ad hoc.

The module hides:

- exact route resolution and structured attestation;
- Claude Code and Codex CLI construction;
- provider authentication and capability preflight;
- process-group launch, deadline handling, cancellation, reap, and output closure;
- typed-result extraction and validation;
- private execution-capsule creation and cleanup;
- Git baseline, binary patch capture, path ownership, and guarded import;
- invocation journaling, retry decisions, crash recovery, receipts, and metrics.

Planning-attempt versioning and approval stay in `planning_attempt`. Pipeline order,
leases, and stage transitions stay in `dispatch_stage`. Worktree bootstrap and the
frozen route snapshot stay in `flow_worktree`. The new module owns only routed
cognitive execution.

### Private execution capsules

Each physical worker runs in a private local clone created from an exact Git object,
never in the authoritative ticket worktree and never in a linked Git worktree that
shares mutable repository metadata.

The capsule contains:

- the exact approved input SHA or immutable planning base SHA;
- a closed, digest-bound input bundle;
- only the credentials and environment required by the selected harness adapter;
- a profile-specific permission policy;
- private scratch, evidence, stdout, and stderr paths;
- no writable path back to the authoritative ticket worktree.

Read-only roles may inspect the capsule but must leave its source snapshot unchanged.
Writer roles may edit and test inside the capsule. Flow, not the model, captures the
resulting binary-aware Git patch, validates it, and imports it into the authoritative
ticket worktree only after the worker is terminal and every receipt passes.

This is preferred to direct worktree execution because a killed or disconnected
worker cannot leave authoritative source half-mutated. It is preferred to asking
the model to serialize a patch because Git preserves binary changes, renames, file
modes, and large diffs without passing them through model output.

## Role and authority catalog

All cognitive profiles are independently routable. Review and mutation are separate
profiles even when they occur within one composite stage.

| Profile | Capsule authority | Typed result | Authoritative effect |
|---|---|---|---|
| `planner` | read-only | `PlanEnvelope` | accepted only through the existing plan-version CAS |
| `plan_assessor` | read-only | `PlanAssessment` | verdict binds the exact plan digest |
| `implementer` | capsule writer | `ImplementationReport` | validated patch imported under a sole-writer claim |
| `e2e` | disposable capsule writer | `E2EReport` | evidence retained; all source mutations discarded |
| `code_reviewer` | read-only | `ReviewFindings` | findings only |
| `diff_reviewer` | read-only, immutable diff input | `ReviewFindings` | findings only; remains plan-blind where policy requires |
| `guard_reviewer` | read-only, immutable guard input | `GuardVerdict` | verdict only |
| `review_fixer` | capsule writer | `FixReport` | validated patch imported under a sole-writer claim |
| `revision_fixer` | capsule writer | `RevisionReport` | validated patch imported under a sole-writer claim |
| `review_brief_author` | read-only | `ReviewBriefModel` | deterministic renderer publishes HTML and receipt |
| `reflector` | read-only | `ReflectionPlan` | deterministic appliers persist allowed memory/actions |
| `machinery_fixer` | capsule writer | `MachineryFixReport` | existing machinery-edit guard validates and applies |

The public stage map records these composite relationships. In particular,
`code_review` records the primary and plan-blind reviewer profiles, `review_loop`
records reviewer and fixer profiles, `review_brief` records
`review_brief_author`, and `reflect` records `reflector` plus the optional
`machinery_fixer` substep. Tool stages such as ticket transition, commit, PR
creation, and merge remain `model = none`.

E2E receives write permission only inside its disposable capsule because tests may
generate fixtures, caches, snapshots, or build products. No E2E patch is imported.
Unexpected source mutations become evidence in the E2E report rather than dirtying
the ticket worktree.

Reflection cognition is read-only. It proposes knowledge entries, supersessions,
project-rule suggestions, and machinery findings as typed actions. Deterministic
Flow code applies memory and ship-event operations. A machinery edit requires a
separate `machinery_fixer` invocation and the existing serialized machinery-edit
guard. Reviewers never gain write authority because they found an issue.

## Work order and result contracts

A `WorkOrder` is a closed, digest-bound value issued by the planning or dispatch
state machine. It contains:

- logical invocation ID and monotonically increasing generation;
- planning-attempt or run/stage identity and expected state;
- frozen route-snapshot reference and digest;
- role profile;
- immutable input-bundle path and digest;
- exact repository source SHA;
- result-schema identifier, version, and digest;
- authority and allowed mutation paths derived from the role catalog;
- output and receipt bindings;
- owner proof and dispatch lease fence where a run exists.

A terminal `WorkOutcome` is one of `Succeeded[T]`, `NeedsInput[T]`,
`Failed[WorkerFailure]`, or `Cancelled`. Every terminal variant carries available
route, attempt, terminal, ownership, capsule, and artifact receipts. Only a validated
`Succeeded` outcome may be accepted by `planning_attempt` or advance a dispatch
stage as completed.

The route receipt records desired and effective harness/model/effort, transport,
adapter and CLI versions, canonical provider identity when exposed, prompt and
schema hashes, and the physical worker identity. Provider prose never proves the
effective route.

The change receipt records the original baseline digest, Git patch path and digest,
allowed and touched paths, binary/rename metadata, import target HEAD, import result,
and the final authoritative diff digest. Commit consumes this receipt instead of
silently recapturing a new ownership baseline.

## Exact route and adapter policy

The only public harness names remain `claude_code` and `codex`. CLI or native are
transport details recorded in receipts.

The module initially provides two internal adapters:

- `ClaudeCodeCliAdapter`;
- `CodexCliAdapter`.

Both must prove exact model selection, effort selection, permission mode, structured
output, process-group ownership, cancellation, terminal acknowledgement, and
provider/version discovery. An adapter that cannot prove a required capability fails
preflight. It never falls back, silently inherits the owner model, or claims shadow
execution as success.

Host-native workers may become additional internal adapters later, but only when
their launch response proves the same contract. Stage callers and route configuration
do not change when the transport changes.

## Execution and import protocol

The required ordering is:

1. validate the work order, owner proof, expected state, route snapshot, input and
   schema digests, absolute roots, and lease fence;
2. resolve the exact route and preflight its adapter before allocating a capsule;
3. recover or refuse any prior physical attempt for the logical invocation;
4. acquire a shared read claim or the required durable mutation-domain claim;
5. create the private clone at the exact source SHA and record its clean baseline;
6. persist the invocation journal in `prepared` state;
7. launch one foreground process group with the closed prompt, schema, environment,
   permissions, and per-attempt budgets;
8. supervise through the soft deadline and cancel the whole process group at the
   hard deadline;
9. require process reap, process-group absence, and stdout/stderr closure;
10. validate the typed result and exact route receipt;
11. verify the capsule postcondition and, for a writer, capture and validate its Git
    patch against the original baseline and allowed paths;
12. for an importable writer, revalidate the authoritative worktree and lease,
    acquire its sole-writer import lock, apply the patch with index and worktree
    guards, and record the resulting authoritative diff;
13. atomically persist result and receipts, mark the invocation terminal, release
    claims, and dispose of or quarantine the capsule.

Import is compare-and-swap. It refuses if the authoritative HEAD, index, owned-file
baseline, dispatch generation, route snapshot, or lease fence differs from the work
order. It never re-baselines over external edits. A failed clean apply preserves the
capsule and patch as recovery evidence and leaves the authoritative worktree at its
pre-import state.

## Cancellation, retry, and recovery

Terminal acknowledgement means the direct child was reaped, its process group no
longer exists, and stdout and stderr reached EOF. Without all three, Flow does not
retry, import, dispose of the capsule, or release an exclusive claim.

Cancellation sends a bounded graceful signal to the whole process group, escalates
when required, drains output, and waits for acknowledgement. If termination cannot
be proven, the capsule and claim enter `quarantined` state and the invocation fails
with `termination_unconfirmed`. A replacement worker is forbidden until explicit
recovery proves terminality.

Read-only roles may receive one fresh retry after acknowledged termination and a
clean capsule guard. Writer roles may retry only from a new capsule at the original
source SHA, after acknowledged termination and before any patch import. Once an
authoritative patch import has started, retry is recovery of that import journal,
not a new model invocation.

The invocation journal supports `prepared`, `running`, `cancelling`, `terminal`,
`validated`, `importing`, `completed`, `blocked`, and `quarantined` states. Repeating
`run` returns a durable completion, resumes supervision of the known process, resumes
an idempotent import, or refuses unsafe relaunch. It never guesses that a lost PID is
dead.

## Error model

Operational failures are provider-neutral and structured. Required codes include:

- `invalid_order`, `stale_order`, and `lost_owner`;
- `route_unavailable`, `route_not_exact`, `auth_unavailable`, and
  `capability_missing`;
- `execution_busy`, `writer_busy`, and `baseline_mismatch`;
- `launch_failed`, `worker_exited`, `hard_timeout`, and
  `termination_unconfirmed`;
- `invalid_result`, `read_only_violation`, and `ownership_violation`;
- `patch_capture_failed`, `patch_import_conflict`, and `artifact_failure`;
- `recovery_required` and `indeterminate_write`.

Every post-launch failure retains a physical-attempt receipt and evidence paths.
Exceptions must not erase process-lifecycle or repository-state evidence.

## Configuration and provenance migration

`[agents.<profile>]` remains the public configuration shape with required `harness`,
`model`, and `effort`. Owner-relative routes continue to use explicit
`[agents.<profile>.by_owner.<harness>]` tables. Per-run `--route` tuples remain atomic
overrides.

The new profiles receive explicit built-in and self-workspace defaults matching the
existing role economics: strong models for assessment/review, faster models for
implementation/fixing/E2E, and owner-relative defaults where cross-harness writing
has not been explicitly selected. The exact defaults are materialized in the route
snapshot and are never inferred later from a lane.

Legacy `[models]` remains the visible compatibility source only when the corresponding
explicit profile is absent. It can produce inherited/shadow provenance for legacy
runs but cannot satisfy an exact configured route. New run snapshots use the
expanded stage map; old snapshots retain their recorded semantics and remain
readable.

The first increment must add `review_brief` to stage execution and replace prose
claims that it is universally routed while omitting that stage. Generated docs,
workspace validation, setup templates, migration output, route overrides, receipts,
and seam checks must all agree on the complete profile catalog.

## Delivery increments

### Increment 1: provenance closure

- add every cognitive profile and composite-stage mapping;
- add `review_brief` and `reflect` route provenance;
- keep deterministic stages model-free;
- update workspace defaults, setup/migration, command validation, documentation,
  inventory, and compatibility tests;
- make no execution behavior active yet beyond the already active planner.

### Increment 2: execution capsules and read-only activation

- extract generalized process supervision from `planner_worker` behind
  `CognitiveWorkers`;
- implement Codex and Claude Code CLI adapters with exact-route preflight;
- implement capsule creation, immutable inputs, invocation journal, receipts, and
  cleanup/quarantine;
- port planner execution without changing its approval or continuity semantics;
- activate `plan_assessor`, `code_reviewer`, `diff_reviewer`,
  `guard_reviewer`, `review_brief_author`, and `reflector`;
- retain deterministic rendering and reflection appliers.

### Increment 3: E2E and writer activation

- add binary-aware patch capture and compare-and-swap import;
- activate `implementer`, `review_fixer`, `revision_fixer`, and
  `machinery_fixer`;
- activate E2E in a disposable write-capable capsule with no patch import;
- replace direct reviewer auto-fixes with explicit fixer invocations;
- preserve a single authoritative writer/import lock per mutation domain;
- remove shadow execution for exact post-plan routes after proof passes.

## Verification and release gate

Interface-level tests use fake process, clock, filesystem, and repository adapters to
prove:

- exact route acceptance and rejection for both harnesses;
- no fallback, owner-model inheritance, or shadow receipt satisfying an exact route;
- typed-result and author/digest validation for every role;
- read-only mutation detection;
- soft/hard deadline events and separate retry budgets;
- process-group cancellation, output draining, and terminal acknowledgement;
- crash recovery from every journal phase;
- no replacement launch after ambiguous termination;
- shared-reader/exclusive-writer fencing and deterministic claim ordering;
- binary patches, renames, mode changes, deletions, untracked additions, and large
  diffs;
- ownership violations and authoritative drift before import;
- atomic import rollback and idempotent import recovery;
- E2E mutation discard and evidence retention;
- review-brief deterministic rendering from routed typed output;
- reflection action validation and separation from machinery writes;
- old route-snapshot compatibility and complete new provenance.

The release gate also runs the repository's full tests, lint, type checks, generated
command validation, and prose/CLI seam check.

Real dogfood proceeds in increasing authority:

1. a cross-harness plan-assessor invocation;
2. a plan-blind diff review of a real Flow PR;
3. an E2E capsule that produces disposable mutations;
4. a cross-harness implementation or revision-fix capsule whose binary-aware patch
   is imported into a Flow-owned ticket worktree;
5. completion of that ticket through review brief, reflection, and merge with every
   cognitive launch represented by an exact route receipt.

The default flips only after all five proofs pass. A configured exact route failure
then stops visibly; no automatic fallback is introduced.

## Acceptance criteria

The design is complete when current-state evidence proves all of the following:

1. every cognitive stage and substep has a public profile and appears in the frozen
   stage-execution map;
2. `review_brief` and `reflect` have truthful desired/effective provenance;
3. Claude Code and Codex can execute any activated profile independently of the
   owner harness with exact model and effort evidence;
4. read-only roles cannot mutate authoritative or capsule source unnoticed;
5. E2E mutations never dirty the authoritative worktree;
6. writer mutations reach the authoritative worktree only through a validated,
   receipt-bound, compare-and-swap patch import;
7. no worker replacement overlaps a live or ambiguously terminated invocation;
8. reviewers and reflectors cannot acquire implicit source-write authority;
9. legacy route snapshots remain readable while new snapshots expose the complete
   model;
10. deterministic fault coverage and the real cross-harness dogfood chain pass;
11. exact post-plan routes are active rather than shadowed; and
12. the repository gate and all generated/interface consistency checks pass.
