# Delivery planning and approval

This is the read-only front half for a fresh target. It produces one approved plan,
seeds an isolated worktree, then hands control to `delivery-loop.md` in the same owner
conversation. The target's public options have already been parsed by the registry.

## 1. Select the approval boundary

Claude Code enters native plan mode. Codex uses native Plan mode when active;
otherwise it uses a soft boundary: present the complete plan, end the turn, and wait
for explicit approval. Generic harnesses use the same soft boundary. A soft boundary
is still a real stop: no repository write, worktree creation, tracker mutation, or
run initialization occurs before approval.

## 2. Ground the target

Fetch the ticket through the tracker seam:

```bash
FLOW_HARNESS="<harness>" "<facade>" tracker --workspace-root . get --key "<ticket>"
```

Read linked tickets and any saved decision. Inspect the repository read-only. General
orientation may use the current checkout, but every fact that determines scope,
planned files, or risk must be verified against the freshly fetched default branch.
Do not base a plan on stale feature-branch content.

For grouped work, fetch every ticket and prove that all are fresh, live, distinct,
non-epic, and coupled enough for one reviewable diff. Pick one lead; it owns the run,
lease, branch, and memory. Each covered ticket remains an explicit acceptance and
closure obligation.

## 3. Search prior memory

Build a multiline query from a short intent preamble followed by the unmodified ticket
title and body. Write it to a temporary file with the host's exact-write primitive and
query through the internal search seam with a generous planning limit. Search is pure
read here; do not record pending usage before approval.

Verify remembered facts against current code. Use applicable decisions, failures,
and patterns in the plan, and name why each matters. A memory result is evidence to
check, not authority over the repository or current user intent.

## 4. Resolve uncertainty

Investigate every answerable fact with reads, search, tests that do not mutate source,
or a fresh independent agent. Only a decision requiring user-only intent reaches the user.
Classify hotness from configured labels and guard/planned-file rules, never from a
model's assertion.

Derive the effective verification lane:

- explicit `--verify` fixes the attended lane;
- configured ticket tiers may choose a lane when none is explicit;
- a hot change always clamps to `full`;
- unattended mode derives policy and cannot be combined with explicit `--verify`.

Settle an e2e recipe from explicit `--e2e`, the workspace cookbook, or the documented
CI-only floor. If e2e is enabled, never silently omit the recipe.

### Routed planner path

Resolve the planner before choosing a planning transport. A configured or built-in
planner with `activation: pending` uses this strict read-only path. A complete
`planner` override wins for this attempt. Standalone legacy `[models]` workspaces keep
their host-native planning behavior because they do not become partial agent routes.

Create one absolute temporary attempt directory outside `.flow/runs/`. Read
`.flow/workspace.toml` at the freshly fetched base SHA with `git show` and write those
exact bytes to `<attempt-dir>/workspace.toml`. Resolve the complete route snapshot
from that file and every supplied override:

```bash
FLOW_HARNESS="<harness>" "<facade>" agent-route snapshot \
  --workspace-config "<attempt-dir>/workspace.toml" --owner-harness "<harness>" \
  --route "<each override>" --output "<attempt-dir>/route.json"
FLOW_HARNESS="<harness>" "<facade>" agent-route resolve \
  --snapshot "<attempt-dir>/route.json" --profile planner
```

Proceed only when the planner has `source: override`, `source: workspace`, or
`source: built_in`, together with `activation: pending` and an exact desired route.
Then emit the provider schema and initialize the ephemeral attempt with the fetched
base SHA and route-snapshot digest. The emitted schema is the provider input: do not
normalize or rewrite a copy. Every object is closed with
`additionalProperties: false`; array uniqueness remains a Python validation rule
because Codex structured output does not accept `uniqueItems`.

```bash
FLOW_HARNESS="<harness>" "<facade>" planning-attempt schema \
  --output "<attempt-dir>/plan-envelope.schema.json"
FLOW_HARNESS="<harness>" "<facade>" planning-attempt create \
  --attempt-dir "<attempt-dir>" --attempt-id "<fresh id>" \
  --base-sha "<fetched SHA>" --route-digest "<route digest>" \
  --owner-identity "<owner identity>"
```

Preflight and launch `planner-worker` with the desired harness, model, and effort.
The prompt includes the exact attempt id, owner-allocated next version, parent digest,
base SHA, route digest, ticket intent, current complete plan when revising, feedback
ledger, and required plan sections. The envelope author id is
`<harness>:<model>`; the worker validates that id, harness, and model against the
route it actually launched before it reports acceptance. Read the structured result
and retain its thread id only in the live owner conversation. Attest its structured
`acceptance` through `agent-route attest`, then pass the complete `envelope` and the
active receipt to `planning-attempt accept --route-receipt <receipt>`. `accept`
refuses shadow, reused, mismatched, or self-declared-only launch evidence. Never put
the thread id in the attempt directory or a Flow run; agent prose cannot activate the
route.

When the envelope status is `NEEDS_INPUT`, show each planner question verbatim and add
separately labelled owner guidance. Send the user's answer back verbatim with anchors
and separately labelled synthesis. Use the same physical thread until three revision
rounds or context-pressure telemetry, then launch fresh with the complete current plan
and feedback ledger. A resumed launch supplies that complete state separately through
`--fresh-prompt-from`. Flow refuses a delta-only fresh retry. Owner loss also
rehydrates fresh. Each physical launch records its own 600-second soft budget,
2400-second hard budget, deadline events, elapsed time, outcome, and terminal
acknowledgement. One hard-timeout retry starts a new 600/2400 budget only after the
first process and output pipe are both closed. Report aggregate wall time separately,
and never describe two attempts as one 80-minute attempt. Never select a fallback
route automatically.

Every revision is a complete plan version. Record human annotations through
`planning-attempt feedback`; every id must be incorporated by a later envelope or
rejected with a visible reason. The owner assesses externally authored attended plans.
Use a fresh physical assessor for owner-authored, unattended, hot, or explicitly
escalated plans, and pass `--require-fresh` when recording that verdict. Findings go
back to the planner; the assessor never edits the plan. Every verdict includes the
exact plan digest and author id, and a stale or differently authored verdict is refused.
A required-fresh verdict also includes the `plan_assessor` launch-receipt digest and
is recorded with `assess --launch-receipt <receipt>`; the receipt's distinct worker id
must match the assessor id.

Immediately before review completion, diff the originally approved base against the
latest fetched default and record the exact changed, planned, and context paths through
`planning-attempt revalidate`. Relevant or ambiguous movement forces a fresh
rehydrated revision on the latest SHA. Unchanged or proven-disjoint movement preserves
the reviewed version.

Render the canonical envelope, route, assessment, and feedback with `plan-review`.
Prefer the local Lavish surface, lead with motivation and before/after scenarios, and
use its built-in send/end semantics. If Lavish cannot open, poll, or recover, state
`Lavish: skipped - <reason>` or `Lavish: degraded mid-loop - <reason>` and render the
same evidence as Markdown. The owner drains the final feedback batch and freezes the
surface before offering the host-native gate. There is no approval control in the
visual companion.

## 5. Write the plan

The plan contains:

- goal and acceptance outcomes for the lead and every covered ticket;
- approach and ordered implementation steps;
- exact files expected to change, including anticipated new tests;
- verification lane, test strategy, and e2e recipe;
- compatibility, rollout, and reversal concerns where relevant;
- remembered evidence used and current-code verification;
- risks and genuinely user-only questions;
- commit type and concise summary;
- independent confidence assessment and its evidence.

Use an independent assessor: Claude Code may use its advisor or a fresh agent; Codex
uses a fresh collaboration agent without a Claude model parameter. For `full`, close
read-only gaps before presenting a plan below the configured confidence floor. One
bounded revision may incorporate concrete assessor gaps. Never self-score.

## 6. Cross the gate

Attended mode presents the full plan and confidence evidence at the selected boundary.
Approval is the single delivery gate. A requested change revises the plan while still
read-only; rejection stops.

For a routed attempt, call `planning-attempt gate` before offering the boundary and
retain its exact `digest` as the optimistic approval token. After the host-native gate
succeeds, and only then, render the exact plan file with
`planning-attempt render-plan --attempt-dir <dir> --output <plan>` and call
`planning-attempt approve` with `--expected-gate-digest <digest>`, the native gate
receipt/id, that canonical plan file, and an absolute temporary output path. `approve`
reloads and compares the complete tuple under the attempt lock, and refuses stale
digests or any other plan bytes.
Any plan, feedback, route, base, assessment, or revalidation change invalidates that
approval attempt.

Unattended mode uses an independent planner and assessor. It may proceed only when the
plan is complete, the safety policy permits it, and no user-only question remains.
Otherwise:

1. record the precise question or safety wall on the ticket;
2. classify it as requiring live human input or as retryable with stronger planning;
3. move it to the configured deferred or blocked state;
4. leave the repository untouched and stop.

An environmental agent/provider failure leaves a fresh ticket open and records no
false product decision. An unattended hot change follows the configured guard policy;
it never bypasses the guard merely because confidence is high.

## 7. Bootstrap and bind

After approval only:

1. write the approved plan to a temporary exact-content file;
2. invoke the worktree bootstrap seam with ticket, plan path, freshly fetched default
   base, branch, planned files, group covers, lane, e2e recipe, commit metadata, and
   every parsed `--route` value; a routed attempt also passes its exact
   `--approval-receipt`;
3. use spill recovery only when the exact files are proven to have been created by
   this planning attempt and do not overlap pre-existing user work;
4. parse the returned `result.worktree` absolute path;
5. set `run_root` to it and `facade` to `<run_root>/.flow/runtime/flow`;
6. verify that every subsequent operation is rooted there and that the facade points
   back to the loaded skill/runtime version;
7. record the recalled ids against this exact branch and worktree for dispatcher
   promotion;
8. continue immediately into `delivery-loop.md`.

The bootstrap owns collision detection, dirty-file ownership, base fetching,
frontmatter persistence, and freezing the route snapshot before exposing the run. An
approval receipt makes it regenerate and compare the route snapshot, use the approved
SHA instead of re-resolving the branch, and journal prepared, worktree-intended,
worktree-created, run-seeded, and committed phases. The intent phase records rollback
coordinates before `git worktree add`. Cleanup resets the journal only after worktree
and branch removal are proven. If cleanup cannot be proven, Flow retains the rollback
coordinates for the next claimed recovery. A receipt-free legacy caller keeps the
existing bootstrap behavior.
Do not hand-create the branch or run directories around it.
Do not pass `--recover-spill` automatically; provenance must be proven first.
Claude Code may additionally switch its native workspace after the absolute binding;
Codex keeps using explicit workdirs. The binding, not the convenience switch, is
authoritative.
