# Scrutinize lenses

The lens registry for the seat's sweep (`references/scrutinize.md` §The sweep owns when a sweep runs; this file owns what it looks through). Every lens is a named, mechanical way of reading the same durable evidence, each earned by a real catch that ad-hoc looking almost missed. A sweep runs every lens whose sources have content for the cursor window; a skipped lens is reported as skipped so silence stays visible. The registry exists so a successor seat runs the identical checklist instead of re-inventing the round's judgment from scratch.

## Shared machinery

**The trace is computed once.** The seat mines the window first and passes the artifact path to every gather agent; agents never re-parse raw transcripts and never load one whole:

```bash
FLOW_HARNESS="<harness>" "<facade>" scrutinize-trace \
  --transcript-dir "<abs transcript dir>" --since "<cursor iso>" --json
```

Capture that JSON to a scratch file. It carries, per session: `flow_calls` (timestamped facade calls with subcommand), `tool_errors`, `user_messages`, `agent_spawns`, and `subagents` (the per-agent spans whose wall clock lives nowhere else).

**The gather-agent contract.** One read-only agent per lens, spawned through the host Agent tool with a sonnet model hint, all in parallel. Agents gather and summarize; the seat judges. An agent NEVER mints a bead, never writes a store or tracker, never runs a mutating probe, and never loads seat memory; one minting seat plus file-anchored dedup keys is what keeps one defect one bead, and that property dies the moment a gatherer files anything. Each agent prompt carries this rooted field block plus the one lens section it executes:

```text
Workspace root: <absolute workspace root under scrutiny>
Trace file: <absolute path to the mined scrutinize-trace JSON>
Window: <cursor iso> .. <sweep start iso>
Memory namespace dir: <absolute path, e.g. <workspace>/.flow/memory/<ns>>
Lens: <lens name; execute only that section of scrutinize-lenses.md>
Return: a JSON list in the shared finding schema, nothing else
```

**The shared finding schema.** Every agent returns a JSON list of findings, each with exactly these fields: `lens` (its own name); `claim` (one present-tense sentence); `witnesses` (list of `{ts, anchor}` where the anchor is a session id, a store path, or a PR/ticket key, never a run directory); `validity` (`still-true` | `stale` | `unprobed`, from re-checking the claim against the present: a read-only machine probe, a config re-read, or `unprobed` when checking would mutate); `remedied_check` (what `git log origin/main --since=<window start>` and the read-only probes said about a fix having already landed); `class` (`defect` | `papercut` | `proposal` | `verified-fix` | `expected-stale`); `suggested_routing` (`bead` | `ledger` | `ruling` | `watch` | `none`). The witness bar, the routing hierarchy, and every filing decision stay owned by `scrutinize.md` §Friction handling; an agent's `suggested_routing` is input to the seat's triage, nothing more. Improvement ideas that are not defects ride the same schema as `class: proposal` and face the §Pickup overengineering filter at triage.

**Shell hygiene for gather agents.** Never put a backtick inside a `python3 -c` string: the outer shell substitutes it as a command, and the first live sweep watched one execute an unintended `claude plugin marketplace update` that way. Write a `.py` file and run it instead.

**Short-circuit rule.** Before spawning, the seat checks each lens's cheap precondition (listed per lens below). A lens with empty sources for the window is skipped without an agent, and the skip appears in the sweep report; a lens skipped silently reads as "covered and clean" when it was neither.

## Lens: close-out integrity

Ship events reconcile with reality. Earned by FT-1499 (2026-08-04: a credential-less finalize reaped the run state before the freeze, losing the ship event forever) and by 2026-08-03's two merged deliveries invisible to `metric time-to-pr`. Precondition: any window run or any merged window PR.

Sources: the finalize sweep preview, the ship-events directory, the forge's merged PRs for the window, and `metric time-to-pr`'s `skipped` list.

```bash
FLOW_HARNESS="<harness>" "<facade>" finalize --workspace-root "<workspace>" --all --dry-run
```

Checks: every merged window PR maps to a frozen ship event; every event carries its `flow_attribution` stamps (planning, plan, create_pr); no reaped run is missing its event (the lost-freeze class); no worktree survives behind a merged PR; the tracker reads terminal for every shipped key; `synthesized_by_seat` events are surfaced so hand-reconstructed records stay visible. The dry-run preview also names still-parked PRs; those are reported for the human, never touched.

## Lens: engine freshness

What code the window's sessions actually ran. Earned by the stuck installed pin (sessions loaded 0.118.83 across two deliveries; the FT-1499 driver ran a retired plan gate until the human nudged it). Precondition: always runs, it is nearly free.

Sources: the flow entry in `~/.claude/plugins/installed_plugins.json`, the marketplace checkout's `plugin.json`, origin/main's stamp, and each session's loaded skill-text version (the `Base directory for this skill` invocation line in the trace names the versioned cache path).

Checks: installed pin version equals the checkout version equals main's stamp, and the pin's `installPath` directory exists (the full verify-by-effect bar from `scrutinize.md` §Merging); every session that ran stale skill text is listed with what the staleness cost in that session (a nudge, a retired surface, a missing contract obligation); a live run sealed to an older engine is noted as the known limit, not a defect.

## Lens: call efficiency and error census

The per-tool-call waste audit, formalized from the 2026-08-04 tool-call sweeps. Earned by `--help` archaeology at session starts, a blocked `sleep 45` poll, double-executed finalize reads, and the `invalid_evidence: missing lease_state` class that a transcript census would have caught before the human reported it. Precondition: any window session with `flow_calls` or `tool_errors`.

Sources: the trace's `flow_calls` and `tool_errors` per session.

Checks: every tool error classified as env (missing binary, credential, sandbox), grammar-guess (a spelling the registry or a seam-checked card already answers), contract-error (an engine error code like `invalid_evidence`, traced to driver fault vs a contract gap), or host-rule (Write-before-Read, blocked sleep); duplicate identical commands run twice for one answer; `--help` reads that a reference card already covers; poll shapes where a watch or notification should wake the driver; per-run facade-call count against the dispatch-spine baseline of roughly nine calls from worktree create to implement launch (measured 8 to 10 across twelve runs, 2026-08-04, with `recall` and `validate` as the legitimate additions to the original seven), with the excess named.

## Lens: env and credential

The environment's honesty around runs. Earned by the FT-1560 mid-stage `aws sso login` block, the Graylog MCP 403 during planning, and FT-1570 skipping the attended preflight check that its workspace configures. Precondition: the workspace configures `[preflight]`, or any auth-shaped error appears in the window.

Sources: `flow_calls` preflight invocations per run, auth failures in the error census (sso, 401, 403), and the workspace's `[preflight]` block.

Checks: the attended `preflight check` ran at plan time on every run whose workspace configures it, and its duration is reported; the silent `preflight probe` fired before e2e; zero interactive logins inside stages; credential gaps outside flow's reach (an MCP token missing a permission) are reported for the human rather than filed, since no flow fix exists for a foreign token.

## Lens: review-bot patterns

What the review bot actually did to the window's PRs. Earned by the CodeRabbit draft-flip discovery (reviewed=false for minutes after green CI, then eleven threads) and by recurring Major classes that distinguish money-path defects from whitespace. Precondition: at least one window PR with bot activity.

Sources: forge review threads, CI runs, and the PR timeline for each window PR.

Checks: thread count and class per PR (money-path Major, cosmetic Major, minor, nit); review-flip latency after CI green; resolution outcomes split fixed vs conceded (the end state is ZERO open threads either way, per the review_loop contract); per-lane bot-thread rates handed to the lane watch; a bot silent on a PR it should have covered, judged by the activity gate, not by workspace declaration.

## Lens: recall efficacy

Memory used versus knowledge re-derived. Earned by FT-1554 burning three MariaDB errors in five minutes re-guessing a prod schema that a knowledge entry could have carried. Precondition: any window run.

Sources: `recall-usage.jsonl` in the memory namespace, the knowledge store, `metric recall-hit-rate` and `metric corpus-health` over the window (the INTERNAL metric names; the public `measure` grammar spells these `recall-quality` and `memory-health`, and the first live sweep burned a failed call on exactly that mismatch), and trace evidence of re-derivation (repeated schema probing, repeated environment discovery, a question the store already answers).

Checks: each run's recall hits and whether they shaped a decision; incidents where a run re-derived something the store already held, named with the store entry it should have hit; store health (growth, prune debt, dead entries contradicted by later evidence); a knowledge entry that a run contradicted without correcting.

## Lens: cross-run contention

What no single run can see about itself; this operationalizes the cross-run pairs claim in `scrutinize.md` §Synthesis. Earned by the FT-1555/FT-1556 race (two branches editing the same two files with the same intent, discovered only at rebase). Precondition: two or more window runs, or any lease/takeover event.

Sources: the lease files, `git worktree list`, friction entries about base movement or stashes colliding, and the window runs' touched-file sets from their PRs.

Checks: two runs whose diffs touch the same files, reported as a pair with both keys; base-branch movement mid-run and which runs absorbed it; lease collisions, takeovers, and stage artifacts quarantined by a takeover; a pattern of the same file contended across three or more runs marks a hot file worth surfacing to the human.

## Lens: performance

Every sweep reports where wall clock went, not only what broke (standing directive, 2026-07-31: run time is the metric the human watches). Precondition: any window run.

Read `metric time-to-pr` and `metric friction-per-run` over the window (both take `--workspace-root` and require `--namespace`), then reconstruct per-stage spans for the window's runs from the trace: the dispatch spine and the subagent spans carry the timestamps, and the gaps between them are the stages. Report the split (planning-to-gate, implement, code_review, e2e, review_loop waits) with the attended/machine attribution the ship-event stamps carry, and name the widest span with its cause when the evidence shows one. Medians hide the tail; the outlier run usually carries the machinery finding, and a stage spending its time on environment bootstrap or a vacuous verdict is a lever, not a constant.

## Lens: nudge

A human prompt that should not have been needed is among the highest-signal friction there is (standing directive, 2026-07-31): every nudge marks a place the pipeline stalled, asked what it should have answered itself, or lost its thread. Precondition: any mid-run user message in the window.

Sweep the trace's `user_messages` for mid-run human prompts, excluding keepalive sentinels, task notifications, and skill invocations: an interruption, a "why did you stop?", a "try again", a re-statement of something already decided. Classify each against its cause before counting it (a usage-limit pause needing a manual resume is host machinery, not a flow stall). Each true nudge is a finding about flow, never about the human, and the stage boundary or gate it landed on names the defect's address. A nudge shape seen twice is bead-worthy like any other friction; three witnesses of the same shape in one day is how the driver-idles defect earned its bead.

## Lens: lane watch

The 2026-08-02 ceremony reduction (verify tiers as execution profiles, code_review unwired on bot-covered workspaces, assessor bounded) is a live rollout judged by the sweep, not a settled fact. A rollout stays live only while it is being judged, so report a lane's OBSERVATION COUNT before its numbers, and say plainly when a lane has none: an unrouted lane produces no evidence, and a lens that reports it as clean reads as coverage it never had. Three consecutive windows through 2026-08-13 closed with zero express-lane runs while the lens reported no revert trigger firing each time, which is true and tells the human nothing. When a configured lane reaches three windows with no observations, escalate it ONCE as an explicit retire-or-route choice for the human (ledger the escalation so a successor seat does not repeat it), then stop re-deriving the same finding every sweep. The seat does not retire a lane on its own: deleting the option forecloses the routing half of the choice, and the machinery costs nothing while parked. Precondition: any window run.

Split every performance and quality read by lane (the frontmatter `lane` field bootstrap records) and report, per lane: time-to-pr, friction per run, review-bot Major threads per PR, CI runs per PR (the early-tail churn gauge: a climbing count means e2e is pushing fixes into open PRs often enough to reconsider the order), and reverts. The revert triggers are explicit and act before a trend accumulates: a Major money-path defect reaching the human merge on an express run, or an express or light lane whose bot-thread rate climbs above the full-lane baseline, restores the dropped layer (re-wire the workspace's code_review handler, demote the ticket class) before the next run starts. Watching the levers is what made removing them safe to try. Read an empty `lane` on a plugin at or past 0.118.80 as `full`: bootstrap deliberately stamps only `express` and `light` (absent is the stages' full-lane default), so an empty field on a newer run is a full-lane run, not a recording hole; one seat already burned a thread re-deriving that.
