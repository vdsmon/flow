# Harness adapters

## Cross-harness cognitive workers

The public route names remain `claude_code` and `codex`; CLI is a receipt-level
transport detail. A routed read-only worker runs in a standalone exact-SHA clone with
closed immutable inputs, exact model and effort flags, typed output, process-group
terminal proof, Git pre/post guards, and disposal evidence. The outer owner harness can
differ from the worker harness. Real smoke evidence must name the actual outer `claude`
or `codex` executable and its version; changing `FLOW_HARNESS` alone proves nothing.

Flow's engine and public grammar are shared. Claude Code and Codex are first-class
adapters; a generic adapter must state capability loss instead of pretending a
weaker operation is equivalent.

## Rooted execution

At entry, bind these absolute logical values in conversation state:

```text
arguments      request text after the host trigger
skill_root     directory containing the loaded SKILL.md
task_root      checkout where the request started
run_root       checkout that currently owns the run
facade         <run_root>/.flow/runtime/flow
harness        claude-code | codex | generic
capabilities   available native operations
```

Shell state does not carry across calls. Every facade invocation uses an explicit
`run_root` workdir and the call-local selector `FLOW_HARNESS=<harness>`. After
worktree creation or adoption, replace both `run_root` and `facade` immediately and
never fall back to `task_root`. Root every read, edit, git operation, artifact, and
worker prompt there.

Claude Code's native worktree switch is a convenience. Codex uses explicit workdirs.
Neither replaces the absolute binding. If the worktree is outside a host's writable
roots, stop for authorization rather than escaping the sandbox.

## Capability matrix

| Capability | Claude Code | Codex | Generic fallback |
|---|---|---|---|
| Trigger | `/flow` | `$flow:flow` | installed skill equivalent |
| Plan gate | native plan mode | native Plan mode when active, else turn boundary | turn boundary |
| Workspace | native switch plus absolute binding | explicit absolute binding | native switch if real, else explicit binding |
| Worker | native collaboration agent, plus the exact read-only CLI planner route | native collaboration agent, plus the exact read-only CLI planner route | independent call or documented inline behavior |
| Exact write | native file writer | rooted safe edit/write | exact writer or collision-safe fallback |
| Wait | native owning-session wait | native owning-session wait | bounded foreground poll |
| Input | native question surface | plain question and wait | plain question and wait |
| Notification | native notification plus durable receipt | in-thread plus durable receipt | in-thread plus durable receipt |
| Background | user backgrounds owner conversation | user backgrounds owner task | host-owned or foreground |

Do not infer the harness from ambient environment. The adapter supplies it. Public
route configuration uses `claude_code` and `codex`; Flow normalizes the ambient
`claude-code` adapter name at the boundary. The configured, built-in, or overridden
planner may activate through an exact structured CLI receipt on either owner harness.
Every non-planner profile remains a desired shadow route in this increment, on both
owner harnesses, even when a native response matches its model and effort. Existing
post-plan handlers continue to inherit the active owner model or run inline.

## Discovery and runtime

Both plugin manifests expose the same `skills/` tree. Codex and Claude Code use
native plugin discovery. Managed `AGENTS.md` guidance is optional and is the generic
fallback, not a second installation locator.

Before an initialized workspace facade is used, invoke the loaded launcher directly:

```bash
FLOW_HARNESS="<codex|claude-code|generic>" \
  python3 "<skill_root>/scripts/flow_launcher.py" \
  --workspace-root "<absolute task_root>"
```

This installs or migrates `.flow/runtime/{flow,skill-root,memory-root,layout-version}`.
It never searches arbitrary plugin caches. The generated facade reads its sibling
`skill-root`, enters its owning workspace, and execs only an allowlisted internal
command. It exports `FLOW_SKILL_DIR` and the legacy child variable
`CLAUDE_SKILL_DIR`; those are engine implementation details, not orchestration state.

Fresh setup calls the loaded setup script directly because no facade exists. Existing
workspace guidance uses that script's guidance-only mode; configuration is not rerun.

## Gate and workers

Fresh targets remain read-only through the complete plan. Claude Code exits native
plan mode; Codex either exits native Plan mode or ends the turn at the soft boundary.
Approval is the only attended delivery gate. No worktree or repository edit exists
before it.

The ordinary planner route uses `planning-attempt`, `planner-worker`, and `plan-review`
through the facade. The worker process has a read-only sandbox and a closed canonical
schema. Each physical launch has its own 10-minute soft deadline and 40-minute
hard deadline. One fresh retry gets a new budget only after cancellation and output
closure are acknowledged, and metrics keep both attempts separate. Its thread id stays
only in the live owner conversation. The attempt bundle may retain complete plan
versions and feedback, but never a resumable worker receipt or a Flow run. The owner
drains the review surface before requesting its host-native gate, then passes the exact
pre-gate digest back to approval and supplies the receipt to `worktree create
--approval-receipt`. A configured route failure stops visibly. No planner fallback runs.

Unattended delivery has no live gate. It proceeds only under the documented
independent-confidence and safety policy; otherwise it records a durable question
and exits.

Every stage or maintenance worker receives:

```text
Workspace root: <absolute run_root>
Skill root: <absolute skill_root>
Facade: <absolute facade>
Harness: <claude-code|codex|generic>
Ticket and stage: <ticket> / <stage>
Ticket dir: <absolute ticket_dir>
Reference path: <absolute reference, or none>
Artifact path: <absolute output_path>
```

The prompt states that inherited cwd is non-authoritative and every facade call applies
the call-local `FLOW_HARNESS` selector to the absolute bound `facade`. Capture the full returned report at the exact
artifact path before advancing.

Agent-written prose never proves which model executed. The route receipt records the
desired route, effective route when proven, activation, source, transport/adapter
identity, canonical provider model when exposed, and prompt/schema hashes. Tool and
inline stages record `none` or the owner-reported identity; missing owner identity is
`unknown`, never an inferred alias.

Maintenance adapters perform launch, wait, and cancel with native collaboration
primitives. They call the `worker-pool` facade for the enforceable capacity,
pre/post-git guard, and durable-recovery reducers; a Python subprocess never pretends
it can invoke a host-native agent tool. One slot remains reserved for the owner. Flow
never starts a detached host CLI, scans host job state, stops host sessions, or
schedules self-teardown.

## Waits, questions, and receipts

Waits remain in the owner session. A child never owns continuation after it returns.
Attended user-only questions use the host input surface. Unattended work records the
question and defers or blocks instead of waiting for an absent user. Notifications
are best-effort; run, tracker, forge, and ship-event evidence is authoritative.
