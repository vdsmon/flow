# State of Flow — 2026-07-17

One page for the maintainer returning after time away. What this system is today, what
each moving part costs and buys, and which parts are safe to simplify. Written at the
close of the audit-delivery session (PRs #512–#537, 28 tickets shipped through Flow
itself, including one engine-performed hot auto-merge under a live guard review).

## What Flow is right now

A ticket→PR pipeline that runs unattended. You approve a plan; Flow implements,
reviews, verifies, commits, opens the PR, and parks it green for your merge. Cognition
runs in routed capsules (codex or claude CLI per profile); the dispatcher owns stage
order and leases; every stage leaves durable evidence on disk. It has now delivered
~30 real changes to its own repository with zero bad merges.

## The load-bearing floor (do not simplify)

Four guards plus the flock substrate, per `references/robustness.md`. Every failure in
the audit session was caught by one of these; none has ever let a bad state through:

- run lease (one writer per run)
- canonical-snapshot TOCTOU guard (capsule sees what it claims to see)
- atomic writes + quarantine (no torn state; suspect results isolated, never trusted)
- content-ownership commit gate (only planned files land)

These are cheap at runtime. Their cost is code complexity already paid for.

## The ceremony dial (safe to turn, in either direction)

Each row is prose/config, not architecture. Turning any of these is a small reversible
change. Decide from real single-ticket runs, not from dogfood-burst memories.

| Step | Buys | Costs | Verdict today |
|---|---|---|---|
| Plan assess per version | Caught real flaws repeatedly (5 rounds on the budgets plan; a recycled-pgid wedge; a prose-only insufficiency) | One reader capsule per plan version | Keep. Highest value-per-token step observed |
| Attest / route receipts | Frozen-route exactness proof | Pure overhead once routes stop changing mid-attempt | Collapse candidate (deferred ticket 10nd) |
| Revalidate at approve | Catches base drift before bootstrap | Cascading replans when merging fast in parallel | Keep; cost only appears in burst mode |
| code_review, two readers | Caught majors in most runs incl. a session-id leak and a bypassable sanitizer | Two reader capsules | Keep on full lane; light lane already runs one |
| e2e capsule | Clean-room proof from a pristine clone | Redundant third suite run for test-ci-only recipes | Lane-gate it (deferred ticket brc8) |
| review_brief | Reviewer companion page | One author capsule | Solved: off in this repo; auto-skips unattended (PR #527) |
| reflect | The self-evolution feedstock; filed the beads that became real fixes | Files ~1–4 beads per run — the backlog-growth feeling | Keep, but see the governor note below |
| merge stage (hot) | Engine self-merge under independent guard review | Only runs for evolve-hot beads | Proven live (PR #534); gated, leave as is |

**Reflect governor (recommended future rule):** reflect may file at most one bead per
run; further proposals go into a digest comment on the run's ticket. Caps backlog
growth structurally. Not yet implemented; do it the first time the backlog feeling
returns.

## Tracker state at freeze

- **5 open** (kept deliberately): mg79 (Jira User-Agent bug), 4ipf (e2e trusts a
  failed recipe), 5ogh (fail-closed review packets), be8j (restore resume-branch test
  coverage), pxcz (a maintainer decision, not a task).
- **29 deferred**: everything else, including qpgd (185-test cut — run in a dedicated
  session; manifest embedded in the ticket) and q75n (staged replan with notes).
  Deferred means invisible to `bd ready` and to drains. Deleting any of them later is
  also fine; they are records, not obligations.
- Machinery-freeze is in effect: no Flow-on-Flow tickets until the maintainer says so.

## Known gaps (the honest list)

1. **Completeness attestation** — a capsule killed at a usage cap can return a
  deceptive terminal outcome (a "clean" review that read nothing; a "passing" e2e that
  ran half its recipe). Humans/drivers caught every instance by reading summaries.
  This is the one gap worth fixing before heavy unattended use on real projects.
2. Scheduling deps between tickets sharing a surface may encode semantic order
  (sweep-before-cluster). Verify intent before removing one; removal cost a full
  re-plan this session.
3. The engine checkout must be advanced (ff-merge + marketplace refresh) after merge
  waves — a stale engine now hard-blocks new-base bootstraps since the route digest
  covers contract code.

## How to come back to this repo

1. `bd ready` — the 5 kept tickets are the only live work.
2. Drive one real ticket end to end. Note which ceremony step annoys you on a normal
  run — that row of the dial above is the one to turn.
3. `git log --oneline --merges -20` + each PR body tells the story of any change; every
  ticket carries its snags and decisions as comments.
