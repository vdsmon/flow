# The manager

Run a flow ticket through a driver agent spawned from a long-lived main session (the manager), which observes friction from outside while the plan-approval keystone stays human. The driver runs the ordinary skill unmodified; the manager adds an outside view — timings, retries, stalls — that the run's own reflect stage cannot see about itself, and owns the work queue around the runs: triage, grouping, sequencing, and merges on met gates. Proven end-to-end in the flow-4vre pilot (PR #566) and refined by the flow-rgqm run (PR #568); every constraint in Roles, Relays, and Observation was witnessed in one of the two, not designed speculatively. The triage and merge authority is maintainer-granted (2026-07-28) rather than witnessed.

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
   branch, ensures the standing bench worktree exists (§The workbench; created
   detached at the remote default when absent, and an existing bench is never
   mutated), and emits a JSON posture: primary-checkout branch, cleanliness, and
   distance from the remote default; bench state; fetch result.

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" manager-seat --workspace-root .
   ```

   A non-zero exit means seating failed; the posture — or stderr, when the probe
   could not assemble one — names the failure. Resolve it before continuing.
   `--dry-run` previews without fetching or creating anything.

2. **Orient.** Read this charter, the project memory's manager entries, and the
   durable ledger, then judge the posture: a primary checkout that is dirty, off the
   default branch, or ahead of the remote default violates the workbench contract
   below and goes to the human before anything else; behind-only is a fast-forward
   the manager performs itself when no run is live. A bench parked mid-task (on a
   branch, or dirty) is in-flight inline work — resume it or park it deliberately,
   never blindly.

3. **Queue.** Read `bd ready`, triage under §Pickup, and act under the authorities
   below.

A ticket handed to a seated manager — `FLOW <target>` or plain words — runs through
the managed topology: the manager spawns the driver (§Spawn) rather than becoming
it, because a manager driving its own ticket loses the outside view. A session that
should drive directly is a fresh one, invoked with the target before any seating.

## Roles

- **Manager**: the main session. Spawns drivers, relays the gates, observes passively, and compiles the friction report; it also triages the queue with veto power, groups/merges tickets, amends plans at the relay (labeled as manager feedback, which the driver treats as revision input), sequences runs, and merges on a met gate. It never edits the run's files and never interrogates the driver mid-stage — questioning a working driver perturbs the thing being measured.
- **Driver**: a named teammate agent running the flow skill exactly as written, with the gate relays below as its only environment delta.

## Pickup — the queue is the manager's

The manager is the consumer of the machinery backlog: the `machinery`-labelled beads that `stage-reflect.md`'s filing recipe produces, alongside every other ready ticket. Between runs: read `bd ready`, triage with veto power, and route each chosen ticket through a managed run below. Sequencing is the manager's call; the human hears what was picked and why, and can overrule any of it.

Triage is a filter against overengineering, not a queue pump. Before working any bead: was it witnessed more than once, or once with real cost? Is the lesson already recorded where its audience looks? Does the fix add standing surface a workaround avoids? Is the fix bigger than the lifetime cost of the friction? A bead that fails these is vetoed — closed, with the reasoning; a closed bead permanently blocks the dedup net from refiling it, so close only what should stay dead and defer the maybe-laters instead. When a bead survives, prefer deletion over a doc line over a new moving part. Group or merge related beads via `bd` parent links or close-as-dup with the surviving bead's scope widened, and group only on shared root cause or shared surface — one plan, one diff, one PR; grouping for tidiness manufactures scope. Vetoing is a first-class act, not a failure to act.

## Spawn

Spawn the driver as a named teammate with the ticket key and an absolute workspace root, and state the harness selector explicitly in its prompt. The driver's model is the manager's call at spawn, recorded in the ledger: opus by default (the driver plans, and planning gates everything downstream), the manager's own model for a hot or unusually complex ticket, never below opus for a driver — the cheap-tier savings belong to the in-run worker roles `[models]` already governs, not to the seat that authors the plan. The team roster is flat: a teammate cannot spawn named teammates, so the driver's own workers (implement, review, assessment) must be unnamed subagents — the skill's native-agent roles work unchanged that way. The driver should spawn stage agents SYNCHRONOUSLY (not in the background): a synchronous spawn returns the result directly, which removes the child-completion routing problem for that call entirely; only a resumed or backgrounded child needs the relay below, and the driver may poll such a child's transcript rather than waiting blind.

## The workbench

The manager never works on the main checkout: that tree is the workspace root — its
`.flow/` holds the shared memory store, the ticket files, and the runtime facade;
drivers mint their worktrees off it and finalize runs from it — so it stays clean, on
main, only ever fast-forwarded, and never advanced while a run is live. Inline work
happens in one standing worktree, `.claude/worktrees/flow-manager`, parked on detached
`origin/main` between tasks; every branch cuts fresh from `origin/main` there. Driver
runs keep their own per-ticket pool worktrees; the manager never edits those. The
janitor preserves the bench automatically (no ticket ownership).

## The manager's own hygiene

Three obligations on the manager's side, each from a witnessed failure or near-failure:

- **Push a notification the moment a gate arrives.** Both pilot runs' keystone waits included minutes-to-hours of discovery latency — the human was not looking when the plan or ask-user relay landed. The manager sends a host push notification (with the gate type and one-line summary) at every gate, in addition to the in-conversation relay.
- **Run a stall watch, never rely on luck.** The 62-minute pilot stall was caught because the human happened to ask for status. Keep a background watcher on the driver's transcript mtime (a shell loop suffices: alert when the file is older than ~10 minutes while no gate is pending) so a silent driver is caught by machinery.
- **Spawn from the template below**, substituting only the bracketed values — two hand-written spawn prompts already drifted from each other between the pilot runs.

```text
You are a flow driver session running under references/manager.md (read it).
Task: run the flow ticket [KEY] through the complete flow pipeline in the
workspace [ABSOLUTE_ROOT] (an initialized flow workspace). FLOW_HARNESS is
[HARNESS]. Invoke the flow skill now and follow it exactly as written,
including the entry contract and the skill-root re-pin rule.
Environment facts, all witnessed in prior managed runs:
- Plan gate: turn-boundary form — render the plan surface AND send the
  complete plain-text plan (exact text, base SHA, confidence + category
  scores, pass facts, resolved findings, residual risks) to "main", then stop
  and wait. Approval arrives as a message containing APPROVED, or revision
  feedback. Nothing mutates before it except the planning-start ticket claim.
- ask-user findings: relay to "main" the same way and wait.
- Spawn your stage agents SYNCHRONOUSLY (unnamed subagents; the roster is
  flat). Only a backgrounded or resumed child needs the manager relay; you
  may poll such a child's transcript rather than waiting blind.
- Machinery APPLY-NOW is unavailable here (protected-branch skill root):
  lens-B findings route to propose-and-record; the refusal is the known
  limit, not a failure.
Work autonomously otherwise; report at natural stops. After done or a
durable stop, send "main" the final status, PR URL, per-stage outcomes, and
anything that differed from what manager.md led you to expect.
```

## Relays — the manager's standing obligations

- **Plan gate.** The driver uses the turn-boundary gate form — the one SKILL.md gives Codex when native Plan mode is inactive; an agent-hosted driver has no plan mode of its own, and SKILL.md's Claude Code row assumes the top-level session: present the complete plan (exact text, base SHA, confidence and category scores, pass facts, resolved findings, residual risks) to the manager and wait. The plan surface is still owed under this topology when its gate passes (`plan-surface.md` makes skipping a defect): the driver renders it AND sends the complete plain-text plan, so the gate never depends on the surface being opened, and the manager relays the surface URL prominently to the human — without that push the surface degrades unused, since the human talks to the manager, not the driver. The manager relays verbatim and returns the approval or revision feedback, appending its own amendments when it has them — labeled as manager feedback, which the driver treats as revision input. Plan approval never originates from an agent, with one recorded exception: the human may delegate approval for a named run, explicitly and in-conversation; the manager then reviews with full scrutiny, approves in their stead, states the delegation in the approval message, and ledgers it — a driver may honor an APPROVED that cites such a delegation. Under this topology the message relay is the authoritative convergence path: the plan surface still renders for the human's later review, and the driver ends its surface session by hand rather than waiting on the surface's own signal.
- **ask-user findings.** Same relay, both directions, verbatim.
- **Child completions.** A BACKGROUNDED subagent's completion notification routes to the top-level session, not to the spawning teammate — the driver sleeps through its child finishing (the pilot lost 62 minutes to exactly this). Synchronous spawns avoid the problem; where a background or resumed child is unavoidable, the manager relays its completion (verdict summary plus where the full result lives) or the driver polls its transcript. A relay of an assessor result carries the numeric score against `delivery-plan.md`'s gate (>= 90.0, unrounded), never just the assessor's English verdict — the two can disagree and the verdict is the more persuasive one. Relays can arrive stale; when a driver's own status report and a child-completion relay conflict, the driver's report is fresher and wins.

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

**Standing merge authority (maintainer-granted 2026-07-28):** a parked PR whose gate is met — CI green, review clean — is merged by the manager, hot PRs included; for a hot PR the manager first executes the guard-property review below; the human gets a notification, never a question. Plan approval stays human; merge on a met gate is delegated. This holds only in flow's own self-target repo — in delivery workspaces the human-merge keystone holds.

**Guard-property review (hot diffs only).** A `hot` diff touches a guard or safety-machinery file. Hot merges serialize: the manager lands at most one hot diff at a time and re-checks CI between them — that isolation, plus this review, plus green is the hot path VISION.md names. A human merging a hot PR by hand owns the same check personally. Spawn one fresh reviewer agent that did not write the change and prompt it to refute:

> Review this PR diff for the flow self-target. Question: does it DELETE or WEAKEN any safety property — lease exclusivity (one run per ticket), snapshot drift-detection, atomic-write + corrupt-file quarantine, content-ownership refusal, or self-edit flock serialization? Guard *code* may be refactored/sped up freely; a guard *property* may only be replaced by a provably-equivalent one, never dropped. Default to "property removed" when uncertain. Return a verdict: `{property_removed: bool, which: str, why: str}`.

`property_removed: true` → do NOT merge; post a PR comment naming the property and leave the PR for the human. Only a clean review (`property_removed: false`) merges. The reviewer has no write or merge authority.

**Merge rules:**

- Verify branch push state first: an uncommitted change to a tracked file, an unpushed commit, or a remote branch already deleted means do not merge — resolve it or hand the PR to the human. Untracked scratch never counts.
- When the run's workspace configures a review brief, verify its freshness before any other gate and block the merge while it is stale or missing — the driver re-renders at the current SHA (`stage-review_brief.md` owns the render):

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
