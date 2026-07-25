"""Append-only durable queue of tracker mutations that failed to apply.

Library + thin CLI. Stdlib-only.

`FLOW workspace sync` replays these against the tracker. File:
`<workspace_root>/.flow/pending-mutations.jsonl`. Single writer via flock_retry
on `<file>.lock`; atomic append + fsync inside the lock.

Idempotency key formula (canonical for cross-run stability):

    idempotency_key = sha256(ticket + op + canonical_args)[:16]
    canonical_args  = json.dumps(args, sort_keys=True, separators=(",", ":"))

The key omits run_id on purpose: a retry from a recovered run must collide with
the original entry so the dedup scan suppresses a second write. first_run_id is
metadata only.

Quarantine semantics (sidecar, main file untouched):
- Malformed lines encountered during scan are appended to `<file>.quarantine`.
- The main file is never rewritten on read (append-only invariant). compact()
  is the sole rewriter, and only it drops entries.

CLI is `compact --drop-keys` only; append/list are library calls
(tracker_cli.py + sync.py). Exit codes:
  0 = compact ok
  2 = lock contention
  4 = I/O error
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from _atomicio import atomic_write_text
from _jsonl import iter_jsonl
from _locking import flock_retry

# "edit" is not a valid op: the Tracker protocol dropped generic edit(fields)
# (see tracker.py), so a queued edit could never be replayed by FLOW workspace sync.
VALID_OPS: tuple[str, ...] = ("create", "transition", "comment", "link")

Clock = Callable[[], str]


# ─── Errors ──────────────────────────────────────────────────────────────────


class _InvalidArgs(Exception):
    """op not in VALID_OPS or args not a dict. Exit code 3."""


# ─── Paths ───────────────────────────────────────────────────────────────────


def pending_mutations_path(workspace_root: Path) -> Path:
    return workspace_root / ".flow" / "pending-mutations.jsonl"


def _lock_path(workspace_root: Path) -> Path:
    path = pending_mutations_path(workspace_root)
    return path.with_name(path.name + ".lock")


def _quarantine_path(workspace_root: Path) -> Path:
    path = pending_mutations_path(workspace_root)
    return path.with_name(path.name + ".quarantine")


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _canonical_args(args: dict[str, Any]) -> str:
    return json.dumps(args, sort_keys=True, separators=(",", ":"))


def compute_key(ticket: str, op: str, args: dict[str, Any]) -> str:
    src = ticket + op + _canonical_args(args)
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]


def _append_line(path: Path, entry: dict[str, Any]) -> None:
    """Append one JSON line, fsynced. Caller holds the lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# ─── Public API ──────────────────────────────────────────────────────────────


def _do_append(
    workspace_root: Path,
    *,
    ticket: str,
    op: str,
    args: dict[str, Any],
    expected_pre_state: dict[str, Any] | None,
    expected_postcondition: dict[str, Any] | None,
    first_run_id: str | None,
    intent_at: str,
) -> tuple[dict[str, Any], bool]:
    """Core of append_mutation. Returns (entry, appended).

    appended is False when an entry with the same idempotency_key was already on
    disk (the existing entry is returned unchanged).
    """
    if op not in VALID_OPS:
        raise _InvalidArgs(f"op {op!r} not in {VALID_OPS}")
    if not isinstance(args, dict):
        raise _InvalidArgs("args must be a dict")

    canonical = _canonical_args(args)
    key = compute_key(ticket, op, args)
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    path = pending_mutations_path(workspace_root)
    quarantine = _quarantine_path(workspace_root)

    with flock_retry(_lock_path(workspace_root)):
        for existing in iter_jsonl(path, quarantine):
            if existing.get("idempotency_key") == key:
                return existing, False
        entry: dict[str, Any] = {
            "idempotency_key": key,
            "ticket": ticket,
            "op": op,
            "args": args,
            "args_fingerprint": fingerprint,
            "expected_pre_state": expected_pre_state,
            "expected_postcondition": expected_postcondition,
            "intent_at": intent_at,
            "first_run_id": first_run_id,
            "attempts": [],
        }
        _append_line(path, entry)
        return entry, True


def append_mutation(
    workspace_root: Path,
    *,
    ticket: str,
    op: str,
    args: dict[str, Any],
    expected_pre_state: dict[str, Any] | None = None,
    expected_postcondition: dict[str, Any] | None = None,
    first_run_id: str | None = None,
    intent_at: str,
) -> dict[str, Any]:
    """Append one mutation. Idempotent on idempotency_key.

    If an entry with the same idempotency_key is already present, this is a no-op
    and the existing on-disk entry is returned. Otherwise a new entry is
    appended (one line + fsync) under the file lock.

    Raises:
        _InvalidArgs
        LockContention
        OSError
    """
    entry, _ = _do_append(
        workspace_root,
        ticket=ticket,
        op=op,
        args=args,
        expected_pre_state=expected_pre_state,
        expected_postcondition=expected_postcondition,
        first_run_id=first_run_id,
        intent_at=intent_at,
    )
    return entry


def list_mutations(workspace_root: Path) -> list[dict[str, Any]]:
    """Return all on-disk mutation entries. Malformed lines go to the sidecar."""
    path = pending_mutations_path(workspace_root)
    quarantine = _quarantine_path(workspace_root)
    return list(iter_jsonl(path, quarantine))


def compact(workspace_root: Path, drop_keys: set[str]) -> int:
    """Rewrite the file keeping only entries whose key is not in drop_keys.

    Holds the file lock for the whole read-rewrite. Returns the number of
    entries removed. A missing file is a no-op returning 0 (no empty file is
    created).
    """
    path = pending_mutations_path(workspace_root)
    quarantine = _quarantine_path(workspace_root)

    with flock_retry(_lock_path(workspace_root)):
        if not path.exists():
            return 0
        kept: list[dict[str, Any]] = []
        removed = 0
        for entry in iter_jsonl(path, quarantine):
            if entry.get("idempotency_key") in drop_keys:
                removed += 1
            else:
                kept.append(entry)
        content = "".join(json.dumps(e, sort_keys=True) + "\n" for e in kept)
        atomic_write_text(path, content)
    return removed


__all__ = [
    "VALID_OPS",
    "append_mutation",
    "compact",
    "compute_key",
    "list_mutations",
    "pending_mutations_path",
]
