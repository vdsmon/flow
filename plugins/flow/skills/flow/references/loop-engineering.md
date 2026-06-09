# Loop engineering: the nested loops flow already runs

flow is an instance of **loop engineering** — the shift, named over 2025-26 by the people running agent fleets, from *prompting a model* to *writing the loop that prompts it*. This doc maps the loops flow runs, so the architecture is legible as one system instead of three disconnected pieces.

## The frame

> "I don't prompt Claude anymore. I have loops running that prompt Claude and figuring out what to do. My job is to write loops." — Boris Cherny (head of Claude Code)

A loop is a harness that **discovers work → hands a chunk to an agent → checks what came back → repeats**, with a **separate verifier** (not the maker self-grading) deciding the stop condition. You stop being the thing in the keystroke path; you build the thing that decides what to work on, dispatches it, and checks the result. Peter Steinberger's version is blunter ("you shouldn't be prompting coding agents anymore") and more anti-framework ("just talk to it"); flow is squarely the build-the-harness camp.

Sources: [Addy Osmani — Loop Engineering](https://addyosmani.com/blog/loop-engineering/) · [Boris Cherny via Product Market Fit](https://www.productmarketfit.tech/p/stop-prompting-ai-and-start-building) · [Peter Steinberger — Just Talk To It](https://steipete.me/posts/just-talk-to-it)

## The three nested loops

```
scheduler loop          (launchd timer, runs on a cadence)         ← ops/, this doc
  └─ self-evolution loop (producer → backlog → drain → merge)       ← self-evolution.md
       └─ per-ticket do-loop (plan → … → merge, one PR)             ← background-pipeline.md
```

The inner loop is one `/flow <key>` run: it plans, enters a worktree, implements, drives the PR green, and self-merges a green leaf. The middle loop (`/flow evolve drain`) is the self-evolution consumer: it reaps finished orphans and launches the next batch of inner-loop runs until the backlog is empty. The outer loop is a **scheduler** that fires the producers + consumer on a timer, unattended — the part `self-evolution.md` omits, which is why its diagram reads "on demand" when a clock actually drives it.

## The scheduled outer loop

A `launchd` agent (`com.vdsmon.flow-evolve`, daily 00:17) runs a single shell runner, vendored as a template in `ops/` (see `ops/README.md`). What it does each night:

```
1. on a clean main: fast-forward to origin/main + update the marketplace checkout
2. PRODUCER:  claude --bg "/flow evolve audit"     (cold scan → files evolve beads)
3. wait_for_session                                 (block until the producer finishes filing)
4. CONSUMER:  claude --bg "/flow evolve drain"      (reap green orphans + launch the fleet)
```

Two design choices carry the weight:

- **`--bg`, not `-p`.** The producer goes quiet for minutes mid-scoring; `claude -p` trips a stream-idle timeout on that silence and dies before filing — total loss. `--bg` has no idle watchdog, so it completes. The cost is sequencing: `--bg` is fire-and-forget, so the runner must explicitly **wait** for the producer to finish filing beads before draining, else `drain` runs on an empty backlog.
- **`wait_for_session` liveness = transcript mtime.** Done = idle > 480s (truly quiet), OR a new bead appeared above the pre-existing backlog AND idle > 180s (filing happens at the very end), OR a 25-min hard cap. The new-bead signal baselines the held backlog so a pre-existing held bead never false-fires the fast path.

The runner never disturbs the working tree: it advances + updates the plugin **only** when sitting on a clean `main`; on any feature branch it audits the current checkout and logs that it skipped advancing.

## The producer-altitude axis

The loop is only as ambitious as its producer. flow has three producer altitudes:

| Altitude | Sub-verb | Unit of work | Disposition | Cadence |
|----------|----------|--------------|-------------|---------|
| **defect** | `evolve audit` | one file, one symptom, one PR | `evolve` bead → auto-drained | nightly |
| **judgment** | `evolve propose` | one feature/refactor, one PR | `proposal` bead → maintainer runs `/flow <key>` | on demand |
| **theme** | `evolve epic` | a capability track → a tree of PRs | `epic` bead + `proposal` children → maintainer accepts | **weekly** |

`audit` and `propose` are biased small by construction ("prefer small, isolated, high-evidence items"). The **theme** altitude — `evolve epic` — is the high-altitude producer: web-grounded, audacious, conviction-gated (not track-record-gated), spike-capable. It discovers at theme altitude and **decomposes** the chosen epic into do-loop-sized children the existing per-ticket consumer can run. See `verb-evolve.md` §epic. It runs weekly, not nightly: at theme altitude a daily cadence has weak signal.

Why weekly + producer-only: an epic is judgment work that must not auto-ship, so the weekly scheduler runs the producer and notifies the maintainer — there is no auto-drain consumer for the epic lane. The nightly defect loop and the weekly theme loop are two scheduled loops at different altitudes and cadences.

## Canon map — loop-engineering components → flow's parts

The components a loop needs (after Addy Osmani), mapped to where flow implements each:

| Component | flow's part | Strength |
|-----------|-------------|----------|
| **Automations** (scheduled discovery on cadence) | `launchd` nightly producer→consumer; weekly epic producer (`ops/`) | strong |
| **Worktrees** (isolated parallel work) | `flow_worktree.py`, `--base @default` | strong |
| **Skills** (reusable project knowledge, no re-derive) | this skill: `SKILL.md` + `references/`, compounding memory | strong |
| **Connectors** (agents reach real tools) | tracker-agnostic adapters (Jira \| beads), MCP-reachable | strong |
| **Maker/checker** (separate verifier, not self-grade) | adversarial-refute in `propose`/`epic`, the code-review pass, the in-run merge reviewer | strong |
| **State / memory** (persist across runs) | `.flow/runs/<key>/state.json` + lease + recall hook + auto-memory | strong |
| **Stop condition** (separate verifier checks "done") | CI-green gate + confidence ≥90% + guard-property review + maintainer-accept (epics) | strong |

flow has the whole skeleton. The leverage that remained was **producer altitude** — discovering work worth the fleet's time — which `evolve epic` opens.

## See also

- `self-evolution.md` — the middle loop in full (producers, the `drain` consumer, the guardrails).
- `verb-evolve.md` — the `evolve` namespace: `audit` / `propose` / `epic` / `drain`.
- `background-pipeline.md` — the inner do-loop and how a run goes unattended.
- `ops/README.md` (repo root) — the scheduler runner + install.
