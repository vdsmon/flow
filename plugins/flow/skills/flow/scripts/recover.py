"""FLOW workspace repair: inspect + remediate a broken per-ticket run.

Operates only on <workspace_root>/.flow/runs/<ticket>/. Reuses state, lease,
snapshot. `detect` never mutates; the other subcommands do the narrow,
user-confirmed remediations the SKILL.md recover prose drives.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import lease
import state
from _timeutil import utcnow_iso
from _workspace import WorkspaceConfigError, load_workspace_toml
from snapshot import verify_snapshot, write_snapshot


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


def _holder_liveness(holder: Any) -> dict[str, Any] | None:
    """Advisory liveness hint for a lease holder, or None when there is no holder.

    Best-effort and wrapped so it can never raise out of detect: it probes the recorded session_pid
    with a read-only `ps -p`. A live result can be a reused pid and a cross-host holder is not
    locally probeable, so this never gates reclaim; `takeover --force` stays the only reclaim path.
    """
    if holder is None:
        return None
    try:
        host = str(holder.get("hostname", ""))
        if host and host != lease.hostname():
            return {"probe": "skipped_cross_host", "alive": None}
        spid = int(holder.get("session_pid", 0))
        if spid <= 0:
            return {"probe": "unrecorded", "alive": None}
        probe = subprocess.run(["ps", "-p", str(spid)], capture_output=True, check=False)
        return {"probe": "ps", "alive": probe.returncode == 0, "session_pid": spid}
    except Exception:
        return {"probe": "error", "alive": None}


def detect(workspace_root: Path, ticket: str, *, now_iso: str | None = None) -> dict[str, Any]:
    now_iso = now_iso or utcnow_iso()
    td = _ticket_dir(workspace_root, ticket)
    ts, state_exit = state.read(td)
    stages = {name: rec.status for name, rec in ts.stages.items()} if ts is not None else None
    lease_info = lease.classify(
        td, now_iso, current_boot=lease.boot_id(), hostname=lease.hostname()
    )
    ok, detail = verify_snapshot(workspace_root, ticket, skill_root=_skill_root())
    return {
        "ticket": ticket,
        "state_exit": state_exit,
        "stages": stages,
        "lease": lease_info,
        "holder_liveness": _holder_liveness(lease_info.get("holder")),
        "snapshot": {"ok": ok, "detail": detail},
        "ship_event_attention": _ship_event_attention(workspace_root),
    }


def takeover(
    workspace_root: Path, ticket: str, *, now_iso: str | None = None, force: bool = False
) -> tuple[int, dict[str, Any]]:
    now_iso = now_iso or utcnow_iso()
    td = _ticket_dir(workspace_root, ticket)
    reset: list[str] = []

    def _reset_and_snapshot() -> None:
        # runs while takeover_clear STILL holds the lease flock, so a concurrent
        # acquire cannot land between the clear and these resets and have its
        # just-begun stage forced back to pending under it.
        ts, _ = state.read(td)
        if ts is not None:
            for name, rec in ts.stages.items():
                if rec.status == "in_progress":
                    state.force_stage_status(td, name, "pending")
                    reset.append(name)
        with contextlib.suppress(Exception):
            write_snapshot(workspace_root, ticket, skill_root=_skill_root())

    result = lease.takeover_clear(
        td,
        now_iso,
        current_boot=lease.boot_id(),
        hostname=lease.hostname(),
        force=force,
        on_cleared=_reset_and_snapshot,
    )
    if not result["cleared"]:
        return 1, {"error": "lease is live; cannot take over", "holder": result["holder"]}
    quarantined = result["quarantined"]
    payload: dict[str, Any] = {"ticket": ticket, "took_over": True, "reset_stages": reset}
    if quarantined is not None:
        payload["quarantined"] = str(quarantined)
    return 0, payload


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


def abort(workspace_root: Path, ticket: str, *, force: bool = False) -> tuple[int, dict[str, Any]]:
    td = _ticket_dir(workspace_root, ticket)
    result = lease.takeover_clear(
        td,
        utcnow_iso(),
        current_boot=lease.boot_id(),
        hostname=lease.hostname(),
        force=force,
    )
    if not result["cleared"]:
        return 1, {
            "ticket": ticket,
            "aborted": False,
            "error": "lease is live; refusing to abort. Re-run with --force to release it.",
            "holder": result["holder"],
        }
    payload: dict[str, Any] = {
        "ticket": ticket,
        "aborted": True,
        "lease_removed": result["state"] != "free",
    }
    if result["quarantined"] is not None:
        payload["quarantined"] = str(result["quarantined"])
    return 0, payload


def reload_snapshot(workspace_root: Path, ticket: str) -> tuple[int, dict[str, Any]]:
    try:
        write_snapshot(workspace_root, ticket, skill_root=_skill_root())
    except Exception as exc:
        return 1, {
            "ticket": ticket,
            "snapshot_reloaded": False,
            "error": f"snapshot write failed: {exc}",
        }
    return 0, {"ticket": ticket, "snapshot_reloaded": True}


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="FLOW workspace repair: inspect + remediate a run."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ticket", required=True)
    common.add_argument("--workspace-root", default=".")
    sub.add_parser("detect", parents=[common], help="Report what is broken (no mutation).")
    p_take = sub.add_parser(
        "takeover", parents=[common], help="Clear a stale lock + reset in_progress stages."
    )
    p_take.add_argument(
        "--force",
        action="store_true",
        help="Reclaim even a LIVE-looking lease (operator asserts holder deadness).",
    )
    p_abort = sub.add_parser("abort", parents=[common], help="Release the run lock; leave state.")
    p_abort.add_argument(
        "--force", action="store_true", help="Release even a LIVE lease (de-mutex; operator-only)."
    )
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
        rc, payload = takeover(workspace_root, args.ticket, force=args.force)
    elif args.cmd == "retry":
        rc, payload = _force(workspace_root, args.ticket, args.stage, "pending")
    elif args.cmd == "skip":
        rc, payload = _force(workspace_root, args.ticket, args.stage, "completed")
    elif args.cmd == "abort":
        rc, payload = abort(workspace_root, args.ticket, force=args.force)
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
