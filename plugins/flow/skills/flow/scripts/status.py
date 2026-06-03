"""/flow status: a local table over per-ticket runs.

Stdlib-only, offline (no tracker / network). Aggregates every
.flow/runs/<ticket>/state.json in the workspace plus its lease state.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import lease
import state
from _workspace import WorkspaceConfigError, load_workspace_toml


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _runs_dir(workspace_root: Path) -> Path:
    return workspace_root / ".flow" / "runs"


def _pipeline_order(workspace_root: Path) -> list[str] | None:
    # state.json serializes stage keys sorted, so ts.stages order is alphabetical,
    # not pipeline order. The authoritative order is workspace.toml [pipeline].stages.
    try:
        data = load_workspace_toml(workspace_root)
    except WorkspaceConfigError:
        return None
    pipeline = data.get("pipeline")
    stages = pipeline.get("stages") if isinstance(pipeline, dict) else None
    if isinstance(stages, list):
        return [str(s) for s in stages]
    return None


def _summarize(
    ts: state.TicketState, ticket_dir: Path, now_iso: str, order: list[str]
) -> dict[str, Any]:
    statuses = {name: rec.status for name, rec in ts.stages.items()}
    completed = sum(1 for s in statuses.values() if s == "completed")
    failed_count = sum(1 for s in statuses.values() if s == "failed")
    in_progress = sum(1 for s in statuses.values() if s == "in_progress")

    failed_stage = state.find_failed(ts)
    if failed_stage is not None:
        next_or_blocked = f"{failed_stage}:failed"
    else:
        nxt = state.pick_next_pending(ts, order)
        next_or_blocked = "done" if nxt is None else f"{nxt}:{statuses[nxt]}"

    classify = lease.classify(ticket_dir, now_iso, current_boot=lease.boot_id())
    return {
        "ticket": ts.ticket,
        "run_id": ts.run_id,
        "backend": ts.backend,
        "total_stages": len(statuses),
        "completed": completed,
        "failed": failed_count,
        "in_progress": in_progress,
        "next_or_blocked": next_or_blocked,
        "lease": classify["state"],
    }


def collect(
    workspace_root: Path,
    *,
    ticket: str | None = None,
    now_iso: str | None = None,
) -> list[dict[str, Any]]:
    now_iso = now_iso or _now_iso()
    runs = _runs_dir(workspace_root)
    rows: list[dict[str, Any]] = []
    if not runs.is_dir():
        return rows
    dirs = [runs / ticket] if ticket else sorted(p for p in runs.iterdir() if p.is_dir())
    for td in dirs:
        if not (td / "state.json").exists():
            continue
        ts, _ = state.read(td)
        if ts is None:
            continue
        order = _pipeline_order(workspace_root) or list(ts.stages.keys())
        rows.append(_summarize(ts, td, now_iso, order))
    rows.sort(key=lambda r: r["ticket"])
    return rows


def render_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no runs)"
    headers = ["TICKET", "PROGRESS", "STATE", "LEASE"]
    table = [headers]
    for r in rows:
        table.append(
            [
                str(r["ticket"]),
                f"{r['completed']}/{r['total_stages']}",
                str(r["next_or_blocked"]),
                str(r["lease"]),
            ]
        )
    widths = [max(len(row[i]) for row in table) for i in range(len(headers))]
    return "\n".join(
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in table
    )


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="/flow status: local run table.")
    parser.add_argument("--ticket", default=None)
    parser.add_argument("--workspace-root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    workspace_root = Path(args.workspace_root).expanduser().resolve()
    if not (workspace_root / ".flow").is_dir():
        sys.stderr.write("status: workspace not initialized; run `/flow init`\n")
        return 1
    rows = collect(workspace_root, ticket=args.ticket)
    if args.json:
        sys.stdout.write(json.dumps(rows, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_table(rows) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["cli_main", "collect", "render_table"]
