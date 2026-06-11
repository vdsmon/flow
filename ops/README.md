# ops/ — the scheduler that runs flow's self-evolution loop unattended

This is the **outer loop** in flow's nested-loop architecture (see `plugins/flow/skills/flow/references/loop-engineering.md`). A `launchd` timer fires flow's producers + consumer on a cadence so the maintainer wakes to merged improvements. The runners here are maintainer-local operational infra, **not** part of the shipped plugin — they are vendored as templates so the loop is reproducible on another machine, not so flow loads them.

## What runs

`nightly-evolve.sh` (daily 00:17), each night:

1. on a clean `main`: fast-forward to `origin/main` + `claude plugin marketplace update` (advance the live checkout the plugin tracks)
2. **producer** — `claude --bg "/flow evolve audit"` (cold scan → files `evolve` beads)
3. **wait** — `wait_for_session` blocks until the producer finishes filing
4. **consumer** — `claude --bg "/flow evolve drain"` (reap green orphans + launch the fleet)

On any feature branch it audits the current checkout and skips advancing — it never disturbs the working tree.

`weekly-epic.sh` (Sunday 00:23) — the **high-altitude producer**, producer-only (no consumer; epics are judgment work that must not auto-ship):

1. on a clean `main`: same advance step as the nightly runner
2. **producer** — `claude --bg "/flow evolve epic"` (web-reaching theme-altitude lenses → files parent `epic` beads + a decomposition preview)
3. **wait** — same transcript-mtime liveness, with longer thresholds (epic runs go quiet longer — web fetches + an optional bounded spike)
4. **report** — lists the filed epics; the maintainer accepts + expands one by hand via `/flow <key>`

Weekly, not nightly: at theme altitude a daily cadence has weak signal. See `plugins/flow/skills/flow/references/verb-evolve.md` (§epic) for the producer itself.

## Why `--bg` and not `-p`

The cold audit goes silent for minutes mid-scoring. `claude -p` trips a stream-idle timeout on that silence and dies before filing — total loss. `--bg` has no idle watchdog, so it completes. The price is sequencing: `--bg` is fire-and-forget, so the runner must explicitly wait for the producer before draining, else `drain` runs on an empty backlog. `wait_for_session` measures liveness by transcript mtime (idle > 480s, or a new bead + idle > 180s, or a 25-min hard cap).

## Install

1. `mkdir -p ~/.flow-evolve/logs`
2. Copy `nightly-evolve.sh.template` → `~/.flow-evolve/nightly-evolve.sh`; replace `{{REPO}}` (absolute path to the flow checkout) and `{{MARKETPLACE}}` (the plugin marketplace name that tracks it). `chmod +x` it.
3. **Test-fire by hand first** and watch it audit + drain cleanly:
   ```
   ~/.flow-evolve/nightly-evolve.sh
   ```
   Only once that is clean should you arm the timer.
4. Copy `com.vdsmon.flow-evolve.plist.template` → `~/Library/LaunchAgents/com.<you>.flow-evolve.plist`; replace `{{USER}}` and `{{HOME}}` (launchd does not expand `$HOME` in plist fields). Then:
   ```
   launchctl load ~/Library/LaunchAgents/com.<you>.flow-evolve.plist
   ```
5. **Weekly epic producer** (optional): same steps with `weekly-epic.sh.template` → `~/.flow-evolve/weekly-epic.sh` and `com.vdsmon.flow-epic.plist.template` → `~/Library/LaunchAgents/com.<you>.flow-epic.plist`. Test-fire by hand first, then `launchctl load`.

## Observability / staleness warning

Each template fire appends to a durable run-record in `~/.flow-evolve/`:

- `nightly.run-record` — written by `nightly-evolve.sh`
- `weekly.run-record` — written by `weekly-epic.sh`

**Format** (plain text, one event per line):

```
start <epoch>
end <epoch> ok|fail
```

`start` is written immediately when the brace group opens (before any `cd` or network call). `end` is written by an EXIT trap, so it fires even on early abort — `_RUN_OUTCOME` defaults to `fail` and is set to `ok` only after a successful producer-launch + drain (nightly) or producer-launch + wait (weekly). Any early exit (bad repo, failed producer launch, `set -uo pipefail` abort) records `outcome=fail`.

**The `session-start.py` hook reads these files on every `SessionStart`** and prints a `## /flow schedule` warning block when any of three conditions apply:

| Condition | Threshold | Meaning |
|-----------|-----------|---------|
| Last `end` recorded `fail` | — | Something died |
| Last `end` too old | >36h nightly, >8d weekly | Loop stopped running |
| `start` with no `end`, past grace | >3h nightly, >6h weekly | Run is hung |

Absent record file = schedule not armed on this machine = silent (no warning).

**Mandatory redeploy step:** the templates are copy-deployed to `~/.flow-evolve/`. A fix to the template is inert on the live machine until re-copied. After merging a template change, copy the updated template:

```
cp ops/nightly-evolve.sh.template ~/.flow-evolve/nightly-evolve.sh
cp ops/weekly-epic.sh.template ~/.flow-evolve/weekly-epic.sh
```

## Gotchas

- **launchd runs with a minimal PATH** (no `~/.local/bin`, where `claude` lives). The runner exports it itself; a by-hand test masks this because your interactive shell already has it. Test via `launchctl start com.<you>.flow-evolve` to catch a PATH regression.
- **Maintainer-only.** The whole loop is dormant unless the checkout carries the `[maintainer] self_target = true` marker in `.flow/workspace.toml`. For a user project the auto-merge envelope stays closed.
- Logs land in `~/.flow-evolve/logs/`; `launchd.out` / `launchd.err` are the cumulative launchd streams, per-run logs are timestamped.
