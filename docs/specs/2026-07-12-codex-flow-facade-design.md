# Codex-first Flow facade hardening

Status: approved direction, written specification pending final review
Date: 2026-07-12

## Context

Flow's Python engine is already harness-independent, but its orchestration prose was
written around Claude Code primitives: `$ARGUMENTS`, plan-mode tools,
`EnterWorktree`, `Agent`, `Write`, `Monitor`, `PushNotification`, and
`CLAUDE_SKILL_DIR`. The current branch adds a workspace-owned `.flow/flow` facade
that hides script installation paths and normalizes child-process execution. That is
the correct deterministic seam.

The remaining portability defect is above that seam. The current Codex fallback says
to `cd` into the seeded worktree and continue, but Codex command calls and subagents
do not inherit a previous command's cwd. Relative commands and file operations can
therefore return to the original checkout after bootstrap. The same prose also treats
all non-Claude harnesses alike even though current Codex supports native skills,
plugins, subagents, Plan mode, and worktree tasks.

This design keeps the existing dispatcher, stage registry, reference documents, and
workspace facade. It introduces an explicit rooted execution contract and two
first-class harness adapters in prose: Claude Code and Codex. A generic capability
fallback remains documented, but true universal harness support is not required.

## Goals

- Make Claude Code and Codex safe, first-class ways to run the same Flow pipeline.
- Ensure every post-bootstrap command, edit, subagent, and artifact targets the
  seeded Flow worktree, independent of mutable shell cwd.
- Keep `.flow/flow` as the single post-init command interface.
- Make Flow natively discoverable and installable as a Codex plugin while preserving
  its Claude Code plugin.
- Upgrade existing initialized workspaces, managed `AGENTS.md` blocks, and paused
  worktrees without discarding user state.
- Preserve the current lease, snapshot, content-ownership, stage, tracker, forge,
  memory, and self-evolution machinery.
- Turn the reviewed failure modes into deterministic regression tests.

## Non-goals

- Replacing the prose orchestration loop with a Python `FlowSession`, MCP server, or
  event protocol.
- Guaranteeing identical hard plan-gate enforcement on every harness.
- Porting Claude Code's self-evolution/background-job machinery to Codex.
- Auto-detecting arbitrary future harnesses from environment variables or tool names.
- Adding Windows support; the existing engine and executable launcher remain POSIX.

## Architecture

### 1. Preserve the deep facade module

The external command interface remains:

```text
pre-init:   python3 <loaded-flow-skill>/scripts/init.py ...
post-init:  <workspace>/.flow/flow <allowlisted-command> [arguments...]
```

The generated launcher owns workspace discovery. `flowctl.py` owns command
allowlisting, child cwd, and child environment. Existing implementation scripts stay
flat and directly testable, but reference documents use the facade after init.

The facade must continue to preserve arguments, stdin/stdout/stderr, signals, and
exit status through `exec`. It exports both `FLOW_SKILL_DIR` and the legacy
`CLAUDE_SKILL_DIR` to child scripts. Those child variables do not form part of the
parent agent's interface.

### 2. Rooted execution context

The router maintains the following logical context in the conversation:

```text
FlowExecutionContext
  arguments       text supplied after the Flow trigger, or the equivalent user request
  skill_root      absolute root of the loaded Flow skill
  task_root       absolute checkout where the Flow request began
  run_root        absolute checkout that currently owns the run
  facade          <run_root>/.flow/flow
  capabilities    selected harness capability profile
```

These are logical values, not shell variables. A one-shot `export` or `cd` is never
used as cross-call state.

Before bootstrap, `run_root` is the initialized checkout. After
`flow_worktree.py create`, the router replaces `run_root` with the absolute
`result.worktree` path and replaces `facade` with that worktree's absolute launcher.
From that point onward:

- every command call uses `run_root` as its explicit workdir;
- every Flow command invokes `facade` absolutely when the harness does not provide a
  persistent workspace switch;
- every file read, edit, and artifact write is absolute or explicitly rooted at
  `run_root`;
- every raw git, test, build, and forge command receives `run_root` as cwd;
- every subagent prompt includes `Workspace root`, `Skill root`, `Ticket dir`,
  `Reference path`, and `Artifact path` as absolute values.

`EnterWorktree` remains a Claude Code ergonomic optimization. Correctness cannot
depend on it or on any persistent shell cwd.

Before creating or adopting a worktree, a harness that exposes writable roots checks
that the candidate Flow worktree is writable. An existing worktree outside Codex's
writable roots produces a clear reopen/authorization instruction before dispatch
acquires a lease. Flow never bypasses the sandbox through shell tricks.

### 3. Capability adapters

`references/harness.md` becomes a capability matrix instead of a binary
Claude-Code/off-Claude split.

| Capability | Claude Code adapter | Codex adapter | Generic fallback |
|---|---|---|---|
| Skill discovery | Claude plugin and `/flow` | Codex plugin/skill and `$flow:flow` | Installed skill path plus managed `AGENTS.md` |
| Arguments | `$ARGUMENTS` | Text after the skill mention or equivalent request | Adapter-supplied request text |
| Plan enforcement | Native plan mode and `ExitPlanMode` | Native Plan mode when user-selected; otherwise a soft turn boundary | Soft turn boundary |
| Workspace binding | `EnterWorktree`, then verify | Explicit `run_root` on every tool call | Native switch if real; otherwise explicit root |
| Subagent | `Agent`, including supported model routing | Codex collaboration agent; omit unsupported Claude model pins | Independent model call or inline fallback |
| Stage output | Native `Write` | Safe exact file-write primitive rooted at `run_root` | Exact file write or collision-safe fallback |
| Long wait | `Monitor` | Owning-session wait/poll or bounded foreground poll | Bounded poll |
| User input | `AskUserQuestion` | Plain user question and wait | Plain user question and wait |
| Notification | `PushNotification`, then durable fallback | In-thread result plus durable forge fallback | In-thread result plus durable fallback |
| Backgrounding | `/bg` and `claude agents` | Host-owned Codex task/background surface; no in-skill toggle assumed | Host-owned or foreground-only |

Capability loss is explicit. Unsupported model pinning means inherit the current
model; it never emits a malformed Codex spawn. A missing subagent may fall back inline
only where the stage protocol allows loss of isolation. The review loop uses its
existing bounded poll when no monitor primitive exists.

### 4. Native Codex packaging

The Flow plugin carries both manifests over the same `skills/` tree:

- `plugins/flow/.claude-plugin/plugin.json`
- `plugins/flow/.codex-plugin/plugin.json`

A repository Codex marketplace entry at `.agents/plugins/marketplace.json` points to
`./plugins/flow`, allowing the repository to be added as a Codex marketplace without
a personal `~/.codex/skills/flow` symlink. The Codex manifest omits the Claude-only
SessionStart hook.

The server-side version stamp updates the Claude manifest, Claude marketplace entry,
and Codex manifest together. The Codex marketplace entry does not duplicate a version;
the Codex manifest is its version source.

`AGENTS.md` remains useful as durable repository guidance and as the generic-harness
entry point, but it is no longer described as Codex's primary skill loader.

## Plan gate and spill handling

Claude Code continues to use its native hard gate. Codex uses native Plan mode when
the user started the task in that mode; otherwise Flow presents the complete plan,
ends the turn, and waits for explicit approval. The same plan/lane/confidence content
is used on both paths.

The generated `AGENTS.md` block no longer passes `--recover-spill` unconditionally.
Files already dirty when planning begins are user-owned and must never be relocated
automatically. `--recover-spill` remains an explicit recovery tool for a confirmed
agent-created pre-bootstrap spill whose paths did not predate the Flow request. If
provenance is ambiguous or overlaps existing WIP, Flow stops and asks rather than
moving the file.

`--auto` remains gate-free and read-only until its documented bootstrap/defer write.

## Installation and migration

### Launcher ownership

`flow_launcher.py` installs the Flow root that is actually executing it. Ambient
`CLAUDE_SKILL_DIR` cannot override repair. Callers that intentionally want another
installation pass it explicitly through the Python interface.

The launcher reads `.flow/skill_dir` with newline-only trimming so valid whitespace in
a path is preserved. Documentation says that each generated file is atomically
replaced; it does not claim that two independent replacements are one atomic
transaction.

### Existing workspaces and paused worktrees

If an initialized checkout has no launcher metadata, the currently loaded Flow skill
repairs it in place. This covers workspaces and paused worktrees created before the
facade existed. A harness that has neither a loaded Flow installation nor a valid
`.flow/skill_dir` receives an installation error rather than circular advice to run an
initializer it cannot locate.

Reconfigure backup/restore includes prior existence, bytes, and executable mode for
`.flow/flow` and `.flow/skill_dir`, plus `AGENTS.md` when its managed block will be
updated. A failure after launcher installation restores the prior coherent state.

The Flow repository's granular `.gitignore` explicitly ignores
`**/.flow/flow` and `**/.flow/skill_dir`; newly initialized repositories remain covered
by init's broad `.flow/*` block.

### Managed `AGENTS.md`

The managed block is replaced between `<!-- flow:begin -->` and
`<!-- flow:end -->`, preserving all user text outside it. An existing marker means the
repository previously opted in, so ordinary `--reconfigure` upgrades that block even
when `--agents-md` is not repeated. A missing block remains untouched unless the flag
is supplied. Missing, duplicated, or reversed marker pairs fail clearly without
rewriting the file.

The block instructs Codex to prefer the installed `$flow:flow` skill and gives generic
harnesses an explicit installed-skill-path contract. It defines request-text mapping,
the approval boundary, rooted worktree execution, and the absence of persistent cwd.

## Seam validation

Facade invocations are executable recipes and therefore validate strictly:

- a missing or misspelled subcommand is an error;
- a flag valid only for a different subcommand is an error;
- an argument value cannot be mistaken for the subcommand;
- quoted absolute facade paths are recognized;
- invalid facade command tokens are reported;
- supported direct pre-init/repair calls remain narrowly allowlisted.

The generated `AGENTS.md` stanza is fed through the same command parser as live
reference documents. The validator's claims are narrowed where arbitrary absolute or
relative script prose cannot be recognized reliably; it must not claim to detect forms
it does not scan.

## Error handling

- **Unknown facade command or invalid arguments:** exit 2 with the allowlist or mapped
  script's argparse error.
- **Missing/stale installation:** repair from the loaded/executing skill when possible;
  otherwise stop with an explicit installation instruction.
- **Unwritable worktree:** stop before dispatcher init and preserve the seeded state for
  recovery or remove the unleased bootstrap safely.
- **Lost worktree binding:** re-read the immutable `run_root`, verify branch and
  workspace ownership, and refuse to fall back to the original checkout.
- **Unavailable subagent/model pin:** apply the declared capability degradation; never
  invent a host tool parameter.
- **Artifact write failure:** do not call `advance`; retry the exact write, then advance
  with the verified existing path.
- **Reconfigure failure:** restore workspace configuration, launcher metadata/mode, and
  any managed guidance changed by the attempt.
- **Notification failure:** remain best-effort; pipeline state stays authoritative.

## Verification strategy

### Launcher and facade

- Invoke a worktree's absolute launcher from a different cwd and prove the child sees
  the owning worktree.
- Reset cwd between every simulated Codex command and complete a dispatcher sequence in
  the seeded worktree.
- Run launcher A with ambient variables pointing at valid installation B and prove A
  remains authoritative.
- Verify exact child arguments, environment aliases, stdio, signals, and exit status.
- Verify paths containing spaces and trailing whitespace.

### Harness behavior

- Give a Codex stage subagent an inherited main-checkout cwd and prove its explicit
  absolute root confines every read/write to the run worktree.
- Verify Codex native-Plan and soft-gate paths both stop before bootstrap until the
  matching approval.
- Verify unsupported model routing inherits without a malformed tool call.
- Exercise bounded CI polling without `Monitor`.
- Verify a candidate worktree outside writable roots refuses before lease acquisition.

### Migration and safety

- Upgrade the previous managed `AGENTS.md` stanza in place while preserving surrounding
  user content, without repeating `--agents-md`.
- Reject partial and duplicated marker pairs without mutation.
- Repair a pre-facade paused worktree containing state, workspace config, and
  `memory-root` but no launcher.
- Force failure after launcher replacement and prove reconfigure restores prior bytes
  and modes.
- Preserve pre-existing dirty planned files; recover only a separately confirmed spill.
- Confirm the repository's granular ignore rules cover both generated launcher files.

### Static and full gates

- Strict facade-subcommand and subcommand-specific flag tests.
- Semantic seam validation of the generated `AGENTS.md` block.
- Codex and Claude manifest schema tests plus version-lockstep tests.
- `git diff --check`, Ruff, formatting, ty, the prose/CLI seam checker, the full engine
  suite, and hook tests.

## Acceptance criteria

1. A Codex run can plan, bootstrap, implement, verify, commit, and create/review a PR
   without any operation landing in the original checkout after worktree creation.
2. A Claude Code run retains native plan gating, model routing, worktree switching,
   monitoring, notifications, and background behavior.
3. The same stage registry and deterministic dispatcher state drive both harnesses.
4. Existing initialized workspaces and paused worktrees self-repair from a loaded Flow
   installation without losing run state.
5. Existing managed `AGENTS.md` blocks upgrade safely and idempotently.
6. Pre-existing user WIP is never moved merely because a non-Claude harness is active.
7. Codex can install/discover Flow through its native plugin manifest and marketplace.
8. Invalid documented facade commands fail the seam gate.
9. All existing and new verification gates pass.

## Rejected alternatives

### Prose-only patch

Changing `cd` wording and a few fallbacks would be smaller, but harness knowledge would
remain scattered and the same wrong-root class could recur in subagent, artifact, wait,
or revision paths.

### Python `FlowSession` or MCP turn protocol

A versioned start/respond protocol would create a deeper interface and let the engine
capture model output itself. It would also move substantial judgment orchestration out
of the skill, introduce protocol/versioning work, and change Flow's current thesis more
than the observed failures require. Reconsider it if the rooted adapter contract still
produces repeated harness drift or a third first-class harness is added.

### Universal auto-detecting adapter

Tool and environment probing cannot establish semantic guarantees such as hard plan
enforcement, persistent cwd, writable roots, or model-pin support. Explicit Claude Code
and Codex mappings are safer; future harnesses implement the documented capability
contract deliberately.
