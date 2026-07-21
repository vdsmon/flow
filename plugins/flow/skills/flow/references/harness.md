# Harness adapters

Claude Code and Codex are first-class hosts for the same Flow engine and public grammar. A generic
adapter states capability loss instead of presenting weaker behavior as equivalent. Native agents
are bounded collaborators, not a second execution system: there are no provider routes, isolated
exact-SHA clones, typed agent envelopes, route receipts, or model-identity gates.

## Vocabulary and rooted execution

The **driver** is the main agent/session that talks to the human and continues the workflow. The
**human** approves plans and supplies decisions. The **host** is the Claude Code, Codex, or generic
adapter. Keep `owner` for actual resource ownership such as leases, repositories, or content.

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

A Codex cache path is bound through the final skill directory, for example
`<codex-home>/plugins/cache/vdsmon-flow/flow/<version>/skills/flow`, not the plugin
package above it. Before launcher execution, require both `<skill_root>/SKILL.md` and
`<skill_root>/scripts/flow_launcher.py`; a missing file means the binding is wrong.

Shell state does not carry across calls. Every facade invocation uses an explicit `run_root`
workdir and a call-local `FLOW_HARNESS=<harness>`. After worktree creation or adoption, replace
both `run_root` and `facade` immediately and never fall back to `task_root`. Root every read, edit,
git operation, artifact, and agent prompt there.

Claude Code's native worktree switch is a convenience. Codex uses explicit workdirs. Neither
replaces the absolute binding. If the worktree is outside a host's writable roots, the driver asks
the human for authorization instead of escaping the sandbox.

## Capability matrix

| Capability | Claude Code | Codex | Generic fallback |
|---|---|---|---|
| Trigger | `/flow` | `$flow:flow` | installed skill equivalent |
| Plan gate | native plan mode | native Plan mode when active, else turn boundary | turn boundary |
| Workspace | native switch plus absolute binding | explicit absolute binding | real native switch or explicit binding |
| Agent | native collaboration agent | native collaboration agent | independent call or disclosed inline fallback |
| Write | native file writer | rooted safe edit/write | exact writer or collision-safe fallback |
| Wait | native driver-session wait | native driver-session wait | bounded foreground poll |
| Input | native question surface | plain question and wait | plain question and wait |
| Notification | native notification plus durable run evidence | in-thread plus durable run evidence | in-thread plus durable run evidence |
| Background | human backgrounds driver conversation | human backgrounds driver task | host-owned or foreground |

Do not infer the harness from ambient environment. The adapter supplies it. Flow normalizes the
ambient `claude-code` name at the boundary where configuration uses `claude_code`.

## Discovery and runtime

Both plugin manifests expose the same `skills/` tree. Codex and Claude Code use native plugin
discovery. Managed `AGENTS.md` guidance is optional and is the generic fallback, not another
installation locator.

Before using an initialized workspace facade, invoke the loaded launcher directly:

```bash
FLOW_HARNESS="<codex|claude-code|generic>" \
  python3 "<skill_root>/scripts/flow_launcher.py" \
  --workspace-root "<absolute task_root>"
```

This installs or migrates `.flow/runtime/{flow,skill-root,memory-root,layout-version}`. It never
searches arbitrary plugin caches. The generated facade reads its sibling `skill-root`, enters its
own workspace, and executes only an allowlisted internal command. It supplies compatibility
environment variables to child processes; those variables are engine details, not driver state.

Fresh setup calls the loaded setup script directly because no facade exists. Existing workspace
guidance uses that script's guidance-only mode; configuration is not rerun.

## Planning gate and assessor

Fresh targets remain read-only through planning. The driver reads the ticket and repository, asks
the human every factual/access/permission question, and writes one complete Markdown plan. The
driver then launches one fresh independent host-native assessor with the plan, base SHA, relevant
repository context, and the adversarial confidence contract from `delivery-plan.md`.

The same assessor receives every complete revision for at most three completed passes in the
round. The gate requires unrounded weighted confidence of at least 90.0 and zero blockers. One
disclosed replacement is allowed if the original assessor is lost; replacement does not reset the
pass count. The driver rechecks the default branch, presents the exact plan and confidence evidence,
and waits for explicit human approval. Confidence cannot replace approval.

No worktree, branch, run, ticket mutation, or approval artifact exists before the gate. A fresh
unattended invocation stops without mutation. The approved plan and base SHA pass directly to
`worktree create`; only its `stages/plan.out` becomes durable planning state.

## Stage and maintenance agents

Every independent stage or maintenance agent receives:

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

The prompt says inherited cwd is non-authoritative and every facade call applies the call-local
`FLOW_HARNESS` selector to the absolute facade. The agent writes only within the authorized
worktree and returns its report at the declared artifact path. Durable run, tracker, lease, fleet,
forge, and ship-event evidence—not a claim about provider identity—proves workflow state.

Discovery agents are read-only. Write-capable agents operate only after the plan gate and within
their declared stage/file boundary. Before a read-only fan-out, the driver may use the `worker-pool`
snapshot and guard commands to prove that collaborators did not mutate Git state. Flow does not
launch detached host CLIs or pretend a Python subprocess can invoke a host-native agent tool.

Maintenance adapters create, wait for, and cancel native agents through host collaboration tools.
They use the `worker-pool` facade for enforceable capacity and durable recovery, reserving one host
slot for the driver. Handles belong to the driver session and are disposable; durable evidence
survives it. Flow never scans host job state, stops unrelated sessions, or schedules self-teardown.

Flow's maintainer-only `evolve` and `queue` verbs require Claude Code where their command reference
says so. This host restriction does not change the ordinary ticket pipeline, where Claude Code and
Codex remain peers.

## Waits, questions, and backgrounding

Waits remain in the driver session. A child agent never owns continuation after it returns.
Attended human-only questions use the host input surface. Fresh unattended work stops before the
plan gate; already-approved unattended delivery records a later question and defers or blocks
instead of waiting for an absent human. Notifications are best-effort; durable evidence is
authoritative.

Backgrounding is a host operation on the driver conversation. It does not create a second Flow
daemon, lease authority, or scheduler.
