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
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

HostStatus = Literal["completed", "failed", "cancelled"]
WorkerStatus = Literal["completed", "failed", "cancelled", "mutated"]


@dataclass(frozen=True)
class WorkerRequest:
    """One native worker request.

    ``key`` is the durable work identity (normally a ticket key; a discovery
    label is also valid). ``task`` is opaque host prompt text. A read-only
    request activates the pre/post git snapshot guard.
    """

    key: str
    task: str
    read_only: bool = False


@dataclass(frozen=True)
class WorkerHandle:
    """Disposable host handle, scoped to exactly one owner session."""

    owner_id: str
    key: str
    native_id: str
    read_only: bool


@dataclass(frozen=True)
class HostCompletion:
    """A native completion emitted by a harness adapter."""

    native_id: str
    status: HostStatus
    detail: str = ""


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


@dataclass(frozen=True)
class WorkerResult:
    """Owner-visible outcome after native completion or cancellation."""

    key: str
    status: WorkerStatus
    detail: str = ""
    changed_git_fields: tuple[str, ...] = ()


class WorkerHost(Protocol):
    """Native collaboration seam supplied by a harness adapter."""

    capacity: int

    def launch(self, request: WorkerRequest) -> str: ...

    def wait(self, handles: Sequence[str]) -> Sequence[HostCompletion]: ...

    def cancel(self, handle: str) -> None: ...


def effective_concurrency(*, configured: int, capacity: int) -> int:
    """Worker slots available while reserving one slot for the owner session."""

    if configured < 0:
        raise ValueError("configured concurrency must be non-negative")
    if capacity < 1:
        raise ValueError("host capacity must include at least the owner session")
    return min(configured, capacity - 1)


def changed_git_fields(before: GitSnapshot, after: GitSnapshot) -> tuple[str, ...]:
    """Snapshot fields changed by a supposedly read-only worker."""

    return tuple(field for field in _GIT_FIELDS if getattr(before, field) != getattr(after, field))


class WorkerPool:
    """Bounded worker handles owned by one live harness session."""

    def __init__(
        self,
        *,
        owner_id: str,
        configured_concurrency: int,
        host: WorkerHost,
        git_snapshot: Callable[[], GitSnapshot] | None = None,
    ) -> None:
        if not owner_id:
            raise ValueError("owner_id must be non-empty")
        self.owner_id = owner_id
        self.host = host
        self.concurrency = effective_concurrency(
            configured=configured_concurrency, capacity=host.capacity
        )
        self._git_snapshot = git_snapshot
        self._active: dict[str, WorkerHandle] = {}
        self._before: dict[str, GitSnapshot] = {}

    @property
    def active_handles(self) -> tuple[WorkerHandle, ...]:
        """Active handles in native launch order."""

        return tuple(self._active.values())

    def launch(self, requests: Sequence[WorkerRequest]) -> list[WorkerHandle]:
        """Launch up to the currently available owner-pool slots."""

        available = self.concurrency - len(self._active)
        if available <= 0:
            return []
        selected = list(requests[:available])
        if any(request.read_only for request in selected) and self._git_snapshot is None:
            raise ValueError("read-only discovery workers require a git snapshot provider")

        launched: list[WorkerHandle] = []
        for request in selected:
            before = self._git_snapshot() if request.read_only and self._git_snapshot else None
            native_id = self.host.launch(request)
            if not native_id:
                raise ValueError("host launch returned an empty worker handle")
            if native_id in self._active:
                raise ValueError(f"host launch returned duplicate worker handle {native_id!r}")
            handle = WorkerHandle(
                owner_id=self.owner_id,
                key=request.key,
                native_id=native_id,
                read_only=request.read_only,
            )
            self._active[native_id] = handle
            if before is not None:
                self._before[native_id] = before
            launched.append(handle)
        return launched

    def wait(self, handles: Sequence[WorkerHandle] | None = None) -> list[WorkerResult]:
        """Wait through the host adapter and settle only handles it completes."""

        selected = self._select(handles)
        if not selected:
            return []
        completions = list(self.host.wait([handle.native_id for handle in selected]))
        allowed = {handle.native_id for handle in selected}
        seen: set[str] = set()
        results: list[WorkerResult] = []
        for completion in completions:
            if completion.native_id not in allowed:
                raise ValueError(f"host completed unknown worker handle {completion.native_id!r}")
            if completion.native_id in seen:
                raise ValueError(f"host completed worker handle twice: {completion.native_id!r}")
            seen.add(completion.native_id)
            handle = self._active[completion.native_id]
            results.append(self._settle(handle, completion.status, completion.detail))
        return results

    def cancel(self, handles: Sequence[WorkerHandle] | None = None) -> list[WorkerResult]:
        """Cancel selected native workers and release their owner-pool slots."""

        selected = self._select(handles)
        results: list[WorkerResult] = []
        for handle in selected:
            self.host.cancel(handle.native_id)
            results.append(self._settle(handle, "cancelled", ""))
        return results

    def _select(self, handles: Sequence[WorkerHandle] | None) -> list[WorkerHandle]:
        selected = list(self._active.values()) if handles is None else list(handles)
        for handle in selected:
            if handle.owner_id != self.owner_id:
                raise ValueError(
                    f"worker handle belongs to {handle.owner_id!r}, not owner {self.owner_id!r}"
                )
            if self._active.get(handle.native_id) != handle:
                raise ValueError(f"worker handle is not active: {handle.native_id!r}")
        return selected

    def _settle(self, handle: WorkerHandle, status: HostStatus, detail: str) -> WorkerResult:
        changed: tuple[str, ...] = ()
        if handle.read_only:
            before = self._before.get(handle.native_id)
            if before is None or self._git_snapshot is None:
                raise RuntimeError("read-only worker lost its pre-snapshot guard")
            changed = changed_git_fields(before, self._git_snapshot())
        self._active.pop(handle.native_id, None)
        self._before.pop(handle.native_id, None)
        return WorkerResult(
            key=handle.key,
            status="mutated" if changed else status,
            detail=detail,
            changed_git_fields=changed,
        )


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
    "GitSnapshot",
    "HostCompletion",
    "OwnerRecoveryOutcome",
    "RecoveryAction",
    "WorkerHandle",
    "WorkerHost",
    "WorkerPool",
    "WorkerRequest",
    "WorkerResult",
    "capture_git_snapshot",
    "changed_git_fields",
    "cli_main",
    "effective_concurrency",
    "owner_recovery_outcome",
    "owner_recovery_plan",
]
