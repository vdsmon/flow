# Modular agent routing and cross-harness planning

## Intent

Flow should make each cognitive role explicit without turning every deterministic
stage into an agent. A user keeps one writable owner cockpit while replaceable
specialists plan, implement, verify, or review through typed routes. The review
surface explains why the change exists and what will execute; the host-native plan
gate remains the authority to begin writes.

This epic is split into three increments:

1. universal route contracts, provenance, compatibility, and owner-native execution;
2. a cross-harness, read-only planning loop with versioned plans and visual review;
3. cancellation and gate fault injection, one real self-dogfood run, then the Codex
   Sol planner default.

## Route and authority model

An explicit route is an indivisible `harness`, `model`, and `effort` triple. The
public harness names are `claude_code` and `codex`; transport terms such as native or
CLI belong only in execution receipts. Per-run overrides outrank workspace routes,
which outrank standalone legacy compatibility and built-in defaults. Resolution is
frozen before a run is exposed.

Common routes and `by_owner` routes are mutually exclusive. Owner-relative routes
keep both supported owner harnesses first-class while post-plan cross-harness writers
remain disabled. The selected defaults are:

| Profile | Claude Code owner | Codex owner |
|---|---|---|
| Planner | `codex / gpt-5.6-sol / xhigh` | same |
| Exceptional plan assessor | `claude_code / opus / high` | same |
| Implementer | `claude_code / sonnet / high` | `codex / gpt-5.6-luna / high` |
| E2E | `claude_code / sonnet / medium` | `codex / gpt-5.6-luna / medium` |
| Diff and guard reviewer | `claude_code / opus / high` | `codex / gpt-5.6-sol / high` |
| Revision fixer | `claude_code / sonnet / high` | `codex / gpt-5.6-luna / high` |

The route is desired state. Effective execution exists only after structured host
evidence proves the exact selection. Agent-authored text is never evidence. Claude
Code can activate an owner-native route after its native launch response accepts the
exact model and effort. The current Codex collaboration spawn cannot select either,
so Codex post-plan routes remain visibly shadowed and inherit the owner model.

Only one named writer profile may mutate product source at a time: implementer,
revision fixer, review fixer, or the maintainer-only machinery fixer. Reviewers do
not acquire write authority by finding a problem. Flow metadata, temporary review
artifacts, ref fetches, and approved bootstrap remain orchestration writes.

## Planning lifecycle

Before approval, Flow has an attempt-scoped coordinator rather than a run, lease, or
worktree. The human talks only to the owner cockpit. The owner relays planner
questions and human feedback verbatim, with its own synthesis separately labeled.
Every planner response is a complete versioned plan; hidden conversation is never
required to reconstruct the reviewed result. The canonical plan requires motivation,
goal, before/after scenarios, architecture, decisions, acceptance outcomes, ordered
steps, files and context, verification and e2e recipe, lane, compatibility, rollout,
and risks. The worker binds the envelope author to the harness/model it actually
launched rather than trusting agent-authored provenance text.
Each accepted version also binds a unique active planner launch receipt. The gate
includes the current receipt digest, so a caller cannot bypass worker attestation by
submitting only a self-declared author.

The same logical planner thread resumes through feedback. It rotates before the
fourth revision or earlier under context pressure. Owner loss starts a new attempt
and a fresh planner rehydrated from the complete plan and feedback ledger; approval
does not transfer. A planner has a ten-minute soft deadline and a forty-minute hard
deadline. One fresh retry receives a new budget, but it cannot start until process
termination and output closure are proven. Every physical attempt keeps its own
600/2400 budget, deadline events, outcome, elapsed time, and terminal acknowledgement.
Aggregate wall time is separate.
When the failed launch was a resumed thread, the retry receives a separate complete
rehydration prompt; the feedback delta alone is never reused as fresh context.

Lavish is the preferred ephemeral local review surface. It emphasizes motivation,
before/after scenarios, system relationships, routes, tests, risks, and the revision
summary. Its final action freezes feedback and requests native approval; it never
authorizes implementation. Markdown is the visible correctness-preserving fallback.

The owner assesses every externally authored revision. A fresh separate assessor is
reserved for inline owner-authored, unattended, hot/guard-sensitive, or explicitly
escalated plans. Author and assessor must differ, and assessment findings return to
the planner rather than being edited into the plan by the assessor. Each verdict also
binds the exact current plan digest and its actual author id.
A policy-fresh verdict additionally binds a structured `plan_assessor` launch receipt
whose distinct worker id matches the assessor identity.

## Gate, bootstrap, and provenance

Immediately before the native gate, Flow revalidates default-branch movement against
planned, evidence, and route/configuration paths. Relevant or ambiguous movement
requires a fresh plan and renewed review. Proven-disjoint movement receives a
revalidation receipt. Native approval binds the exact plan digest, approved base
SHA, feedback watermark, route digest, assessment verdict, and revalidation receipt.
The owner passes the digest returned immediately before the host-native gate back to
approval, and the attempt lock rejects a changed tuple.
The native-gate plan file is the deterministic rendering of that same envelope;
arbitrary bytes cannot be paired with its digest.

Bootstrap uses the approved SHA even if the remote default advances afterward. Its
ticket claim covers exact tuple validation, worktree creation, state seeding, and
route-snapshot persistence. A prepared/worktree-intended/worktree-created/run-seeded/committed journal
lets the same tuple finish or retry after interruption. A concurrent
loser cannot expose a partial run or substitute a different tuple. Journal filenames
derive from the verified approval digest, never a planner-provided attempt id, and a
committed recovery re-verifies state, route, approval, and plan artifacts before return.
The intended phase records rollback coordinates before `git worktree add`.
Cleanup clears those coordinates only after worktree and branch removal are proven.

Route provenance pairs a frozen desired-state snapshot with per-launch receipts.
Together they record desired and effective routes separately, activation, source,
owner, adapter/transport identity, canonical provider model when available,
prompt/schema hashes, and canonical digests. Tool stages record `model = none`.
Inline cognitive stages record the owner-reported identity or `unknown`; Flow never
guesses it from a configured alias.

## Compatibility and proof

`[models]` remains a standalone compatibility mode with its exact lane skip, OFF,
fail-open, stage precedence, and Codex inheritance. It is not converted to a partial
AgentRoute. The migration command first shows a surgical append proposal, requires
explicit confirmation to apply, writes atomically, preserves unrelated bytes, and
refuses values that cannot be translated safely. Generic setup writes no unusable
explicit routes.

The planner route is unconditional after deterministic route, cancellation, drift,
feedback, and bootstrap fault tests pass and this self-dogfood ticket proves one human
revision, planner-thread resume, Lavish freeze, native approval, exact-SHA bootstrap,
and a post-fix live schema launch. Configured, built-in, and overridden planners may
activate only after an exact read-only CLI receipt. There is no automatic route
fallback. Plan assessors and post-plan cross-harness workers remain shadowed.

## Increment 3 self-dogfood evidence

The attended `flow-j2fc.3` attempt started from
`c375c7029dce56a23ea8bee829985babf95334e7`. Before approval, the owner observed no
Flow run, ticket worktree, ticket branch, or bootstrap journal. The proof artifact
hash is `f79a3eec0f8f188036a57894a311e81f71b1d7001cebbdbec519c6567f7195f2`.
It records only the one-way planner continuity hash
`712cb8d8f2e7d15b98597b9aa5f67a5c57fac69a26c627905b4df6dba1d84348`.
The raw thread id is absent from the attempt bundle, Flow run, and repository.

The first two real launches were failures before model work. Codex first rejected
`uniqueItems`, then rejected open nested object schemas. The event log hash is
`da0f9ad1c383d49ce564d280113392a105864b6e53af13462e7f9c6a6db70450`.
Neither launch produced an accepted plan version. The later accepted launch used a
temporary closed schema with hash
`6320148d18019cce6b7c93cf58b83e09edae364a1c95b841402307a3b45b1ca7`.
That workaround is historical evidence, not a second schema path in Flow.

The accepted planner route was `codex / gpt-5.6-sol / xhigh`. Version 2 has plan
digest `f4796dba97ff42e781ad018de72d045dd49912f1b9921dd77010bf5689740f88`
and launch receipt digest
`e3e4461b7ea3b6fc91a010ea03830f33682964d5be8f46460fdac555dd6601b9`.
It resumed the same thread and incorporated `F-1` verbatim: `10min soft / 40min hard.
resets on retry`. The owner synthesis required separate physical-attempt metrics and a
fresh 600/2400 budget on retry. The fresh assessment passed with digest
`6185fd3b87a13eb927e51389d0dbcf75655bf7f9324530bbccf76eda46dc9651`.

The final Lavish feedback batch was empty and the surface was frozen before native
approval. The HTML and Markdown companion hashes are
`eb1fd1ceca53e52c1e30e7afac338df89406d81f8b599c4795d79e7ba352e1f2` and
`5d1bb70082c8419195b1bae01e77076306322fdc0677e841ae96da49f5f960e1`.
The gate digest is
`6e27848d65263bcf1b2395127380dc11c392b38f8312a88822beb9b511597ed8`,
and the approval receipt digest is
`18b6a5de71352f1c10824da092485a3e4e160a015004e16c0f89fd422e3af085`.
The canonical plan bytes hash is
`2a4ab0f6f775237623e099ec47c2d77c29ad761d572b82572dedf93504609fb6`.

Bootstrap used the approved SHA, created branch `feat/flow-j2fc.3-planner-rollout`,
and committed journal record
`c7352d072a308179cfd5a9c5f5654d06097544871969110f441246927919aa5e` for run
`aab3fbe8171d2903`. The seeded route digest is
`89b81628ca49a0f221ddabc8185d5f80c5a124767f248f91de94b6985dfdbe14`.
Seeded plan, route, approval, and state artifacts are checked again on committed
recovery.

After the source schema fix, a separate live Sol/xhigh smoke consumed the bytes
emitted directly by `planning-attempt schema`, with no normalization or rewritten
copy. Codex accepted schema hash
`b6ed6dfddd50e0d6f4d4e4b78e1c99b13352a0eae9c183e2c1455e3dd892b96e`
and returned plan digest
`23f62e5007d99cac655ba0fb0599545800fa1219b2ea8f096b711101a8ba56cd`.
Its one physical-attempt record reports a 600-second soft budget, 2400-second hard
budget, no deadline event, success, terminal acknowledgement, and 91.75 seconds of
elapsed time. The worker artifact hash is
`722cf7d732df0e5c3cb77476e0376ccbe036cf8a8872d9f96cf2f17aacce977a`.

The deterministic increment-3 suite covers route activation and receipt mismatch,
duplicate semantic validation, concurrent attempt mutation and sibling-version CAS,
stale gate digests, exact plan bytes, selective drift, review-surface parity, two
separate deadline budgets, cancellation acknowledgement, every journal phase,
rollback-before-retry, and committed-artifact tampering. The seven focused test files
pass 314 tests after code review tightened legacy compatibility, invalid-output
telemetry, and concurrency assertions. The repository gate then passes 3,393 tests
with one expected skip, and lint, type checks,
generated-command validation, and the prose/CLI seam check also pass.
