"""Crash-recoverable journal for tuple-bound post-approval bootstrap."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from _atomicio import atomic_write_text
from _locking import flock_blocking
from planning_attempt import APPROVAL_SCHEMA, canonical_digest

SCHEMA = "flow.bootstrap-journal/v1"
_PHASES = ("prepared", "worktree_intended", "worktree_created", "run_seeded", "committed")


class JournalError(RuntimeError):
    """The bootstrap journal conflicts with the approved tuple or phase order."""


@dataclass(frozen=True)
class JournalRecord:
    ticket: str
    approval_digest: str
    attempt_id: str
    approved_base_sha: str
    route_digest: str
    phase: str
    worktree: str | None
    branch: str | None
    run_id: str | None
    digest: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "ticket": self.ticket,
            "approval_digest": self.approval_digest,
            "attempt_id": self.attempt_id,
            "approved_base_sha": self.approved_base_sha,
            "route_digest": self.route_digest,
            "phase": self.phase,
            "worktree": self.worktree,
            "branch": self.branch,
            "run_id": self.run_id,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class Recovery:
    action: str
    phase: str | None
    worktree: str | None
    branch: str | None
    run_id: str | None


def _record(body: dict[str, Any]) -> JournalRecord:
    phase = str(body["phase"])
    if phase not in _PHASES:
        raise JournalError(f"unknown bootstrap journal phase {phase!r}")
    if _PHASES.index(phase) >= _PHASES.index("worktree_intended") and (
        not isinstance(body.get("worktree"), str)
        or not body["worktree"]
        or not isinstance(body.get("branch"), str)
        or not body["branch"]
    ):
        raise JournalError(f"bootstrap phase {phase} requires worktree and branch")
    if _PHASES.index(phase) >= _PHASES.index("run_seeded") and (
        not isinstance(body.get("run_id"), str) or not body["run_id"]
    ):
        raise JournalError(f"bootstrap phase {phase} requires a run id")
    return JournalRecord(
        ticket=str(body["ticket"]),
        approval_digest=str(body["approval_digest"]),
        attempt_id=str(body["attempt_id"]),
        approved_base_sha=str(body["approved_base_sha"]),
        route_digest=str(body["route_digest"]),
        phase=phase,
        worktree=body.get("worktree"),
        branch=body.get("branch"),
        run_id=body.get("run_id"),
        digest=canonical_digest({"schema": SCHEMA, **body}),
    )


class BootstrapJournal:
    """Serialize bootstrap phases for one approved attempt."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def _read(self) -> JournalRecord | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise JournalError(f"cannot read bootstrap journal: {exc}") from exc
        if not isinstance(value, dict) or value.get("schema") != SCHEMA:
            raise JournalError("unsupported bootstrap journal schema")
        body = {key: item for key, item in value.items() if key not in {"schema", "digest"}}
        expected = canonical_digest({"schema": SCHEMA, **body})
        if value.get("digest") != expected:
            raise JournalError("bootstrap journal digest does not match its content")
        record = _record(body)
        if record.phase not in _PHASES:
            raise JournalError(f"unknown bootstrap journal phase {record.phase!r}")
        return record

    def _write(self, record: JournalRecord) -> None:
        atomic_write_text(
            self.path, json.dumps(record.to_mapping(), indent=2, sort_keys=True) + "\n"
        )

    def prepare(self, *, ticket: str, approval: Mapping[str, Any]) -> JournalRecord:
        if approval.get("schema") != APPROVAL_SCHEMA:
            raise JournalError("bootstrap requires a typed plan approval receipt")
        required = ("digest", "attempt_id", "approved_base_sha", "route_digest")
        if any(not isinstance(approval.get(key), str) or not approval.get(key) for key in required):
            raise JournalError("approval receipt is missing its approved tuple")
        with flock_blocking(self.lock_path):
            existing = self._read()
            if existing is not None:
                if existing.approval_digest != approval["digest"] or existing.ticket != ticket:
                    raise JournalError("bootstrap journal belongs to a different approved tuple")
                return existing
            record = _record(
                {
                    "ticket": ticket,
                    "approval_digest": approval["digest"],
                    "attempt_id": approval["attempt_id"],
                    "approved_base_sha": approval["approved_base_sha"],
                    "route_digest": approval["route_digest"],
                    "phase": "prepared",
                    "worktree": None,
                    "branch": None,
                    "run_id": None,
                }
            )
            self._write(record)
            return record

    def advance(self, phase: str, **updates: str) -> JournalRecord:
        if phase not in _PHASES:
            raise JournalError(f"unknown bootstrap journal phase {phase!r}")
        with flock_blocking(self.lock_path):
            current = self._read()
            if current is None:
                raise JournalError("bootstrap journal has not been prepared")
            current_index = _PHASES.index(current.phase)
            target_index = _PHASES.index(phase)
            if target_index == current_index:
                return current
            if target_index != current_index + 1:
                raise JournalError(
                    "bootstrap phase must advance from "
                    f"{current.phase} to {_PHASES[current_index + 1]}"
                )
            body = {
                key: item
                for key, item in current.to_mapping().items()
                if key not in {"schema", "digest"}
            }
            body.update(updates)
            body["phase"] = phase
            record = _record(body)
            self._write(record)
            return record

    def recovery(self) -> Recovery:
        with flock_blocking(self.lock_path):
            record = self._read()
        if record is None:
            return Recovery("start", None, None, None, None)
        action = "return_committed" if record.phase == "committed" else "rollback_then_retry"
        return Recovery(action, record.phase, record.worktree, record.branch, record.run_id)

    def restart_after_rollback(self) -> JournalRecord:
        with flock_blocking(self.lock_path):
            current = self._read()
            if current is None:
                raise JournalError("bootstrap journal has not been prepared")
            if current.phase == "committed":
                raise JournalError("a committed bootstrap cannot be restarted")
            record = _record(
                {
                    "ticket": current.ticket,
                    "approval_digest": current.approval_digest,
                    "attempt_id": current.attempt_id,
                    "approved_base_sha": current.approved_base_sha,
                    "route_digest": current.route_digest,
                    "phase": "prepared",
                    "worktree": None,
                    "branch": None,
                    "run_id": None,
                }
            )
            self._write(record)
            return record


__all__ = ["BootstrapJournal", "JournalError", "JournalRecord", "Recovery"]
