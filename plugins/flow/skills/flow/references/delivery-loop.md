<!-- flow:activation-truth:begin -->
# Dispatcher delivery loop

## Cognitive outcome fence

Dispatch seals each cognitive substep with run, stage, substep, stage generation,
source SHA, route snapshot, owner, and lease facts. Resuming an in-progress stage reuses
the generation; an explicit retry increments it. Stage completion accepts only matching
successful outcomes or a reasoned conditional skip. The model never advances pipeline
state directly.

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
`$TICKET_DIR/route-snapshot.json` through the facade. The stage map is frozen in the
same snapshot. Current agent-routed descriptors still map `implement` to
`implementer` and `e2e` to `e2e`:

```bash
FLOW_HARNESS="<harness>" "<facade>" agent-route resolve \
  --snapshot "$TICKET_DIR/route-snapshot.json" --profile "<profile>"
```

The read-only profiles, the disposable E2E capsule, the importing writers (implementer,
review_fixer, revision_fixer), and the read-only machinery_fixer have `activation: pending`
on an exact route; under the generic owner adapter a route stays shadow. A shadow desired
route is provenance for a future
execution capsule and
does not change the current native launch. Capture the native tool request and response
as JSON, and never use the worker's prose as acceptance evidence. Attest and persist it:

```bash
FLOW_HARNESS="<harness>" "<facade>" agent-route attest \
  --snapshot "$TICKET_DIR/route-snapshot.json" --profile "<profile>" \
  --acceptance-from "<absolute-acceptance-json>" \
  --output "$TICKET_DIR/stages/<stage>.route.json"
```

Only an `active` receipt proves exact routed execution, and only a receipt carrying a
terminal physical attempt and a disposed capsule can become active. A shadow receipt,
including a same-owner exact native acceptance, means the handler launched through its
existing owner-native or inline path and recorded the desired route without claiming it
ran. Do not retry because a desired route stayed shadowed. A `legacy` route follows
`model_resolve.py` unchanged, including lane skips, OFF, fail-open reads, and Codex
inheritance. A missing route snapshot identifies a pre-upgrade run and takes the
same legacy path.

### Activated cognitive substeps

When the descriptor carries `cognitive_substeps` with `activation: pending`, one
deterministic command executes every one of them. Do not launch a capsule by hand, and
do not assemble a provider prompt: build each substep's closed facts and its immutable
input bundle, then hand both to the executor. It launches only the substeps the frozen
snapshot recorded active, publishes each typed result separately for the deterministic
renderers and appliers, and writes the outcome fence the dispatcher validates.

```bash
FLOW_HARNESS="<harness>" "<facade>" cognitive-worker run-stage \
  --descriptor-from "<descriptor_path>" \
  --inputs-from "<absolute-cognitive-inputs-json>" \
  --source-root . \
  --artifact-root "$TICKET_DIR/cognitive/<stage>" \
  --capsule-root "$TICKET_DIR/cognitive/capsules" \
  --output "$TICKET_DIR/stages/<stage>.cognitive.json"
```

Each `--inputs-from` entry is keyed by substep and holds either `facts` plus an
`input_bundle` path, or a `skip` with an exact `reason` when a conditional substep does
not apply. An exact-route failure stops the stage visibly: never fall back to a native
or alternate-model reader.

Each profile's fact bundle is closed: exactly these keys, no more and no fewer. An extra
key is refused rather than ignored, so a prompt can never be extended from the outside.

| profile | facts |
|---|---|
| `planner` | `stage_plan`, `ticket`, `base_sha`, `route`, `current_envelope`, `feedback_ledger`, `version_requirements`, `approved_design_digest`, `mode` |
| `plan_assessor` | `ticket`, `base_sha`, `route_digest`, `candidate_plan`, `planner_receipt`, `assessment_rubric` |
| `code_reviewer` | `stage_code_review`, `ticket`, `accepted_plan`, `source_sha`, `review_bundle` |
| `diff_reviewer` | `source_sha`, `review_bundle`, `review_rubric` |
| `guard_reviewer` | `probe`, `guard_diff`, `guard_properties` |
| `review_brief_author` | `ticket`, `plan`, `pr`, `review`, `e2e`, `ci`, `content_contract` |
| `reflector` | `reflection_input`, `stage_reflect`, `action_contract` |
| `e2e` | `stage_e2e`, `ticket`, `source_sha`, `e2e_recipe`, `evidence_contract` |
| `implementer` | `stage_implement`, `ticket`, `source_sha`, `plan`, `planned_files`, `report_contract` |
| `review_fixer` | `stage_review_loop`, `ticket`, `source_sha`, `review_findings`, `planned_files`, `report_contract` |
| `revision_fixer` | `stage_review_loop`, `ticket`, `source_sha`, `revision_instruction`, `planned_files`, `report_contract` |

Two activated substeps launch through a write-capable capsule. `e2e`
(`authority: disposable_writer`) writes fixtures, caches, snapshots, and build products
inside its capsule. Delivery runs implement -> code_review -> e2e ->
commit, so the ticket's changes are still uncommitted at e2e time; dispatch seals that
working-state delta as an immutable seed patch and the executor seeds the capsule with it,
so the recipe runs against the ticket's real code. It imports nothing and takes no writer
lock: Flow captures what the recipe mutated on top of that seeded baseline (touched paths
and diffstat) into the result's `capsule_mutations`, then discards the whole capsule, so
the authoritative worktree is provably untouched. Its `input_bundle` may be any immutable
evidence path the recipe needs; no `bundle-review` call is required.

The importing writers (`authority: capsule_writer`) — `implementer` at implement, and
`review_fixer` / `revision_fixer` at the review-loop fix step — edit and test inside a
private capsule, then Flow, not the model, captures the binary-aware Git patch and imports
it into the authoritative worktree under a sole-writer, compare-and-swap claim. Each order's
`allowed_mutation_paths` is sealed to the run's `planned_files` (from `baseline.json`, the
same set the content-ownership commit gate re-scans), so touching any path outside that set
makes the whole change an `ownership_violation` and nothing is imported. A fixer runs over
the ticket's uncommitted edits, so dispatch seeds its capsule with that working-state delta
and Flow captures the writer's own change against the seeded baseline (never double-counting
the seed). The worker returns only a typed report (`summary`, `evidence`, `source_sha`); it
never serializes a diff. On a successful import the capsule is disposed and the change
receipt records the patch digest, touched paths, and import result.

Build the reviewers' `input_bundle` from the authoritative tree first. The bundle is
immutable, content-addressed evidence, never an appliable patch:

```bash
FLOW_HARNESS="<harness>" "<facade>" cognitive-worker bundle-review \
  --source-root . --output "$TICKET_DIR/cognitive/<stage>/review-bundle"
```

A `code_reviewer` or `diff_reviewer` verdict must cite that bundle's exact digest in its
`input_digest`, or the worker refuses the result: a clean verdict reached over the wrong
evidence is worse than none.

`advance` reads each worker's receipt from the invocation directory the dispatcher sealed,
not from the structured output you pass it. Only a conditional skip travels through
`--skill-output-from`; a fabricated outcome cannot complete a stage.

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

### review_brief's unattended skip signal

`review_brief_author`'s `main` substep is conditional. The bootstrapped `unattended`
ticket-frontmatter boolean (stamped once at `worktree create`, from `auto` or a
`@default` base) is the SOLE run-mode signal for whether it launches: never
re-derive unattendedness from lane, route activation, browser/`--no-open`
configuration, environment, or owner memory — `stage-review_brief.md` reads that one
frontmatter value and reuses it for both the skip decision and `--no-open`.

When `unattended` is `true`, the terminal `advance` for `review_brief` carries a
`main` skip through `--skill-output-from` with the exact reason `unattended run has
no live human reviewer`; the generic conditional fence accepts any reasoned skip
here (it is not lane-gated), but that acceptance is not authorization. `merge`'s
eligibility probe separately calls `review_brief.py freshness`, which re-reads this
same run's `unattended` frontmatter and the persisted skip receipt, and returns
`disabled` (non-blocking) only when both agree; an attended run whose tail emitted
the canonical skip anyway gets blocking `missing` instead, so the brief is refreshed
rather than silently lost.

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
  [--output-path "<absolute-existing-artifact>"] \
  [--skill-output-from "$TICKET_DIR/stages/<stage>.cognitive.json"]
```

An artifact path must exist before advance. If it does not, write it and retry the
same advance; the stage has not finished. A failed advance returns a blocking
descriptor. A stage with activated cognitive substeps passes its outcome fence through
`--skill-output-from`; the evidence is far too large for one shell argument, and
`advance` refuses to complete the stage without a matching successful outcome or a
reasoned conditional skip.

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

```bash
FLOW_HARNESS="<harness>" "<facade>" friction \
  --ticket <KEY> --run-id <run_id> --stage <stage> \
  --type <TYPE> --severity <sev> \
  --body "<what>" --detail "<why>" \
  --workspace-root . || true
```

`<TYPE>` is one of `BLOCKER`, `RETRY`, `MISSING_TOOL`, `DRIFT`, `LEASE_LOSS`,
`RECONCILE`, `STAGE_FAILED`, or `STATE_ROLLBACK`; `<sev>` is `major` or `minor`.

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
