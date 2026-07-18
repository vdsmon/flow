# Maintain commands

Except for workspace-local worktree and quarantine cleanup, maintenance is restricted
to workspaces whose configuration identifies the current repository as Flow's
maintainer target:

```bash
FLOW_HARNESS="<harness>" "<facade>" maintainer --workspace-root . --require-current
```

A refusal stops the command and names any configured target outside the invoking
repository. Internal scheduling code may still resolve that target, but no public
maintenance command follows the pointer implicitly. Maintenance never assumes a
particular host process, terminal, or background-job implementation.

Before every maintenance operation except `FLOW maintain worktrees clean` and
`FLOW maintain quarantine clean`, collect the host-neutral schedule and ship-event
senses diagnostics:

```bash
FLOW_HARNESS="<harness>" "<facade>" maintainer-preflight --json
FLOW_HARNESS="<harness>" "<facade>" maintainer-senses --workspace-root . --dry-run --json
```

Surface warning states in the initial report. These reads do not file alarms or
change the requested operation. A failed diagnostic is visible as unavailable
evidence; it is not silently converted to healthy. Commands whose own decision needs
that evidence stop rather than guessing. Scheduled nightly operation may run the
senses seam without `--dry-run` as its explicitly configured alarm producer.

## Owner-session worker pool

The conversation invoking a drain is the owner. Native worker creation, waiting, and
cancellation exist only in the harness tool API, so a Python subprocess cannot call
them on the owner's behalf. The adapter drives those native operations and uses the
executable `worker-pool` facade for the deterministic decisions around them:

```text
capacity  total collaboration slots, including the owner
launch    create a host-native worker for one bounded request
wait      return completion for selected owner handles
cancel    stop selected owner handles
```

Effective worker concurrency is
`min(configured_concurrency, capacity - 1)`. Calculate it before launch rather than
reimplementing the reservation in prose:

```bash
FLOW_HARNESS="<harness>" "<facade>" worker-pool limit \
  --configured <configured-concurrency> --capacity <host-capacity>
```

Always reserve one slot for the owner.
Claude Code uses native collaboration agents and may honor a configured Claude model
hint. Codex uses native collaboration agents that inherit the active model. Never
launch a detached host CLI or emulate backgrounding with shell processes.

Worker handles belong to one owner session and are disposable. Durable fleet entries,
run state, leases, worktrees, tracker state, and PRs decide whether work is running or
settled. If the owner disappears, normalize durable evidence to an absolute JSON array
of `{key,state,run_id}` rows and reduce it before any relaunch:

```bash
FLOW_HARNESS="<harness>" "<facade>" worker-pool recover \
  --evidence "<absolute-recovery-evidence-file>"
```

The closed result maps absent -> relaunch, bootstrapping/running -> monitor, succeeded
-> settled, and failed/corrupt -> repair. It never accepts a worker handle as evidence.

Discovery workers are `read_only=true`. Write a pre-launch receipt to an absolute
temporary file, then run the guard after the native wait returns:

```bash
FLOW_HARNESS="<harness>" "<facade>" worker-pool snapshot \
  --workspace-root "<run_root>" > "<absolute-before-file>"
# launch and wait through the host's native collaboration API
FLOW_HARNESS="<harness>" "<facade>" worker-pool guard \
  --workspace-root "<run_root>" --before "<absolute-before-file>"
```

Guard exit 3 names changed canonical `flow.git-receipt/v1` fields. Discard that
worker's findings and stop before filing tickets or applying a proposal. A legacy
four-field receipt fails closed with exit 2. Pre-existing dirt is allowed only when
the receipts are exactly equal.

The user may background the owner conversation through the host. Flow does not
background itself, inspect host job directories, stop host sessions, or tear down its
owner.

## `FLOW maintain backlog status [--preview]`

This command is strictly read-only. Gather:

```bash
FLOW_HARNESS="<harness>" "<facade>" queue-status --workspace-root .
```

Render:

- ready day-job tickets ordered by priority and key;
- active runs with lease liveness;
- open-PR backpressure and parked tickets;
- newly actionable PR feedback;
- launch-pending fleet entries;
- the advisory next action.

`--preview` additionally shows the bounded batch a drain would launch and its
verification/model policy, but launches nothing and writes no fleet entry. Human-input,
hot, evolution, proposal, and epic tickets remain outside the ordinary ready set.
Actionable review rows point to `FLOW pr:<number>`.

## `FLOW maintain backlog drain [--dry-run]`

The ordinary backlog drain owns ready day-job work until it is delivered, parked for
review, deferred for a decision, or durably blocked.

Each loop turn:

1. Classify through the drain seam:

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" queue-drain --workspace-root .
   ```

2. Interpret exactly one action: `launch`, `recover`, `wait`, or `done`.
3. `launch`: register every key in the durable fleet before creating its worker, then
   launch up to the owner-pool limit with the logical task
   `FLOW <key> --unattended`. Wait through the host adapter for completions or enough
   durable evidence to reclassify.
4. `recover`: re-check the lease under the authoritative seam, checkpoint any dirty
   stranded pre-PR worktree to a rescue ref, reap only after the checkpoint succeeds,
   then reopen for one fresh launch. A repeated deterministic strand becomes blocked
   with evidence rather than looping forever.
5. `wait`: wait on owner handles when available and durable lease/fleet/PR changes in
   all cases. Use a bounded poll and a consecutive-error budget; then reclassify.
6. `done`: report delivered, parked, deferred, blocked, recovered, and failed work.

Ordinary backlog delivery parks a green PR for human review; it does not merge it.
`--dry-run` performs one classification and renders launches/recovery without fleet
writes, worker launches, tracker transitions, worktree reaping, or merges.

## `FLOW maintain evolution audit`

Audit mines concrete defects in Flow's own machinery and files bounded, reviewable
fix tickets. Fan out read-only workers across correctness, robustness, documentation
drift, tests, friction recurrence, and measurement regressions. Each finding must
carry a repository witness, blast radius, severity, confidence, and stable dedup key.

Synthesize and independently verify findings. Drop refuted or already-covered work.
Classify hot/guard-file changes and cheap behavior-preserving work. File confirmed
fixes through the tracker seam with the evolution label and any verified tier label.
Deduplication covers open and closed tickets so a rerun converges. The command files
work but does not implement it.

## `FLOW maintain evolution propose`

Propose explores judgment work that cannot be justified as a mechanical fix: new
capabilities, true simplifications, reorganization, deletion, architecture coherence,
and missing symmetry. Every read-only worker receives one lens and grounds candidates
in repository evidence and the repository vision.

An independent skeptic tries to refute each candidate. Quiet lenses and an empty
result are valid. Split survivors:

- provably safe, behavior-preserving work becomes an evolution fix eligible for the
  normal evolution drain;
- judgment work becomes a proposal ticket for attended `FLOW <key>` delivery.

Rank by vision alignment, value, evidence, and reviewability. Every proposal records
confidence, blast radius, stable dedup key, and a recommended default: build, shelve,
or discuss. This command files proposals but never delivers them.

## `FLOW maintain evolution epic`

The epic producer works at initiative altitude: capability tracks, system-wide
architecture shifts, unfinished strategic tracks, and improvements to the evolution
loop itself. It is deliberately lower-frequency than fix discovery.

Read-only workers inspect repository vision, architecture maps, friction/measurement
history, current epics, and—when the host permits current research—relevant external
primary sources. Every candidate needs externalized grounding: a cited field advance,
a witnessed internal signal, an unfinished track, or a bounded scratch-worktree
spike. A spike never touches the maintainer checkout and its only durable output is
evidence in the proposal.

An independent skeptic rejects groundless, frivolous, or non-decomposable candidates.
For each survivor, file only a deduplicated epic parent. Its description contains an
ordered child preview, marking each child as either net-new with its own stable dedup
key or an existing ticket to reparent. Children remain lazy until explicit expansion.
The epic itself is never drainable.

## `FLOW maintain evolution expand <epic>`

Expansion materializes an accepted epic's approved preview. Show the exact child and
reparent plan, then confirm because the operation mutates the tracker.

- For each net-new child, file one proposal leaf with the recorded parent and dedup
  key.
- For an explicitly marked existing child, reparent that exact ticket without
  changing its identity, labels, or status.
- A plain ticket mention is not a reparent instruction.

Verify the resulting parent/child graph and report each leaf as `FLOW <key>`. Never
deliver children automatically from expansion.

## `FLOW maintain evolution drain [--dry-run] [--include-proposals]`

Evolution drain uses the same owner pool and durable authority as the ordinary
backlog, but additionally reaps green evolution PRs after guard checks and serializes
hot work.

Each bounded loop turn:

1. Reap-classify existing PRs through the reap seam, forwarding `--include-proposals`
   whenever the public invocation carries it (both classifiers MUST see the same
   flag, or the launch and reap populations diverge) and forwarding `--dry-run` the
   same way:

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" evolve-reap --workspace-root . [--include-proposals] [--dry-run]
   ```

   Buckets: `merge`, `not_green`, `skipped_hot`, `skipped_live`, `blocked`,
   `held_main_red`, plus the `main_red_p0` record. `evolve-reap` probes main's OWN CI
   health every turn and, when main is genuinely red, routes every would-be merge into
   `held_main_red`. Without `--dry-run` it also files its best-effort, deduplicated
   `main-ci-red` P0 tracker alert; with `--dry-run` it files nothing and `main_red_p0`
   instead carries the would-file record naming the failing sha + check(s) (see
   Dry-run below).

2. Decide the launch/recover/wait/done action through the drain seam (`evolve-drain`,
   same `--include-proposals` forwarding). On `--dry-run` this runs right here, after
   step 1 (see Dry-run below). On a real turn it instead runs after the merge set is
   fully processed (see "Non-dry-run: decide the launch/recover/wait/done action")
   — a merge executed this turn frees the PR-cap backpressure `evolve-drain` counts
   against, and deciding on the pre-merge open-PR count could return `done` while
   that freed capacity sits unused.

### Dry-run

`--dry-run` runs both classifications: step 1 above, then step 2 immediately:

```bash
FLOW_HARNESS="<harness>" "<facade>" evolve-drain --workspace-root . [--include-proposals]
```

Reports `action` (`launch`/`recover`/`wait`/`done`), `launch`, `stranded_pre_pr`, and
`parked`. Dry-run then reports the would-merge set (the reap `merge` bucket), the
would-launch set (the drain `launch` batch), the would-recover set (the drain
`stranded_pre_pr` list), and, when main CI is genuinely red, "would file P0:
<sha> <failing checks>" (from the reap `main_red_p0` record), then stops. It performs
NO merge, tracker write, fleet registration, worktree reaping, or worker launch, with
no exceptions: every write belongs to the non-dry-run branches below.

### Non-dry-run: reap the `merge` set

For each entry:

1. Re-check its lease liveness immediately before mutating anything — a classification
   goes stale the instant a parked run resumes:

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" fleet is-live --key "<key>" --workspace-root .
   ```

   Exit 0 (live) withdraws the candidate for this turn, the same as `skipped_live`.

2. When `is_hot`, first quiesce every other evolve run: `evolve-reap`'s isolation only
   serializes hot PRs within one classification pass, not across concurrent
   maintenance drains, so the operator must confirm no sibling drain is mid-hot-pass
   before merging. Report an inability to quiesce as a hold; never merge past it.

3. When `is_hot`, run the independent guard-property review exactly as
   `references/stage-merge.md` §2 describes: a fresh reviewer, not the diff's author,
   asked to REFUTE against the same guard properties. A `property_removed: true`
   verdict, or any reviewer failure, holds the PR (report it `held_guard`); only a
   clean verdict proceeds.

4. Mark a draft ready, then squash-merge, through the forge seam:

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . mark-ready --pr "<pr>"
   FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . merge --pr "<pr>" --squash
   ```

5. Only once the merge call reports success: close the lead ticket and every key in
   `covers` through the tracker seam,

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" tracker --workspace-root . transition --key "<key>" --to-state closed
   ```

   observe the ship event exactly as `references/stage-reflect.md` §Step 6 describes
   when the orphaned run's own reflect stage never recorded it, delete the remote
   branch,

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . delete-branch --branch "<branch>"
   ```

   and reap the local worktree:

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" worktree reap --ticket "<key>" --main-root .
   ```

   A merge-tool failure closes nothing and leaves the PR for a later turn. A
   post-merge close/observe/branch-delete/reap hiccup is best-effort — warn and
   continue, since the diff is already merged and none of these steps is what makes
   that safe.

Leave `not_green`, `blocked`, `skipped_live`, `skipped_hot`, and `held_main_red`
untouched; they are not this turn's problem.

### Non-dry-run: decide the launch/recover/wait/done action

Run step 2 now, only after the merge set above is fully processed, so a productive
merge turn cannot report `done` on backpressure the merges just freed:

```bash
FLOW_HARNESS="<harness>" "<facade>" evolve-drain --workspace-root . [--include-proposals]
```

Reports `action` (`launch`/`recover`/`wait`/`done`), `launch`, `stranded_pre_pr`, and
`parked`.

### Non-dry-run: consume the drain decision

- `launch`: register every key in the durable fleet before creating its worker, then
  launch exactly as `FLOW maintain backlog drain` step 3, scoped to the evolution
  batch the classifier chose.
- `recover`: for each key in `stranded_pre_pr`, re-check its lease liveness, then reap
  its dirty worktree (the reap facade checkpoints uncommitted work to a
  `flow-rescue/*` ref before removing anything) only after the checkpoint succeeds,
  then reopen the bead for one fresh launch next turn — the same shape as backlog
  drain step 4. A repeated deterministic strand becomes blocked with evidence rather
  than looping forever.
- `wait`: wait on owner handles when available and durable lease/fleet/PR changes in
  all cases, exactly as backlog drain step 5.
- `done`: report merged, delivered, parked, recovered, blocked, and held work.

Tier and model hints are policy inputs, not worker identity. Claude Code may apply a
supported Claude model hint; Codex inherits the active model while preserving the
same verification lane. Hot work takes the configured high-scrutiny lane and only
one hot slot.

`--include-proposals` deliberately widens the ready set to proposal tickets and thus
permits unattended delivery of judgment work. Surface that risk in the initial and
final report.

Do not update an installed plugin or advance the maintainer checkout while any base
or revision run is live. The scheduler proves this with
`FLOW_HARNESS="<harness>" "<facade>" maintainer-preflight --workspace-root
"<run_root>" --require-clean-boundary` before either mutation. At
clean boundaries, a fast-forward-only update may be attempted; dirty, corrupt, or
diverged state is reported and left untouched.

## `FLOW maintain worktrees clean [--dry-run]`

Sweep worktrees owned by the invoking workspace only. Resolve the absolute primary
checkout from the first `git worktree list --porcelain` stanza and recognize only
registered worktrees beneath its `.claude/worktrees` or legacy `.flow/worktrees`
directory. Never consider the invoking checkout itself.

A candidate is removable only when its normalized tracker state is `done` or
`cancelled`, its exact run lease is not live or corrupt, and one of these PR proofs
holds:

- a merged PR has a head SHA equal to the local worktree tip;
- no open or merged PR exists, the local `origin/HEAD` SHA matches a read-only
  `git ls-remote` result, and the branch has zero commits unique from that default.

An open PR always preserves its worktree. Missing ticket ownership, a stale remote
default, a merged-head mismatch, unique commits, or any candidate probe failure also
preserves it.

```bash
FLOW_HARNESS="<harness>" "<facade>" worktree-janitor sweep --workspace-root . --dry-run
```

First show the absolute `target_root`, every reapable candidate and its `confirmation_id`, and every
preserved candidate with its reason. If the public invocation included `--dry-run`, stop there.
Otherwise obtain confirmation for that exact target and candidate set. Then bind the destructive
invocation to the preview values:

```bash
FLOW_HARNESS="<harness>" "<facade>" worktree-janitor sweep --workspace-root . \
  --confirmed-target "<target_root>" \
  --confirmed-candidate "<confirmation_id>" [...]
```

The second invocation re-probes ownership, tracker, forge, remote-default, unique-commit, and exact
base/revision-lease evidence before it removes anything. A candidate absent from the preview or
whose path, branch, or tip changed has a different confirmation ID and is preserved.

A dirty candidate is checkpointed to a rescue ref before removal. Capture failure
leaves the worktree intact. `observe_at_close` runs inside the guarded teardown after checkpointing
and immediately before each removal attempt; the preview never observes or reaps. Never remove an
unrecognized worktree merely because its branch name resembles Flow.

## `FLOW maintain quarantine clean [--dry-run]`

Sweep quarantined cognitive capsules owned by the invoking workspace only: every
`.flow/runs/<ticket>/cognitive/<stage>/invocations/*/journal.json` and its
`revisions/<revision>/` sibling. Ephemeral planner and plan-assessor invocation roots
live outside the workspace, under each launch's own private cache directory, and are
bounded separately by that worker's own reaper; this command never touches them.

A quarantined journal always records the capsule path `_dispose_failed_capsule` moved
it to. Seven days since the journal's last transition is the default aged threshold; a
candidate at or past it is listed as reapable without further acknowledgement. A
younger candidate is listed too, but only an explicit confirmed candidate ID selects
it for the real pass. A recorded quarantine path that does not exist on disk (a
suppressed move failure) is reported as its own row rather than silently dropped or
treated as an error.

```bash
FLOW_HARNESS="<harness>" "<facade>" worktree-janitor quarantine-clean --workspace-root . --dry-run
```

First show the absolute `target_root`, every aged candidate under `reapable`, every
younger candidate under `younger`, and every recorded-but-absent path under
`recorded_missing`, each with its `confirmation_id`. If the public invocation included
`--dry-run`, stop there. Otherwise obtain confirmation for that exact target and
candidate set. Then bind the destructive invocation to the preview values:

```bash
FLOW_HARNESS="<harness>" "<facade>" worktree-janitor quarantine-clean --workspace-root . \
  --confirmed-target "<target_root>" \
  --confirmed-candidate "<confirmation_id>" [...]
```

The second invocation re-reads each confirmed journal and re-checks containment and
the digest-bound confirmation ID under the same per-invocation lock the cognitive
executor itself uses, before it archives anything. A candidate whose journal changed
since the preview (a fresh recovery, a concurrent annotation) has a different
confirmation ID and is preserved. A confirmed, still-matching candidate is archived by
rename into a sibling `archive/` directory next to `capsules/quarantine/`; this command
never deletes a capsule. The still-quarantined journal is annotated with the archive
path afterward; a failed annotation leaves a visible row but does not undo the rename.
