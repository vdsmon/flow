---
name: flow
description: State-aware ticket-to-PR delivery and workspace operations. Use FLOW for a cockpit, a ticket or PR target, or the ticket, memory, measure, workspace, maintain namespaces.
allowed-tools: Bash(.flow/runtime/flow:*), Bash(*/.flow/runtime/flow:*), Bash(python3:*), Bash(git:*), Bash(bd:*), Bash(jq:*), Bash(gh:*), Read, Write, Edit, Agent, Skill, AskUserQuestion, PushNotification, EnterWorktree
---

# Flow

## Native roles

The driver is the main agent/session and the single human cockpit; the dispatcher
remains the only stage-state authority. The driver owns planning. Implementation,
review, assessment, and optional authorship roles use fresh host-native agents when
their stage calls for independence. They work in the one authoritative ticket
worktree. Flow may offer a simple model hint that the active host can accept or
ignore; provider and model identity are not delivery gates.

Use **human** for the user or maintainer at an approval gate and **host** for the
Claude Code, Codex, or generic adapter. Reserve **owner** for actual resource
ownership such as a lease, repository, branch, or content boundary.

Flow is one state-aware path from a tracker ticket to a reviewable pull request.
The user owns intent, plan approval, and PR review. Flow owns the isolated worktree
and the implementation, review, verification, commit, and PR stages between them.

`FLOW` is the logical invocation used throughout this skill. Render it as:

- `/flow` in Claude Code;
- `$flow:flow` in Codex;
- the installed skill's equivalent invocation in another harness.

Never expose a host's rendering in reusable state, tracker comments, memory, or
generated help. Store and display logical `FLOW` there, substituting only at the
conversation boundary.

## Entry contract

Before routing, read `references/harness.md`. Bind these absolute logical values in
conversation state, not in a shell that may disappear between calls:

```text
arguments      request text after the host skill trigger
skill_root     directory containing this SKILL.md
task_root      checkout in which the request began
run_root       checkout that currently owns the run
facade         <run_root>/.flow/runtime/flow
harness        claude-code | codex | generic
capabilities   the host operations available to this invocation
```

For example, a Codex cache binding ends at the skill directory itself:

```text
<codex-home>/plugins/cache/vdsmon-flow/flow/<version>/skills/flow
```

Before invoking the launcher, verify that both `<skill_root>/SKILL.md` and
`<skill_root>/scripts/flow_launcher.py` exist. If either is absent, the binding is
wrong; stop and correct `skill_root` instead of guessing another scripts path.

Every facade call is absolute and uses `run_root` as its explicit workdir. On Codex,
prefix that same call with `FLOW_HARNESS=codex`; on Claude Code use
`FLOW_HARNESS=claude-code`; generic adapters use `FLOW_HARNESS=generic`. Do not rely
on a prior `export` or `cd`. After creating or adopting a worktree, immediately
replace both `run_root` and `facade` with the returned absolute paths. Never fall
back to `task_root` after that binding.

Before the first workspace-dependent operation, install or migrate the runtime from
the loaded skill with one call rooted at `task_root`:

```bash
FLOW_HARNESS="<codex|claude-code|generic>" \
  python3 "<skill_root>/scripts/flow_launcher.py" \
  --workspace-root "<absolute task_root>"
```

Skip that call only when routing fresh `workspace setup` and no
`.flow/workspace.toml` exists yet. On success, bind `run_root=task_root` and
`facade=<task_root>/.flow/runtime/flow`. The launcher migrates an initialized v1
workspace to runtime layout v2 before any other workspace command. Migration is
journaled and forward-resumable. A live base or revision lease, corrupt evidence,
or two non-empty memory stores is a hard stop; preserve both stores and report the
conflict. A normal upgrade needs no new workspace setup.

After a successful launcher call on an initialized workspace, read
`<run_root>/.flow/runtime/skill-root`. When it names a different directory than the
bound `skill_root`, re-bind `skill_root` to that pinned path and say so in one line.
The installed skill is the sealed contract for this workspace; the invocation copy
may be a stale host plugin cache, and references read from it silently drop newer
contract obligations.

## Public router

`public-commands.toml` is authoritative for command paths, arguments, options,
effects, workspace requirements, help, harness parity, and reference ownership.
Reject every unregistered path or option. Do not redirect, reinterpret, or suggest a
removed spelling. No option may be silently ignored.

Route the safely tokenized request through the loaded registry before any workspace
operation:

```bash
python3 "<skill_root>/scripts/public_commands_cli.py" route \
  [--workspace-root "<absolute task_root>"] -- <request tokens>
```

Pass tokens as distinct process arguments through the host command API; never build a
shell string from free text. The JSON result supplies `command_id`, `effect`,
`workspace`, `reference`, parsed positionals, and option names. Pass `--workspace-root`
for an initialized workspace so the router derives the Jira or beads key grammar from
`workspace.toml`; do not invent a regex in prose. For setup/help outside a workspace,
static routes still resolve, and explicit `ticket:<key>` remains available.

<!-- flow:public-router:begin -->
Interpret the invocation through `public-commands.toml`.
Static namespaces win over target parsing.
Static roots: `ticket | memory | measure | workspace | maintain | help`.
Bare `FLOW` is the read-only cockpit; a recognized target enters the lifecycle reducer.
Unknown tokens stop. Never reinterpret removed commands as ticket keys.
<!-- flow:public-router:end -->

The complete public grammar is generated from the registry:

<!-- flow:public-grammar:begin -->
```text
FLOW
FLOW <target> [<target> ...] [--unattended] [--together] [--verify express|light|full] [--e2e <recipe>] [--request <additional-intent>]
FLOW ticket create [--request <problem>]
FLOW ticket group (<ticket> ... | --mine) [--state open]
FLOW ticket split <ticket>
FLOW memory search [<query>] [--ticket <key>]... [--label <facet:value>] [--digest] [--semantic] [--threshold <float>] [--branch <branch>] [--limit <n>]
FLOW memory prune
FLOW memory rebuild [--full]
FLOW measure <throughput|lead-time|friction|reverts|experiment|trend|memory-health|recall-quality|fix-efficacy> [--since <date>] [--until <date>] [--json]
FLOW measure throughput --checkpoint <personal|work> [--manifest <path>]
FLOW workspace setup [--guidance]
FLOW workspace inspect [<target>] [--json]
FLOW workspace repair [<target>]
FLOW workspace sync
FLOW maintain backlog status [--preview]
FLOW maintain backlog drain [--dry-run]
FLOW maintain evolution audit
FLOW maintain evolution propose
FLOW maintain evolution epic
FLOW maintain evolution expand <epic>
FLOW maintain evolution drain [--dry-run] [--include-proposals]
FLOW maintain worktrees clean [--dry-run]
FLOW help [ticket|memory|measure|workspace|maintain]
```
<!-- flow:public-grammar:end -->

Targets are configured tracker keys, Jira `/browse/<key>` URLs whose extracted key
matches the configured tracker grammar, `ticket:<key>` for a key that collides with a
static root, `pr:<number>`, or forge PR URLs. Resolve PR forms through the forge seam,
then enter the same ticket lifecycle. A static namespace always wins over target
parsing.

Use the command's registry effect before acting:

- `read`: perform no durable mutation;
- `confirm`: present the exact proposed write and obtain confirmation before it;
- `write`: perform the explicitly requested, bounded write without adding another
  approval gate.

Load only the routed public reference:

| Route | Reference |
|---|---|
| bare invocation or target | `references/command-target.md` |
| `ticket` | `references/command-ticket.md` |
| `memory` | `references/command-memory.md` |
| `measure` | `references/command-measure.md` |
| `workspace` | `references/command-workspace.md` |
| `maintain` | `references/command-maintain.md` |

## Bare cockpit

Bare `FLOW` is read-only. Build one compact view from durable evidence, in this
order: active or stuck runs, deferred decisions, pending tracker mutations,
actionable PR feedback, then the most useful next invocations. Use
`references/command-target.md`; do not start, repair, or drain anything from the
cockpit.

## Target lifecycle

Join tracker, run, lease, snapshot, revision, and forge evidence without mutation,
then feed normalized evidence into the deterministic lifecycle reducer. It returns
exactly one action:

```text
start | answer | resume | running | repair | revise | show | conflict
```

Obey its result rather than inferring a second route:

1. Unknown target: stop with an error.
2. Fresh live ticket: plan, cross the one approval gate, then deliver.
3. Deferred or blocked: show the stored question. `--request` records the answer,
   reopens the ticket, re-probes, and continues.
4. Healthy incomplete run: continue it. Once approved work is active, reject
   `--request` because it changes owned scope mid-run.
5. Live foreign lease: show the holder and stop. Lease takeover is available only
   through `FLOW workspace repair`.
6. Failed, stale, drifted, or corrupt evidence: diagnose and offer only applicable
   repairs. Confirm every write, re-probe after it, and continue in the same
   invocation when healthy.
7. Open PR with actionable feedback, or an explicit `--request`: update that same PR
   through a revision sub-run.
8. Open PR without actionable feedback: show its ready state without mutation.
9. Merged or closed delivery: show the durable receipt.
10. Contradictory evidence: preserve it and report `conflict`.

Multiple targets passed with `--together` must all be fresh and groupable. Without
that option, attended mode asks whether to deliver sequentially or together;
unattended mode stops because that choice cannot be inferred. `--unattended`
conflicts with `--verify`. Full behavior is in `references/command-target.md`.

## The one approval gate

For a fresh target, the driver plans read-only with the human. Fetch the ticket,
inspect default-branch code, search relevant memory, settle factual questions, and
write one complete plan with a verification lane and E2E recipe. The driver alone
asks human questions or requests access and permission.

Every plan receives one independent adversarial assessment. Continue the same
assessor with the complete revised plan for at most three completed passes in one
autonomous round. Score repository grounding/design correctness/scope completeness/
verification quality/operational feasibility at weights 25/25/20/20/10. The gate
requires an unrounded weighted score of at least 90.0 and zero blockers. One
disclosed replacement assessor is allowed if the original context is lost; it does
not reset the pass count. Read `references/delivery-plan.md` for the full contract.

- Claude Code uses native plan mode and its exit boundary.
- Codex uses native Plan mode when active; otherwise present the complete plan, end
  the turn, and wait for explicit approval.
- A generic adapter uses the same soft turn boundary.

Before approval, re-fetch the default branch and restart bounded assessment when
relevant paths moved. Present the exact plan, base SHA, confidence and category
scores, pass/replacement facts, resolved findings, and residual risks. No worktree,
repository edit, run, or ticket mutation exists before explicit human approval. A
fresh unattended invocation stops before mutation; confidence never substitutes for
human approval.

## Delivery loop

After approval, persist the plan, seed or adopt the ticket worktree, bind the
absolute rooted context, and drive the dispatcher until it returns done or a durable
stop condition. The dispatcher owns run state, snapshots, stage transitions, and
leases; this skill executes each returned handler descriptor.

The hot path is:

1. Validate the workspace.
2. Initialize or reacquire the ticket run and retain its `session_nonce`.
3. Request the first descriptor.
4. For each descriptor, run its pre-hook, execute exactly its declared handler,
   capture the artifact, then atomically advance and receive the next descriptor.
   Log any workaround as best-effort friction (`references/delivery-loop.md`).
5. Release the lease on every post-acquisition exit path.
6. Surface the durable result and PR URL.

Resource pressure changes topology, never the loop. Under a host usage-guard warning
against spawning agents, run an agent-handler stage in the driver instead
(`references/delivery-loop.md`), still through its descriptor, artifact, and advance.
Never ship around the dispatcher with raw git or forge calls: the PR may land, but
the run strands with every stage pending — no commit gate, no review loop, no
reflect. The run is done when the dispatcher returns done, not when a PR exists.

Parse the returned branches structurally: `{"done": true}` is complete, while
`{"done": false, "blocked_by": "<stage>", "reason": "<text>"}` is a durable
stop. Otherwise expect a handler descriptor with `stage`, `handler_type`, `head_sha`,
`ticket_dir`, `output_path`, and `roles`. If `descriptor.roles` includes `"records_diff_baseline"`,
capture the owned-file baseline before the handler. The driver follows
`references/delivery-plan.md`.

Every independent stage-agent prompt includes these exact rooted fields. The agent
applies the call-local `FLOW_HARNESS` selector to the bound absolute `facade`:

```text
Workspace root: <absolute run_root>
Skill root: <absolute skill_root>
Facade: <absolute facade>
Harness: <claude-code|codex|generic>
Ticket dir: <absolute ticket_dir>
Reference path: <absolute reference, or none>
Artifact path: <absolute output_path>
```

Handlers may be inline, independent native agents, installed skills, or no-ops. Every
agent receives absolute workspace, skill, ticket, reference, and artifact paths plus
the harness identity.
Read `references/delivery-loop.md` before starting or continuing a run.

## Internal delivery references

- Planning and approval: `references/delivery-plan.md`
- Dispatcher execution: `references/delivery-loop.md`
- Same-PR feedback cycles: `references/delivery-revision.md`
- Diagnosis and repair: `references/delivery-repair.md`
- Host capability mapping: `references/harness.md`
- Background ownership: `references/background-pipeline.md`
- Stage protocols: `references/stage-*.md`

Backgrounding is a host capability chosen by the human. Flow does not spawn a detached CLI,
scan a host job directory, stop host jobs, or tear down its own session. The owning
driver retains continuation responsibility even when the human backgrounds it.
