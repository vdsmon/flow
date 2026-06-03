"""Append-only durable queue of tracker mutations that failed to apply.

Library + thin CLI. Stdlib-only.

`/flow sync` replays these against the tracker. File:
`<workspace_root>/.flow/pending-mutations.jsonl`. Single writer via flock_retry
on `<file>.lock`; atomic append + fsync inside the lock.

Idempotency key formula (canonical for cross-run stability):

    idempotency_key = sha256(ticket + op + canonical_args)[:16]
    canonical_args  = json.dumps(args, sort_keys=True, separators=(",", ":"))

The key omits run_id on purpose: a retry from a recovered run must collide with
the original entry so the dedup scan suppresses a second write. first_run_id is
metadata only.

Quarantine semantics (sidecar — main file untouched):
- Malformed lines encountered during scan are appended to `<file>.quarantine`.
- The main file is never rewritten on read (append-only invariant). compact()
  is the sole rewriter, and only it drops entries.

Exit codes:
  0 = appended (or list/compact ok)
  1 = duplicate key (no-op, append only)
  2 = lock contention
  3 = schema / invalid args
  4 = I/O error
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from _atomicio import atomic_write_text
from _jsonl import iter_jsonl
from _locking import LockContention, flock_retry

VALID_OPS: tuple[str, ...] = ("create", "edit", "transition", "comment", "link")

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


def _utcnow_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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
    key = hashlib.sha256((ticket + op + canonical).encode("utf-8")).hexdigest()[:16]
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


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Durable queue of failed tracker mutations.")
    parser.add_argument("--workspace-root", default=".")
    sub = parser.add_subparsers(dest="command", required=True)

    p_append = sub.add_parser("append", help="append one mutation (idempotent).")
    p_append.add_argument("--ticket", required=True)
    p_append.add_argument("--op", required=True)
    p_append.add_argument("--args-json", required=True, help="mutation args as a JSON object.")
    p_append.add_argument("--expected-pre", default=None, help="expected_pre_state JSON.")
    p_append.add_argument("--expected-post", default=None, help="expected_postcondition JSON.")
    p_append.add_argument("--first-run-id", default=None)

    sub.add_parser("list", help="print all entries as a JSON array.")

    p_compact = sub.add_parser("compact", help="drop named keys, rewrite the file.")
    p_compact.add_argument("--drop-keys", default="", help="comma-separated idempotency_keys.")

    return parser.parse_args(argv)


def _parse_json_object(raw: str, field: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _InvalidArgs(f"{field} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise _InvalidArgs(f"{field} is not a JSON object")
    return value


def _cmd_append(args: argparse.Namespace, workspace_root: Path, clock: Clock) -> int:
    try:
        parsed_args = _parse_json_object(args.args_json, "--args-json")
        pre = _parse_json_object(args.expected_pre, "--expected-pre") if args.expected_pre else None
        post = (
            _parse_json_object(args.expected_post, "--expected-post")
            if args.expected_post
            else None
        )
    except _InvalidArgs as exc:
        sys.stderr.write(f"pending-mutations: {exc}\n")
        return 3
    try:
        entry, appended = _do_append(
            workspace_root,
            ticket=args.ticket,
            op=args.op,
            args=parsed_args,
            expected_pre_state=pre,
            expected_postcondition=post,
            first_run_id=args.first_run_id,
            intent_at=clock(),
        )
    except _InvalidArgs as exc:
        sys.stderr.write(f"pending-mutations: {exc}\n")
        return 3
    except LockContention as exc:
        sys.stderr.write(f"pending-mutations: {exc}\n")
        return 2
    except OSError as exc:
        sys.stderr.write(f"pending-mutations: I/O error: {exc}\n")
        return 4
    sys.stdout.write(json.dumps(entry, sort_keys=True) + "\n")
    return 0 if appended else 1


def _cmd_list(workspace_root: Path) -> int:
    try:
        entries = list_mutations(workspace_root)
    except OSError as exc:
        sys.stderr.write(f"pending-mutations: I/O error: {exc}\n")
        return 4
    sys.stdout.write(json.dumps(entries, sort_keys=True) + "\n")
    return 0


def _cmd_compact(args: argparse.Namespace, workspace_root: Path) -> int:
    drop_keys = {k.strip() for k in args.drop_keys.split(",") if k.strip()}
    try:
        removed = compact(workspace_root, drop_keys)
    except LockContention as exc:
        sys.stderr.write(f"pending-mutations: {exc}\n")
        return 2
    except OSError as exc:
        sys.stderr.write(f"pending-mutations: I/O error: {exc}\n")
        return 4
    sys.stdout.write(json.dumps({"removed": removed}) + "\n")
    return 0


def cli_main(argv: list[str], clock: Clock = _utcnow_iso) -> int:
    args = _parse_args(argv)
    workspace_root = Path(args.workspace_root).resolve()
    if args.command == "append":
        return _cmd_append(args, workspace_root, clock)
    if args.command == "list":
        return _cmd_list(workspace_root)
    if args.command == "compact":
        return _cmd_compact(args, workspace_root)
    return 3  # unreachable: argparse requires a subcommand.


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "VALID_OPS",
    "append_mutation",
    "cli_main",
    "compact",
    "compute_key",
    "list_mutations",
    "pending_mutations_path",
]
