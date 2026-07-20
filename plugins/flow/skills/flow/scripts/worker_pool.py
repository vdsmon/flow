"""Host-neutral driver-session worker-pool core.

The host adapter performs native worker creation, waiting, and cancellation. This
module owns the invariants shared by Claude Code and Codex:

* one host slot always stays with the driver session;
* native worker handles are driver-scoped and disposable;
* driver recovery trusts durable Flow run evidence, never a surviving handle;
* read-only discovery workers must leave the git snapshot byte-for-byte equal.

There is deliberately no process, terminal, or shell-detachment implementation
here. A harness adapter maps ``launch`` / ``wait`` / ``cancel`` to its native
collaboration primitives.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path


@dataclass(frozen=True)
class GitSnapshot:
    """Git state sufficient to prove a discovery worker was read-only.

    The adapter decides how to compute each opaque digest. Equality is the
    contract: pre-existing dirt is legal, but the post-worker snapshot must
    match it exactly.
    """

    head: str
    index_tree: str
    tracked_worktree: str
    untracked_worktree: str

    @classmethod
    def from_dict(cls, value: object) -> GitSnapshot:
        """Parse one snapshot receipt without accepting partial evidence."""

        if not isinstance(value, dict):
            raise ValueError("git snapshot must be a JSON object")
        fields: dict[str, str] = {}
        for field in _GIT_FIELDS:
            item = value.get(field)
            if not isinstance(item, str) or not item:
                raise ValueError(f"git snapshot field {field!r} must be a non-empty string")
            fields[field] = item
        return cls(**fields)


_GIT_FIELDS = ("head", "index_tree", "tracked_worktree", "untracked_worktree")


def effective_concurrency(*, configured: int, capacity: int) -> int:
    """Worker slots available while reserving one slot for the driver session."""

    if configured < 0:
        raise ValueError("configured concurrency must be non-negative")
    if capacity < 1:
        raise ValueError("host capacity must include at least the driver session")
    return min(configured, capacity - 1)


def changed_git_fields(before: GitSnapshot, after: GitSnapshot) -> tuple[str, ...]:
    """Snapshot fields changed by a supposedly read-only worker."""

    return tuple(field for field in _GIT_FIELDS if getattr(before, field) != getattr(after, field))


class DurableRunState(StrEnum):
    """Durable evidence visible after a driver session and handles disappear."""

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
    """New-driver action chosen only from durable Flow evidence."""

    RELAUNCH = "relaunch"
    MONITOR = "monitor"
    SETTLED = "settled"
    REPAIR = "repair"


@dataclass(frozen=True)
class DriverRecoveryOutcome:
    key: str
    action: RecoveryAction
    run_id: str = ""


def driver_recovery_outcome(evidence: DurableRunEvidence) -> DriverRecoveryOutcome:
    """Choose a post-driver-failure action without consulting worker handles.

    Native handles die with, or become inaccessible after, their driver session.
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
    return DriverRecoveryOutcome(key=evidence.key, action=action, run_id=evidence.run_id)


def driver_recovery_plan(
    keys: Sequence[str], evidence_by_key: Mapping[str, DurableRunEvidence]
) -> list[DriverRecoveryOutcome]:
    """Recovery outcomes in request order; missing evidence means no run started."""

    outcomes: list[DriverRecoveryOutcome] = []
    for key in keys:
        evidence = evidence_by_key.get(key)
        if evidence is None:
            evidence = DurableRunEvidence(key=key, state=DurableRunState.ABSENT)
        elif evidence.key != key:
            raise ValueError(
                f"durable evidence key mismatch: expected {key!r}, got {evidence.key!r}"
            )
        outcomes.append(driver_recovery_outcome(evidence))
    return outcomes


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_bytes(workspace_root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=workspace_root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"git {' '.join(args)} failed (rc={result.returncode}): {detail}")
    return result.stdout


def capture_git_snapshot(workspace_root: Path) -> GitSnapshot:
    """Capture a deterministic, read-only digest of repository-visible state.

    ``git write-tree`` is deliberately avoided because it can add an object to the
    repository. The index and tracked-worktree fields hash binary diffs against the
    unchanged HEAD instead. Untracked files include their path, mode, kind, and
    bytes (or symlink target), so equal receipts are strong enough to accept a
    discovery worker's result even when the checkout was already dirty.
    """

    root = workspace_root.expanduser().resolve()
    if not root.is_absolute() or not root.is_dir():
        raise ValueError(f"workspace root is not a directory: {root}")
    head = _git_bytes(root, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
    index = _git_bytes(root, "diff", "--cached", "--binary", "--no-ext-diff", "HEAD", "--")
    tracked = _git_bytes(root, "diff", "--binary", "--no-ext-diff", "--")
    untracked_names = _git_bytes(
        root, "ls-files", "--others", "--exclude-standard", "-z", "--"
    ).split(b"\0")
    untracked = hashlib.sha256()
    for raw_name in sorted(name for name in untracked_names if name):
        name = os.fsdecode(raw_name)
        path = root / name
        stat = path.lstat()
        untracked.update(len(raw_name).to_bytes(8, "big"))
        untracked.update(raw_name)
        untracked.update(stat.st_mode.to_bytes(8, "big"))
        if path.is_symlink():
            body = os.fsencode(os.readlink(path))
            kind = b"symlink"
        elif path.is_file():
            body = path.read_bytes()
            kind = b"file"
        else:
            body = b""
            kind = b"other"
        untracked.update(kind)
        untracked.update(len(body).to_bytes(8, "big"))
        untracked.update(body)
    return GitSnapshot(
        head=head,
        index_tree=_sha256(index),
        tracked_worktree=_sha256(tracked),
        untracked_worktree=untracked.hexdigest(),
    )


def _absolute_file(raw: str, *, option: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{option} must be an absolute path")
    return path


def _read_snapshot(path: Path) -> GitSnapshot:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read git snapshot {path}: {exc}") from exc
    return GitSnapshot.from_dict(value)


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
        description="Deterministic seams for a host-native Flow driver worker pool."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    limit = subparsers.add_parser("limit", help="Reserve the driver slot and bound fan-out.")
    limit.add_argument("--configured", type=int, required=True)
    limit.add_argument("--capacity", type=int, required=True)

    snapshot = subparsers.add_parser("snapshot", help="Capture a read-only git receipt.")
    snapshot.add_argument("--workspace-root", required=True)

    guard = subparsers.add_parser("guard", help="Compare current git state to a receipt.")
    guard.add_argument("--workspace-root", required=True)
    guard.add_argument("--before", required=True)

    recover = subparsers.add_parser("recover", help="Reduce durable post-driver evidence.")
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
                "driver_slots": 1,
            }
        elif args.command == "snapshot":
            root = _absolute_file(args.workspace_root, option="--workspace-root")
            payload = asdict(capture_git_snapshot(root))
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
            payload = {"outcomes": [asdict(item) for item in driver_recovery_plan(keys, evidence)]}
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
    "DriverRecoveryOutcome",
    "DurableRunEvidence",
    "DurableRunState",
    "GitSnapshot",
    "RecoveryAction",
    "capture_git_snapshot",
    "changed_git_fields",
    "cli_main",
    "driver_recovery_outcome",
    "driver_recovery_plan",
    "effective_concurrency",
]
