# Harness adapters

Claude Code and Codex are the two hosts for the same Flow engine and public grammar. There is no
third adapter: an unrecognized `FLOW_HARNESS` fails instead of degrading. Native agents are
bounded collaborators, not a second execution system: there are no provider routes, isolated
exact-SHA clones, typed agent envelopes, route receipts, or model-identity gates.

## Vocabulary and rooted execution

The **driver** is the main agent/session that talks to the human and continues the workflow. The
**human** approves plans and supplies decisions. The **host** is the Claude Code or Codex adapter.
Keep `owner` for actual resource ownership such as leases, repositories, or content.

The entry binding (the seven logical values), the two-file `skill_root` precondition, the
launcher bootstrap recipe, and the rooted-call rules are SKILL.md §Entry contract — one
statement, read at every entry. This file adds only what differs per host:

Claude Code's native worktree switch is a convenience. Codex uses explicit workdirs. Neither
replaces the absolute binding. If the worktree is outside a host's writable roots, the driver asks
the human for authorization instead of escaping the sandbox.

## Capability matrix

| Capability | Claude Code | Codex |
|---|---|---|
| Trigger | `/flow` | `$flow:flow` |
| Plan gate | native plan mode | native Plan mode when active, else turn boundary |
| Workspace | native switch plus absolute binding | explicit absolute binding |
| Agent | native collaboration agent | native collaboration agent |
| Write | native file writer | rooted safe edit/write |
| Wait | native driver-session wait | native driver-session wait |
| Input | native question surface | plain question and wait |
| Notification | native notification plus durable run evidence | in-thread plus durable run evidence |
| Background | human backgrounds driver conversation | human backgrounds driver task |

Do not infer the harness from ambient environment. The adapter supplies it. Flow normalizes the
ambient `claude-code` name at the boundary where configuration uses `claude_code`.

## Discovery and runtime

Both plugin manifests expose the same `skills/` tree. Claude Code and Codex both discover Flow
natively; there is no managed repository-guidance fallback.

The launcher bootstrap (SKILL.md §Entry contract) installs or migrates
`.flow/runtime/{flow,skill-root,memory-root,layout-version}`. It never searches arbitrary plugin
caches. The generated facade reads its sibling `skill-root`, enters its own workspace, and
executes only an allowlisted internal command. It supplies compatibility environment variables to
child processes; those variables are engine details, not driver state.

Fresh setup calls the loaded setup script directly because no facade exists.

### Engine resolution: which code is actually running

Three distinct locations can hold flow's engine, and confusing them is the classic fresh-session
mistake:

1. **The source checkout** — a git clone of the flow repository. Editing it changes nothing about
   any workspace until the change is installed.
2. **The host's versioned plugin cache** — where the harness materializes an installed plugin
   (e.g. `plugins/cache/<marketplace>/<plugin>/<version>/skills/flow`). This is the directory a
   loaded skill executes from, and it can lag what the marketplace now serves.
3. **The workspace pin** — `.flow/runtime/skill-root`, written at install time. This is the sealed
   contract for the workspace: every facade call resolves the engine through it, so a run keeps
   its engine even while caches and checkouts move (see SKILL.md's re-bind rule — when the pin
   and the loaded copy disagree, the pin wins).

Two facts decide what gets pinned. First, the harness selector: when `FLOW_HARNESS` is unset,
`_harness.flow_harness` defaults it to `claude-code`; the value is closed-validated
(`codex`, `claude-code`), and unknown names fail instead of guessing. Second, the
cache-stabilization fork: at install, `flow_launcher.stabilize_skill_dir` prefers the harness's
stable marketplace source over a versioned cache path — under `claude-code` it resolves the
marketplace manifest under `plugins/marketplaces/`, and under `codex` it reads the `.agents`
marketplace roots (`CODEX_HOME` respected). The resolved directory is what `install` writes into
the pin — so the ambient harness at install time determines which
engine every later run resolves.

## Planning gate and assessor

Fresh targets remain read-only through planning. The driver reads the ticket and repository, asks
the human every factual/access/permission question, and writes one complete Markdown plan. The
driver then launches one fresh independent host-native assessor with the plan, base SHA, relevant
repository context, and the adversarial confidence contract from `delivery-plan.md`.

The pass budget, replacement rule, and confidence threshold are `delivery-plan.md`'s contract —
stated there and summarized in SKILL.md's gate, nowhere else. The driver rechecks the default
branch, presents the exact plan and confidence evidence, and waits for explicit human approval.
Confidence cannot replace approval.

No worktree, branch, run, ticket mutation, or approval artifact exists before the gate. A fresh
unattended invocation stops without mutation. The approved plan and base SHA pass directly to
`worktree create`; only its `stages/plan.out` becomes durable planning state.

## Stage and maintenance agents

Every independent stage or maintenance agent receives the exact rooted field block in
`references/delivery-loop.md` §Independent agent (also inlined in SKILL.md's do-loop).
The prompt says inherited cwd is non-authoritative and every facade call applies the call-local
`FLOW_HARNESS` selector to the absolute facade. The agent writes only within the authorized
worktree and returns its report at the declared artifact path. Durable run, tracker, lease,
forge, and ship-event evidence—not a claim about provider identity—proves workflow state.

Discovery agents are read-only. Write-capable agents operate only after the plan gate and within
their declared stage/file boundary. Before a read-only fan-out, the driver may use the `worker-pool`
snapshot and guard commands to prove that collaborators did not mutate Git state. Flow does not
launch detached host CLIs or pretend a Python subprocess can invoke a host-native agent tool.

Detached is the operative word. A bundled stage handler may run an external reviewer CLI as one
bounded foreground call rooted in `run_root`, reading its report from a file the call wrote before
it returned. That leaves no job directory, transcript, or session for Flow to inspect, and no
continuation for Flow to own. The handler, not the CLI, produces the stage artifact, and no route
receipt, capsule, or model attestation is created or required.

Driver sessions create, wait for, and cancel native agents through host collaboration tools.
They use the `worker-pool` facade for enforceable capacity and durable recovery, reserving one host
slot for the driver. Handles belong to the driver session and are disposable; durable evidence
survives it. Flow never scans host job state, stops unrelated sessions, or schedules self-teardown.

The ordinary ticket pipeline treats Claude Code and Codex as peers.

## Waits, questions, and backgrounding

Waits remain in the driver session. A child agent never owns continuation after it returns.
Attended human-only questions use the host input surface. Fresh unattended work stops before the
plan gate; already-approved unattended delivery records a later question and defers or blocks
instead of waiting for an absent human. Notifications are best-effort; durable evidence is
authoritative.

Backgrounding is a host operation on the driver conversation. It does not create a second Flow
daemon, lease authority, or scheduler.
