# Overseeing a run

Run a flow ticket through a driver agent spawned from a long-lived main session (the overseer), which observes friction from outside while every human gate stays human. The driver runs the ordinary skill unmodified; the overseer adds an outside view — timings, retries, stalls — that the run's own reflect stage cannot see about itself. Proven end-to-end in the flow-4vre pilot (PR #566); every constraint below was witnessed there, not designed speculatively.

## Roles

- **Overseer**: the main session. Spawns the driver, relays the gates, observes passively, compiles the friction report. It never edits the run's files and never interrogates the driver mid-stage — questioning a working driver perturbs the thing being measured.
- **Driver**: a named teammate agent running the flow skill exactly as written, with the gate relays below as its only environment delta.

## Spawn

Spawn the driver as a named teammate with the ticket key and an absolute workspace root, and state the harness selector explicitly in its prompt. The team roster is flat: a teammate cannot spawn named teammates, so the driver's own workers (implement, review, assessment) must be unnamed subagents — the skill's native-agent roles work unchanged that way.

## Relays — the overseer's standing obligations

- **Plan gate.** The driver uses the skill's no-native-plan-mode form: present the complete plan (exact text, base SHA, confidence and category scores, pass facts, resolved findings, residual risks) to the overseer and wait. The overseer relays verbatim to the human and returns the approval or revision feedback. Approval never originates from an agent.
- **ask-user findings.** Same relay, both directions, verbatim.
- **Child completions.** A teammate's own subagent completion notification routes to the top-level session, not to the spawning teammate — the driver sleeps through its child finishing. The overseer MUST relay every child completion (verdict summary plus where the full result lives), or mandate poll-after-spawn in the driver prompt. The pilot lost 62 minutes to exactly this before the relay existed.

## Observation channels (all passive)

- **state.json** in the run worktree: per-stage `started_at_iso`/`finished_at_iso` are the timing backbone. A force-reset nulls a stage's timestamps; the rotating backups keep the prior snapshot.
- **The driver transcript** (session JSONL): parse incrementally at driver stops for tool errors, retries, and time gaps — never load it whole. The deleted transcript miner is restorable from history for a deeper pass (`git show 0bed292^:plugins/flow/skills/flow/scripts/trace_mine.py`); it extracts tool errors, silent retries, drift markers, and stall gaps bucketed by dispatch stage, and runs unchanged on a teammate transcript once the file is copied under the workspace's `~/.claude/projects` slug (its path guard requires that layout).
- **An overseer ledger** kept outside the repo: timestamped notes on gates, stalls, and surprises. Cross-referencing the ledger against mined stall gaps is what separates human-wait (keystone cost) from machine friction — the miner alone cannot tell them apart.

## Report and filing

After the run: a per-stage wall-clock table, the mined event summary, qualitative observations, and ranked suggestions, each classified ground truth vs judgment per the repo-root VISION.md's operating line. Machinery-shaped findings file through the `flow-beads-create` recipe that `stage-reflect.md` owns, with file-anchored dedup keys — the run's own reflect files independently, and the dedup net keeps the two producers from double-filing. After the human merges the parked PR, close the ticket with the finalize recipe `command-ticket.md` owns, run from the primary checkout.

Multiple tickets may route through one overseer; cross-run pattern detection is where the outside view beats per-run reflect outright, because the same hiccup seen twice files once, pre-deduplicated, with two witnesses.
