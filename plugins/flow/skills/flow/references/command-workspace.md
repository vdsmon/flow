# Workspace commands

Workspace commands manage Flow's local installation, health, repairs, queued tracker
writes, and runtime layout. The loaded skill directory is the only trusted source for
installing or repairing runtime metadata. Never search arbitrary plugin caches.

## Runtime layout and automatic migration

Initialized workspaces converge on:

```text
.flow/
  runtime/
    flow
    skill-root
    memory-root
    layout-version
.flow/memory/<namespace>/
```

Before any workspace-dependent command, run the loaded runtime migrator exactly as
specified by `SKILL.md`'s entry contract. Migration
acquires its own lock, refuses while a base or revision lease is live, hashes and
backs up legacy memory, atomically moves it under `.flow/memory`, writes runtime
metadata, verifies relative paths/sizes/SHA-256, and only then removes legacy
metadata. Interrupted work resumes forward from its journal. If both legacy and v2
stores are non-empty, preserve both and stop. Never choose one by timestamp or size.

## `FLOW workspace setup`

Setup is convergent. It initializes a new workspace, continues an interrupted setup,
migrates an older layout, repairs runtime files from the loaded skill, or validates an
already healthy workspace. Rerunning it after a normal plugin upgrade is
unnecessary: entry migration is automatic.

1. Bind `task_root` absolutely and inspect initialization and migration markers.
2. For an uninitialized workspace, collect:

   - tracker backend: Jira or beads;
   - stage handlers: bare defaults, or explicit custom overrides;
   - Jira cloud/project and optional default assignee, or a beads prefix.

   The flat answer object must include `workspace_root` with the absolute
   `task_root` (`<absolute task_root>`, never a relative path or `$(pwd)`).

3. Write the flat answer object to a secure temporary JSON file using the host's
   exact-write primitive and retain its absolute path as `answers_path` across host
   calls. Call the loaded script directly because no facade exists:

   ```bash
   FLOW_HARNESS="<codex|claude-code>" \
     python3 "<skill_root>/scripts/init.py" --config "<absolute-answers-file>"
   ```

4. If setup was interrupted, use its durable marker to continue the same
   transaction. Do not discard partial state or start a second initialization.
5. In an initialized workspace, invoke the loaded launcher installer/migrator from
   `skill_root`, then validate through the resulting absolute runtime facade. Do not
   rerun the configuration transaction.
6. Remove the temporary answer file on every exit where its path is known.

Success reports tracker backend, namespace, runtime layout version, facade path, and
the host-rendered invocation for bare `FLOW`. A healthy second setup is a successful
validation, not an error and not a destructive reconfiguration.

An optional `[models]` table may provide agent hints, keyed by stage. A stage's value
is either one model string — applied to every agent that stage launches — or a table
keyed by the ROLE the stage launches (`[models.code_review]` with `reviewer` and
`fixer`), where each role takes a model string or `{ model = "...", effort = "..." }`.
The validator rejects a hint for a stage or role that launches nothing, so a dead key
fails loudly instead of silently doing nothing. Reconfiguration preserves the whole
table. Setup does not create provider matrices or require model identity as execution
evidence.

A value's vocabulary belongs to whatever launches the agent: a host model name for a
native agent, the reviewer CLI's own model and effort names for the bundled Codex
reviewer (`[models.code_review].reviewer`). Flow checks types, never values — a value
the CLI rejects surfaces at review time, where the reviewer drops that flag, runs once
more without it, and reports the substitution. Effort buys review depth at the cost of
wall clock against a fail-closed stage timeout, so the top of the range suits a manual
review rather than a pipeline default.

Setup writes `code_review` to the bundled reviewer
when the `codex` executable resolves and the harness is Claude Code, and to `inline`
otherwise; verify `codex exec` runs authenticated before relying on it, because an
unauthenticated CLI exits fast and reads like a broken launch.

Reconfiguration drops the bundled reviewer when Codex is gone, so an uninstall cannot
leave a dead handler wired. It does not do the reverse: a stored `inline` is
indistinguishable from an operator who wants inline review, so adopting Codex in an
existing workspace takes an explicit `--handler code_review=subagent:flow:codex-reviewer`.

The same invariant holds for any bundled agent: a workspace must never name an agent type its installed engine does not provide. A fresh Claude Code init writes `implement` and `e2e` to the bundled `flow:implementer` and `flow:e2e-runner` agent types; a Codex reconfigure drops both back to `subagent:general-purpose` for the same reason the reviewer does. An existing workspace adopts the bundled types only through a fresh init or an explicit `--handler implement=subagent:flow:implementer` (and the equivalent `--handler e2e=subagent:flow:e2e-runner`).

Setup derives the `[forge]` block from the repository's `origin` remote: a
github.com remote writes `backend = "github"`, a bitbucket.org remote writes
`backend = "bitbucket"` with `workspace` and `repo_slug` parsed from the URL, and
any other remote derives nothing. The derived block wins on reconfigure, so the
workspace converges after a repository move; a hand-authored block (the escape
hatch for exotic remotes) is preserved only while the remote stays underivable.
A Bitbucket forge reaches the host through the `bkt` CLI, so install it first:

```bash
brew install avivsinai/tap/bitbucket-cli
```

Validation probes PATH for the binary and fails with that install command when it
is missing, so a run cannot start against a Bitbucket forge the machine cannot
reach.

## `FLOW workspace inspect [<target>] [--json]`

Inspection is read-only. With no target, report every run, stage progress, lease,
snapshot health, runtime layout, pending tracker mutations, and attention flags:

```bash
FLOW_HARNESS="<harness>" "<facade>" status --workspace-root . [--json]
```

With a target, resolve it as in `command-target.md` and restrict output to the
associated ticket/base run/revision/PR. Include source paths or external ids for
conflicting evidence. Exit success with an empty result when the initialized
workspace simply has no runs. A missing workspace directs the human to
`FLOW workspace setup`.

## `FLOW workspace repair [<target>]`

Repair first performs the same read-only diagnosis as inspect, then offers only
actions justified by observed evidence. The operator confirms every write. Read
`delivery-repair.md` for leases, failed stages, snapshots, and ship-event attention.

Workspace-level repairs include:

- reinstall missing/stale runtime files from the currently loaded `skill_root`;
- continue a journaled layout migration;
- validate memory after migration without changing the corpus;
- target-specific takeover, retry, skip, abort, or snapshot reload;
- checkpoint and remove a safe stale worktree.

There is no global force. A live-lease takeover must be target-specific, display the
holder and evidence, and require an explicit confirmation. After every repair,
re-probe and report the resulting state; when invoked through a target lifecycle,
continue that lifecycle if it becomes healthy.

## `FLOW workspace sync`

Drain queued tracker mutations from `.flow/pending-mutations.jsonl` and reconcile
each operation against current tracker state:

```bash
FLOW_HARNESS="<harness>" "<facade>" sync --workspace-root .
```

Report `applied`, `applied_externally`, `superseded`, `failed`, `parked`, and
`removed` separately. Already-satisfied operations are idempotent successes. A
changed precondition is superseded, not replayed blindly. Failed operations stay in
the queue. Unsupported operations remain parked with their evidence and do not poison
replayable entries.

## `FLOW workspace worktrees clean [--dry-run]`

Sweep worktrees owned by the invoking workspace only. Resolve the absolute primary
checkout from the first `git worktree list --porcelain` stanza and recognize only
registered worktrees beneath its `.claude/worktrees` or legacy `.flow/worktrees`
directory. Never consider the invoking checkout itself.

A candidate is removable only when its normalized tracker state is `done` or
`cancelled`, its exact run lease is not live or corrupt, and one of these PR proofs
holds:

- a merged PR has a head SHA equal to the local worktree tip;
- no open or merged PR exists, the local `origin/HEAD` SHA matches a read-only
  `git ls-remote` result, and the branch has zero commits unique from that default.

An open PR always preserves its worktree. Missing ticket ownership, a stale remote
default, a merged-head mismatch, unique commits, or any candidate probe failure also
preserves it.

```bash
FLOW_HARNESS="<harness>" "<facade>" worktree-janitor sweep --workspace-root . --dry-run
```

First show the absolute `target_root`, every reapable candidate and its `confirmation_id`, and every
preserved candidate with its reason. If the public invocation included `--dry-run`, stop there.
Otherwise obtain confirmation for that exact target and candidate set. Then bind the destructive
invocation to the preview values:

```bash
FLOW_HARNESS="<harness>" "<facade>" worktree-janitor sweep --workspace-root . \
  --confirmed-target "<target_root>" \
  --confirmed-candidate "<confirmation_id>" [...]
```

The second invocation re-probes ownership, tracker, forge, remote-default, unique-commit, and exact
base/revision-lease evidence before it removes anything. A candidate absent from the preview or
whose path, branch, or tip changed has a different confirmation ID and is preserved.

A dirty candidate is checkpointed to a rescue ref before removal. Capture failure
leaves the worktree intact. `observe_at_close` runs inside the guarded teardown after checkpointing
and immediately before each removal attempt; the preview never observes or reaps. Never remove an
unrecognized worktree merely because its branch name resembles Flow.

## Harness parity

Claude Code may use its native worktree switch after Flow returns an absolute path;
Codex binds the path explicitly for every call. Either way the absolute binding is
authoritative. Setup and repair use the loaded skill, not a host-global shell
variable. No workspace command depends on a session-start hook.
