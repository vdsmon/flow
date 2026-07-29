# Dispatcher delivery loop

The dispatcher owns state, lease refresh, snapshot validation, stage transitions, and
the canonical descriptor. The driver conversation executes handlers and persists their
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
   `next`, `advance`, and `release`. It distinguishes the driver holding the lease from a second session
   that merely knows the run id.

Do not clear leases automatically. A live holder, stale holder, corrupt lock,
unrecoverable state, or workspace violation returns to the target lifecycle as
`running` or `repair`. If acquisition failed, do not release because this driver never
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
blob capture before the handler; a non-zero exit marks the stage failed. The
baseline and planned-file list are the commit ownership boundary:

```bash
FLOW_HARNESS="<harness>" "<facade>" diff record-baseline \
  --stage "<stage>" --ticket "<ticket>" --ticket-dir "<ticket_dir>" \
  --files "<comma-separated planned_files>" --capture-blobs --cwd .
```

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
Harness: <claude-code|codex>
Ticket and stage: <ticket> / <stage>
Ticket dir: <absolute ticket_dir>
Reference path: <absolute reference, or none>
Artifact path: <absolute output_path>
```

State that inherited cwd is non-authoritative, every repository operation stays
beneath the workspace, and every facade call applies the call-local `FLOW_HARNESS`
selector to the absolute bound `facade`.

State the write-confinement rule in the same prompt. The host binds a session's
file-write tool to its PINNED worktree rather than to its working directory, the pin
can name another live run's worktree on any call, and a subagent's pin is fixed at
spawn. An agent whose write is refused as isolated in a worktree that is not its run
root cannot fix that for itself: `cd` moves the working directory and not the pin, and
a subagent cannot re-pin at all. Warn it that the host's worktree switch can report
SUCCESS to a subagent and still leave the write refused, which is worse than refusing,
so that return value is no evidence about its writer; only attempting the write is.
Tell it to return BLOCKED at once, naming the worktree the refusal named, rather than
routing around the guard or diagnosing it; the driver's takeover below is cheaper than
either. Briefed agents recognize the refusal and stop, where unbriefed ones each spend
turns rediscovering it and tend to reinvent a worse workaround than the recorded one.

The field block is a minimum, not a closed set. When an earlier `friction` call in
this run answered with related knowledge (below), carry what the run leaned on into
this prompt. The stage that hit the snag has already closed, so the entry reaches a
reader only through the prompt of a stage that has not started yet.

`Artifact path` always carries the descriptor's real `output_path`, never a
placeholder. A stage reference may assign the WRITE of that file to the driver rather
than to the agent, which changes who writes it, not what the field says: this section's
capture rule is the driver's half of the same contract.

A workspace may provide an optional agent hint per stage, or per role within a
stage. A bare stage string and a single-role table both resolve with no `--role`;
only a stage that launches several roles (code_review's reviewer and fixer) needs
the launching role named. `--field effort` reads the optional effort hint:

```bash
FLOW_HARNESS="<harness>" "<facade>" model --workspace-root . --stage "<stage>" \
  [--role "<role>"] [--field model|effort]
```

An empty result means inherit the driver session model. Apply a non-empty hint only
when the current host supports it; unsupported hints also inherit. This is a
convenience, not execution provenance: Flow does not attest the provider or model.

Capture the complete returned report at the exact absolute artifact path before
advancing. Prefer the host's exact-write primitive. If unavailable, use a
collision-safe quoted heredoc from a command rooted in `run_root`; never interpolate
model output into a shell argument.

When the host cannot or should not launch an independent agent — the capability is
absent, or the host's usage guard warns against spawning new agents — the driver
executes the same stage itself: read the stage reference, produce the artifact at
the declared absolute path, and advance normally. Flow does not attest execution
provenance, so a driver-executed stage is legitimate. Log one best-effort friction
event for the downgrade so the pattern stays visible. The downgrade never skips the
descriptor, the artifact, or the advance.

The same downgrade answers an agent that returns BLOCKED on write confinement, and it
is the preferred answer. First re-pin this session on the run root through the host's
native worktree switch (on Claude Code, `EnterWorktree` with an explicit path), then
run the stage inline: same reference, same artifact path, same advance. That route
keeps atomic replacement and the read-before-edit guard, and a driver session re-pins
reliably where a subagent cannot. Reliably means per attempt, not durably: the pin can be
taken again mid-stage, so re-issue the switch on every refusal rather than assuming one
re-pin holds for the run. Treat the write attempt as the only evidence, and note that
confinement guards tracked paths, so a successful write under a gitignored path is not
proof the re-pin took. Root every git and grep call with an absolute path or `-C
<absolute>`: an agent thread's cwd can reset between calls, and a relative path then
reads a different worktree silently, which is worse than a refusal because it points at
the wrong conclusion. Only if no such switch exists, or the re-pin does not
take, write through Bash: an exact-match replacement asserting one hit per substitution
when editing an existing file, and the collision-safe quoted heredoc above when creating
a new one, which is the shape a stage artifact needs. Say so in the report: that route
works and agents have completed correctly on it, but it gives up read-before-edit and
atomic replacement, so it trades a refused write for a wrong-write risk. Log the takeover
as friction either way.

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
- No cross-agent deadline exists; a stage's timeout lives only in the run-lease TTL.
  Agents run long commands in bounded foreground calls and never return while owning
  a background task needed for continuation.

Log friction before working around drift, lease loss, reconciliation, missing tools,
blockers, failed stages, retries, and state rollback. The `--type` names the snag the
workaround answers, from the closed set `flow_friction.py` accepts; there is no
workaround type. Friction logging is best-effort and cannot fail the run.

The command answers. After the appended entry it prints the live knowledge entries
whose text describes the same snag, above an absolute similarity floor, and prints
nothing at all when the corpus holds none. Read what it prints before improvising a
workaround. The corpus is otherwise queried once per run, at planning, against the
ticket intent, so a snag that first appears mid-run has never been looked up. Silence
is the common case and means the corpus has no answer, not that nothing was asked.
Silence on stdout is not silence on stderr: a near-miss line naming the best entry that
fell just under the floor is a diagnostic for recalibrating it, not an answer to read.
The durable record is unchanged and no failure of the lookup can change the exit code.

## Post-implementation ownership reconcile

If implementation identifies necessary files outside `planned_files`, widen the
ticket frontmatter before advancing, re-record the baseline, recapture the
implementation diff, and verify it applies cleanly with binary support. Do not widen
for incidental files. Planned binary deliverables that an agent could not create are
copied into the worktree before diff capture and remain inside the declared set.

Re-recording moves the diff anchor (`baseline.head_sha`) to live HEAD and leaves the
ownership anchor (`baseline.origin_sha`) at the run's origin. So the widened set is what
changes for `check-ownership`, never the range it scans: work committed during implement
stays inside that range and an unplanned file committed there is still refused. The
capture anchor does move, which is why a recapture after a mid-implement commit can
return a payload missing the committed half (`stage-code_review.md`).

Never stage unrelated changes. If the ownership patch cannot apply to the clean
index, stop for repair rather than forcing or overwriting drift.

## PR and notification

For grouped delivery, post the created PR URL to each covered ticket best-effort. A
PR-ready notification fires at most once when the review loop is genuinely green and
actionable feedback is resolved. Claude Code may use its notification capability;
Codex reports in-thread; the forge receipt is the durable fallback. Unattended runs
rely on durable reporting rather than a live notification.

Backgrounding and session lifetime remain host-owned. The loop never stops its host,
removes host session files, or schedules self-teardown.

## Release and finish

After every post-acquisition exit—done, blocked, drift, or lost lease—release:

```bash
FLOW_HARNESS="<harness>" "<facade>" dispatch release \
  --workspace-root . --ticket "<ticket>" --session-nonce "<nonce>"
```

Release is safe when ownership was lost, but must not be called on acquisition aborts.
For a clean run, summarize the ticket, tests, verification, commit, and residual risk.
End with the PR URL as a distinct final block. If no PR stage ran, omit that block
rather than printing an empty link.
