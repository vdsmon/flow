"""/flow recover: inspect + remediate a broken per-ticket run.

Operates only on <workspace_root>/.flow/runs/<ticket>/. Reuses state, lease,
snapshot, heartbeat. `detect` never mutates; the other subcommands do the
narrow, user-confirmed remediations the SKILL.md recover prose drives.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import heartbeat
import lease
import state
from _workspace import WorkspaceConfigError, load_workspace_toml
from snapshot import verify_snapshot, write_snapshot


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ticket_dir(workspace_root: Path, ticket: str) -> Path:
    return workspace_root / ".flow" / "runs" / ticket


def _skill_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _ship_event_attention(workspace_root: Path) -> int:
    try:
        data = load_workspace_toml(workspace_root)
    except WorkspaceConfigError:
        return 0
    memory = data.get("memory")
    namespace = memory.get("namespace") if isinstance(memory, dict) else None
    if not namespace:
        return 0
    ship_dir = workspace_root / ".flow" / str(namespace) / "ship-events"
    if not ship_dir.is_dir():
        return 0
    count = 0
    for p in ship_dir.iterdir():
        name = p.name
        if ".dupe." in name or ".corrupt" in name or name.startswith(".quarantine-intent"):
            count += 1
    return count


def detect(workspace_root: Path, ticket: str, *, now_iso: str | None = None) -> dict[str, Any]:
    now_iso = now_iso or _now_iso()
    td = _ticket_dir(workspace_root, ticket)
    ts, state_exit = state.read(td)
    stages = {name: rec.status for name, rec in ts.stages.items()} if ts is not None else None
    lease_info = lease.classify(td, now_iso, current_boot=lease.boot_id())
    ok, detail = verify_snapshot(workspace_root, ticket, skill_root=_skill_root())
    progress: dict[str, str] = {}
    if ts is not None:
        for name, rec in ts.stages.items():
            if rec.status != "in_progress":
                continue
            prog = heartbeat.read_progress(td, name)
            if prog is not None:
                progress[name] = heartbeat.detect_hung(prog, now_iso)
    return {
        "ticket": ticket,
        "state_exit": state_exit,
        "stages": stages,
        "lease": lease_info,
        "snapshot": {"ok": ok, "detail": detail},
        "progress": progress,
        "ship_event_attention": _ship_event_attention(workspace_root),
    }


def takeover(
    workspace_root: Path, ticket: str, *, now_iso: str | None = None
) -> tuple[int, dict[str, Any]]:
    now_iso = now_iso or _now_iso()
    td = _ticket_dir(workspace_root, ticket)
    info = lease.classify(td, now_iso, current_boot=lease.boot_id())
    if info["state"] == "live":
        return 1, {"error": "lease is live; cannot take over", "holder": info["holder"]}
    lease.run_lock_path(td).unlink(missing_ok=True)
    reset: list[str] = []
    ts, _ = state.read(td)
    if ts is not None:
        for name, rec in ts.stages.items():
            if rec.status == "in_progress":
                state.force_stage_status(td, name, "pending")
                reset.append(name)
    with contextlib.suppress(Exception):
        write_snapshot(workspace_root, ticket, skill_root=_skill_root())
    return 0, {"ticket": ticket, "took_over": True, "reset_stages": reset}


def _force(
    workspace_root: Path, ticket: str, stage: str, status: state.StageStatus
) -> tuple[int, dict[str, Any]]:
    td = _ticket_dir(workspace_root, ticket)
    ts, _ = state.read(td)
    if ts is None:
        return 2, {"error": f"no state.json at {td}"}
    try:
        state.force_stage_status(td, stage, status)
    except ValueError as exc:
        return 1, {"error": str(exc)}
    return 0, {"ticket": ticket, "stage": stage, "status": status}


def abort(workspace_root: Path, ticket: str) -> tuple[int, dict[str, Any]]:
    td = _ticket_dir(workspace_root, ticket)
    lock = lease.run_lock_path(td)
    removed = lock.exists()
    lock.unlink(missing_ok=True)
    return 0, {"ticket": ticket, "aborted": True, "lease_removed": removed}


def reload_snapshot(workspace_root: Path, ticket: str) -> tuple[int, dict[str, Any]]:
    with contextlib.suppress(Exception):
        write_snapshot(workspace_root, ticket, skill_root=_skill_root())
    return 0, {"ticket": ticket, "snapshot_reloaded": True}


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="/flow recover: inspect + remediate a run.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ticket", required=True)
    common.add_argument("--workspace-root", default=".")
    sub.add_parser("detect", parents=[common], help="Report what is broken (no mutation).")
    sub.add_parser(
        "takeover", parents=[common], help="Clear a stale lock + reset in_progress stages."
    )
    sub.add_parser("abort", parents=[common], help="Release the run lock; leave state.")
    sub.add_parser("reload-snapshot", parents=[common], help="Accept current config (clear drift).")
    p_retry = sub.add_parser("retry", parents=[common], help="Reset a stage to pending.")
    p_retry.add_argument("--stage", required=True)
    p_skip = sub.add_parser("skip", parents=[common], help="Mark a stage completed.")
    p_skip.add_argument("--stage", required=True)
    args = parser.parse_args(argv)

    workspace_root = Path(args.workspace_root).expanduser().resolve()
    if args.cmd == "detect":
        rc, payload = 0, detect(workspace_root, args.ticket)
    elif args.cmd == "takeover":
        rc, payload = takeover(workspace_root, args.ticket)
    elif args.cmd == "retry":
        rc, payload = _force(workspace_root, args.ticket, args.stage, "pending")
    elif args.cmd == "skip":
        rc, payload = _force(workspace_root, args.ticket, args.stage, "completed")
    elif args.cmd == "abort":
        rc, payload = abort(workspace_root, args.ticket)
    elif args.cmd == "reload-snapshot":
        rc, payload = reload_snapshot(workspace_root, args.ticket)
    else:
        sys.stderr.write(f"unknown subcommand {args.cmd!r}\n")
        return 1

    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    if rc != 0 and "error" in payload:
        sys.stderr.write(str(payload["error"]) + "\n")
    return rc


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "abort",
    "cli_main",
    "detect",
    "reload_snapshot",
    "takeover",
]
