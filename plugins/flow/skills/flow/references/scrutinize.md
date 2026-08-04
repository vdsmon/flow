# Scrutinize

Scrutiny is flow's post-hoc maintenance verb. `FLOW scrutinize [<workspace>]` seats the invoking session (the seat, everywhere below) in this repository only, and the seat's view is every workspace flow runs in: runs are ordinary flow sessions that know nothing about the seat, in delivery workspaces and the self-target alike, and a scrutiny happens between runs to sweep the durable evidence they all left behind, synthesize what no single run can see, tend the machinery queue, and merge this repository's parked PRs whose gate is met. Delivery-run evidence is the primary signal, not an extra: the veto-pass test that shaped this seat found the valuable friction in real delivery runs and the weak findings in flow examining itself. Owning the queue means judging it and recommending from it, not starting work the human did not ask for. The triage and merge authority is maintainer-granted rather than witnessed; merge authority and inline work never leave this repository, and a delivery workspace's PRs stay the human's.

There is no live supervision. The seat never spawns, watches, relays for, or interrogates a running session: the one topology that did (drivers as background teammates, gate relays, stall watches) was retired after its failure modes proved to be the topology's own, and its play-by-play is preserved in the ledger archives. A session that should run a ticket is a fresh one, invoked with the target directly.

## Seating

`FLOW scrutinize` routes here: it seats the invoking session. The seat
is a role, not a process: its continuity lives in this charter, the
project memory (the seat entries and the durable ledger), and the tracker, never in
any one session. A successor
seat inherits everything a predecessor ledgered; nothing is handed off
conversationally. The seat script refuses to seat outside the self-target
workspace: a delivery workspace runs plain driver sessions instead.

Seating runs a mechanical half and a judgment half, in this order:

1. **Posture.** Run the seat script. It refuses outside the self-target, fetches
   origin, resolves the remote default branch **and the integration branch**, ensures
   the standing bench worktree exists (§The workbench), reads configured integration
   names without constructing their adapters, and scans registered worktrees for
   non-terminal local runs. The JSON posture includes primary-checkout and bench Git
   state; `default_branch`; `integration_branch` (`[create_pr] base` when configured,
   otherwise the remote default); `integrations.tracker` (`jira` or `beads`);
   `integrations.forge` (`github`, `bitbucket`, or null); and `local_runs` for
   unfinished, failed, stale, corrupt, or contradictory base and revision runs.

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" scrutinize-seat --workspace-root .
   ```

   A non-zero exit means seating failed; the posture, or stderr when the probe
   could not assemble one, names the failure. Resolve it before continuing.
   `--dry-run` previews without fetching or creating anything. A non-terminal entry
   in `local_runs` belongs to a session that has not finished: leave it alone, and
   surface it so the human can resume it directly (`FLOW <key>`).

2. **Orient locally.** Judge the posture against `integration_branch`, never
   `default_branch`. A primary checkout that is dirty, off the integration branch,
   or ahead of it is unsafe. A bench on a branch or with local changes is non-idle.
   An `integration_unresolved` reason makes the branch posture unsafe. Surface any
   such state and ask what to do. The seat script fast-forwards a clean, behind-only
   primary checkout and re-parks a clean, detached, behind-only bench only when
   `local_runs` is empty. It never resumes a run or discards a commit during seating.

3. **Report, then ask.** Report Git posture, configured tracker and forge, and any
   `local_runs`. Do not load seat memory, the ledger, tracker tickets, pull
   requests, CI, reviews, or comments. Do not suggest a menu or rank work before the
   human names a direction. End the initial seating response with exactly:
   “What would you like to do?”

After the human chooses a direction, load only the context needed for it. “My
tickets” calls `tracker list-assigned --filter open`; select from its compact key,
summary, status, and priority rows, then call `tracker get` only for the chosen
ticket. “My PRs” calls `forge list-authored --state open`; select from its compact
title, draft, update-time, and URL rows, then fetch CI, reviews, and comments only
for the chosen PR. Load relevant seat memory and ledger context only after the
choice. A ticket named after seating is the human's to run directly in a fresh
session; the seat does not run tickets, and a general queue scan still waits for
the human to ask for one.

## Pickup

The seat is the consumer of the machinery backlog: the `machinery`-labelled beads its own scrutiny minted (§Friction handling), alongside every other ready ticket. Between runs: read `bd ready` and triage with veto power. The triage and the sequencing are the seat's; the decision to start is the human's, so the queue is presented, not consumed (§Seating step 3), and a surviving machinery bead is the seat's to implement inline (§Friction handling) once the human names it; any other surviving ticket is handed back as a recommendation to run directly, never spawned, and routes through an ordinary session when the human starts it.

Triage is a filter against overengineering, not a queue pump. Before working any bead: was it witnessed more than once, or once with real cost? Is the lesson already recorded where its audience looks? Does the fix add standing surface a workaround avoids? Is the fix bigger than the lifetime cost of the friction? A bead that fails these is vetoed and closed, with the reasoning; a closed bead permanently blocks the dedup net from refiling it, so close only what should stay dead and defer the maybe-laters instead. When a bead survives, prefer deletion over a doc line over a new moving part. Group or merge related beads via `bd` parent links or close-as-dup with the surviving bead's scope widened, and group only on shared root cause or shared surface: one plan, one diff, one PR; grouping for tidiness manufactures scope. Vetoing is a first-class act, not a failure to act.

Dissolving a group means clearing its recorded cover set with `group-persist clear`, because §Merging closes a lead's covers with it, so a marker left behind closes a ticket that was deliberately kept open.

## The workbench

The seat never works on the main checkout: that tree is the workspace root, whose
`.flow/` holds the shared memory store, the ticket files, and the runtime facade, and
runs mint their worktrees off it and finalize from it, so it stays clean, on
its integration branch, only ever fast-forwarded, and never advanced while a run is
live. Inline work happens in one standing worktree, `.claude/worktrees/flow-bench`,
parked detached on the integration branch between tasks; every branch cuts fresh from
that ref there: `[create_pr] base` when the workspace declares one, the remote
default otherwise, named `integration_branch` in the seat posture. Run worktrees
belong to their runs; the seat never edits those. The janitor preserves the bench
automatically (no ticket ownership).

One hygiene rule survives from the live era, promoted on its second witness: the
seat's shell cwd silently snaps between checkouts mid-work, so prefix every
state-changing git command with an explicit `cd <tree> &&` and verify with `pwd` in
the same command. A control action is verified by its effect, never by the request.

## The sweep

The seat's observation is entirely post-hoc, and a bare `FLOW scrutinize` covers
every initialized flow workspace on this machine, not just the self-target. A
workspace argument (a name matched against the discovered roots' basenames, or a
path) scopes the scrutiny to that one workspace; only the scoped workspace's
cursor row advances, and an unknown or ambiguous name refuses with the candidate
list rather than guessing. Discover them by finding
`.flow/workspace.toml` under the human's project roots (the 2026-07-28 sweep's
whole-home find is the precedent); each workspace's evidence lives where its own
layout puts it, so resolve per workspace rather than assuming one shared root.
After runs finish (a drain, a morning pickup, or whenever the human asks), sweep
the durable evidence and synthesize. The sources, all passive and all survivable
past the run:

- **Session transcripts** under `~/.claude/projects/<workspace-slug>/`, keyed by the
  session's working directory (the slug moves with cwd, so resolve it fresh, never
  from a cached path). Mine them with the engine's miner rather than a fresh one-off
  (every seat before it rebuilt the same ~90-line script from scratch); it parses
  incrementally and never loads a transcript whole:

  ```bash
  FLOW_HARNESS="<harness>" "<facade>" scrutinize-trace \
    --transcript-dir "<abs transcript dir>" --since "<cursor iso>" [--session <id>] [--json]
  ```

- **Subagent transcripts** beside each session: `<session-id>/subagents/agent-*.jsonl`
  with a `.meta.json` naming each agent. Stage-agent wall clock lives HERE, not in the
  driver transcript (the driver only shows spawn and notification); the miner above
  joins the spans automatically. The 2026-08-03 assessor finding (11-15 min wrappers
  around a 2.4-min codex call) was only visible at this layer.
- **The friction log** (`friction.jsonl` in the workspace memory namespace): the
  run's own account of what bit it, and the join key for recurrence and
  fix-efficacy measures. Runs under-log: two identical missing-tool hits in one day
  left no entry, so treat the friction log as the floor and the transcripts' tool
  errors as the census.
- **Ship events** (frozen at finalize): per-run timings, tiers, and acceptance
  invariants; the timing backbone now that run directories are reaped.
- **The knowledge store** and `bd`: what reflect recorded and filed, the dedup net
  the seat must not race (§Synthesis).
- **The forge**: parked PRs, CI, review threads, for §Merging.

Never read a run directory for evidence: finalize reaps it, so anything worth
keeping is already in the stores above, and prose that cites a run directory
outlives its citation. Cite a ticket key or a merged commit instead.

**Heal close-out before measuring.** Ship events freeze at finalize, so a merged run
nobody finalized is a hole in the timing backbone the lenses below read (witnessed
2026-08-03: two merged deliveries invisible to `metric time-to-pr`, worktrees and
claims still live). Before the lens work, run the finalize sweep on the scoped
workspace and relay its receipts; it writes only behind merged-PR proof, reports
still-parked tickets without touching them, and skips ad-hoc worktrees that carry no
run. Runs mid-flight hold their leases, so the sweep leaves them alone by
construction.

```bash
FLOW_HARNESS="<harness>" "<facade>" finalize --workspace-root "<workspace>" --all
```

The tracker adapter reads credentials from the process environment, and a delivery workspace usually keeps them in its own `.env` (drivers inherit them; the seat's shell does not), so source that file into the sweep's environment first or every tracker step fails on the factory error. Finalize refuses the destructive steps when the ship event did not freeze, so the failure is loud and a re-run with the credentials converges; still, capture the full JSON report to a scratch file instead of piping through `head`, because the per-key `steps` block is the only record of what each close-out actually did (witnessed 2026-08-04: a truncated report hid two failed tracker steps until the ship-event hole surfaced in the metric).

**The sweep cursor bounds how far back to read.** `sweep-cursor.json`, kept beside
the ledger in the project memory directory, holds one row per workspace root with
the ISO timestamp its evidence was last swept through. A sweep reads each
workspace's evidence newer than its row and advances that row only after the
sweep's outputs are durable (beads minted, ledger line written), never before: a
crashed sweep re-reads its window, and re-reading is idempotent because minting
dedups on the file-anchored keys. Advance a workspace's row only when its evidence
was actually read; a workspace the sweep skipped or failed on keeps its old row, so
nothing is silently dropped. No row yet means a bounded default window (the last
seven days), never all of history. The cursor is seat state on the seat's
side; no workspace carries it.

**The lens roster.** The sweep's looking is a checklist, not vibes: `references/scrutinize-lenses.md` is the registry of every lens (close-out integrity, engine freshness, call efficiency and error census, env/credential, review-bot patterns, recall efficacy, cross-run contention, performance, nudge, lane watch), each with its sources, its mechanical recipe, and the checks a real catch earned it. The seat mines the window once with `scrutinize-trace`, then runs every lens whose sources have content through parallel read-only gather agents (sonnet hint, one per lens) that return findings in the registry's shared schema; a lens with empty sources is skipped and the skip is reported. Gather agents summarize evidence and suggest routing; they never mint, never write, and never carry seat judgment. The roster grew out of the 2026-08-04 rounds, where the biggest findings (a lost ship event, a stale installed pin, an undocumented evidence schema) sat in exactly the evidence no named lens covered.

## Friction handling

What the sweep observes routes by one hierarchy, and beads are minted here and nowhere else: reflect records, the seat files. A bead requires a delivery-workspace witness, or a second independent witness with real cost. Friction witnessed only by a self-target run is flow examining itself and never becomes backlog on its own; it gets a ledger line or a knowledge entry and waits for its delivery witness. Friction that clears the bar gets a bead always, because beads are what the `friction_recurrence` and fix-efficacy measures join against, and a silent fix starves the measurement loop. A single-witness papercut gets a ledger line instead; the second witness promotes it. Before minting, dedup against reflect's `MACHINERY:` entries and existing beads with the file-anchored keys; one defect, one bead. Mint with the shared recipe, carrying the entry's evidence and its dedup anchor:

```bash
FLOW_HARNESS="<harness>" "<facade>" flow-beads-create \
  --workspace-root . \
  --summary "<the finding title>" \
  --description "<the MACHINERY entry body + the file:line evidence + the witness that cleared the bar>" \
  --type chore --labels machinery \
  --dedup-key "<primary-relfile>::<short-symptom>"
```

Exit 0 prints the bead key; exit 5 means a bead for this finding already exists (reference it, never refile); a finding marked HOT in its entry adds `hot` to `--labels` so the bead rides the high-scrutiny lane and the merge-time guard-property review.

Beads are not the only dedup surface. Before minting, also sweep `git log origin/main --since=<window start>` for a fix that already landed: an inline fix can land the same day as the friction it answers (witnessed 2026-07-31, PR #619 shipped the bkt preflight three hours before a sweep minted its duplicate), and the local beads DB can lag or carry holes. A finding whose fix is already on main gets a ledger line crediting the PR, never a bead.

A finding is a claim about the present, not the past. Before reporting or minting anything, re-check that the situation a run recorded still holds: probe the machine, since the binary, daemon, or credential that was missing may have been installed since (witnessed 2026-07-31, a run's no-container-runtime friction was answered by a Rancher install and the finding was stale by sweep time); read main's log for a landed fix; re-read the workspace config for a changed wiring. A finding whose trigger no longer exists is reported as resolved with the evidence of its resolution, never filed: stale findings cost the human a veto each and teach the queue to be ignored.

Who implements: the seat, inline, on the bench, always. Machinery work the seat files or picks up is never routed through a flow pipeline run (standing directive, 2026-07-31): pipeline runs are for delivery tickets, and the seat owns its own backlog end to end, carving the branch, running the gates, opening the PR, and merging under §Merging, hot diffs included via the guard-property review. An **obvious** improvement goes in one motion with no bead unless it was friction-shaped; the PR is the record. Judgment-shaped work does not skip scrutiny, it relocates it: settle the design choices with the human in conversation first (present the alternatives, get the ruling), then ship the same inline way. Never touch files a live run snapshots.

## Merging

**Standing merge authority (maintainer-granted):** a parked PR whose gate is met, meaning CI green and review clean, is merged by the seat, hot PRs included; for a hot PR the seat first executes the guard-property review below; the human gets a notification, never a question. Plan approval stays human; merge on a met gate is delegated. The seat exists only in flow's own repo; in delivery workspaces every merge is the human's.

**Guard-property review (hot diffs only).** A `hot` diff touches a guard or safety-machinery file. Hot merges serialize: the seat lands at most one hot diff at a time and re-checks CI between them; that isolation, plus this review, plus green is the hot path VISION.md names. A human merging a hot PR by hand owns the same check personally. Spawn one fresh reviewer agent that did not write the change and prompt it to refute:

> Review this PR diff for the flow self-target. Question: does it DELETE or WEAKEN any safety property: lease exclusivity (one run per ticket), snapshot drift-detection, atomic-write + corrupt-file quarantine, content-ownership refusal, or self-edit flock serialization? Guard *code* may be refactored/sped up freely; a guard *property* may only be replaced by a provably-equivalent one, never dropped. Default to "property removed" when uncertain. Return a verdict: `{property_removed: bool, which: str, why: str}`.

`property_removed: true` → do NOT merge; post a PR comment naming the property and leave the PR for the human. Only a clean review (`property_removed: false`) merges. The reviewer has no write or merge authority.

**Merge rules:**

- Verify branch push state first: an uncommitted change to a tracked file, an unpushed commit, or a remote branch already deleted means do not merge; resolve it or hand the PR to the human. Untracked scratch never counts.
- When the run's workspace configures a review brief, verify its freshness before any other gate and block the merge while it is stale or missing (`stage-review_brief.md` owns the render). Exit 1 is the refusal and exit 0 covers both `current` and `disabled`, so a workspace with no `review_brief` stage wired passes this gate legitimately rather than silently. Read the exit code, and read `.status` when you need to tell those two apart:

  ```bash
  FLOW_HARNESS="<harness>" "<facade>" review-brief freshness \
    --workspace-root . --ticket-dir "<ticket-dir>" --pr-id "<pr>"
  ```

- A `DIRTY` PR is a genuine code conflict; leave it for the human. A CLOSED-but-not-merged PR is never auto-handled.
- Mark a draft ready before merging; merge without squash (this repo keeps merge commits). Merges never stamp the version; the server-side `version-stamp.yml` Action stamps `main` after the merge lands.
- A grouped lead's covers close with it: after the merge, close every key in the lead's `covers` frontmatter through the tracker seam and drop the `bd dep` suppression edge, so a covered sibling never re-surfaces in `bd ready`. Best-effort, like the lead close.
- Close order: merge first, then finalize; the bead close is bookkeeping, never what makes the merge safe.

**Delivery closes the merge (standing duty, 2026-07-31).** Landed engine changes reach sessions only through the installed plugin, so after the seat's merges settle and the server-side stamp lands on main, the seat runs `claude plugin marketplace update vdsmon-flow` and then `claude plugin update flow@vdsmon-flow`: the marketplace update refreshes the checkout that workspace skill-root pins resolve through, and only the plugin update advances the installed pin that decides which SKILL.md, references, and agent registry a fresh session loads. Verify by effect, never by an update command's success line, and never by `claude plugin details` (it reports the running session's own version and can hang in-session): the flow entry in `~/.claude/plugins/installed_plugins.json` must carry the version main's plugin.json carries, with its `installPath` naming that version's cache directory, and that directory must exist. The checkout-plus-cache check alone has already passed while sessions kept loading a pin three versions stale; the cost was a delivery driver running a retired plan gate until the human nudged it, and a seat misreading the stale version as the known in-session limit (2026-08-04, FT-1499). A stale pin is the seat's defect to fix before the scrutiny ends; a stale registry has also cost a session its agent types (2026-07-29). State the limit honestly when reporting: a session compiles its skill text and agent registry at start, so already-open sessions keep both until restarted, and a live run stays sealed to the engine pinned at its start either way.

## The ledger

The seat's ledger lives outside the repo in the project memory directory, durable
across seats: timestamped notes on merges, vetoes, surprises,
delegations, and single-witness papercuts. The papercut record is what lets a later
seat promote on the second witness instead of restarting the count. At each
scrutiny's end the seated session compacts it: the play-by-play moves to a dated
archive file beside the ledger, and the working ledger keeps one line per run plus
the papercut register. Archive-then-compact preserves the full history while
keeping the working file small enough to read at every pickup; an uncompacted
ledger grows past what a successor can afford to load, which defeats its purpose.

## Synthesis

Reflect records; the seat synthesises. Both are needed and neither substitutes for the other.
A run's reflect writes at maximum context, with the evidence still on disk, and its
`flow-beads-create` recipe mints the `evid:` and `evidfile:` labels that make a finding checkable
later. A seat reading evidence after the reap cannot produce either. So the division is by
altitude, not by ownership.

**Runs record; only the seat files.** Reflect leaves `MACHINERY:` entries and friction events
behind; the sweep is what turns the qualifying ones into beads (§Friction handling owns the
witness bar). One minting seat plus the file-anchored dedup keys is what keeps one defect one
bead; sweep after the runs have finished, never while one is writing.

What only the outside view sees, and what the seat therefore owns:

- **Cross-run pairs.** Two runs whose separate records only mean something together. One run
  sees its own pin drift; two runs drifting into each other's worktrees is contention, and
  neither run can see it alone.
- **Repeated shapes.** The same class of defect surfacing in unrelated tickets. A single instance
  is a bug; the fourth is a property of the system, and only the seat holding all four can say so.
- **Promotion on the second witness.** A single-witness papercut stays in the ledger. The second
  witness, usually from a different run, is what earns it a rule in `AGENTS.md` or a bead. Holding
  the line at one witness is what keeps the gotcha lists worth reading.

The sweep's output follows one pipeline, every round, so a successor seat produces the same artifacts:

1. **Gather.** The lens agents return schema-bound finding fragments (`references/scrutinize-lenses.md` owns the schema and the agent contract).
2. **Triage.** The seat merges the fragments, dedups against existing beads AND `git log origin/main --since=<window start>`, probes each claim's present-tense validity, and vetoes. Judgment is never delegated to a gatherer: an agent's `suggested_routing` is input, the §Friction handling hierarchy is the law.
3. **Report.** One ranked register to the human, each finding classified ground truth vs judgment per the repo-root VISION.md's operating line; verified-fixes and expected-stales are reported with their evidence, never filed.
4. **Round plan.** The findings that survive triage become the round's plan in one artifact: an ordered build list for machinery findings (one bead, one commit each, the PR #625 shape), ruling requests for judgment-shaped items with the alternatives stated, and a watch list for single-witness papercuts entering the ledger.
5. **Proposals.** Non-defect improvements ride the same register as `class: proposal` and face the §Pickup overengineering filter at triage; a proposal that survives joins the build list or the ruling list like any finding.

Machinery-shaped findings file through the `flow-beads-create` recipe with file-anchored dedup keys. Sweep when runs have drained, not per run: the patterns are not visible until the runs that carry them have finished.
