# Scheduled Flow maintenance

These templates run Flow's maintainer loop on macOS. They are local operations
infrastructure, not plugin runtime files. The scheduler starts an owner session;
the owner uses host-native collaboration agents, with Flow's worker-pool reducers
enforcing capacity, read-only guards, and durable recovery around native launch/wait.

## What runs

The nightly schedule runs:

1. a read-only clean-boundary proof, then a fast-forward-only update only when the
   checkout is clean, on `main`, and no base or revision lease is live/corrupt;
2. `FLOW maintain evolution audit` in one synchronous owner session;
3. the ship-event senses deadman and health digest;
4. `FLOW maintain evolution drain` in a second synchronous owner session.

The weekly schedule runs `FLOW maintain evolution epic` in one producer-only owner
session. It files initiative-level parents but does not expand or deliver them.

An owner session may be backgrounded by the host or by the caller of the wrapper.
The templates do not create detached child sessions or inspect host-private job state.

## Host adapter

Set `FLOW_HOST` to `claude` or `codex`. The adapter translates the logical command
at the boundary:

```text
claude  -> claude -p "/flow maintain ..."
codex   -> codex exec "$flow:flow maintain ..."
```

The shell scripts escape the `$` in the Codex prompt so the shell cannot expand it.
Both commands run synchronously: completion means the owner has settled its native
workers, not merely that a child session was started.

`FLOW_REFRESH_CMD` is optional. When set, the wrapper runs it after a successful
fast-forward so a locally configured host installation can be refreshed. Keeping
refresh policy outside the template avoids assuming a Claude marketplace or a Codex
plugin cache layout.

The same boundary gates both the git update and `FLOW_REFRESH_CMD`. A dirty checkout,
live/corrupt lease, or failed boundary probe skips both and continues maintenance
against the untouched checkout.

## Install

1. Create the state directory:

   ```bash
   mkdir -p ~/.flow-evolve/logs
   ```

2. Copy `nightly-evolve.sh.template` to
   `~/.flow-evolve/nightly-evolve.sh`, replace `{{REPO}}` with the absolute path to
   the Flow checkout, and make it executable.
3. Choose the host. Either edit the deployed script's default or set `FLOW_HOST` in
   the LaunchAgent template. Ensure the selected `claude` or `codex` executable is
   on the template's `PATH`.
4. Test-fire the wrapper synchronously and inspect its log before arming anything:

   ```bash
   FLOW_HOST=codex ~/.flow-evolve/nightly-evolve.sh
   ```

5. Copy `com.vdsmon.flow-evolve.plist.template` to your LaunchAgents directory,
   replace `{{USER}}`, `{{HOME}}`, and `{{FLOW_HOST}}`, then load it.
6. Optionally repeat for `weekly-epic.sh.template` and its LaunchAgent.
7. Optionally install `loopctl.sh.template` to arm, disarm, or inspect both timers.

## Maintainer preflight

Every fire appends durable JSONL events to
`~/.flow-evolve/run-record.jsonl`: `{schedule, phase, ts, outcome}`. An EXIT trap
writes the end event, so ordinary command failures become `outcome=fail` immediately.
The host-neutral maintainer preflight reports:

- `hung`: a start has no later end after 3h nightly or 6h weekly;
- `failed`: the latest completed fire recorded `fail`;
- `stale`: the latest completion is older than 36h nightly or 8d weekly;
- `disarmed`: the matching `disarmed-nightly` or `disarmed-weekly` marker exists.

Bare `FLOW` and maintenance commands surface this preflight. It can also be inspected
directly through a workspace's v2 facade:

```bash
.flow/runtime/flow maintainer-preflight --json
```

An absent run record means schedules were never armed on this machine and stays
silent. `loopctl.sh disarm` writes the durable marker; `arm` removes it.

The nightly senses check covers a different failure: the scheduler may be healthy
while ship-event observation has gone dark. `senses_deadman.py` joins recent terminal
tracker work to ship-event evidence, files one deduplicated high-priority alarm on
divergence, and prints the folded telemetry/metric/liveness digest into the nightly
log. Maintenance preflight and the cockpit surface that evidence alongside the
run-record check.

## Safety and operations

- Maintainer commands remain dormant unless `.flow/workspace.toml` marks the checkout
  as Flow's self target.
- The wrapper advances only a clean `main` with `git merge --ff-only`. Other branches
  are maintained in place.
- Durable run, fleet, lease, worktree, tracker, and PR evidence is authoritative.
  Owner or worker handles are disposable.
- Logs live under `~/.flow-evolve/logs/`.
- launchd has a minimal environment. Test with `launchctl start`, not only from an
  interactive shell.
