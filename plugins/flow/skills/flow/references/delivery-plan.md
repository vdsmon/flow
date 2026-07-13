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
   base, branch, planned files, group covers, lane, e2e recipe, and commit metadata;
3. use spill recovery only when the exact files are proven to have been created by
   this planning attempt and do not overlap pre-existing user work;
4. parse the returned `result.worktree` absolute path;
5. set `run_root` to it and `facade` to `<run_root>/.flow/runtime/flow`;
6. verify that every subsequent operation is rooted there and that the facade points
   back to the loaded skill/runtime version;
7. record the recalled ids against this exact branch and worktree for dispatcher
   promotion;
8. continue immediately into `delivery-loop.md`.

The bootstrap owns collision detection, dirty-file ownership, base fetching, and
frontmatter persistence. Do not hand-create the branch or run directories around it.
Do not pass `--recover-spill` automatically; provenance must be proven first.
Claude Code may additionally switch its native workspace after the absolute binding;
Codex keeps using explicit workdirs. The binding, not the convenience switch, is
authoritative.
