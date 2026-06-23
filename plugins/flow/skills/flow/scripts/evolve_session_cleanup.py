"""Classify finished background sessions for the drain loop to stop + tombstone (pure core + read-only CLI).

A `claude --bg /flow <key> --auto` run does not exit when its work finishes: after
the PR merges + the reflect stage runs, the session goes idle but lingers in the
agents panel as a job under `~/.claude/jobs/<id>/`. A multi-bead `/flow evolve drain`
leaves a pile of these for the maintainer to `claude stop` + Ctrl+X by hand. This
module is the read-only half of an auto-cleanup pass: each drain turn (after the
step-A reap) it enumerates the jobs, applies the liveness + cwd + terminal-bead +
self gates, and prints the set that is safe to stop. The destructive ops
(`claude stop <id>`, `rm -rf <job_dir>`) live in reviewable prose in
`references/verb-evolve.md` (§drain step A2) — mirrors `evolve_reap.py`: pure
classify here, the loop runs the side effects.

Enumeration + liveness are FILESYSTEM-ONLY. Never `claude agents --json`: it blocks
on a TTY and the drain itself can run headless (flow memory, hit twice 2026-06-05).
We scan `~/.claude/jobs/*/state.json` directly. Confirmed-live schema fields:
state, tempo, cwd, sessionId, intent, name, daemonShort, backend, linkScanPath,
firstTerminalAt, updatedAt. There is NO `pid` field, so no process-liveness check.

Session→bead map: the `intent` field. A drain-launched `claude --bg "/flow <key>
--auto"` job records `intent == "/flow <key> --auto"` (confirmed live on a done +
a working sample 2026-06-08), `cwd == repo root` (NOT the worktree — the bg
orchestrator runs from the maintainer checkout), and an empty `name`. So the key
comes from `intent`, not the cwd basename; the same regex doubles as the flow-job
filter (a foreign job like `ft-1121` has a non-matching intent → excluded).

A session is STOPPABLE only when ALL hold (any busy/unknown signal → skip; the
classifier fails safe toward NOT stopping):
  - self-job — its job_id is not the orchestrator's own `$CLAUDE_JOB_DIR` basename.
  - flow-job + cross-project — `intent` parses a `<key>` AND `cwd` is the maintainer
    repo root (`<repo>`); a foreign project / non-flow job is skipped. The
    orchestrator's own bg job sits at the same repo root with the same intent shape,
    so the self-job flag + the non-terminal-bead gate (its own bead is in_progress)
    are what exclude it, not cwd.
  - activity — `tempo ∈ {idle, blocked}` (never stop a session reporting active work).
    `blocked` is admitted because a bg run that DIED blocked (rate limit, permission
    ask, auth outage) rests at `tempo == blocked` forever; the terminal-bead gate
    below separates that dead zombie from a genuine needs-input run, and the three
    independent signals (lease non-live ∧ transcript idle past stale ∧ bead terminal)
    still gate it. NOTE: `state` is deliberately NOT gated. A finished bg run rests at
    `state == working` (or `blocked`) INDEFINITELY — a `session_cron` keepalive task, or simply a daemon
    that never flips the field, holds it there; a clean `done` is the exception, not
    the rule (witnessed: a whole drain's worth of finished runs all sat at `working`,
    cron-bearing or not). Gating on `state ∈ {done, stopped}` therefore skipped the
    COMMON case and leaked every run as a zombie. Doneness instead rests on the three
    INDEPENDENT signals below; when `state` is not a clean terminal, the transcript
    must be idle past a LONGER `stale_idle_threshold` before those signals are trusted
    over the stale field. `claude stop` is non-destructive + resumable, so this
    replaces an unreliable proxy with direct evidence — not an erosion of the fail-safe.
  - lease (PRIMARY guard) — resolve the worktree run dir for `<key>`
    (`<repo>/.flow/worktrees/feat-<key>-<slug>/.flow/runs/<key>/`, the pool glob
    reap/drain use) and call `lease.classify(run_dir, now)`; `live` or `corrupt`
    → skip. This is the same mechanism reap uses to skip a mid-reflect session, so
    the catastrophic kill-mid-reflect failure is inherited-guarded. An ABSENT run
    dir (the worktree was already reaped, the COMMON post-reap cleanup case) reads
    as non-live and PROCEEDS — treating absent as skip would rebuild a silent
    no-op that never cleans up after reap.
  - transcript mtime — `state.json.linkScanPath` → the session transcript; mtime
    fresher than `now - idle_threshold` (or `now - stale_idle_threshold` when `state`
    is not a clean terminal) → still writing (mid-reflect even if tempo lags) → skip.
    Missing/empty/unreadable path → cannot prove idle → skip.
  - terminal bead — the `<key>` maps to a bead whose status ∈ {closed, blocked,
    deferred}. Open/in_progress → skip (it may relaunch). The bead lookup is
    injected (CLI backs it with `bd show <key> --json`).

Side effects are non-destructive to history: the transcript at
`~/.claude/projects/<slug>/<id>.jsonl` is untouched, so the session stays resumable
after stop or dir-removal.

CLI:
  evolve_session_cleanup.py --workspace-root <dir> [--self-job <basename>]
                            [--idle-threshold-secs N] [--stale-idle-threshold-secs N]
                            [--jobs-root <dir>] [--now <iso>]

Exit codes:
  0 = ok (prints the classification JSON)
  2 = tool error (bd failed; stderr propagated)
  4 = not a maintainer setup (dormant; nothing to clean)
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import lease
from _evolve_common import run_dir_for as _run_dir_for
from _timeutil import parse_iso, utcnow_iso
from maintainer import resolve_maintainer_repo

DEFAULT_IDLE_THRESHOLD_SECS = 300
# When `state` is NOT a clean terminal (the common case — a finished bg run rests at
# 'working'/'blocked'), the transcript must be idle past THIS longer threshold before
# the three done-signals are trusted over the stale state field. Longer than the
# normal idle threshold: extra caution before stopping a session whose own state field
# still claims it is working.
DEFAULT_STALE_IDLE_THRESHOLD_SECS = 600

_FLOW_INTENT_RE = re.compile(r"/flow\s+(flow-[a-z0-9]+(?:\.\d+)?)\b", re.IGNORECASE)
_TERMINAL_BEAD_STATUSES = {"closed", "blocked", "deferred"}
_STOPPABLE_STATES = {"done", "stopped"}

# A bd-status lookup: key -> status string (or None when unknown/missing).
BeadStatusLookup = Callable[[str], str | None]


class NotMaintainer(Exception):
    """Raised when the run is not in maintainer mode. Exit 4."""


class ToolError(Exception):
    """Raised when an injected tool (bd) fails. Exit 2."""


@dataclass(frozen=True)
class JobRecord:
    """A parsed `~/.claude/jobs/<id>/state.json` plus its dir.

    job_dir is the absolute path to the job directory (named by the 8-hex
    daemonShort). job_id is that basename — the handle `claude stop` accepts; the
    full session UUID does NOT work (`claude stop <uuid>` → "No job matching"). The
    session_id is kept only to locate the resumable transcript, never as a stop
    handle. The CLI builds these; classify is pure over them.
    """

    job_id: str  # the job dir basename (daemonShort, 8 hex) — the `claude stop` handle
    job_dir: str  # absolute path to ~/.claude/jobs/<job_id>/
    session_id: str  # full sessionId UUID (transcript handle, NOT a stop handle)
    state: str
    tempo: str
    cwd: str
    intent: str  # the launch prompt; "/flow <key> --auto" for a drain-launched run
    link_scan_path: str


def _key_from_intent(intent: str) -> str | None:
    """The flow key from a `/flow <key> --auto` launch intent, else None.

    Doubles as the flow-job filter: a foreign or non-flow job has a non-matching
    intent and yields None, so it is excluded.
    """
    m = _FLOW_INTENT_RE.search(intent)
    return m.group(1) if m else None


def _transcript_is_idle(link_scan_path: str, now_epoch: float, idle_threshold_secs: int) -> bool:
    """True iff the transcript exists and its mtime is older than the idle threshold.

    Missing/empty path or an unreadable/stat-failing file → False (cannot prove
    idle → caller skips, fail-safe).
    """
    if not link_scan_path:
        return False
    try:
        mtime = Path(link_scan_path).stat().st_mtime
    except OSError:
        return False
    return mtime <= now_epoch - idle_threshold_secs


def classify(
    records: list[JobRecord],
    repo: Path,
    now_iso: str,
    *,
    self_job: str | None,
    idle_threshold_secs: int,
    stale_idle_threshold_secs: int = DEFAULT_STALE_IDLE_THRESHOLD_SECS,
    bead_status: BeadStatusLookup,
) -> dict:
    """Pure core: bucket job records into stoppable / skipped.

    Gates run cheapest-first so the bead lookup (the only external dep) fires last,
    only for the handful of records that survive the filesystem gates. ANY busy or
    unprovable signal → skipped (fail-safe toward NOT stopping).
    """
    now_epoch = parse_iso(now_iso)
    now_ts = now_epoch.timestamp() if now_epoch is not None else None
    repo_resolved = repo.resolve()
    current_boot = lease.boot_id()
    host = lease.hostname()

    stoppable: list[dict] = []
    skipped: list[dict] = []

    def skip(rec: JobRecord, reason: str) -> None:
        skipped.append({"session_id": rec.session_id, "reason": reason})

    for rec in records:
        if self_job is not None and rec.job_id == self_job:
            skip(rec, "self-job")
            continue
        key = _key_from_intent(rec.intent)
        if key is None:
            skip(rec, "intent is not a /flow <key> --auto launch")
            continue
        if Path(rec.cwd).resolve() != repo_resolved:
            skip(rec, "cwd is not this repo's root")
            continue
        # tempo is the activity signal: never stop a session reporting active work.
        # `idle` and `blocked` are both admitted — a bg run that DIED blocked (rate
        # limit, permission ask, auth outage) rests at tempo=blocked forever, and the
        # bead-terminal gate below separates that dead zombie (bead terminal → eligible)
        # from a genuine needs-input run (bead open/in_progress → skipped). Any other
        # non-idle tempo (e.g. `active`) is real work → skip. `state` is NOT gated — a
        # finished bg run rests at 'working'/'blocked' indefinitely (module docstring),
        # so doneness rests on the three independent signals below (lease non-live ∧
        # transcript idle ∧ bead terminal).
        if rec.tempo not in ("idle", "blocked"):
            skip(rec, f"tempo not idle (tempo={rec.tempo!r})")
            continue

        run_dir = _run_dir_for(repo, key)
        lease_state = (
            "absent"
            if run_dir is None
            else str(
                lease.classify(run_dir, now_iso, current_boot=current_boot, hostname=host).get(
                    "state"
                )
            )
        )
        if lease_state in ("live", "corrupt"):
            skip(rec, f"lease is {lease_state}")
            continue

        # a clean terminal state trusts the normal idle threshold; a stale
        # 'working'/'blocked' state demands the LONGER threshold before we override it.
        # tempo=blocked never trusts the short bar even if state reads clean — a
        # dead-blocked zombie must clear the stale-idle bar.
        clean_terminal = rec.state in _STOPPABLE_STATES and rec.tempo != "blocked"
        required_idle = idle_threshold_secs if clean_terminal else stale_idle_threshold_secs
        if now_ts is None or not _transcript_is_idle(rec.link_scan_path, now_ts, required_idle):
            skip(
                rec,
                "transcript not provably idle"
                if clean_terminal
                else f"transcript not idle past stale threshold ({stale_idle_threshold_secs}s)",
            )
            continue

        status = bead_status(key)
        if status not in _TERMINAL_BEAD_STATUSES:
            skip(rec, f"bead {key} not terminal (status={status!r})")
            continue

        stoppable.append(
            {
                "session_id": rec.session_id,
                "job_id": rec.job_id,  # the `claude stop` handle (NOT session_id)
                "key": key,
                "cwd": rec.cwd,
                "job_dir": rec.job_dir,
                "reason": (
                    f"{'done' if clean_terminal else f'stale-{rec.state}'}/idle, "
                    f"bead {status}, lease {lease_state}"
                ),
            }
        )

    return {"stoppable": stoppable, "skipped": skipped}


def _enumerate_jobs(jobs_root: Path) -> list[JobRecord]:
    """Parse every `<jobs_root>/*/state.json`, skipping empty/malformed files.

    Most job dirs on disk carry a 0-byte state.json (a session that never wrote
    one); `json.loads("")` raises, so an empty or unparseable file is silently
    dropped (cannot classify → omit).
    """
    records: list[JobRecord] = []
    for state_path in sorted(glob.glob(str(jobs_root / "*" / "state.json"))):
        path = Path(state_path)
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        job_dir = path.parent
        records.append(
            JobRecord(
                job_id=job_dir.name,
                job_dir=str(job_dir),
                session_id=str(data.get("sessionId") or ""),
                state=str(data.get("state") or ""),
                tempo=str(data.get("tempo") or ""),
                cwd=str(data.get("cwd") or ""),
                intent=str(data.get("intent") or ""),
                link_scan_path=str(data.get("linkScanPath") or ""),
            )
        )
    return records


def _bd_status_lookup() -> BeadStatusLookup:
    """A live bead-status lookup backed by `bd show <key> --json` (`.status`).

    `bd show` returns the status regardless of state; `bd list` hides closed beads
    by default, and closed is the common terminal case here, so it must NOT back
    this lookup (it would return nothing for every shipped bead).
    """

    def lookup(key: str) -> str | None:
        result = subprocess.run(["bd", "show", key, "--json"], capture_output=True, text=True)
        if result.returncode != 0:
            raise ToolError(f"bd show {key} failed: {result.stderr.strip()}")
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return None
        if isinstance(data, list):
            data = data[0] if data else {}
        status = data.get("status") if isinstance(data, dict) else None
        return str(status) if status else None

    return lookup


def cleanup(
    workspace_root: Path,
    *,
    self_job: str | None,
    idle_threshold_secs: int,
    stale_idle_threshold_secs: int = DEFAULT_STALE_IDLE_THRESHOLD_SECS,
    jobs_root: Path,
    now_iso: str,
    bead_status: BeadStatusLookup | None = None,
) -> dict:
    repo = resolve_maintainer_repo(workspace_root)
    if repo is None:
        raise NotMaintainer("not a flow maintainer setup; nothing to clean")
    records = _enumerate_jobs(jobs_root)
    lookup = bead_status or _bd_status_lookup()
    return classify(
        records,
        repo,
        now_iso,
        self_job=self_job,
        idle_threshold_secs=idle_threshold_secs,
        stale_idle_threshold_secs=stale_idle_threshold_secs,
        bead_status=lookup,
    )


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Classify finished background sessions for the drain loop to stop."
    )
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument(
        "--self-job",
        default=None,
        help="the orchestrator's own $CLAUDE_JOB_DIR basename; skipped outright.",
    )
    parser.add_argument(
        "--idle-threshold-secs",
        type=int,
        default=DEFAULT_IDLE_THRESHOLD_SECS,
        help="a transcript with a fresher mtime than this is treated as still writing.",
    )
    parser.add_argument(
        "--stale-idle-threshold-secs",
        type=int,
        default=DEFAULT_STALE_IDLE_THRESHOLD_SECS,
        help="idle bar for a session whose state field is not a clean terminal "
        "(working/blocked); longer than --idle-threshold-secs.",
    )
    parser.add_argument("--jobs-root", default=None, help="override (default ~/.claude/jobs).")
    parser.add_argument("--now", default=None, help="override the clock (ISO8601).")
    args = parser.parse_args(argv)

    jobs_root = Path(args.jobs_root) if args.jobs_root else Path.home() / ".claude" / "jobs"
    now_iso = args.now or utcnow_iso()

    try:
        result = cleanup(
            Path(args.workspace_root),
            self_job=args.self_job,
            idle_threshold_secs=args.idle_threshold_secs,
            stale_idle_threshold_secs=args.stale_idle_threshold_secs,
            jobs_root=jobs_root,
            now_iso=now_iso,
        )
    except NotMaintainer as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
