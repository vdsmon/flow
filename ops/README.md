# ops/ — the scheduler that runs flow's self-evolution loop unattended

This is the **outer loop** in flow's nested-loop architecture (see `plugins/flow/skills/flow/references/loop-engineering.md`). A `launchd` timer fires flow's producers + consumer on a cadence so the maintainer wakes to merged improvements. The runners here are maintainer-local operational infra, **not** part of the shipped plugin — they are vendored as templates so the loop is reproducible on another machine, not so flow loads them.

## What runs

`nightly-evolve.sh` (daily 00:17), each night:

1. on a clean `main`: fast-forward to `origin/main` + `claude plugin marketplace update` (advance the live checkout the plugin tracks)
2. **producer** — `claude --bg "/flow evolve audit"` (cold scan → files `evolve` beads)
3. **wait** — `wait_for_session` blocks until the producer finishes filing
4. **producer** — `trace_mine.py runs → extract | cluster | file` (deterministic, read-only transcript mining; not `--bg`; files deduped `trace-mined` proposal beads, never auto-drained)
5. **consumer** — `claude --bg "/flow evolve drain"` (reap green orphans + launch the fleet)

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
6. **loopctl helper** (optional but recommended): same copy-and-replace pattern for `loopctl.sh.template` → `~/.flow-evolve/loopctl.sh`; replace only `{{USER}}` (your macOS username). Then `chmod +x ~/.flow-evolve/loopctl.sh`. Usage: `loopctl.sh arm|disarm|status [nightly|weekly]`.

## Deadman (staleness surface)

A dead loop used to be discovered only by noticing PRs stopped appearing. Each runner appends one JSON line per event to `~/.flow-evolve/run-record.jsonl` — `{schedule, phase, ts, outcome}`, where `phase` is `start` or `end`, `ts` is UTC ISO-8601, and `outcome` is `ok`/`fail` on an `end` (empty on a `start`).

The `end` line is written by an **EXIT trap**, so it fires on ANY exit — clean finish, `set -uo pipefail` abort, a bad `cd`, a crash. `_RUN_OUTCOME` defaults to `fail` and is set to `ok` only after a clean producer launch (nightly: producer session captured + drain launched; weekly: producer captured + wait completed). A run that dies partway records `end … fail` immediately rather than going silent until the staleness bar trips.

The SessionStart hook (`plugins/flow/hooks/session-start.py`) reads the file inside any `.flow` workspace and prints a `## /flow ops` warning under any of three per-schedule conditions:

- **hung** — a `start` with no later `end`, past a grace of 3h (nightly) / 6h (weekly). A run still in flight within grace stays silent.
- **fail** — the latest `end` recorded `outcome=fail`.
- **stale** — the latest `end` is older than 36h (nightly) / 8d (weekly).

What this catches: a never-firing timer (stale), a crashed/aborted fire (fail, immediately), and a wrapper stuck or killed before it can write `end` (hung — e.g. SIGKILL, reboot). What it does NOT catch: a wrapper that runs to completion while the `claude` job it launched is a zombie — that still records `end … ok` and looks healthy. Outcome-on-a-completed-run alerting is a separate, out-of-scope surface.

Absence of the file means no schedule is armed on this machine, so the check self-gates to nowhere.

When a loop is deliberately disarmed via `loopctl.sh disarm`, it writes a marker file (`~/.flow-evolve/disarmed-{nightly,weekly}`). The SessionStart hook reads this marker and emits an informational (non-warning) line instead of stale/hung/fail warnings. Running `loopctl.sh arm` removes the marker. **Migration note:** if you disarmed the loops before deploying the new loopctl template, run `loopctl.sh disarm [nightly|weekly]` once after redeploying to materialize the marker files.

**Redeploy is manual.** The runners are copy-deployed (`~/.flow-evolve/*.sh`), so the run-record changes are inert on the live machine until you re-copy the templates over the deployed scripts (Install steps 2 and 5). The hook half ships with the plugin and activates on the next marketplace update; it stays silent until the file appears.

## Gotchas

- **launchd runs with a minimal PATH** (no `~/.local/bin`, where `claude` lives). The runner exports it itself; a by-hand test masks this because your interactive shell already has it. Test via `launchctl start com.<you>.flow-evolve` to catch a PATH regression.
- **Maintainer-only.** The whole loop is dormant unless the checkout carries the `[maintainer] self_target = true` marker in `.flow/workspace.toml`. For a user project the auto-merge envelope stays closed.
- Logs land in `~/.flow-evolve/logs/`; `launchd.out` / `launchd.err` are the cumulative launchd streams, per-run logs are timestamped.
