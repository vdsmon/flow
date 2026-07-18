# Flow stabilization freeze design

## Purpose

Stop Flow's self-evolution loop and preserve the current evidence before deleting
the post-`eb1e2d9` routing and capsule architecture. This checkpoint changes only
repository operating policy. It does not simplify runtime code yet.

## Motivation

This work began with a useful idea from `PiLastDigit/TRIP-workflow`: give planning,
implementation, and review to agents with distinct roles and contexts while one
human-facing owner coordinates the ticket. The intended benefit was independent
judgment without an agent swarm. A planner could concentrate on design, an
implementer could write the change, and a fresh reviewer could challenge it. Mixing
Claude Code and Codex for those roles also looked valuable.

The first design recommendation was proportionate: keep role contracts, use
different writer and reviewer threads, begin with one bounded experiment, and
measure quality, time, and cost. The implementation moved away from that boundary.
Cross-harness planning became exact model and effort routing. Pressure-testing
became a theoretical exercise in solving every lifecycle failure before observing a
minimal prototype. Lost sessions, base drift, stale feedback, route changes,
cancellation, retries, and crash recovery each gained durable records and guards.
Planner-only routing then became a universal profile map, followed by private
execution capsules, patch import, compare-and-swap, quarantine, and separate fixer
roles.

Those mechanisms were individually defensible but collectively disproportionate.
They also duplicated protection already supplied by the ticket worktree, run lease,
atomic state, base-drift check, and planned-file commit gate. Automatic reflection
amplified the problem: machinery failures generated machinery tickets whose fixes
expanded the machinery and exposed more interactions to fix.

The observed `flow-yahm` run made the cost concrete. A bounded receipt refactor used
at least 22.28 million reported delivery input tokens and 84 minutes of cognitive
runtime, consumed nearly two hours of active machine time, regenerated a successful
fix because two index contracts contradicted each other, and still stopped on a
one-line type error. Since `a94e65e`, the repository had grown by 41,015 insertions
and 5,726 deletions across 228 files and 122 commits in five days. Mechanical
correctness was not producing a workflow the maintainer could understand or
comfortably supervise.

The stabilization does not reject modular roles. It separates that original idea
from the proof platform built around it. The target principle is: roles remain
modular, orchestration becomes boring. `eb1e2d9` is the conceptual boundary because
it retains native Codex support and the state-aware public command while preceding
the explicit route, planning-attempt, review-brief, and capsule expansion. This
freeze stops further compounding before the repository returns toward that simpler
shape through forward deletion rather than rewritten history.

## Scope

The checkpoint has four outputs:

1. Branches `archive/pre-simplification-2026-07-18` and
   `archive/flow-yahm-2026-07-18` preserve the current `main` tip and the exact
   staged product tree from the incomplete `flow-yahm` worktree. Both refs exist
   locally and on `origin`. The second ref is created with Git plumbing against a
   copied index so the live worktree's index and files are not changed.
2. A binary patch and SHA-256 digest under
   `/Users/victordsm/.local/share/flow/archives/2026-07-18/` provide a durable,
   separately inspectable copy of the staged `flow-yahm` implementation.
3. A new `stabilize/simplify-flow` branch starts from the preserved `main` tip and
   carries one freeze commit that changes `.flow/workspace.toml`.
4. The stabilization branch is merged into `origin/main` through an ordinary PR.
   The checkpoint is complete only after the remote default branch contains the
   frozen configuration and the local main checkout is advanced to it.

The incomplete `flow-yahm` implementation is preserved but not repaired, committed
as product work, or delivered. `flow-10nd` and `flow-qpgd` do not start.

## Freeze configuration

The stabilization branch makes these explicit workspace changes:

- set `[maintainer].self_target = false`;
- set `[evolve].auto_merge_hot = false`;
- reduce the pipeline to `ticket`, `plan`, `implement`, `code_review`, `commit`,
  `create_pr`, and `review_loop`;
- remove handlers for `e2e`, `review_brief`, `reflect`, and `merge` from this
  workspace pipeline;
- retain `[reflect]` but set `machinery = false` as an explicit dormant guard;
- set `[memory].compounding = false`;
- remove the dormant `[review_brief]` workspace block.

This disables automatic machinery-ticket creation, compounding reflection,
test-only E2E repetition, review-brief generation, and self-merge for this repository.
It does not delete the corresponding plugin features in this checkpoint.

The remaining `[agents.*]` blocks, `[models].e2e`, and inactive `[evolve]` tuning
keys are deliberate. This checkpoint makes the smallest policy change that stops
execution; the later architectural deletion removes obsolete route and evolution
configuration together with the code it describes.

## Activation and interim posture

The freeze has two distinct states:

1. **Prepared locally:** the freeze commit exists and
   `stabilize/simplify-flow` is checked out in the main checkout. The global
   maintainer pointer targets this checkout, so its `self_target = false` value
   disables local maintainer resolution. No Flow delivery or maintenance run may be
   launched in any checkout while `origin/main` is still unfrozen.
2. **Activated remotely:** the freeze PR is merged and a fresh read of
   `origin/main:.flow/workspace.toml` contains the frozen values. New worktrees and
   unattended clones now start from the frozen policy. Only then may the main
   checkout return to `main`, after advancing it to that remote commit.

Worktrees branch from a freshly fetched remote default and retain their tracked
`workspace.toml`; a local-only freeze therefore cannot protect a new run. Existing
runs retain the configuration from their starting commit and are not altered by the
freeze. At design review time there are no live run leases. Four historical
`run.lock` files remain, all expired on 2026-07-17, and `flow-yahm` released its
lease at the observed stop.

The local nightly and weekly evolve schedules are already disarmed and are rechecked
before the prepared state begins. The maintainer confirmed there is no cloud
scheduler; the checkpoint records that confirmation rather than inventing an
external shutdown step.

## Preservation and safety

- No history rewrite, reset, force-push, tracker mutation, worktree deletion, or
  installed-plugin refresh occurs. The only PR is the bounded freeze PR.
- Archival refs are named with the `2026-07-18` checkpoint date and pushed as
  archive branches without opening PRs for them.
- The `flow-yahm` worktree remains untouched while its patch and archive identity are
  captured. Its untracked operational ticket mirror is not product code and remains
  in place; the archive ref and patch cover the ten staged product/configuration paths.
- The existing unpushed observed-run design commit remains in the preserved ancestry.
- All edits use normal Git history on the stabilization branch.

## Verification

Before committing the freeze:

- parse `.flow/workspace.toml` as TOML;
- run the workspace validator and confirm the seven-stage order and handlers;
- record the expected non-fatal validator warning,
  `models: retained for rollback only; every profile is explicit`;
- confirm maintainer resolution is disabled;
- confirm `memory.compounding` and `reflect.machinery` are false;
- inspect the staged diff and ensure `.flow/workspace.toml` is the only freeze file;
- confirm local schedules remain disarmed, record the maintainer's confirmation that
  no cloud scheduler exists, and confirm no live run lease appeared.

Before opening the freeze PR:

- verify both archival refs resolve locally and on `origin` and that the durable
  binary patch matches its recorded digest; and
- confirm the original `main` ancestry and `flow-yahm` worktree remain recoverable.

To activate the freeze:

- wait for the freeze PR checks and merge it;
- verify the frozen values directly from a freshly fetched `origin/main`;
- advance the local main checkout to the activated remote commit; and
- recheck the archive refs, `flow-yahm` worktree, schedule state, and absence of live
  leases.

The checkpoint ends after showing the maintainer the commit, exact configuration
diff, archive identities, and activated remote SHA. Architectural deletion requires
a separate approved implementation plan. That later plan must also rewrite the
opening route and capsule claims in `CLAUDE.md`; leaving them active after code
deletion would instruct future sessions to rebuild the architecture being removed.
