"""Claude Code SessionStart hook for /flow.

Renders the evolve-loop deadman/staleness block (a machine-wide maintainer-ops
monitor) to stdout (Claude Code injects stdout as session context).

Recall moved to the PLAN phase (it lives in skill prose now, so it ports to any
agent that runs the skill); this hook no longer recalls. What stays is the
staleness block, which is unrelated to recall and not a portability concern.

Robustness: any error must NOT crash the session. `cli_main` wraps the whole
thing and always exits 0. A hook that errors should be silent, not fatal.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

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
    return Path.home() / ".flow-evolve" / "run-record.jsonl"


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


def staleness_block(record_path: Path, now: datetime) -> str:
    """Warn when an armed evolve schedule's run-record signals trouble.

    Three conditions, per schedule, in priority order:
      1. hung: a `start` with no `end` after it, past the zombie grace (3h nightly
         / 6h weekly). A run in flight within grace stays silent.
      2. fail: the latest `end` recorded outcome `fail` (trap-EXIT crash-capture).
      3. stale: the latest `end` is older than the staleness threshold (36h / 8d).

    Hung keys on `last_start > last_end` (not `last_end is None`), so a fresh
    hung start is caught even with prior completed runs in the accumulating file.
    Returns "" when the record file is absent (no schedule armed here) or every
    armed schedule is healthy. Any io/parse error is swallowed -> "".
    """
    try:
        text = record_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    last_start, last_end, last_outcome = _parse_run_records(text)
    warnings: list[str] = []
    for sched, threshold in _STALE_THRESHOLDS_S.items():
        marker = record_path.parent / f"disarmed-{sched}"
        if marker.exists():
            warnings.append(_disarmed_line(sched))
            continue
        start = last_start.get(sched)
        end = last_end.get(sched)
        if start is not None and (end is None or start > end):
            grace = _ZOMBIE_GRACE_S[sched]
            age = (now - start).total_seconds()
            if age > grace:
                warnings.append(_hung_line(sched, age, grace))
            continue
        if end is None:
            continue
        if last_outcome.get(sched) == "fail":
            warnings.append(_fail_line(sched))
            continue
        age = (now - end).total_seconds()
        if age > threshold:
            warnings.append(_stale_line(sched, age, threshold))
    if not warnings:
        return ""
    return "\n".join(["## /flow ops", "", *warnings])


# ─── Orchestration ─────────────────────────────────────────────────────────────


def cli_main(run_record_path: Path | None = None) -> int:
    try:
        record = run_record_path if run_record_path is not None else _run_record_path()
        # The evolve deadman is machine-level (~/.flow-evolve/), so it renders in every
        # session, not only ones started inside a flow workspace.
        staleness = staleness_block(record, _now())
        if staleness:
            sys.stdout.write(staleness + "\n")
    except Exception:
        # A hook crash must never break the session; swallow everything.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())


__all__ = ["cli_main", "staleness_block"]
