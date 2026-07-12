# Harness adapters

Flow's Python engine is harness-independent. The prose router is responsible for
mapping a small set of host capabilities onto the same ticket pipeline. Claude Code
and Codex are first-class adapters. Other harnesses use the generic fallback and must
state any capability loss instead of pretending a weaker operation is equivalent.

The self-evolution and maintainer verbs (`evolve`, `queue`) remain Claude-Code-only.
The ordinary ticket pipeline, including plan-time recall, does not depend on the
Claude Code SessionStart hook.

## Bind the execution context once

At entry, bind these logical values in the orchestration context:

```text
arguments      text supplied after the skill trigger, or the equivalent request text
skill_root     absolute directory containing this loaded SKILL.md
task_root      absolute checkout where the request started
run_root       absolute checkout that currently owns the run
facade         <run_root>/.flow/flow
harness        claude-code | codex | generic
capabilities   supported operation profile selected from the matrix below
```

These are conversation state, not shell environment variables. Never expect an
`export`, `cd`, or prior command's cwd to survive into another tool call or subagent.

`harness` is explicit adapter selection, not environment-based host detection. Codex
sets `FLOW_HARNESS=codex` on every direct init, repair, and facade invocation in that
same command. Claude Code may set `FLOW_HARNESS=claude-code`; leaving it unset retains
the backward-compatible Claude bundle roots. A generic adapter sets
`FLOW_HARNESS=generic` and supplies `FLOW_BUNDLE_SEARCH_ROOTS` when it wants bundle
discovery. The only accepted non-empty values are `codex`, `claude-code`, and
`generic`; never persist the choice with a shell export.

Before worktree bootstrap, `run_root` is the initialized checkout. After `worktree
create` succeeds, parse its absolute `result.worktree`, assign that value to
`run_root`, and set `facade` to the absolute `<run_root>/.flow/flow` path. From then
on:

- run every command with explicit workdir `run_root`;
- invoke `facade` by its absolute path;
- resolve every read, edit, test, git operation, and artifact beneath `run_root`;
- give every subagent the absolute workspace, skill, ticket, reference, and artifact
  paths plus the `harness` identity, and tell it to apply the same call-local
  `FLOW_HARNESS` selector to every facade invocation;
- refuse to fall back to `task_root` if the workspace binding is lost.

If a host command tool has no workdir field, make that individual call
self-rooting, for example `git -C "<run_root>" ...` or
`cd "<run_root>" && <command>` within the same call. A standalone `cd` whose effect
must survive the call is never valid state.

Throughout Flow's other documents, a recipe beginning with `.flow/flow` is shorthand
for the absolute logical `facade`, and `--workspace-root .` means `run_root` supplied
as that command call's explicit workdir. On Codex it also includes the call-local
`FLOW_HARNESS=codex` prefix; generic uses `generic`, while Claude Code may use
`claude-code` or its compatibility default. Do not execute the shorthand from an
inherited or assumed cwd, and do not rely on a prior export.

`EnterWorktree` is a Claude Code convenience. It does not relax any of these rooting
rules. A harness that exposes writable roots must verify that a new or adopted
worktree is writable before dispatcher `init` acquires the lease. If it is outside
Codex's writable roots, stop with a reopen/authorization instruction; never bypass
the sandbox with shell indirection.

## Capability matrix

| Capability | Claude Code | Codex | Generic fallback |
|---|---|---|---|
| Discovery | Claude plugin and `/flow` | Codex plugin/skill and `$flow:flow` | Installed skill path plus managed `AGENTS.md` |
| Arguments | `$ARGUMENTS` | Text after the skill mention or equivalent request | Adapter-supplied request text |
| Plan gate | Native plan mode and `ExitPlanMode` | Native Plan mode when active; otherwise soft turn boundary | Soft turn boundary |
| Workspace | `EnterWorktree`, then verify rooted context | Explicit `run_root` on every operation | Native switch if real; otherwise explicit root |
| Subagent | `Agent`, with supported model routing | Codex collaboration agent; no Claude model parameter | Independent model call or allowed inline fallback |
| Exact artifact write | `Write` | Rooted safe file edit/write | Exact write primitive or collision-safe fallback |
| Wait | `Monitor` when available | Owning-session wait/poll or bounded foreground poll | Bounded foreground poll |
| User input | `AskUserQuestion` | Plain question and wait | Plain question and wait |
| Notification | `PushNotification`, then durable fallback | In-thread plus durable forge fallback | In-thread plus durable fallback |
| Backgrounding | User-owned `/bg` and `claude agents` | Host-owned task/background surface | Host-owned or foreground only |

Do not detect a harness from environment variables. The host already knows which
adapter it is running. Probe optional Claude Code tools only where that adapter needs
to distinguish model or tool availability.

## Discovery, init, and repair

Both native plugins expose the same `skills/` tree:

```text
plugins/flow/.claude-plugin/plugin.json
plugins/flow/.codex-plugin/plugin.json
```

Codex should load Flow through its plugin/skill discovery. `AGENTS.md` is durable
repository guidance and the generic fallback, not Codex's primary loader.

Before a workspace facade exists, invoke init directly from the loaded absolute
`skill_root`:

```bash
FLOW_HARNESS="<codex|claude-code|generic>" \
  python3 "<skill-root>/scripts/init.py" --config "<absolute answers_path>"
```

Every successful init or reconfigure installs `.flow/skill_dir` and executable
`.flow/flow`. Each file is atomically replaced; the two replacements are not one
filesystem transaction. Worktree create and reload stamp the Flow installation that
is actually executing the operation.

For an initialized workspace, prefer its absolute facade. If launcher metadata is
missing in a legacy workspace or paused worktree, repair it from the currently loaded
`skill_root`. If metadata exists but points to a stale installation, report the stale
binding and repair only from a known loaded installation. Never search arbitrary
plugin caches or marketplace directories.

The facade derives its workspace from its own location, reads the sibling
`.flow/skill_dir`, changes the child process cwd to the owning workspace, and `exec`s
only an allowlisted command. It exports both `FLOW_SKILL_DIR` and the legacy
`CLAUDE_SKILL_DIR` to child scripts. Those child variables are implementation details;
the parent router still uses logical `skill_root` and `facade` values.

The call-local `FLOW_HARNESS` selector prevents a machine with both hosts installed
from resolving a bundle out of the wrong plugin tree. `codex` searches only Codex
layouts beneath `${CODEX_HOME:-~/.codex}/plugins` (including the CLI cache); it never
searches `~/plugins`, repository plugin trees, or `.claude/plugins`.
`claude-code` and the unset compatibility default search Claude roots and never Codex
roots. Explicit `generic` searches only `FLOW_BUNDLE_SEARCH_ROOTS`; when that override
is absent it discovers no bundle roots, so it cannot configure a skill handler its
loader may not support. An unknown non-empty selector fails clearly.
The same selector governs both handler-bundle discovery and version-stable cache-source
resolution; neither may cross into another host's roots.

## The one plan gate

The gate presents the plan and prevents implementation from starting before explicit
approval.

- Claude Code uses native plan mode and `ExitPlanMode`.
- Codex uses native Plan mode when the session is in that mode. Otherwise, present
  the complete plan and confidence rating, end the turn, and wait for explicit
  approval.
- A generic harness uses the same soft turn boundary.

The soft boundary is an honest degradation: the model must exercise restraint because
the host does not enforce read-only access. It is still a real stop. Do not seed a
worktree, edit a repository file, or continue into `do` in the same turn.

Do not pass `--recover-spill` automatically on any adapter. A dirty planned file may
be user-owned work that predates Flow, and the engine cannot infer provenance from
the dirty state. Use spill recovery only after confirming that the exact paths were
created by this Flow attempt after its initial status snapshot and do not overlap
pre-existing WIP. If provenance is ambiguous, stop and ask instead of moving or
reverting the file.

The `--auto` path has no interactive gate. It remains read-only until its documented
self-approval bootstrap or defer/block transition.

## Independent reasoning and model routing

For confidence rating and adjudication, use this order:

1. On Claude Code, prefer `advisor` when available. Fable models have no advisor, so
   skip the probe there.
2. Otherwise use a fresh independent subagent or second model call with the same
   ticket context, plan, and rubric.
3. If no independent call is available, follow the stage's documented defer behavior
   when independence is required. Inline fallback is allowed only for protocols that
   explicitly tolerate loss of isolation.

`model` resolution returns Claude model names for Claude Code. Pass that value only
when the host's subagent API accepts it. Codex collaboration agents do not accept
Claude model pins, so omit the model parameter and inherit the active model. Never
invent a host parameter to preserve a hint.

## Stage agents and artifacts

Every stage-agent prompt must include values in this shape:

```text
Workspace root: /absolute/run/worktree
Skill root: /absolute/loaded/flow/skill
Harness: claude-code | codex | generic
Ticket dir: /absolute/run/worktree/.flow/runs/<KEY>
Reference path: /absolute/loaded/flow/skill/references/<stage>.md
Artifact path: /absolute/run/worktree/.flow/runs/<KEY>/stages/<STAGE>.out
```

Tell the agent that its inherited cwd is non-authoritative, all commands use the
workspace root explicitly, the absolute `<Workspace root>/.flow/flow` is its Flow
facade, and all repository writes stay beneath the workspace root. This is required
even when the host currently appears to inherit cwd. Tell it to prefix every Flow
facade invocation with `FLOW_HARNESS=<Harness>` in that same command; a shell export is
not continuation state.

Capture the complete returned report at the absolute artifact path before calling
`dispatch advance`. Prefer the host's exact file-write primitive. When that primitive
is unavailable, use the documented collision-safe quoted-heredoc fallback; never put
model output in a shell argument or an unquoted redirect.

For `skill:<name>` handlers, use the host's native skill loader. No default stage uses
a skill handler. If a configured skill is unavailable, fail that stage or replace the
workspace configuration with an equivalent inline/subagent handler; do not silently
pretend it ran.

## Waits, questions, and notifications

- A Claude Code review loop may use `Monitor`. Codex keeps waits in the owning session
  with its wait/poll mechanism. A generic adapter uses the documented bounded poll.
  A child agent must not own continuation after it returns.
- When input is required, Claude Code may use `AskUserQuestion`; Codex and generic
  adapters ask plainly and wait. Detached and `--auto` runs follow the existing
  defer/block protocol instead of waiting for an absent user.
- PR-ready notification is best-effort. Claude Code may send `PushNotification`.
  Every adapter also surfaces the result in-thread and uses the forge's durable PR
  comment fallback where the stage protocol requests it. Pipeline state remains
  authoritative if notification fails.
- `${CLAUDE_JOB_DIR}` and self-teardown are Claude Code background-job details. When
  absent, skip that branch. Codex backgrounding is owned by the host, not toggled by
  Flow prose.
