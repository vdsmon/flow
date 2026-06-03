"""Per-ticket state.json reader/writer.

Library + thin CLI. Stdlib-only.

Invariants:

- One state.json per ticket at `.flow/runs/<ticket>/state.json`.
- All writes go through atomic temp-fsync-rename + flock(EX) on the sibling
  `state.json.lock` file.
- Each write rotates a backup at `state.json.<ts>.bak`. Last 5 kept.
- Malformed JSON on read triggers quarantine path: move corrupt file to
  `state.json.quarantine.<ts>`, try newest `.bak`, then next-newest, etc.
  Exit code 1 (warning, loaded from .bak). If no .bak parses → exit 2.

Schema version: 1. Stage lifecycle: `pending → in_progress → (completed |
failed)`. The `dispatched | timed_out | hung` states from the literal plan
spec are deferred to phase 7-full (when heartbeat + lease lifecycle land).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from _locking import flock_blocking

SCHEMA_VERSION = 1

StageStatus = Literal["pending", "in_progress", "completed", "failed"]

BACKUP_RETENTION = 5


# ─── Types ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StageRecord:
    status: StageStatus = "pending"
    started_at_iso: str | None = None
    started_at_sha: str | None = None
    finished_at_iso: str | None = None
    finished_at_sha: str | None = None
    agent_id: str | None = None
    output_path: str | None = None
    skill_output: dict[str, Any] | None = None
    failure_detail: str | None = None


@dataclass(frozen=True)
class TicketState:
    schema_version: int
    ticket: str
    run_id: str
    backend: str
    started_at: str
    stages: dict[str, StageRecord] = field(default_factory=dict)


class StateUnrecoverable(Exception):
    """Raised when state.json is corrupt AND no backup parses."""


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _utcnow_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ts_token() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _state_path(ticket_dir: Path) -> Path:
    return ticket_dir / "state.json"


def _lock_path(ticket_dir: Path) -> Path:
    return ticket_dir / "state.json.lock"


def _backups(ticket_dir: Path) -> list[Path]:
    return sorted(
        ticket_dir.glob("state.json.*.bak"),
        key=lambda p: p.name,
        reverse=True,
    )


def _trim_backups(ticket_dir: Path, keep: int = BACKUP_RETENTION) -> None:
    for stale in _backups(ticket_dir)[keep:]:
        with contextlib.suppress(OSError):
            stale.unlink()


def _serialize(state: TicketState) -> str:
    return json.dumps(asdict(state), indent=2, sort_keys=True) + "\n"


def _deserialize(raw: str) -> TicketState:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("state.json root is not an object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"state.json schema_version={data.get('schema_version')!r}, expected {SCHEMA_VERSION}"
        )
    required = {"ticket", "run_id", "backend", "started_at"}
    missing = required - data.keys()
    if missing:
        raise ValueError(f"state.json missing top-level keys: {sorted(missing)}")
    stages_raw = data.get("stages", {})
    if not isinstance(stages_raw, dict):
        raise ValueError("state.json `stages` is not an object")
    stages = {name: StageRecord(**entry) for name, entry in stages_raw.items()}
    return TicketState(
        schema_version=int(data["schema_version"]),
        ticket=str(data["ticket"]),
        run_id=str(data["run_id"]),
        backend=str(data["backend"]),
        started_at=str(data["started_at"]),
        stages=stages,
    )


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)
    # fsync the parent dir so the rename itself is durable across a crash.
    with contextlib.suppress(OSError):
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


# ─── Quarantine ──────────────────────────────────────────────────────────────


def _try_load_from_bak(ticket_dir: Path) -> TicketState | None:
    for bak in _backups(ticket_dir):
        try:
            raw = bak.read_text(encoding="utf-8")
            return _deserialize(raw)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return None


def _quarantine_corrupt(ticket_dir: Path) -> None:
    src = _state_path(ticket_dir)
    if not src.exists():
        return
    dst = ticket_dir / f"state.json.quarantine.{_ts_token()}"
    with contextlib.suppress(OSError):
        os.replace(src, dst)


# ─── Public API ──────────────────────────────────────────────────────────────


def _read_locked(ticket_dir: Path) -> tuple[TicketState | None, int]:
    """Read body assuming the flock is already held. See read() for semantics.

    Must not re-acquire the lock: callers (read, _update) hold it via a single
    flock_blocking and a second acquisition would deadlock under blocking LOCK_EX.
    """
    path = _state_path(ticket_dir)
    if not path.exists():
        return None, 0
    try:
        raw = path.read_text(encoding="utf-8")
        return _deserialize(raw), 0
    except (TypeError, ValueError, json.JSONDecodeError):
        _quarantine_corrupt(ticket_dir)
        recovered = _try_load_from_bak(ticket_dir)
        if recovered is None:
            return None, 2
        _atomic_write(_state_path(ticket_dir), _serialize(recovered))
        return recovered, 1


def read(ticket_dir: Path) -> tuple[TicketState | None, int]:
    """Read state.json. Returns (state, exit_code).

    Exit codes:
        0 = ok (or absent file → state=None).
        1 = quarantine triggered; loaded from .bak.
        2 = unrecoverable (state.json missing + no usable .bak).

    Note: `state=None` with exit_code 0 is the "not yet initialized" signal.
    `state=None` with exit_code 2 is the "broken and no backup" signal —
    callers MUST distinguish these by checking exit_code.
    """
    with flock_blocking(_lock_path(ticket_dir)):
        return _read_locked(ticket_dir)


def init(
    ticket_dir: Path,
    ticket: str,
    backend: str,
    stages: list[str],
    run_id: str | None = None,
) -> TicketState:
    state = TicketState(
        schema_version=SCHEMA_VERSION,
        ticket=ticket,
        run_id=run_id or secrets.token_hex(8),
        backend=backend,
        started_at=_utcnow_iso(),
        stages={name: StageRecord() for name in stages},
    )
    _write(ticket_dir, state)
    return state


def begin_stage(
    ticket_dir: Path,
    stage: str,
    head_sha: str,
    agent_id: str | None = None,
) -> TicketState:
    def mutate(state: TicketState) -> TicketState:
        if stage not in state.stages:
            raise ValueError(f"stage {stage!r} not in state.stages")
        record = state.stages[stage]
        if record.status not in ("pending", "in_progress"):
            raise ValueError(f"cannot begin stage {stage!r}: current status is {record.status!r}")
        new_record = replace(
            record,
            status="in_progress",
            started_at_iso=record.started_at_iso or _utcnow_iso(),
            started_at_sha=record.started_at_sha or head_sha,
            agent_id=agent_id or record.agent_id,
        )
        return replace(state, stages={**state.stages, stage: new_record})

    return _update(ticket_dir, mutate)


def finish_stage(
    ticket_dir: Path,
    stage: str,
    status: StageStatus,
    head_sha: str,
    output_path: str | None = None,
    skill_output: dict[str, Any] | None = None,
    failure_detail: str | None = None,
) -> TicketState:
    if status not in ("completed", "failed"):
        raise ValueError(f"finish_stage status must be completed|failed, got {status!r}")

    def mutate(state: TicketState) -> TicketState:
        if stage not in state.stages:
            raise ValueError(f"stage {stage!r} not in state.stages")
        record = state.stages[stage]
        new_record = replace(
            record,
            status=status,
            finished_at_iso=_utcnow_iso(),
            finished_at_sha=head_sha,
            output_path=output_path or record.output_path,
            skill_output=skill_output if skill_output is not None else record.skill_output,
            failure_detail=failure_detail,
        )
        return replace(state, stages={**state.stages, stage: new_record})

    return _update(ticket_dir, mutate)


def force_stage_status(ticket_dir: Path, stage: str, status: StageStatus) -> TicketState:
    """Recovery-only: force a stage to a given status, outside begin/finish.

    Used by /flow recover to retry (-> pending), skip (-> completed), or reset a
    stage. Resetting to pending clears the stage's timestamps + failure detail so
    the retry starts clean.
    """
    if status not in ("pending", "in_progress", "completed", "failed"):
        raise ValueError(f"force_stage_status: invalid status {status!r}")

    def mutate(state: TicketState) -> TicketState:
        if stage not in state.stages:
            raise ValueError(f"stage {stage!r} not in state.stages")
        record = state.stages[stage]
        if status == "pending":
            new_record = replace(
                record,
                status="pending",
                started_at_iso=None,
                started_at_sha=None,
                finished_at_iso=None,
                finished_at_sha=None,
                failure_detail=None,
            )
        else:
            new_record = replace(record, status=status)
        return replace(state, stages={**state.stages, stage: new_record})

    return _update(ticket_dir, mutate)


def pick_next_pending(state: TicketState, pipeline_order: list[str]) -> str | None:
    for name in pipeline_order:
        record = state.stages.get(name)
        if record is None:
            continue
        # in_progress is resumable: a stage left in_progress by a crashed run
        # must be picked up again, not skipped forever.
        if record.status in ("pending", "in_progress"):
            return name
    return None


def find_failed(state: TicketState) -> str | None:
    for name, record in state.stages.items():
        if record.status == "failed":
            return name
    return None


# ─── Internal: write under lock with rolling backup ──────────────────────────


def _write_locked(ticket_dir: Path, state: TicketState) -> None:
    """Write body assuming the flock is already held. See _write()."""
    path = _state_path(ticket_dir)
    if path.exists():
        bak = ticket_dir / f"state.json.{_ts_token()}.bak"
        with contextlib.suppress(OSError):
            bak.write_bytes(path.read_bytes())
    _atomic_write(path, _serialize(state))
    _trim_backups(ticket_dir)


def _write(ticket_dir: Path, state: TicketState) -> None:
    ticket_dir.mkdir(parents=True, exist_ok=True)
    with flock_blocking(_lock_path(ticket_dir)):
        _write_locked(ticket_dir, state)


def _update(ticket_dir: Path, mutate_fn: Callable[[TicketState], TicketState]) -> TicketState:
    """Atomic read-modify-write under a single held flock.

    Holds the lock across the whole read-mutate-write so two concurrent
    callers cannot lose each other's update (the flock is never released
    between the read and the write).
    """
    ticket_dir.mkdir(parents=True, exist_ok=True)
    with flock_blocking(_lock_path(ticket_dir)):
        state, _ = _read_locked(ticket_dir)
        if state is None:
            raise StateUnrecoverable(f"no state.json at {ticket_dir}")
        new_state = mutate_fn(state)
        _write_locked(ticket_dir, new_state)
        return new_state


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Per-ticket state.json reader/writer.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_read = sub.add_parser("read", help="Read state.json and emit to stdout.")
    p_read.add_argument("ticket_dir")

    p_init = sub.add_parser("init", help="Create new state.json with pending stages.")
    p_init.add_argument("ticket_dir")
    p_init.add_argument("--ticket", required=True)
    p_init.add_argument("--backend", choices=("jira", "beads"), required=True)
    p_init.add_argument("--stage", action="append", required=True, dest="stages")
    p_init.add_argument("--run-id", default=None)

    p_begin = sub.add_parser("begin", help="Mark stage in_progress.")
    p_begin.add_argument("--ticket-dir", required=True)
    p_begin.add_argument("--stage", required=True)
    p_begin.add_argument("--head-sha", required=True)
    p_begin.add_argument("--agent-id", default=None)

    p_finish = sub.add_parser("finish", help="Mark stage completed|failed.")
    p_finish.add_argument("--ticket-dir", required=True)
    p_finish.add_argument("--stage", required=True)
    p_finish.add_argument("--status", choices=("completed", "failed"), required=True)
    p_finish.add_argument("--head-sha", required=True)
    p_finish.add_argument("--output-path", default=None)
    p_finish.add_argument("--skill-output", default=None, help="JSON string")
    p_finish.add_argument("--failure-detail", default=None)

    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    if args.cmd == "read":
        td = Path(args.ticket_dir).resolve()
        state, exit_code = read(td)
        if exit_code == 2:
            sys.stderr.write(f"state.py: no recoverable state.json under {td}\n")
            return 2
        if state is None:
            sys.stdout.write("null\n")
            return exit_code
        sys.stdout.write(_serialize(state))
        return exit_code

    if args.cmd == "init":
        td = Path(args.ticket_dir).resolve()
        state = init(td, args.ticket, args.backend, args.stages, run_id=args.run_id)
        sys.stdout.write(_serialize(state))
        return 0

    if args.cmd == "begin":
        td = Path(args.ticket_dir).resolve()
        try:
            state = begin_stage(td, args.stage, args.head_sha, agent_id=args.agent_id)
        except (ValueError, StateUnrecoverable) as exc:
            sys.stderr.write(f"state.py begin: {exc}\n")
            return 1
        sys.stdout.write(_serialize(state))
        return 0

    if args.cmd == "finish":
        td = Path(args.ticket_dir).resolve()
        skill_output: dict[str, Any] | None = None
        if args.skill_output:
            try:
                parsed = json.loads(args.skill_output)
            except json.JSONDecodeError as exc:
                sys.stderr.write(f"state.py finish: --skill-output not JSON: {exc}\n")
                return 1
            if not isinstance(parsed, dict):
                sys.stderr.write("state.py finish: --skill-output must be a JSON object\n")
                return 1
            skill_output = parsed
        try:
            state = finish_stage(
                td,
                args.stage,
                args.status,
                args.head_sha,
                output_path=args.output_path,
                skill_output=skill_output,
                failure_detail=args.failure_detail,
            )
        except (ValueError, StateUnrecoverable) as exc:
            sys.stderr.write(f"state.py finish: {exc}\n")
            return 1
        sys.stdout.write(_serialize(state))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "BACKUP_RETENTION",
    "SCHEMA_VERSION",
    "StageRecord",
    "StageStatus",
    "StateUnrecoverable",
    "TicketState",
    "begin_stage",
    "cli_main",
    "find_failed",
    "finish_stage",
    "force_stage_status",
    "init",
    "pick_next_pending",
    "read",
]
