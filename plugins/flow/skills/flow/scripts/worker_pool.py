"""Host-neutral owner-session worker-pool core.

The host adapter owns native worker creation, waiting, and cancellation. This
module owns the invariants shared by Claude Code and Codex:

* one host slot always stays with the owner session;
* native worker handles are owner-scoped and disposable;
* owner recovery trusts durable Flow run evidence, never a surviving handle;
* read-only discovery workers must leave the git snapshot byte-for-byte equal.

There is deliberately no process, terminal, or shell-detachment implementation
here. A harness adapter maps ``launch`` / ``wait`` / ``cancel`` to its native
collaboration primitives.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import _gitreceipt


def effective_concurrency(*, configured: int, capacity: int) -> int:
    """Worker slots available while reserving one slot for the owner session."""

    if configured < 0:
        raise ValueError("configured concurrency must be non-negative")
    if capacity < 1:
        raise ValueError("host capacity must include at least the owner session")
    return min(configured, capacity - 1)


def changed_git_fields(
    before: Mapping[str, object], after: Mapping[str, object]
) -> tuple[str, ...]:
    """Snapshot fields changed by a supposedly read-only worker."""
    try:
        return _gitreceipt.changed_fields(before, after)
    except _gitreceipt.GitReceiptError as exc:
        raise ValueError(str(exc)) from exc


class DurableRunState(StrEnum):
    """Durable evidence visible after an owner session and handles disappear."""

    ABSENT = "absent"
    BOOTSTRAPPING = "bootstrapping"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CORRUPT = "corrupt"


@dataclass(frozen=True)
class DurableRunEvidence:
    key: str
    state: DurableRunState
    run_id: str = ""


class RecoveryAction(StrEnum):
    """New-owner action chosen only from durable Flow evidence."""

    RELAUNCH = "relaunch"
    MONITOR = "monitor"
    SETTLED = "settled"
    REPAIR = "repair"


@dataclass(frozen=True)
class OwnerRecoveryOutcome:
    key: str
    action: RecoveryAction
    run_id: str = ""


def owner_recovery_outcome(evidence: DurableRunEvidence) -> OwnerRecoveryOutcome:
    """Choose a post-owner-failure action without consulting worker handles.

    Native handles die with, or become inaccessible after, their owner session.
    Durable run state is therefore the only authority that may suppress a
    relaunch or require repair.
    """

    action = {
        DurableRunState.ABSENT: RecoveryAction.RELAUNCH,
        DurableRunState.BOOTSTRAPPING: RecoveryAction.MONITOR,
        DurableRunState.RUNNING: RecoveryAction.MONITOR,
        DurableRunState.SUCCEEDED: RecoveryAction.SETTLED,
        DurableRunState.FAILED: RecoveryAction.REPAIR,
        DurableRunState.CORRUPT: RecoveryAction.REPAIR,
    }[evidence.state]
    return OwnerRecoveryOutcome(key=evidence.key, action=action, run_id=evidence.run_id)


def owner_recovery_plan(
    keys: Sequence[str], evidence_by_key: Mapping[str, DurableRunEvidence]
) -> list[OwnerRecoveryOutcome]:
    """Recovery outcomes in request order; missing evidence means no run started."""

    outcomes: list[OwnerRecoveryOutcome] = []
    for key in keys:
        evidence = evidence_by_key.get(key)
        if evidence is None:
            evidence = DurableRunEvidence(key=key, state=DurableRunState.ABSENT)
        elif evidence.key != key:
            raise ValueError(
                f"durable evidence key mismatch: expected {key!r}, got {evidence.key!r}"
            )
        outcomes.append(owner_recovery_outcome(evidence))
    return outcomes


def capture_git_snapshot(workspace_root: Path) -> dict[str, object]:
    """Capture the canonical read-only Git receipt."""
    try:
        return _gitreceipt.capture(workspace_root)
    except _gitreceipt.GitReceiptError as exc:
        raise ValueError(str(exc)) from exc


def _absolute_file(raw: str, *, option: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{option} must be an absolute path")
    return path


def _read_snapshot(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read git snapshot {path}: {exc}") from exc
    try:
        return _gitreceipt.validate(value)
    except _gitreceipt.GitReceiptError as exc:
        raise ValueError(str(exc)) from exc


def _read_recovery_evidence(path: Path) -> tuple[list[str], dict[str, DurableRunEvidence]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read recovery evidence {path}: {exc}") from exc
    if not isinstance(value, list):
        raise ValueError("recovery evidence must be a JSON array")
    keys: list[str] = []
    evidence: dict[str, DurableRunEvidence] = {}
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("each recovery evidence row must be an object")
        key = row.get("key")
        state = row.get("state")
        run_id = row.get("run_id", "")
        if not isinstance(key, str) or not key:
            raise ValueError("recovery evidence key must be a non-empty string")
        if key in evidence:
            raise ValueError(f"duplicate recovery evidence key {key!r}")
        if not isinstance(state, str):
            raise ValueError(f"recovery evidence for {key!r} is missing state")
        try:
            parsed_state = DurableRunState(state)
        except ValueError as exc:
            raise ValueError(f"recovery evidence for {key!r} has invalid state {state!r}") from exc
        if not isinstance(run_id, str):
            raise ValueError(f"recovery evidence for {key!r} has invalid run_id")
        keys.append(key)
        evidence[key] = DurableRunEvidence(key=key, state=parsed_state, run_id=run_id)
    return keys, evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic seams for a host-native Flow owner worker pool."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    limit = subparsers.add_parser("limit", help="Reserve the owner slot and bound fan-out.")
    limit.add_argument("--configured", type=int, required=True)
    limit.add_argument("--capacity", type=int, required=True)

    snapshot = subparsers.add_parser("snapshot", help="Capture a read-only git receipt.")
    snapshot.add_argument("--workspace-root", required=True)

    guard = subparsers.add_parser("guard", help="Compare current git state to a receipt.")
    guard.add_argument("--workspace-root", required=True)
    guard.add_argument("--before", required=True)

    recover = subparsers.add_parser("recover", help="Reduce durable post-owner evidence.")
    recover.add_argument("--evidence", required=True)
    return parser


def cli_main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "limit":
            effective = effective_concurrency(configured=args.configured, capacity=args.capacity)
            payload = {
                "configured": args.configured,
                "effective_concurrency": effective,
                "host_capacity": args.capacity,
                "owner_slots": 1,
            }
        elif args.command == "snapshot":
            root = _absolute_file(args.workspace_root, option="--workspace-root")
            payload = capture_git_snapshot(root)
        elif args.command == "guard":
            root = _absolute_file(args.workspace_root, option="--workspace-root")
            before = _read_snapshot(_absolute_file(args.before, option="--before"))
            after = capture_git_snapshot(root)
            changed = changed_git_fields(before, after)
            payload = {
                "changed_git_fields": list(changed),
                "unchanged": not changed,
            }
            sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
            return 0 if not changed else 3
        elif args.command == "recover":
            evidence_path = _absolute_file(args.evidence, option="--evidence")
            keys, evidence = _read_recovery_evidence(evidence_path)
            payload = {"outcomes": [asdict(item) for item in owner_recovery_plan(keys, evidence)]}
        else:
            raise ValueError(f"unknown worker-pool command {args.command!r}")
    except ValueError as exc:
        sys.stderr.write(f"worker-pool: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "DurableRunEvidence",
    "DurableRunState",
    "OwnerRecoveryOutcome",
    "RecoveryAction",
    "capture_git_snapshot",
    "changed_git_fields",
    "cli_main",
    "effective_concurrency",
    "owner_recovery_outcome",
    "owner_recovery_plan",
]
