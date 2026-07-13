"""Host-neutral maintainer preflight for Flow's scheduled loops.

The launchd templates append durable run records under ``~/.flow-evolve``.
This module reduces that ledger into the same deadman signals on every harness:
hung, failed, stale, or deliberately disarmed.  Bare Flow and ``FLOW maintain``
run it explicitly; it is not tied to a session-start hook.

The check is diagnostic.  An absent ledger means the schedules have never been
armed on this machine and is therefore healthy and silent.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import lease

# evolve-loop deadman: ops/*-evolve.sh.template append a run-record line per fire to
# ~/.flow-evolve/run-record.jsonl. Absence of the file means no schedule is armed on
# this machine, so the warning self-gates. Thresholds give one missed fire of slack.
_STALE_THRESHOLDS_S = {"nightly": 36 * 3600, "weekly": 8 * 86400}
# A start with no end past this grace = a hung run (the witnessed zombie class).
# Tighter than the staleness bar: a genuinely-stuck run surfaces in hours, not days.
_ZOMBIE_GRACE_S = {"nightly": 3 * 3600, "weekly": 6 * 3600}
_SCHEDULE_LABEL = {"nightly": "nightly evolve", "weekly": "weekly epic"}


# ─── Evolve-loop staleness (deadman) ───────────────────────────────────────────


def _run_record_path() -> Path:
    root = Path(os.environ.get("FLOW_MAINTAINER_HOME", Path.home() / ".flow-evolve"))
    return root.expanduser() / "run-record.jsonl"


def _now() -> datetime:
    return datetime.now(UTC)


def _stale_line(sched: str, age_s: float, threshold_s: int) -> str:
    label = _SCHEDULE_LABEL.get(sched, sched)
    if threshold_s >= 2 * 86400:
        age, thr = f"{age_s / 86400:.1f}d", f"{threshold_s // 86400}d"
    else:
        age, thr = f"{age_s / 3600:.0f}h", f"{threshold_s // 3600}h"
    return (
        f"- ⚠️ {label} loop stale: last run {age} ago (>{thr}); "
        f"check the launchd timer and ~/.flow-evolve/logs."
    )


def _fail_line(sched: str) -> str:
    label = _SCHEDULE_LABEL.get(sched, sched)
    return f"- ⚠️ {label} last run recorded `fail`; check ~/.flow-evolve/logs for the failed fire."


def _hung_line(sched: str, age_s: float, grace_s: int) -> str:
    label = _SCHEDULE_LABEL.get(sched, sched)
    return (
        f"- ⚠️ {label} run started {age_s / 3600:.0f}h ago with no completion "
        f"(>{grace_s // 3600}h grace — hung?); check ~/.flow-evolve/logs."
    )


def _disarmed_line(sched: str) -> str:
    label = _SCHEDULE_LABEL.get(sched, sched)
    return f"- {label} loop disarmed (re-arm with: loopctl.sh arm {sched})"


@dataclass(frozen=True)
class PreflightIssue:
    schedule: str
    state: str
    severity: str
    message: str


@dataclass(frozen=True)
class PreflightReport:
    record_path: str
    configured: bool
    issues: tuple[PreflightIssue, ...]

    @property
    def attention_required(self) -> bool:
        return any(issue.severity == "warning" for issue in self.issues)

    def to_dict(self) -> dict[str, object]:
        return {
            "record_path": self.record_path,
            "configured": self.configured,
            "attention_required": self.attention_required,
            "issues": [asdict(issue) for issue in self.issues],
        }


@dataclass(frozen=True)
class MaintenanceBoundaryReport:
    workspace_root: str
    checkout_clean: bool
    live_leases: tuple[str, ...]
    corrupt_leases: tuple[str, ...]
    error: str = ""

    @property
    def clear(self) -> bool:
        return (
            self.checkout_clean
            and not self.live_leases
            and not self.corrupt_leases
            and not self.error
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "checkout_clean": self.checkout_clean,
            "clear": self.clear,
            "corrupt_leases": list(self.corrupt_leases),
            "error": self.error,
            "live_leases": list(self.live_leases),
            "workspace_root": self.workspace_root,
        }


def _parse_run_records(
    text: str,
) -> tuple[dict[str, datetime], dict[str, datetime], dict[str, str]]:
    """Reduce a run-record.jsonl body to the latest start/end/outcome per schedule."""
    last_start: dict[str, datetime] = {}
    last_end: dict[str, datetime] = {}
    last_outcome: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        sched = str(rec.get("schedule") or "")
        phase = str(rec.get("phase") or "")
        ts_raw = str(rec.get("ts") or "")
        if sched not in _STALE_THRESHOLDS_S or not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if phase == "start" and (sched not in last_start or ts > last_start[sched]):
            last_start[sched] = ts
        elif phase == "end" and (sched not in last_end or ts > last_end[sched]):
            last_end[sched] = ts
            last_outcome[sched] = str(rec.get("outcome") or "")
    return last_start, last_end, last_outcome


def evaluate_run_records(record_path: Path, now: datetime) -> PreflightReport:
    """Reduce an evolve schedule ledger to host-neutral preflight issues.

    Three conditions, per schedule, in priority order:
      1. hung: a `start` with no `end` after it, past the zombie grace (3h nightly
         / 6h weekly). A run in flight within grace stays silent.
      2. fail: the latest `end` recorded outcome `fail` (trap-EXIT crash-capture).
      3. stale: the latest `end` is older than the staleness threshold (36h / 8d).

    Hung keys on `last_start > last_end` (not `last_end is None`), so a fresh
    hung start is caught even with prior completed runs in the accumulating file.
    An absent ledger means no schedule is configured here. An unreadable ledger is
    surfaced as unavailable evidence rather than silently looking healthy.
    """
    try:
        text = record_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return PreflightReport(str(record_path), False, ())
    except OSError as exc:
        issue = PreflightIssue(
            "all",
            "unavailable",
            "warning",
            f"schedule ledger unavailable at {record_path}: {exc}",
        )
        return PreflightReport(str(record_path), True, (issue,))
    last_start, last_end, last_outcome = _parse_run_records(text)
    issues: list[PreflightIssue] = []
    for sched, threshold in _STALE_THRESHOLDS_S.items():
        marker = record_path.parent / f"disarmed-{sched}"
        if marker.exists():
            issues.append(PreflightIssue(sched, "disarmed", "info", _disarmed_line(sched)[2:]))
            continue
        start = last_start.get(sched)
        end = last_end.get(sched)
        if start is not None and (end is None or start > end):
            grace = _ZOMBIE_GRACE_S[sched]
            age = (now - start).total_seconds()
            if age > grace:
                issues.append(
                    PreflightIssue(sched, "hung", "warning", _hung_line(sched, age, grace)[2:])
                )
            continue
        if end is None:
            continue
        if last_outcome.get(sched) == "fail":
            issues.append(PreflightIssue(sched, "failed", "warning", _fail_line(sched)[2:]))
            continue
        age = (now - end).total_seconds()
        if age > threshold:
            issues.append(
                PreflightIssue(
                    sched,
                    "stale",
                    "warning",
                    _stale_line(sched, age, threshold)[2:],
                )
            )
    return PreflightReport(str(record_path), True, tuple(issues))


def render_preflight(report: PreflightReport) -> str:
    """Render only actionable or informational schedule state."""
    if not report.issues:
        return ""
    return "\n".join(
        ["Flow maintainer preflight", "", *(f"- {issue.message}" for issue in report.issues)]
    )


def staleness_block(record_path: Path, now: datetime) -> str:
    """Return the pure deadman rendering for a run-record ledger."""
    return render_preflight(evaluate_run_records(record_path, now))


def evaluate_maintenance_boundary(
    workspace_root: Path, now: datetime | None = None
) -> MaintenanceBoundaryReport:
    """Prove a clean, lease-free boundary before checkout or plugin mutation."""

    root = workspace_root.expanduser().resolve()
    if not root.is_dir():
        return MaintenanceBoundaryReport(
            str(root), False, (), (), "workspace root is not a directory"
        )
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        return MaintenanceBoundaryReport(
            str(root), False, (), (), f"git status failed (rc={result.returncode}): {detail}"
        )

    runs = root / ".flow" / "runs"
    live: list[str] = []
    corrupt: list[str] = []
    now_iso = (now or _now()).astimezone(UTC).isoformat()
    if runs.is_dir():
        current_boot = lease.boot_id()
        hostname = socket.gethostname()
        for lock in sorted(runs.glob("**/run.lock")):
            owner_dir = lock.parent
            relative = owner_dir.relative_to(runs).as_posix()
            info = lease.classify(owner_dir, now_iso, current_boot=current_boot, hostname=hostname)
            if info.get("state") == "live":
                live.append(relative)
            elif info.get("state") == "corrupt":
                corrupt.append(relative)
    return MaintenanceBoundaryReport(str(root), not result.stdout, tuple(live), tuple(corrupt))


# ─── Orchestration ─────────────────────────────────────────────────────────────


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect Flow maintainer schedule health.")
    parser.add_argument("--run-record", type=Path, default=None)
    parser.add_argument("--now", help="UTC ISO-8601 timestamp (tests/diagnostics)")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument(
        "--require-clean-boundary",
        action="store_true",
        help="exit 3 unless the checkout is clean and no base/revision lease is live",
    )
    return parser


def _parse_now(raw: str | None) -> datetime:
    if raw is None:
        return _now()
    parsed = datetime.fromisoformat(raw)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def cli_main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        now = _parse_now(args.now)
    except ValueError as exc:
        parser.error(f"invalid --now timestamp: {exc}")
    record = args.run_record if args.run_record is not None else _run_record_path()
    report = evaluate_run_records(record.expanduser(), now)
    boundary = (
        evaluate_maintenance_boundary(args.workspace_root, now)
        if args.workspace_root is not None
        else None
    )
    if args.as_json:
        payload = report.to_dict()
        if boundary is not None:
            payload["boundary"] = boundary.to_dict()
        print(json.dumps(payload, sort_keys=True))
    else:
        rendered = render_preflight(report)
        if rendered:
            print(rendered)
        if boundary is not None and not boundary.clear:
            print("Flow maintenance boundary blocked")
            if not boundary.checkout_clean:
                print("- checkout is dirty")
            if boundary.live_leases:
                print(f"- live leases: {', '.join(boundary.live_leases)}")
            if boundary.corrupt_leases:
                print(f"- corrupt leases: {', '.join(boundary.corrupt_leases)}")
            if boundary.error:
                print(f"- error: {boundary.error}")
    if args.require_clean_boundary:
        if boundary is None:
            parser.error("--require-clean-boundary requires --workspace-root")
        if not boundary.clear:
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "MaintenanceBoundaryReport",
    "PreflightIssue",
    "PreflightReport",
    "cli_main",
    "evaluate_maintenance_boundary",
    "evaluate_run_records",
    "render_preflight",
    "staleness_block",
]
