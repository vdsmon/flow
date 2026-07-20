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

## `FLOW workspace setup [--guidance]`

Setup is convergent. It initializes a new workspace, continues an interrupted setup,
migrates an older layout, repairs runtime files from the loaded skill, or validates an
already healthy workspace. Users do not need to rerun it after a normal plugin
upgrade because entry migration is automatic.

1. Bind `task_root` absolutely and inspect initialization and migration markers.
2. For an uninitialized workspace, collect:

   - tracker backend: Jira or beads;
   - stage bundle: bare, recommended, or explicit custom handlers;
   - Jira cloud/project and optional default assignee, or a beads prefix.

   The flat answer object must include `workspace_root` with the absolute
   `task_root`. When `--guidance` is present, include `agents_md: true`.

3. Write the flat answer object to a secure temporary JSON file using the host's
   exact-write primitive and retain its absolute path as `answers_path` across host
   calls. Call the loaded script directly because no facade exists:

   ```bash
   FLOW_HARNESS="<codex|claude-code|generic>" \
     python3 "<skill_root>/scripts/init.py" --config "<absolute-answers-file>"
   ```

4. If setup was interrupted, use its durable marker to continue the same
   transaction. Do not discard partial state or start a second initialization.
5. In an initialized workspace, invoke the loaded launcher installer/migrator from
   `skill_root`, then validate through the resulting absolute runtime facade. Do not
   rerun the configuration transaction. When `--guidance` is present, update only
   the managed guidance block:

   ```bash
   FLOW_HARNESS="<codex|claude-code|generic>" \
     python3 "<skill_root>/scripts/init.py" \
     --guidance-only --workspace-root "<absolute task_root>"
   ```

6. `--guidance` installs or updates the managed repository guidance block. Native
   Claude Code and Codex plugin discovery do not require it; it is useful for a
   generic harness or repository-local operational guidance.
7. Remove the temporary answer file on every exit where its path is known.

Success reports tracker backend, namespace, runtime layout version, facade path, and
the host-rendered invocation for bare `FLOW`. A healthy second setup is a successful
validation, not an error and not a destructive reconfiguration.

An optional `[models]` table may provide host-native stage hints. Reconfiguration
preserves those hints. Setup does not create provider matrices or require model
identity as execution evidence.

## `FLOW workspace inspect [<target>] [--json]`

Inspection is read-only. With no target, report every run, stage progress, lease,
snapshot health, runtime layout, pending tracker mutations, and attention flags:

```bash
FLOW_HARNESS="<harness>" "<facade>" status --workspace-root . [--json]
```

With a target, resolve it as in `command-target.md` and restrict output to the
associated ticket/base run/revision/PR. Include source paths or external ids for
conflicting evidence. Exit success with an empty result when the initialized
workspace simply has no runs. A missing workspace directs the user to
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

## Harness parity

Claude Code may use its native worktree switch after Flow returns an absolute path;
Codex binds the path explicitly for every call. Either way the absolute binding is
authoritative. Setup and repair use the loaded skill, not a host-global shell
variable. No workspace command depends on a session-start hook.
