# Overseeing a run

Run a flow ticket through a driver agent spawned from a long-lived main session (the manager), which observes friction from outside while the plan-approval keystone stays human. The driver runs the ordinary skill unmodified; the manager adds an outside view — timings, retries, stalls — that the run's own reflect stage cannot see about itself, and owns the work queue around the runs: triage, grouping, sequencing, and merges on met gates. Proven end-to-end in the flow-4vre pilot (PR #566) and refined by the flow-rgqm run (PR #568); every constraint in Roles, Relays, and Observation was witnessed in one of the two, not designed speculatively. The triage and merge authority is maintainer-granted (2026-07-28) rather than witnessed.

## Roles

- **Manager** (the overseer): the main session. Spawns drivers, relays the gates, observes passively, and compiles the friction report; it also triages the queue with veto power, groups/merges tickets, amends plans at the relay (labeled as manager feedback, which the driver treats as revision input), sequences runs, and merges on a met gate. It never edits the run's files and never interrogates the driver mid-stage — questioning a working driver perturbs the thing being measured.
- **Driver**: a named teammate agent running the flow skill exactly as written, with the gate relays below as its only environment delta.

## Pickup — the queue is the manager's

The manager is the consumer of the machinery backlog: the `evolve,machinery` beads that `stage-reflect.md`'s filing recipe produces, alongside every other ready ticket. Between runs: read `bd ready`, triage with veto power — file less than observed, veto duplication and surface-restoration, defer maybe-laters rather than close them (a closed bead permanently blocks the dedup net from refiling it) — group or merge related beads via `bd` parent links or close-as-dup with the surviving bead's scope widened, and route the chosen ticket through an overseen run below. Sequencing is the manager's call; the human hears what was picked and why, and can overrule any of it.

## Spawn

Spawn the driver as a named teammate with the ticket key and an absolute workspace root, and state the harness selector explicitly in its prompt. The team roster is flat: a teammate cannot spawn named teammates, so the driver's own workers (implement, review, assessment) must be unnamed subagents — the skill's native-agent roles work unchanged that way. The driver should spawn stage agents SYNCHRONOUSLY (not in the background): a synchronous spawn returns the result directly, which removes the child-completion routing problem for that call entirely; only a resumed or backgrounded child needs the relay below, and the driver may poll such a child's transcript rather than waiting blind.

## The manager's own hygiene

Three obligations on the manager's side, each from a witnessed failure or near-failure:

- **Push a notification the moment a gate arrives.** Both pilot runs' keystone waits included minutes-to-hours of discovery latency — the human was not looking when the plan or ask-user relay landed. The manager sends a host push notification (with the gate type and one-line summary) at every gate, in addition to the in-conversation relay.
- **Run a stall watch, never rely on luck.** The 62-minute pilot stall was caught because the human happened to ask for status. Keep a background watcher on the driver's transcript mtime (a shell loop suffices: alert when the file is older than ~10 minutes while no gate is pending) so a silent driver is caught by machinery.
- **Spawn from the template below**, substituting only the bracketed values — two hand-written spawn prompts already drifted from each other between the pilot runs.

```text
You are a flow driver session running under references/oversee.md (read it).
Task: run the flow ticket [KEY] through the complete flow pipeline in the
workspace [ABSOLUTE_ROOT] (an initialized flow workspace). FLOW_HARNESS is
[HARNESS]. Invoke the flow skill now and follow it exactly as written,
including the entry contract and the skill-root re-pin rule.
Environment facts, all witnessed in prior overseen runs:
- Plan gate: turn-boundary form — render the plan surface AND send the
  complete plain-text plan (exact text, base SHA, confidence + category
  scores, pass facts, resolved findings, residual risks) to "main", then stop
  and wait. Approval arrives as a message containing APPROVED, or revision
  feedback. Nothing mutates before it except the planning-start ticket claim.
- ask-user findings: relay to "main" the same way and wait.
- Spawn your stage agents SYNCHRONOUSLY (unnamed subagents; the roster is
  flat). Only a backgrounded or resumed child needs the overseer relay; you
  may poll such a child's transcript rather than waiting blind.
- Machinery APPLY-NOW is unavailable here (protected-branch skill root):
  lens-B findings route to propose-and-record; the refusal is the known
  limit, not a failure.
Work autonomously otherwise; report at natural stops. After done or a
durable stop, send "main" the final status, PR URL, per-stage outcomes, and
anything that differed from what oversee.md led you to expect.
```

## Relays — the manager's standing obligations

- **Plan gate.** The driver uses the turn-boundary gate form — the one SKILL.md gives Codex when native Plan mode is inactive; an agent-hosted driver has no plan mode of its own, and SKILL.md's Claude Code row assumes the top-level session: present the complete plan (exact text, base SHA, confidence and category scores, pass facts, resolved findings, residual risks) to the manager and wait. The plan surface is still owed under this topology when its gate passes (`plan-surface.md` makes skipping a defect): the driver renders it AND sends the complete plain-text plan, so the gate never depends on the surface being opened, and the manager relays the surface URL prominently to the human — without that push the surface degrades unused, since the human talks to the manager, not the driver. The manager relays verbatim and returns the approval or revision feedback, appending its own amendments when it has them — labeled as manager feedback, which the driver treats as revision input. Plan approval never originates from an agent.
- **ask-user findings.** Same relay, both directions, verbatim.
- **Child completions.** A BACKGROUNDED subagent's completion notification routes to the top-level session, not to the spawning teammate — the driver sleeps through its child finishing (the pilot lost 62 minutes to exactly this). Synchronous spawns avoid the problem; where a background or resumed child is unavoidable, the manager relays its completion (verdict summary plus where the full result lives) or the driver polls its transcript.

## Observation channels (all passive)

- **state.json** in the run worktree: per-stage `started_at_iso`/`finished_at_iso` are the timing backbone. A force-reset nulls a stage's timestamps; the rotating backups keep the prior snapshot.
- **The driver transcript** (session JSONL): parse incrementally at driver stops for tool errors, retries, and time gaps — never load it whole. The deleted transcript miner is restorable from history for a deeper pass (`git show 0bed292^:plugins/flow/skills/flow/scripts/trace_mine.py`); it extracts tool errors, silent retries, drift markers, and stall gaps bucketed by dispatch stage, and runs unchanged on a teammate transcript once the file is copied under the workspace's `~/.claude/projects` slug (its path guard requires that layout).
- **A manager ledger** kept outside the repo: timestamped notes on gates, stalls, and surprises. Cross-referencing the ledger against mined stall gaps is what separates human-wait (keystone cost) from machine friction — the miner alone cannot tell them apart.

## Known limits of the overseen topology

Machinery APPLY-NOW is structurally unavailable: the driver's `skill_root` resolves to the marketplace clone, which sits on a protected branch, so `machinery_edit` refuses (exit 2) and every lens-B finding routes to propose-and-record. State this expectation in the driver's prompt so the refusal is read as the known limit, not a failure.

## Merging

**Standing merge authority (maintainer-granted 2026-07-28):** a parked PR whose gate is met — CI green, review clean — is merged by the manager, hot PRs included; for a hot PR the manager first executes the guard-property review below; the human gets a notification, never a question. Plan approval stays human; merge on a met gate is delegated. This holds only in flow's own self-target repo — in delivery workspaces the human-merge keystone holds.

**Guard-property review (hot diffs only).** A `hot` diff touches a guard or safety-machinery file. Spawn one fresh reviewer agent that did not write the change and prompt it to refute:

> Review this PR diff for the flow self-target. Question: does it DELETE or WEAKEN any safety property — lease exclusivity (one run per ticket), snapshot drift-detection, atomic-write + corrupt-file quarantine, content-ownership refusal, or self-edit flock serialization? Guard *code* may be refactored/sped up freely; a guard *property* may only be replaced by a provably-equivalent one, never dropped. Default to "property removed" when uncertain. Return a verdict: `{property_removed: bool, which: str, why: str}`.

`property_removed: true` → do NOT merge; post a PR comment naming the property and leave the PR for the human. Only a clean review (`property_removed: false`) merges.

**Merge rules:**

- Verify branch push state first: an uncommitted change to a tracked file, an unpushed commit, or a remote branch already deleted means do not merge — resolve it or hand the PR to the human. Untracked scratch never counts.
- A `DIRTY` PR is a genuine code conflict; leave it for the human. A CLOSED-but-not-merged PR is never auto-handled.
- Merges never stamp the version — the server-side `version-stamp.yml` Action stamps `main` after the merge lands.
- Close order: merge first, then finalize — the bead close is bookkeeping, never what makes the merge safe.

## Report and filing

After the run: a per-stage wall-clock table, the mined event summary, qualitative observations, and ranked suggestions, each classified ground truth vs judgment per the repo-root VISION.md's operating line. Machinery-shaped findings file through the `flow-beads-create` recipe that `stage-reflect.md` owns, with file-anchored dedup keys — the run's own reflect files independently, and the dedup net keeps the two producers from double-filing. After the merge (manager or human), close the ticket with the finalize recipe `command-ticket.md` owns, run from the primary checkout.

Cross-run pattern detection is where the outside view beats per-run reflect outright: the same hiccup seen twice files once, pre-deduplicated, with two witnesses.
