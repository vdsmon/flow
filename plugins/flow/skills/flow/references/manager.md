# The manager

Run a flow ticket through a driver agent spawned from a long-lived main session (the manager), which observes friction from outside while the plan-approval keystone stays human. The driver runs the ordinary skill unmodified; the manager adds an outside view (timings, retries, stalls) that the run's own reflect stage cannot see about itself, and owns the work queue around the runs: triage, grouping, sequencing, and merges on met gates. Owning the queue means judging it and recommending from it, not starting work the human did not ask for. The triage and merge authority is maintainer-granted rather than witnessed.

## Seating the manager

`FLOW manager` routes here: it seats the invoking session as the manager. The manager
is a role, not a process: its continuity lives in this charter, the
project memory (the manager entries and the durable ledger), and the tracker — not in
any one session. A successor
manager inherits everything a predecessor ledgered; nothing is handed off
conversationally. Outside the self-target workspace the observation and relay duties
apply unchanged, but the merge authority and machinery-bead pickup do not — the
human-merge keystone holds there.

Seating runs a mechanical half and a judgment half, in this order:

1. **Posture.** Run the seat script. It fetches origin, resolves the remote default
   branch **and the integration branch**, ensures the standing bench worktree exists
   (§The workbench), reads configured integration names without constructing their
   adapters, and scans registered worktrees for non-terminal local runs. The JSON
   posture includes primary-checkout and bench Git state; `default_branch`;
   `integration_branch` (`[create_pr] base` when configured, otherwise the remote
   default); `integrations.tracker` (`jira` or `beads`);
   `integrations.forge` (`github`, `bitbucket`, or null); and `local_runs` for
   unfinished, failed, stale, corrupt, or contradictory base and revision runs.

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" manager-seat --workspace-root .
   ```

   A non-zero exit means seating failed; the posture — or stderr, when the probe
   could not assemble one — names the failure. Resolve it before continuing.
   `--dry-run` previews without fetching or creating anything.

2. **Orient locally.** Judge the posture against `integration_branch`, never
   `default_branch`. A primary checkout that is dirty, off the integration branch,
   or ahead of it is unsafe. A bench on a branch or with local changes is non-idle.
   An `integration_unresolved` reason makes the branch posture unsafe. Surface any
   such state and ask what to do. The seat script fast-forwards a clean, behind-only
   primary checkout and re-parks a clean, detached, behind-only bench only when
   `local_runs` is empty. It never resumes a run or discards a commit during seating.

3. **Report, then ask.** Report Git posture, configured tracker and forge, and any
   `local_runs`. Do not load manager memory, the ledger, tracker tickets, pull
   requests, CI, reviews, or comments. Do not suggest a menu or rank work before the
   human names a direction. End the initial seating response with exactly:
   “What would you like to do?”

After the human chooses a direction, load only the context needed for it. “My
tickets” calls `tracker list-assigned --filter open`; select from its compact key,
summary, status, and priority rows, then call `tracker get` only for the chosen
ticket. “My PRs” calls `forge list-authored --state open`; select from its compact
title, draft, update-time, and URL rows, then fetch CI, reviews, and comments only
for the chosen PR. Load relevant manager memory and ledger context only after the
choice. A ticket named after seating goes straight to the managed driver path
without a general queue scan.

A ticket handed to a seated manager — `FLOW <target>` or plain words — runs through
the managed topology: the manager spawns the driver (§Spawn) rather than becoming
it, because a manager driving its own ticket loses the outside view. A session that
should drive directly is a fresh one, invoked with the target before any seating.

## Roles

- **Manager**: the main session. Spawns drivers, relays the gates, observes passively, and compiles the friction report; it also triages the queue with veto power, groups/merges tickets, amends plans at the relay (labeled as manager feedback, which the driver treats as revision input), sequences runs — recommending what to start rather than starting it (§Seating step 3) — and merges on a met gate. It never edits the run's files and never interrogates the driver mid-stage — questioning a working driver perturbs the thing being measured.
- **Driver**: a named teammate agent running the flow skill exactly as written, with the gate relays below as its only environment delta.

## Pickup — the queue is the manager's

The manager is the consumer of the machinery backlog: the `machinery`-labelled beads that `stage-reflect.md`'s filing recipe produces, alongside every other ready ticket. Between runs: read `bd ready` and triage with veto power. The triage and the sequencing are the manager's; the decision to start is the human's — the queue is presented, not consumed (§Seating step 3). Route each chosen ticket through a managed run below.

Triage is a filter against overengineering, not a queue pump. Before working any bead: was it witnessed more than once, or once with real cost? Is the lesson already recorded where its audience looks? Does the fix add standing surface a workaround avoids? Is the fix bigger than the lifetime cost of the friction? A bead that fails these is vetoed — closed, with the reasoning; a closed bead permanently blocks the dedup net from refiling it, so close only what should stay dead and defer the maybe-laters instead. When a bead survives, prefer deletion over a doc line over a new moving part. Group or merge related beads via `bd` parent links or close-as-dup with the surviving bead's scope widened, and group only on shared root cause or shared surface — one plan, one diff, one PR; grouping for tidiness manufactures scope. Vetoing is a first-class act, not a failure to act.

Dissolving a group means clearing its recorded cover set with `group-persist clear`, because §Merging closes a lead's covers with it, so a marker left behind closes a ticket that was deliberately kept open.

## Spawn

Spawn the driver as a named teammate with the ticket key and an absolute workspace root, and state the harness selector explicitly in its prompt. The driver's model is the manager's call at spawn, recorded in the ledger: opus by default (the driver plans, and planning gates everything downstream), the manager's own model for a hot or unusually complex ticket, never below opus for a driver, because the cheap-tier savings belong to the in-run worker roles `[models]` already governs, not to the seat that authors the plan. The driver's own workers (implement, review, assessment) stay unnamed subagents; the skill's native-agent roles work unchanged that way. Naming them is the obvious-looking fix and the wrong one: a *named* spawn is coerced into the asynchronous teammate model regardless of `run_in_background: false`, and its result never returns to the spawner (probed directly; the host's own tool description does not state this, so re-probe rather than trust this line if the host changes). Each worker's own first spawn, unnamed, is the only call that returns synchronously, so that is the shape stage workers keep.

The manager also decides at spawn whether the plan assessment runs at all, and says so in the
prompt. A ticket that is one defect, one call site, one test does not need it: the assessment
answers whether this is the right thing to build, and a ticket with one obvious shape has nothing
to be wrong about there. Its defects live in the diff, where code review reaches them. Skip it and
say why; when the ticket carries a design choice or two plausible shapes, keep it. A guard file is
deliberately not one of those triggers. It states blast radius, which the full verification lane
and the merge-time guard-property review already price, and an assessor holding no diff cannot
check a safety property in the first place. Keep the triggers that ask whether this is the right
thing to build, and leave the one that asks whether it is dangerous to the stages holding the code.

Synchronous return covers that first spawn and nothing after it. There is no synchronous resume: `SendMessage` to an idle agent offers no such option and always restarts it in the background. Capture the raw agent id from the spawn result at dispatch, because an unnamed agent has no other handle. Because the assessment must continue the *same* assessor across a confirm pass, pass 1 returns to the driver and any later pass routes its completion to the manager instead. On a multi-pass loop the relay below is the normal path rather than the exception, so tell the driver to expect it.

## The workbench

The manager never works on the main checkout: that tree is the workspace root — its
`.flow/` holds the shared memory store, the ticket files, and the runtime facade;
drivers mint their worktrees off it and finalize runs from it — so it stays clean, on
its integration branch, only ever fast-forwarded, and never advanced while a run is
live. Inline work happens in one standing worktree, `.claude/worktrees/flow-manager`,
parked detached on the integration branch between tasks; every branch cuts fresh from
that ref there: `[create_pr] base` when the workspace declares one, the remote
default otherwise, named `integration_branch` in the seat posture. Driver runs keep
their own per-ticket pool worktrees; the manager never edits those. The janitor
preserves the bench automatically (no ticket ownership).

## The manager's own hygiene

Four obligations on the manager's side, each from a witnessed failure or near-failure. Two of them, the stall watch and the release, share one shape, and it is the shape to watch for: a control or observation action that fails open, looking like success from the manager's seat while doing nothing at all. Verify that a control action took effect; the request is not the effect.

- **Push a notification the moment a gate arrives.** Keystone waits include minutes-to-hours of discovery latency, because the human is not looking when the plan or ask-user relay lands. The manager sends a host push notification (with the gate type and one-line summary) at every gate, in addition to the in-conversation relay.
- **Run a stall watch on the whole subtree, never on the driver alone.** A 62-minute stall was caught only because the human happened to ask for status. But a driver blocked on a synchronous stage child writes nothing to its own transcript, and that is the normal condition of every stage of every run, so watching the driver's own mtime reports healthy work as a stall: it fired on driver-azbx at 638s idle while a live child of that driver was writing at that instant. The wrong predicate fails both ways, noisily first and then silently, because a watch you have learned to ignore is a watch that is not running. A driver is stalled only when it and every descendant are quiet, so alert on the newest mtime anywhere in the subtree. Build the parent map from `parentAgentId` in each `agent-<id>.meta.json` under the session's `subagents/` directory and take recency from each `agent-<id>.jsonl`, rebuilding it every poll so children spawned mid-run are counted. Watch an explicit list of live driver ids, because `meta.json` carries no status field, so a scan of every root reports every finished agent as stalled. `parentAgentId` is recorded but honored nowhere else the manager sees, not in the roster and not in completion routing, and this predicate is its one working consumer. No runnable recipe is given here on purpose: earlier drafts each shipped a new way to fail open, so it is being designed once, with tests, in its own ticket. Until it lands, run your own and hold it to the three rules those drafts broke: one driver alerting must never end coverage of the others silently; a missing transcript means unknown rather than quiet; and a watched id with no `meta.json` at all never existed, so fail loud instead of treating it as unknown.
- **Release an agent with `TaskStop`, not with words.** A message telling a driver to stand down only asks. The driver goes idle but stays alive, holding a roster slot and emitting idle notifications that route to the manager and on to the human; witnessed on driver-4tk0, whose pings the manager kept dismissing as stale after it believed it had released it. `TaskStop` takes the bare teammate name or the `name@team` agent id. Terminate a driver when its run reaches done, and at a durable stop only once you know the run will not be resumed from that session.
- **Spawn from the template below**, substituting only the bracketed values, because two hand-written spawn prompts already drifted from each other.

```text
You are a flow driver session running under references/manager.md (read it).
Task: run the flow ticket [KEY] through the complete flow pipeline in the
workspace [ABSOLUTE_ROOT] (an initialized flow workspace). FLOW_HARNESS is
[HARNESS]. Invoke the flow skill now and follow it exactly as written,
including the entry contract and the skill-root re-pin rule.
Environment facts, all witnessed in prior managed runs:
- Plan assessment: [ASSESSMENT] (either "run one assessment" or
  "SKIP the assessment, this ticket is one defect / one call site / one
  test"). The gate is zero blockers; there is no score to compute or report.
- Plan gate: turn-boundary form. Render the plan surface AND send the
  complete plain-text plan (exact text, base SHA, whether the assessment was
  skipped or a replacement assessor was used, resolved findings, residual
  risks) to "main", then stop and wait. Approval arrives as a message
  containing APPROVED, or revision feedback. Nothing mutates before it except
  the planning-start ticket claim.
- ask-user findings: relay to "main" the same way and wait.
- Spawn stage agents synchronously where you can (unnamed subagents).
  Known limit: there is NO synchronous resume, so every assessor pass
  after the first is backgrounded and its completion routes to me, not to
  you. Expect my relay. If you poll a resumed child, poll ASSISTANT-ROLE
  messages only: grepping the raw JSONL matches your own prompt text and
  returns instantly, looking like success.
- PAUSE after each assessor verdict before dispatching the next pass. A
  relay often lands in exactly that window.
- Machinery APPLY-NOW is unavailable here (protected-branch skill root):
  lens-B findings route to propose-and-record; the refusal is the known
  limit, not a failure.
- Write-tool confinement: a session's Edit/Write binds to its PINNED
  worktree, not its working directory, and the pin can migrate to another
  live run's worktree on any call, including after successful writes to the
  same path. A refusal naming a worktree that is not your run root is this
  defect, not a path mistake. Read the worktree the refusal names, because
  it selects the remedy. Named a sibling worktree: re-pin through the host's
  worktree switch (on Claude Code, EnterWorktree with an explicit path to
  the run root), which is reliable for a driver session; moving the shell
  cwd does nothing here. Named the repository root instead: that is the
  other launch shape, where the switch can refuse and moving the shell cwd
  into the run root is the fix. A stage subagent is pinned at spawn, cannot
  re-pin, and should return BLOCKED at once rather than fight it, because
  you have a documented takeover: re-pin yourself and run that stage inline
  under the delivery loop's downgrade. Warn your agents that the switch can
  report SUCCESS to a subagent and still leave the write refused, so its
  return value proves nothing; only attempting the write does. Brief your
  own stage agents on all of this in their prompts, and log it as friction.
Work autonomously otherwise; report at natural stops. After done or a
durable stop, send "main" the final status, PR URL, per-stage outcomes, and
anything that differed from what manager.md led you to expect.
```

## Relays — the manager's standing obligations

- **Plan gate.** The driver uses the turn-boundary gate form, the one SKILL.md gives Codex when native Plan mode is inactive; an agent-hosted driver has no plan mode of its own, and SKILL.md's Claude Code row assumes the top-level session: present the complete plan (exact text, base SHA, whether the assessment was skipped or a replacement assessor was used, resolved findings, residual risks) to the manager and wait. The plan surface is still owed under this topology when its gate passes (`plan-surface.md` makes skipping a defect): the driver renders it AND sends the complete plain-text plan, so the gate never depends on the surface being opened, and the manager relays the surface URL prominently to the human, because without that push the surface degrades unused, since the human talks to the manager, not the driver. The manager relays verbatim and returns the approval or revision feedback, appending its own amendments when it has them, labeled as manager feedback, which the driver treats as revision input. Plan approval never originates from an agent, with one recorded exception: the human may delegate approval for a named run, explicitly and in-conversation; the manager then reviews with full scrutiny, approves in their stead, states the delegation in the approval message, and ledgers it, after which a driver may honor an APPROVED that cites such a delegation. Under this topology the message relay is the authoritative convergence path: the plan surface still renders for the human's later review, and the driver ends its surface session by hand rather than waiting on the surface's own signal.
- **ask-user findings.** Same relay, both directions, verbatim.
- **Child completions.** A BACKGROUNDED subagent's completion notification routes to the top-level session, not to the spawning teammate, so the driver sleeps through its child finishing (one run lost 62 minutes to exactly this). Only an unnamed first spawn avoids the problem (§Spawn); a resumed child, which is every assessor pass after the first, always routes its completion here, so on a multi-pass loop the relay is the normal path rather than the fallback. The manager relays that completion (verdict summary plus where the full result lives), or the driver polls the child's transcript. Poll a monotonic property, such as the count of assistant-role messages above a baseline captured at dispatch. Filtering to assistant-role messages is necessary, because the spawning prompt sits in the child's own transcript as a `user`-role message and a raw grep matches the driver's own words; it is not sufficient, because a content marker naming the next pass can already appear in the previous one. The failure is the same either way: a poll that matches what is already there returns at once and looks like success. A poll can also observe the transcript absent; missing must mean unknown and retry, never quiet, and resolve from the session id each poll rather than caching a path, because the session's directory is keyed by its current working directory and moves with it. A relay of an assessor result carries every blocker verbatim, never just the assessor's English verdict, because a verdict summary is the more persuasive artifact and the blockers are the gate. Relays can arrive stale; when a driver's own status report and a child-completion relay conflict, the driver's report is fresher and wins.
- **Close the relay window from both ends.** The driver holds after each assessor verdict and before dispatching the next pass. The manager, before relaying, checks whether a pass is in flight and either waits for the verdict or marks the message explicitly as mid-pass, so the driver does not have to infer it; that check reads transcript recency passively, treating a recently-written assessor subtree as a pass possibly in flight, and never asks the driver mid-stage, which §Roles forbids. Neither rule alone is enough: on the very run that added them, the driver held exactly as prescribed, dispatched, and the relay still landed mid-pass. Together they shrink the window rather than closing it, because a relay composed while a pass is idle can still land after a dispatch. So a driver must be able to absorb a mid-pass relay by letting the in-flight pass return its verdict and revising after it, never by editing the plan file under a reader.

## Observation channels (all passive)

- **state.json** in the run worktree: per-stage `started_at_iso`/`finished_at_iso` are the timing backbone. A force-reset nulls a stage's timestamps; the rotating backups keep the prior snapshot.
- **The driver transcript** (session JSONL): parse incrementally at driver stops for tool errors, retries, and time gaps — never load it whole. The deleted transcript miner is restorable from history for a deeper pass (`git show 0bed292^:plugins/flow/skills/flow/scripts/trace_mine.py`); it extracts tool errors, silent retries, drift markers, and stall gaps bucketed by dispatch stage, and runs unchanged on a teammate transcript once the file is copied under the workspace's `~/.claude/projects` slug (its path guard requires that layout).
- **A manager ledger** kept outside the repo, in the project memory directory, append-only and durable across manager sessions: timestamped notes on gates, stalls, surprises, delegations, and single-witness papercuts — the papercut record is what lets a later manager promote on the second witness instead of restarting the count. Cross-referencing the ledger against mined stall gaps is what separates human-wait (keystone cost) from machine friction — the miner alone cannot tell them apart.

## Known limits of the managed topology

Machinery APPLY-NOW is structurally unavailable: the driver's `skill_root` resolves to the marketplace clone, which sits on a protected branch, so `machinery_edit` refuses (exit 2) and every lens-B finding routes to propose-and-record. State this expectation in the driver's prompt so the refusal is read as the known limit, not a failure.

## Friction handling

What the manager observes routes by one hierarchy. Friction that bit a run gets a bead — always — because beads are the ledger `friction_recurrence` and the fix-efficacy measure join against; a silent fix starves the measurement loop. A single-witness papercut gets a ledger line in the run report instead; the second witness promotes it to a bead. Friction inside a run belongs to the run's own reflect first — the manager files only what the outside view sees (stalls, relay latency, cross-run patterns), and the dedup net converges the two producers.

Who implements: an **obvious** improvement is implemented fully, in one motion — branch, gates, PR, merge under §Merging — with no bead unless it was friction-shaped; the PR is the record. Obvious means no alternatives worth weighing; the moment there are, it is judgment and routes to a proposal or a managed run. Judgment-shaped, hot, or large work always goes through the pipeline with the plan gate. Never touch files a live run snapshots, and never perturb a working driver to fix friction live — observe, ledger, act after the run.

## Merging

**Standing merge authority (maintainer-granted):** a parked PR whose gate is met, meaning CI green and review clean, is merged by the manager, hot PRs included; for a hot PR the manager first executes the guard-property review below; the human gets a notification, never a question. Plan approval stays human; merge on a met gate is delegated. This holds only in flow's own self-target repo, because in delivery workspaces the human-merge keystone holds.

**Guard-property review (hot diffs only).** A `hot` diff touches a guard or safety-machinery file. Hot merges serialize: the manager lands at most one hot diff at a time and re-checks CI between them — that isolation, plus this review, plus green is the hot path VISION.md names. A human merging a hot PR by hand owns the same check personally. Spawn one fresh reviewer agent that did not write the change and prompt it to refute:

> Review this PR diff for the flow self-target. Question: does it DELETE or WEAKEN any safety property — lease exclusivity (one run per ticket), snapshot drift-detection, atomic-write + corrupt-file quarantine, content-ownership refusal, or self-edit flock serialization? Guard *code* may be refactored/sped up freely; a guard *property* may only be replaced by a provably-equivalent one, never dropped. Default to "property removed" when uncertain. Return a verdict: `{property_removed: bool, which: str, why: str}`.

`property_removed: true` → do NOT merge; post a PR comment naming the property and leave the PR for the human. Only a clean review (`property_removed: false`) merges. The reviewer has no write or merge authority.

**Merge rules:**

- Verify branch push state first: an uncommitted change to a tracked file, an unpushed commit, or a remote branch already deleted means do not merge — resolve it or hand the PR to the human. Untracked scratch never counts.
- When the run's workspace configures a review brief, verify its freshness before any other gate and block the merge while it is stale or missing, and have the driver re-render at the current SHA (`stage-review_brief.md` owns the render). Exit 1 is the refusal and exit 0 covers both `current` and `disabled`, so a workspace with no `review_brief` stage wired passes this gate legitimately rather than silently. Read the exit code, and read `.status` when you need to tell those two apart:

  ```bash
  FLOW_HARNESS="<harness>" "<facade>" review-brief freshness \
    --workspace-root . --ticket-dir "<ticket-dir>" --pr-id "<pr>"
  ```

- A `DIRTY` PR is a genuine code conflict; leave it for the human. A CLOSED-but-not-merged PR is never auto-handled.
- Mark a draft ready before merging; merge without squash (this repo keeps merge commits). Merges never stamp the version — the server-side `version-stamp.yml` Action stamps `main` after the merge lands.
- A grouped lead's covers close with it: after the merge, close every key in the lead's `covers` frontmatter through the tracker seam and drop the `bd dep` suppression edge, so a covered sibling never re-surfaces in `bd ready`. Best-effort, like the lead close.
- Close order: merge first, then finalize — the bead close is bookkeeping, never what makes the merge safe.

## Report and filing

After the run: a per-stage wall-clock table, the mined event summary, qualitative observations, and ranked suggestions, each classified ground truth vs judgment per the repo-root VISION.md's operating line. Machinery-shaped findings file through the `flow-beads-create` recipe that `stage-reflect.md` owns, with file-anchored dedup keys — the run's own reflect files independently, and the dedup net keeps the two producers from double-filing. After the merge (manager or human), close the ticket with the finalize recipe `command-ticket.md` owns, run from the primary checkout.

Cross-run pattern detection is where the outside view beats per-run reflect outright: the same hiccup seen twice files once, pre-deduplicated, with two witnesses.

## Synthesis

Reflect records; the manager synthesises. Both are needed and neither substitutes for the other.
A driver's reflect writes at maximum context, with the evidence still on disk, and its
`flow-beads-create` recipe mints the `evid:` and `evidfile:` labels that make a finding checkable
later. A manager reading a relay summary cannot produce either. So the division is by altitude,
not by ownership.

**The manager files nothing a driver reported.** A relayed finding goes back to that driver's
reflect, which has the dedup net; filing it directly races that net and loses, because two
producers with no shared index converge on duplicates. Dedup AFTER reflect has run, not before.

What only the outside view sees, and what the manager therefore owns:

- **Cross-run pairs.** Two runs whose separate records only mean something together. One driver
  sees its own pin drift; two drivers drifting into each other's worktrees is contention, and
  neither run can see it alone.
- **Repeated shapes.** The same class of defect surfacing in unrelated tickets. A single instance
  is a bug; the fourth is a property of the system, and only the seat holding all four can say so.
- **Promotion on the second witness.** A single-witness papercut stays in the ledger. The second
  witness, usually from a different run, is what earns it a rule in `AGENTS.md` or a bead. Holding
  the line at one witness is what keeps the gotcha lists worth reading.

Run this when a fleet drains, not per run: the patterns are not visible until the runs that
carry them have finished.
