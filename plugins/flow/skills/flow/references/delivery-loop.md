# Dispatcher delivery loop

The dispatcher owns state, lease refresh, snapshot validation, stage transitions, and
the canonical descriptor. The owner conversation executes handlers and persists their
artifacts. All commands use the absolute runtime facade and `run_root` workdir.

## Acquire

1. Validate:

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" validate --workspace-root .
   ```

2. Initialize the base run:

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" dispatch init --workspace-root . --ticket "<ticket>"
   ```

3. Capture `run_id` and `session_nonce`. Carry the nonce verbatim on every later
   `next`, `advance`, and `release`. It distinguishes the owner from a second session
   that merely knows the run id.

Do not clear leases automatically. A live holder, stale holder, corrupt lock,
unrecoverable state, or workspace violation returns to the target lifecycle as
`running` or `repair`. If acquisition failed, do not release because this owner never
held the lease.

## Iterate

Request the first descriptor:

```bash
FLOW_HARNESS="<harness>" "<facade>" dispatch next \
  --workspace-root . --ticket "<ticket>" --session-nonce "<nonce>"
```

After each handler, `advance` both finishes that stage and returns the next descriptor;
do not issue a redundant `next` between stages.

Descriptor cases:

- `done: true`: exit cleanly;
- `blocked_by`: surface the failed stage and stop the loop;
- otherwise: execute the declared stage descriptor.

If `roles` contains `records_diff_baseline`, record the planned-file baseline with
blob capture before the handler. Failure marks the stage failed. The baseline and
planned-file list are the commit ownership boundary.

## Handler dispatch

### Inline

Resolve `reference_doc` beneath the absolute `skill_root`, read it, and follow it.
Inline stages may write their declared artifact; absence is normal unless that stage's
protocol requires one.

### Independent agent

Read the stage reference first. Give the host-native agent:

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

State that inherited cwd is non-authoritative, every repository operation stays
beneath the workspace, and every facade call applies the call-local `FLOW_HARNESS`
selector to the absolute bound `facade`.

When `descriptor.roles` includes `"agent_routed"`, resolve its frozen profile from
`$TICKET_DIR/route-snapshot.json` through the facade. `implement` maps to
`implementer`; `e2e` maps to `e2e`:

```bash
FLOW_HARNESS="<harness>" "<facade>" agent-route resolve \
  --snapshot "$TICKET_DIR/route-snapshot.json" --profile "<profile>"
```

An `activation: pending` Claude Code route supplies the exact desired model and
effort to the native launch. Capture the native tool request and response as JSON;
never use the worker's prose as acceptance evidence. Attest and persist it:

```bash
FLOW_HARNESS="<harness>" "<facade>" agent-route attest \
  --snapshot "$TICKET_DIR/route-snapshot.json" --profile "<profile>" \
  --acceptance-from "<absolute-acceptance-json>" \
  --output "$TICKET_DIR/stages/<stage>.route.json"
```

Only an `active` receipt proves exact execution. A `shadow` Codex, generic, or
cross-harness route launches through the existing owner-native path with no model or
effort selector and records the shadow receipt. Codex does not retry merely because
a desired route stayed shadowed. A `legacy` route follows
`model_resolve.py` unchanged, including lane skips, OFF, fail-open reads, and Codex
inheritance. A missing route snapshot identifies a pre-upgrade run and takes the
same legacy path.

Capture the complete returned report at the exact absolute artifact path before
advancing. Prefer the host's exact-write primitive. If unavailable, use a
collision-safe quoted heredoc from a command rooted in `run_root`; never interpolate
model output into a shell argument.

Only one independent writer may own a stage at a time. If the agent has not returned
by the descriptor timeout, inspect three pieces of evidence before recovering: the
host's agent status, the declared artifact path, and the worktree diff. A still-running
agent gets another bounded wait while this owner refreshes the lease; do not launch a
second writer. A complete artifact is captured and advanced even if the host's status
message arrived late. Only after the original agent is confirmed terminal or stopped,
with no complete artifact, may the owner log `RETRY` and either finish the stage itself
or launch one replacement against the same baseline and ownership boundary. Never
overlap the replacement with the original: two plausible writers turn a recoverable
stall into ambiguous code ownership.

### Installed skill

Resolve the configured handler through the facade, then invoke it with the host's
native skill loader and exact declared arguments. A missing or invalid handler fails
the stage. Capture the full skill response before advancing. An inline skill response
is not a legitimate turn boundary: continue through artifact capture and advance in
the same turn.

### None or unknown

`none` completes without work. An unknown handler is a validation failure and stops;
never claim it ran.

## Advance

```bash
FLOW_HARNESS="<harness>" "<facade>" dispatch advance \
  --workspace-root . --ticket "<ticket>" --session-nonce "<nonce>" \
  --stage "<stage>" --status "<completed|failed>" \
  [--output-path "<absolute-existing-artifact>"]
```

An artifact path must exist before advance. If it does not, write it and retry the
same advance; the stage has not finished. A failed advance returns a blocking
descriptor.

## Safety markers and exit handling

- Backup state restoration: log `STATE_ROLLBACK`. Before rerunning a non-idempotent
  stage, verify whether its external effect already landed; if it did, complete the
  stage without replaying it.
- Owned configuration drift may reconcile only when every changed file is inside the
  run's declared ownership. Foreign, handler-tree, dirty-engine, or ambiguous drift
  stops for repair.
- Lost lease stops immediately. Never continue with a rotated nonce or missing lock.
- Workspace violations and unrecoverable state stop for diagnosis.
- A descriptor timeout is advisory where the host has no cross-agent deadline. Agents
  run long commands in bounded foreground calls and never return while owning a
  background task needed for continuation.

Log friction before working around drift, lease loss, reconciliation, missing tools,
blockers, failed stages, retries, and state rollback. Friction logging is best-effort
and cannot fail the run.

## Post-implementation ownership reconcile

If implementation identifies necessary files outside `planned_files`, widen the
ticket frontmatter before advancing, re-record the baseline, recapture the
implementation diff, and verify it applies cleanly with binary support. Do not widen
for incidental files. Planned binary deliverables that an agent could not create are
copied into the worktree before diff capture and remain inside the declared set.

Never stage unrelated user changes. If the ownership patch cannot apply to the clean
index, stop for repair rather than forcing or overwriting drift.

## PR and notification

For grouped delivery, post the created PR URL to each covered ticket best-effort. A
PR-ready notification fires at most once when the review loop is genuinely green and
actionable feedback is resolved. Claude Code may use its notification capability;
Codex reports in-thread; the forge receipt is the durable fallback. Unattended drains
rely on durable reporting rather than a live notification.

Backgrounding and session lifetime remain host-owned. The loop never stops its host,
removes host session files, or schedules self-teardown.

## Release and finish

When the descriptor returns `done: true`, generate the durable local run receipt
before release:

```bash
FLOW_HARNESS="<harness>" "<facade>" run-report \
  --workspace-root . --ticket-dir "$TICKET_DIR" \
  --output "$TICKET_DIR/run-report.json"
```

This is best-effort reporting, not a delivery gate. It ranks stage durations and
between-stage gaps separately, scoped to state timestamps, and joins only friction
events carrying this run's `run_id`. A gap is neutral evidence; do not infer whether
the human, forge, or agent caused it without corroboration. Include the total, the
largest contributors, and recorded friction (or “none recorded”) in the final
summary. For a long or frictional run, add one sentence explaining the dominant
segment and the workaround/fix; for a short clean run, the compact block is enough.

After every post-acquisition exit—done, blocked, drift, or lost lease—release:

```bash
FLOW_HARNESS="<harness>" "<facade>" dispatch release \
  --workspace-root . --ticket "<ticket>" --session-nonce "<nonce>"
```

Release is safe when ownership was lost, but must not be called on acquisition aborts.
For a clean run, summarize the ticket, tests, verification, commit, and residual risk.
End with the PR URL as a distinct final block. If no PR stage ran, omit that block
rather than printing an empty link.
