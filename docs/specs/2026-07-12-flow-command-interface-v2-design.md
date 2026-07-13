# Flow command interface v2

## Intent

Flow exposes one state-aware daily driver and five focused namespaces. The public
interface describes user intent; the existing facade and dispatcher remain internal
implementation. Claude Code and Codex receive the same logical command after their
host-specific skill trigger.

The cutover is intentionally breaking. Existing memory, runs, tickets, leases,
worktrees, and PR resumability are preserved, but old commands and flags receive no
aliases or redirects.

## Public grammar

`FLOW` means `/flow` in Claude Code and `$flow:flow` in Codex.

```text
FLOW
FLOW <target> [<target> ...]
     [--unattended] [--together]
     [--verify express|light|full]
     [--e2e "<recipe>"]
     [--request "<additional intent>"]

FLOW ticket create [--request "<problem>"]
FLOW ticket group (<ticket>... | --mine) [--state open]
FLOW ticket split <ticket>

FLOW memory search [<query>]
     [--ticket <key>]... [--label <facet:value>] [--digest]
     [--semantic] [--threshold <float>] [--branch <branch>] [--limit <n>]
FLOW memory prune
FLOW memory rebuild [--full]

FLOW measure <throughput|lead-time|friction|reverts|experiment|
              trend|memory-health|recall-quality|fix-efficacy>
     [--since <date>] [--until <date>] [--json]
FLOW measure throughput --checkpoint <personal|work> [--manifest <path>]

FLOW workspace setup [--guidance]
FLOW workspace inspect [<target>] [--json]
FLOW workspace repair [<target>]
FLOW workspace sync

FLOW maintain backlog status [--preview]
FLOW maintain backlog drain [--dry-run]
FLOW maintain evolution audit|propose|epic
FLOW maintain evolution expand <epic>
FLOW maintain evolution drain [--dry-run] [--include-proposals]
FLOW maintain worktrees clean [--dry-run]

FLOW help [ticket|memory|measure|workspace|maintain]
```

Targets are configured tracker keys, `ticket:<key>` when a key collides with a
reserved root token, `pr:<number>`, or forge PR URLs. Static namespaces win over
target parsing.

Bare `FLOW` renders a read-only cockpit. Old verbs and flags are unknown input, not
migration aliases.

## State-aware target

A read-only classifier joins tracker, run, lease, snapshot, revision, and forge
evidence. A pure reducer returns exactly one action:

```text
start | answer | resume | running | repair | revise | show | conflict
```

Priority is fixed:

1. Unknown target: error.
2. Fresh live ticket: plan, cross the one approval gate, and deliver.
3. Deferred or blocked: surface the stored question; `--request` records the answer
   and reopens.
4. Healthy incomplete run: resume. Reject `--request` after approval because it would
   change owned scope mid-run.
5. Live foreign lease: report the holder and stop.
6. Failed, stale, drifted, or corrupt run: diagnose, offer only applicable repairs,
   and confirm every write.
7. Open PR with actionable feedback or `--request`: revise the same PR.
8. Open PR without actionable feedback: show ready status without mutation.
9. Merged or closed delivery: show the receipt.
10. Contradictory evidence: preserve it and return `conflict`.

After a confirmed takeover, snapshot reload, retry, or skip, Flow re-probes and
continues in the same invocation. Abort and unresolved ship-event corruption stop.

Multiple tickets with `--together` must all be fresh and groupable. Without it,
attended mode asks sequential versus together; unattended mode errors.
`--unattended` conflicts with `--verify`. No option is silently ignored.

## Runtime layout v2

Runtime metadata and namespaced memory occupy separate directories:

```text
.flow/runtime/flow
.flow/runtime/skill-root
.flow/runtime/memory-root
.flow/runtime/layout-version
.flow/memory/<namespace>/
```

The loaded plugin performs a journaled v1 to v2 migration before any
workspace-dependent command. It refuses while a base or revision lease is live,
backs up and hashes memory, atomically moves the legacy namespace, installs the new
runtime files, verifies path/size/hash equality, and removes legacy metadata only
after validation. If old and new stores are both non-empty, it preserves both and
refuses. Interrupted migration resumes forward. Fresh setup and worktree creation
write only v2.

## Command registry and references

`public-commands.toml` is the authored source for paths, arguments, options, effect
classes, workspace requirements, help, harness capabilities, and reference ownership.
A check-only generator renders the compact router/help block in `SKILL.md`, the
trigger description, and public command documentation. CI rejects stale output.

Public references are organized by target, ticket, memory, measure, workspace, and
maintain. Delivery planning, execution, revision, and repair remain internal
references. All user-facing hints render logical `FLOW` through the harness adapter.

## Cross-harness maintenance

Maintenance uses an owner-session worker pool with `capacity`, `launch`, `wait`, and
`cancel`. Claude Code and Codex both use host-native collaboration agents. The owner
may be backgrounded by the user. Durable fleet state, leases, runs, and PRs are
authoritative; worker handles are disposable.

This replaces detached `claude --bg` workers, Claude job-directory scans,
`CLAUDE_JOB_DIR`, `claude stop`, and self-teardown. Claude may honor a model hint;
Codex workers inherit the active model. Read-only discovery workers are guarded by
pre/post git snapshots and abort before filing findings if they write unexpectedly.

## Release contract

Ship one feature release with hard internal gates in this order: runtime v2,
registry and state modules, namespaces, worker pool, then public cutover and deletion.
Test migration against a copy of the live `.flow` data before upgrading installed
plugins. Start fresh Claude Code and Codex sessions after upgrade; no setup rerun is
required.
