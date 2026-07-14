"""Explain where a Flow run spent time and which friction it recorded.

The analysis core is pure: ordered stage intervals, between-stage gaps, and
run-scoped friction entries go in; a deterministic receipt comes out. The CLI only
loads state/friction and optionally publishes that receipt atomically.

CLI:
  run_report.py --workspace-root DIR --ticket-dir DIR [--json] [--output PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import _memory_paths
import state
from _atomicio import atomic_write_text
from _jsonl import read_jsonl_lenient
from _timeutil import parse_iso

SCHEMA_VERSION = 1


class RunReportError(Exception):
    """The run receipt could not be constructed from local evidence."""


def _seconds(start: datetime | None, end: datetime | None) -> float:
    if start is None or end is None:
        return 0.0
    return max(0.0, round((end - start).total_seconds(), 3))


def _stage_rows(ticket_state: state.TicketState, now: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, record in ticket_state.stages.items():
        started = parse_iso(record.started_at_iso)
        finished = parse_iso(record.finished_at_iso)
        effective_end = finished or (now if started and record.status == "in_progress" else None)
        rows.append(
            {
                "stage": name,
                "status": record.status,
                "started_at": record.started_at_iso,
                "finished_at": record.finished_at_iso,
                "seconds": _seconds(started, effective_end),
            }
        )
    return rows


def _gap_rows(ticket_state: state.TicketState) -> list[dict[str, Any]]:
    started_run = parse_iso(ticket_state.started_at)
    prior_end = started_run
    prior_name = "run start"
    rows: list[dict[str, Any]] = []
    for name, record in ticket_state.stages.items():
        started = parse_iso(record.started_at_iso)
        if started is None:
            continue
        gap = _seconds(prior_end, started)
        if gap > 0:
            rows.append(
                {
                    "after": prior_name,
                    "before": name,
                    "label": f"wait after {prior_name} before {name}",
                    "seconds": gap,
                }
            )
        finished = parse_iso(record.finished_at_iso)
        prior_end = finished or started
        prior_name = name
    return rows


def _friction_receipt(
    ticket_state: state.TicketState, friction_entries: list[dict[str, Any]]
) -> dict[str, Any]:
    events = [entry for entry in friction_entries if entry.get("run_id") == ticket_state.run_id]
    events.sort(key=lambda entry: str(entry.get("ts", "")))
    by_type = Counter(str(entry.get("type") or "UNKNOWN") for entry in events)
    by_stage = Counter(str(entry.get("stage") or "unknown") for entry in events)
    normalized = [
        {
            "ts": entry.get("ts"),
            "stage": entry.get("stage"),
            "type": entry.get("type"),
            "severity": entry.get("severity"),
            "body": entry.get("body"),
            "detail": entry.get("detail"),
        }
        for entry in events
    ]
    return {
        "count": len(events),
        "by_type": dict(sorted(by_type.items())),
        "by_stage": dict(sorted(by_stage.items())),
        "events": normalized,
    }


def analyze(
    ticket_state: state.TicketState,
    friction_entries: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return deterministic timing and friction evidence for one run."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    stages = _stage_rows(ticket_state, current)
    gaps = _gap_rows(ticket_state)
    started = parse_iso(ticket_state.started_at)
    terminal_times = [parse_iso(row["finished_at"]) for row in stages if row["finished_at"]]
    has_active = any(row["status"] == "in_progress" for row in stages)
    ended = (
        current
        if has_active
        else max((value for value in terminal_times if value), default=started)
    )
    total = _seconds(started, ended)
    contributions: list[dict[str, Any]] = [
        {"kind": "stage", "label": row["stage"], "seconds": row["seconds"]}
        for row in stages
        if row["seconds"] > 0
    ]
    contributions.extend(
        {"kind": "gap", "label": row["label"], "seconds": row["seconds"]}
        for row in gaps
        if row["seconds"] > 0
    )
    contributions.sort(key=lambda item: (-item["seconds"], item["label"]))
    for item in contributions:
        seconds_value = float(item["seconds"])
        item["percent"] = round(seconds_value / total * 100, 1) if total else 0.0
    return {
        "schema_version": SCHEMA_VERSION,
        "ticket": ticket_state.ticket,
        "run_id": ticket_state.run_id,
        "started_at": ticket_state.started_at,
        "ended_at": ended.isoformat() if ended else None,
        "total_seconds": total,
        "stages": stages,
        "gaps": gaps,
        "top_time": contributions[:5],
        "friction": _friction_receipt(ticket_state, friction_entries),
    }


def _duration(seconds: float) -> str:
    rounded = round(seconds)
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def render_text(report: dict[str, Any]) -> str:
    """Render a compact final-summary block without inferring who caused a gap."""
    lines = [f"Run time: {_duration(report['total_seconds'])}"]
    top_time = report["top_time"]
    if top_time:
        lines.append("Most time:")
        lines.extend(
            (f"- {item['label']}: {_duration(item['seconds'])} ({item['percent']:.1f}%)")
            for item in top_time[:3]
        )
    friction = report["friction"]
    if friction["count"] == 0:
        lines.append("Friction: none recorded.")
    else:
        type_summary = ", ".join(f"{name} x{count}" for name, count in friction["by_type"].items())
        lines.append(f"Friction: {friction['count']} event(s) — {type_summary}.")
        lines.extend(
            f"- {event['stage']} · {event['type']}: {event['body']}"
            for event in friction["events"][:3]
        )
    return "\n".join(lines) + "\n"


def _load_friction(workspace_root: Path) -> list[dict[str, Any]]:
    try:
        namespace = _memory_paths.resolve_namespace(workspace_root)
        path = _memory_paths.friction_path(workspace_root, namespace)
        return read_jsonl_lenient(path)
    except (OSError, UnicodeError, _memory_paths._MemoryConfigError):
        return []


def build_report(workspace_root: Path, ticket_dir: Path) -> dict[str, Any]:
    ticket_state, read_code = state.read(ticket_dir)
    if ticket_state is None:
        detail = "unrecoverable state" if read_code == 2 else "state.json is missing"
        raise RunReportError(f"cannot report run: {detail} at {ticket_dir}")
    return analyze(ticket_state, _load_friction(workspace_root))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--ticket-dir", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser


def cli_main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        report = build_report(args.workspace_root.resolve(), args.ticket_dir.resolve())
    except RunReportError as exc:
        print(f"run-report: {exc}", file=sys.stderr)
        return 2
    serialized = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.output is not None:
        atomic_write_text(args.output.resolve(), serialized)
    print(serialized if args.json else render_text(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))
