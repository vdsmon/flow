"""State-machine driver for /flow do <ticket>.

Library + thin CLI. Stdlib-only. Imports `state` + `validate_workspace`.

Subcommands: `init`, `next`, `finish`, `status`. The dispatcher does NOT
invoke handlers itself; it reads/writes state.json and emits a handler-
descriptor JSON for the SKILL.md prose layer to act on.

Lifecycle: pending → in_progress (via `next`) → completed | failed (via
`finish`).

HARD GATE: validate_workspace.validate() runs on every `init` and every
`next`. Schema violation = exit 1, stderr lists violations.

Exit codes:
    0 = ok
    1 = generic error / validate-workspace failure / state malformed /
        ticket locked by a live run / config-version drift mid-run
    2 = no such ticket dir / not yet initialized
    5 = stale foreign lease (needs /flow recover --takeover)
    7 = lost lease (another run took over)
"""

from __future__ import annotations

import argparse
import contextlib
import json
import secrets
import socket
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import lease
import recall_pending
import state
import validate_workspace as vw
from _registry import registry_by_name
from snapshot import verify_snapshot, write_snapshot

_STAGE_REGISTRY_RELATIVE = Path("stage-registry.toml")

# Lease covering the init handshake before the first stage timeout is known.
_INIT_TTL_S = 600
# Slack added to a stage's timeout so the lease outlives the stage it covers.
_LEASE_BUFFER_S = 300


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── Handler-string parsing ──────────────────────────────────────────────────


def _parse_handler(value: str) -> dict[str, Any]:
    """Return a handler-descriptor dict. Assumes validate-workspace already passed."""
    if value == "inline":
        return {"handler_type": "inline"}
    if value == "none":
        return {"handler_type": "none"}
    if value.startswith("subagent:"):
        return {"handler_type": "subagent", "subagent_type": value[len("subagent:") :]}
    if value.startswith("skill:"):
        rest = value[len("skill:") :]
        if ":" in rest:
            name, _, args = rest.partition(":")
            return {"handler_type": "skill", "skill_name": name, "skill_args": args}
        return {"handler_type": "skill", "skill_name": rest, "skill_args": None}
    return {"handler_type": "unknown", "raw": value}


# ─── Git HEAD probe ──────────────────────────────────────────────────────────


def _git_head_sha(workspace_root: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return ""
    if cp.returncode != 0:
        return ""
    return cp.stdout.strip()


def _git_branch(workspace_root: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return ""
    if cp.returncode != 0:
        return ""
    return cp.stdout.strip()


def _promote_recall_log(workspace_root: Path, ticket: str) -> None:
    # Best-effort: fold any matching SessionStart recall-pending entries into the
    # per-ticket recall-log on run start. A promotion failure must never abort init.
    with contextlib.suppress(Exception):
        recall_pending.promote_matching(
            workspace_root,
            ticket=ticket,
            branch=_git_branch(workspace_root),
            head_sha=_git_head_sha(workspace_root),
            cwd=str(workspace_root),
            now_iso=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )


# ─── Public API ──────────────────────────────────────────────────────────────


def _skill_root_from_script() -> Path:
    # `__file__` = .../plugins/flow/skills/flow/scripts/dispatch_stage.py
    return Path(__file__).resolve().parent.parent


def _ticket_dir(workspace_root: Path, ticket: str) -> Path:
    return workspace_root / ".flow" / "runs" / ticket


def cmd_init(workspace_root: Path, ticket: str, force: bool = False) -> tuple[int, dict[str, Any]]:
    result, ws = vw.validate(workspace_root)
    if ws is None:
        return 1, {
            "error": "validate-workspace failed",
            "violations": result.violations,
        }
    td = _ticket_dir(workspace_root, ticket)

    # run_id is the stable per-ticket identity. Reuse the existing one whenever a
    # valid state is present (resume AND --force reset stay the same logical run),
    # so the lease sees us as the owner rather than a foreign run.
    existing, exit_code = state.read(td)
    have_valid = existing is not None and exit_code == 0
    resuming = have_valid and not force
    run_id = existing.run_id if (existing is not None and exit_code == 0) else secrets.token_hex(8)

    boot, host, cwd, now = lease.boot_id(), socket.gethostname(), str(workspace_root), _now_iso()
    try:
        lease.acquire(
            td,
            run_id,
            _INIT_TTL_S,
            now,
            stage="init",
            current_boot=boot,
            hostname=host,
            cwd=cwd,
            force=force,
        )
    except lease.LeaseHeld as exc:
        return 1, {
            "error": "ticket locked by another live run",
            "holder": asdict(exc.holder),
            "hint": f"/flow recover --takeover {ticket}",
        }
    except lease.LeaseExpiredForeign as exc:
        return 5, {
            "error": "stale lease from another run",
            "holder": asdict(exc.holder),
            "hint": f"/flow recover --takeover {ticket}",
        }

    # Canonical snapshot for later `next` TOCTOU checks. Best-effort: a snapshot
    # write failure must not block the run (verify treats absence as no-op).
    with contextlib.suppress(Exception):
        write_snapshot(workspace_root, ticket, skill_root=_skill_root_from_script())

    if resuming:
        _promote_recall_log(workspace_root, ticket)
        return 0, {
            "ticket": ticket,
            "run_id": run_id,
            "stages": ws.stages,
            "ticket_dir": str(td),
            "resumed": True,
        }

    state.init(td, ticket, ws.backend, ws.stages, run_id=run_id)
    _promote_recall_log(workspace_root, ticket)
    return 0, {
        "ticket": ticket,
        "run_id": run_id,
        "stages": ws.stages,
        "ticket_dir": str(td),
        "resumed": False,
    }


def cmd_next(workspace_root: Path, ticket: str) -> tuple[int, dict[str, Any]]:
    result, snapshot = vw.validate(workspace_root)
    if snapshot is None:
        return 1, {
            "error": "validate-workspace failed",
            "violations": result.violations,
        }
    td = _ticket_dir(workspace_root, ticket)
    ts, exit_code = state.read(td)
    if ts is None:
        if exit_code == 2:
            return 1, {"error": f"unrecoverable state.json at {td}"}
        return 2, {"error": f"no state.json at {td}; run `dispatch init` first"}

    # TOCTOU: refuse if workspace.toml / registry / a handler plugin drifted
    # since the run started.
    ok, detail = verify_snapshot(workspace_root, ticket, skill_root=_skill_root_from_script())
    if not ok:
        return 1, {
            "error": "config/version drift mid-run",
            "detail": detail,
            "hint": "/flow recover --reload-snapshot or --abort",
        }
    # Lease: if one exists it must still be ours (detects a takeover). A run with
    # no lease (legacy / direct test call) proceeds without one.
    boot, host = lease.boot_id(), socket.gethostname()
    if lease.read_lease(td) is not None:
        try:
            lease.assert_lease_still_mine(td, ts.run_id, current_boot=boot, hostname=host)
        except lease.LeaseLost as exc:
            return lease.EXIT_LEASE_LOST, {
                "error": "lost lease",
                "detail": str(exc),
                "hint": "/flow recover",
            }

    failed = state.find_failed(ts)
    if failed is not None:
        record = ts.stages[failed]
        return 0, {
            "done": False,
            "blocked_by": failed,
            "reason": record.failure_detail or "stage failed",
        }

    next_stage = state.pick_next_pending(ts, snapshot.stages)
    if next_stage is None:
        return 0, {"done": True}

    head_sha = _git_head_sha(workspace_root)

    # Assemble the full descriptor BEFORE mutating state. If descriptor
    # assembly raises, the stage must stay pending rather than be stuck
    # in_progress.
    registry_path = _skill_root_from_script() / _STAGE_REGISTRY_RELATIVE
    stage_meta = registry_by_name(registry_path).get(next_stage)
    handler_descriptor = _parse_handler(snapshot.handlers[next_stage])
    output_path = td / "stages" / f"{next_stage}.out"
    payload: dict[str, Any] = {
        "done": False,
        "stage": next_stage,
        "timeout_min": stage_meta.default_timeout_min if stage_meta else 10,
        "head_sha": head_sha,
        "ticket_dir": str(td),
        "output_path": str(output_path),
        "roles": stage_meta.roles if stage_meta else [],
        **handler_descriptor,
    }
    # Attach reference_doc regardless of handler type so the do-loop can pass it
    # to a spawned subagent (and to inline / skill / none handlers alike).
    if stage_meta is not None and stage_meta.reference_doc:
        payload["reference_doc"] = stage_meta.reference_doc

    # Refresh the lease to cover this stage's timeout window before marking it
    # in_progress, so a multi-minute stage does not self-expire the lease.
    if lease.read_lease(td) is not None:
        ttl = (stage_meta.default_timeout_min if stage_meta else 10) * 60 + _LEASE_BUFFER_S
        try:
            lease.refresh(
                td,
                ts.run_id,
                ttl,
                _now_iso(),
                stage=next_stage,
                current_boot=boot,
                hostname=host,
                cwd=str(workspace_root),
            )
        except lease.LeaseLost as exc:
            return lease.EXIT_LEASE_LOST, {
                "error": "lost lease",
                "detail": str(exc),
                "hint": "/flow recover",
            }

    state.begin_stage(td, next_stage, head_sha)
    return 0, payload


def cmd_finish(
    workspace_root: Path,
    ticket: str,
    stage_name: str,
    status_value: str,
    output_path: str | None = None,
    skill_output: dict[str, Any] | None = None,
    failure_detail: str | None = None,
) -> tuple[int, dict[str, Any]]:
    if status_value not in ("completed", "failed"):
        return 1, {"error": f"--status must be completed|failed, got {status_value!r}"}
    td = _ticket_dir(workspace_root, ticket)
    ts, exit_code = state.read(td)
    if ts is None:
        if exit_code == 2:
            return 1, {"error": f"unrecoverable state.json at {td}"}
        return 2, {"error": f"no state.json at {td}; run `dispatch init` first"}

    if lease.read_lease(td) is not None:
        try:
            lease.assert_lease_still_mine(
                td, ts.run_id, current_boot=lease.boot_id(), hostname=socket.gethostname()
            )
        except lease.LeaseLost as exc:
            return lease.EXIT_LEASE_LOST, {
                "error": "lost lease",
                "detail": str(exc),
                "hint": "/flow recover",
            }

    head_sha = _git_head_sha(workspace_root)
    try:
        new_state = state.finish_stage(
            td,
            stage_name,
            status_value,  # type: ignore[arg-type]
            head_sha,
            output_path=output_path,
            skill_output=skill_output,
            failure_detail=failure_detail,
        )
    except (ValueError, state.StateUnrecoverable) as exc:
        return 1, {"error": str(exc)}

    # Compute next_pending for caller convenience.
    _, snapshot = vw.validate(workspace_root)
    next_pending: str | None = None
    if snapshot is not None and state.find_failed(new_state) is None:
        next_pending = state.pick_next_pending(new_state, snapshot.stages)

    # Run finished cleanly (last stage completed, nothing pending or failed):
    # drop the lease. A failed run keeps its lease so /flow recover can act.
    if (
        status_value == "completed"
        and snapshot is not None
        and next_pending is None
        and state.find_failed(new_state) is None
    ):
        with contextlib.suppress(Exception):
            lease.release(td, new_state.run_id)

    return 0, {
        "stage": stage_name,
        "status": status_value,
        "next_pending": next_pending,
    }


def cmd_advance(
    workspace_root: Path,
    ticket: str,
    stage_name: str,
    status_value: str,
    output_path: str | None = None,
    skill_output: dict[str, Any] | None = None,
    failure_detail: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Finish the current stage and return the next descriptor in one call.

    Composes cmd_finish + cmd_next so the do-loop spends one script round-trip per
    stage instead of two. The returned payload spreads cmd_next's output at the top
    level (so it parses identically to `next`: {done} / descriptor / {blocked_by})
    and adds a `finished` object confirming the prior stage closed. On a finish
    error it returns immediately without advancing.
    """
    finish_rc, finish_payload = cmd_finish(
        workspace_root,
        ticket,
        stage_name,
        status_value,
        output_path=output_path,
        skill_output=skill_output,
        failure_detail=failure_detail,
    )
    if finish_rc != 0:
        return finish_rc, finish_payload
    next_rc, next_payload = cmd_next(workspace_root, ticket)
    return next_rc, {"finished": {"stage": stage_name, "status": status_value}, **next_payload}


def cmd_status(workspace_root: Path, ticket: str) -> tuple[int, dict[str, Any]]:
    td = _ticket_dir(workspace_root, ticket)
    ts, exit_code = state.read(td)
    if ts is None:
        if exit_code == 2:
            return 1, {"error": f"unrecoverable state.json at {td}"}
        return 2, {"error": f"no state.json at {td}"}
    return exit_code, asdict(ts)


def cmd_release(workspace_root: Path, ticket: str) -> tuple[int, dict[str, Any]]:
    td = _ticket_dir(workspace_root, ticket)
    ts, _ = state.read(td)
    released = False
    if ts is not None:
        released = lease.release(td, ts.run_id)
    return 0, {"ticket": ticket, "released": released}


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="/flow dispatcher state machine.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ticket", required=True)
    common.add_argument("--workspace-root", default=".")

    p_init = sub.add_parser("init", parents=[common], help="Initialize per-ticket state.json.")
    p_init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing state with a fresh all-pending run.",
    )
    sub.add_parser("next", parents=[common], help="Pick next pending stage.")
    sub.add_parser("status", parents=[common], help="Emit full state.json.")
    sub.add_parser("release", parents=[common], help="Release the run lease.")

    p_finish = sub.add_parser("finish", parents=[common], help="Mark stage terminal.")
    p_finish.add_argument("--stage", required=True)
    p_finish.add_argument(
        "--status", dest="status_value", choices=("completed", "failed"), required=True
    )
    p_finish.add_argument("--output-path", default=None)
    p_finish.add_argument("--skill-output", default=None)
    p_finish.add_argument("--failure-detail", default=None)

    p_advance = sub.add_parser(
        "advance", parents=[common], help="Finish current stage AND return next descriptor."
    )
    p_advance.add_argument("--stage", required=True)
    p_advance.add_argument(
        "--status", dest="status_value", choices=("completed", "failed"), required=True
    )
    p_advance.add_argument("--output-path", default=None)
    p_advance.add_argument("--skill-output", default=None)
    p_advance.add_argument("--failure-detail", default=None)

    return parser.parse_args(argv)


def _parse_skill_output_arg(raw: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """Parse the --skill-output JSON arg. Returns (parsed_or_None, error_or_None)."""
    if not raw:
        return None, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"--skill-output not JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, "--skill-output must be a JSON object"
    return parsed, None


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    workspace_root = Path(args.workspace_root).expanduser().resolve()

    if args.cmd == "init":
        rc, payload = cmd_init(workspace_root, args.ticket, force=args.force)
    elif args.cmd == "next":
        rc, payload = cmd_next(workspace_root, args.ticket)
    elif args.cmd == "finish":
        skill_output, err = _parse_skill_output_arg(args.skill_output)
        if err:
            sys.stderr.write(f"dispatch finish: {err}\n")
            return 1
        rc, payload = cmd_finish(
            workspace_root,
            args.ticket,
            args.stage,
            args.status_value,
            output_path=args.output_path,
            skill_output=skill_output,
            failure_detail=args.failure_detail,
        )
    elif args.cmd == "advance":
        skill_output, err = _parse_skill_output_arg(args.skill_output)
        if err:
            sys.stderr.write(f"dispatch advance: {err}\n")
            return 1
        rc, payload = cmd_advance(
            workspace_root,
            args.ticket,
            args.stage,
            args.status_value,
            output_path=args.output_path,
            skill_output=skill_output,
            failure_detail=args.failure_detail,
        )
    elif args.cmd == "status":
        rc, payload = cmd_status(workspace_root, args.ticket)
    elif args.cmd == "release":
        rc, payload = cmd_release(workspace_root, args.ticket)
    else:
        sys.stderr.write(f"unknown subcommand {args.cmd!r}\n")
        return 1

    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    if rc != 0:
        if "violations" in payload:
            for v in payload["violations"]:
                sys.stderr.write(v + "\n")
        elif "error" in payload:
            sys.stderr.write(str(payload["error"]) + "\n")
    return rc


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "cli_main",
    "cmd_advance",
    "cmd_finish",
    "cmd_init",
    "cmd_next",
    "cmd_release",
    "cmd_status",
]
