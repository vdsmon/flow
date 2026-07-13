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
required to reconstruct the reviewed result.

The same logical planner thread resumes through feedback. It rotates before the
fourth revision or earlier under context pressure. Owner loss starts a new attempt
and a fresh planner rehydrated from the complete plan and feedback ledger; approval
does not transfer. A planner has a ten-minute soft deadline and a forty-minute hard
deadline. One fresh retry receives a new budget, but it cannot start until process
termination and output closure are proven.

Lavish is the preferred ephemeral local review surface. It emphasizes motivation,
before/after scenarios, system relationships, routes, tests, risks, and the revision
summary. Its final action freezes feedback and requests native approval; it never
authorizes implementation. Markdown is the visible correctness-preserving fallback.

The owner assesses every externally authored revision. A fresh separate assessor is
reserved for inline owner-authored, unattended, hot/guard-sensitive, or explicitly
escalated plans. Author and assessor must differ, and assessment findings return to
the planner rather than being edited into the plan by the assessor.

## Gate, bootstrap, and provenance

Immediately before the native gate, Flow revalidates default-branch movement against
planned, evidence, and route/configuration paths. Relevant or ambiguous movement
requires a fresh plan and renewed review. Proven-disjoint movement receives a
revalidation receipt. Native approval binds the exact plan digest, approved base
SHA, feedback watermark, route digest, assessment verdict, and revalidation receipt.

Bootstrap uses the approved SHA even if the remote default advances afterward. Its
ticket claim covers exact tuple validation, worktree creation, state seeding, and
route-snapshot persistence. A concurrent loser cannot expose a partial run. Later
increments add a recoverable prepared/committed bootstrap journal around this claim.

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

The planner route becomes unconditional only after deterministic route, cancellation,
drift, feedback, and bootstrap fault tests pass and a real self-dogfood ticket proves
one human revision, planner-thread resume, Lavish freeze, native approval, and exact
SHA bootstrap. Run reports separate planner activity, owner assessment, and human
review idle time so latency remains explainable.

Until the planning-loop increment lands, planner and assessor routes are desired
shadow state only. Setup may materialize those defaults so configuration and
provenance stabilize early, but no pre-approval worker claims to have executed them.
