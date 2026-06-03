"""Recall-pending protocol: SessionStart hook appends, dispatcher promotes.

Library + thin CLI. Stdlib-only.

Two files, two roles:
- `<workspace_root>/.flow/recall-pending.jsonl` — the hook is the SOLE writer,
  appending one entry per recall it observed. The dispatcher is the SOLE
  compactor: it reads, promotes matching entries, and rewrites the file.
- `<workspace_root>/.flow/runs/<ticket>/recall-log.jsonl` — promoted entries
  land here, dispatcher-stamped with `recalled_at`.

Idempotency key:

    pending_id = sha256(hook_observed_at + branch + head_sha + cwd)[:16]

query / returned_ids / rank_scores are NOT in the hash, so a re-append with the
same observation but a different payload is a no-op returning what is on disk.

Promotion rules (an entry promotes iff ALL hold):
  (a) entry.branch == branch
  (b) entry.cwd == cwd
  (c) entry.hook_observed_at within 24h before now_iso
  (d) entry.hook_time_resolved_ticket in ("", ticket)
  (e) entry.head_sha is an ancestor of current HEAD
      (git merge-base --is-ancestor returns 0)

Per-entry three-way partition (stale checked FIRST): older than 24h -> stale;
else all five rules pass -> promote; else -> keep.

Exit codes:
  0 = ok
  2 = lock contention
  3 = invalid args
  4 = I/O error
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from _jsonl import iter_jsonl
from _locking import LockContention, flock_retry

Runner = Callable[..., subprocess.CompletedProcess[str]]

_WINDOW = timedelta(hours=24)


# ─── Runner ──────────────────────────────────────────────────────────────────


def _default_runner() -> Runner:
    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )

    return run


# ─── Paths ───────────────────────────────────────────────────────────────────


def recall_pending_path(workspace_root: Path) -> Path:
    return workspace_root / ".flow" / "recall-pending.jsonl"


def _lock_path(workspace_root: Path) -> Path:
    return recall_pending_path(workspace_root).with_name("recall-pending.jsonl.lock")


def _quarantine_path(workspace_root: Path) -> Path:
    path = recall_pending_path(workspace_root)
    return path.with_name(path.name + ".quarantine")


def _stale_path(workspace_root: Path) -> Path:
    path = recall_pending_path(workspace_root)
    return path.with_name(path.name + ".stale")


def _recall_log_path(workspace_root: Path, ticket: str) -> Path:
    return workspace_root / ".flow" / "runs" / ticket / "recall-log.jsonl"


# ─── Helpers ───────────────────────────────────────────────────────────────────


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_pending_id(hook_observed_at: str, branch: str, head_sha: str, cwd: str) -> str:
    src = hook_observed_at + branch + head_sha + cwd
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]


def _parse_iso(value: str) -> datetime | None:
    """Parse a UTC ISO8601 timestamp into a tz-aware datetime, or None on failure."""
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _append_line(path: Path, entry: dict[str, Any]) -> None:
    """Append one JSON line, fsynced. Caller holds any required lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _atomic_rewrite(path: Path, entries: list[dict[str, Any]]) -> None:
    """Replace `path` with `entries` (one JSON line each) atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(e, sort_keys=True) + "\n" for e in entries)
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
    with contextlib.suppress(OSError):
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


# ─── Public API ──────────────────────────────────────────────────────────────


def append_pending(
    workspace_root: Path,
    *,
    hook_observed_at: str,
    branch: str,
    head_sha: str,
    cwd: str,
    hook_time_resolved_ticket: str,
    query: str,
    returned_ids: list[str],
    rank_scores: list[float],
) -> dict[str, Any]:
    """Append one recall-pending entry. Idempotent on pending_id.

    If an entry with the same pending_id is already present, this is a no-op and
    the existing on-disk entry is returned. Otherwise a new entry is appended.

    Raises:
        LockContention
        OSError
    """
    pending_id = compute_pending_id(hook_observed_at, branch, head_sha, cwd)
    path = recall_pending_path(workspace_root)
    quarantine = _quarantine_path(workspace_root)

    with flock_retry(_lock_path(workspace_root)):
        for existing in iter_jsonl(path, quarantine):
            if existing.get("pending_id") == pending_id:
                return existing
        entry: dict[str, Any] = {
            "pending_id": pending_id,
            "hook_observed_at": hook_observed_at,
            "branch": branch,
            "head_sha": head_sha,
            "cwd": cwd,
            "hook_time_resolved_ticket": hook_time_resolved_ticket,
            "query": query,
            "returned_ids": returned_ids,
            "rank_scores": rank_scores,
        }
        _append_line(path, entry)
    return entry


def list_pending(workspace_root: Path) -> list[dict[str, Any]]:
    """Read all valid recall-pending entries. Malformed lines are quarantined."""
    path = recall_pending_path(workspace_root)
    quarantine = _quarantine_path(workspace_root)
    return list(iter_jsonl(path, quarantine))


def _is_ancestor(entry: dict[str, Any], cwd: Path, runner: Runner) -> bool:
    head_sha = entry.get("head_sha")
    if not isinstance(head_sha, str) or not head_sha:
        return False
    result = runner(["git", "merge-base", "--is-ancestor", head_sha, "HEAD"], cwd)
    return result.returncode == 0


def promote_matching(
    workspace_root: Path,
    *,
    ticket: str,
    branch: str,
    head_sha: str,  # accepted for CLI symmetry; rule (e) compares entry.head_sha to "HEAD"
    cwd: str,
    now_iso: str,
    runner: Runner | None = None,
) -> list[dict[str, Any]]:
    """Promote matching pending entries into the per-ticket recall log.

    Holds the recall-pending flock for the whole operation. Each entry is
    partitioned: older than 24h -> stale; else all five rules pass -> promoted
    (stamped recalled_at=now_iso); else -> kept. Durability order under the lock:
    append promoted, append stale, then atomic-rewrite the pending file to the
    kept set. Returns the promoted entries (each with recalled_at).

    Raises:
        LockContention
        OSError
    """
    runner = runner or _default_runner()
    cwd_path = Path(cwd)
    now = _parse_iso(now_iso) or datetime.now(UTC)
    cutoff = now - _WINDOW

    path = recall_pending_path(workspace_root)
    quarantine = _quarantine_path(workspace_root)

    promoted: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []

    with flock_retry(_lock_path(workspace_root)):
        entries = list(iter_jsonl(path, quarantine))
        for entry in entries:
            observed = _parse_iso(str(entry.get("hook_observed_at", "")))
            if observed is not None and observed < cutoff:
                stale.append(entry)
                continue
            matches = (
                entry.get("branch") == branch
                and entry.get("cwd") == cwd
                and observed is not None
                and entry.get("hook_time_resolved_ticket") in ("", ticket)
                and _is_ancestor(entry, cwd_path, runner)
            )
            if matches:
                stamped = dict(entry)
                stamped["recalled_at"] = now_iso
                promoted.append(stamped)
            else:
                kept.append(entry)

        if promoted:
            log_path = _recall_log_path(workspace_root, ticket)
            for stamped in promoted:
                _append_line(log_path, stamped)
        for entry in stale:
            _append_line(_stale_path(workspace_root), entry)
        _atomic_rewrite(path, kept)

    return promoted


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _split_csv(value: str) -> list[str]:
    if not value:
        return []
    return value.split(",")


def _parse_rank_scores(value: str) -> list[float]:
    if not value:
        return []
    return [float(part) for part in value.split(",")]


def _parse_args(argv: list[str]) -> argparse.Namespace:
    # --workspace-root lives on a parent parser so it is accepted both before
    # and after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace-root", default=".")

    parser = argparse.ArgumentParser(
        description="Recall-pending append / list / promote.", parents=[common]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_append = sub.add_parser("append", parents=[common])
    p_append.add_argument("--branch", required=True)
    p_append.add_argument("--head-sha", required=True)
    p_append.add_argument("--cwd", required=True)
    p_append.add_argument("--resolved-ticket", default="")
    p_append.add_argument("--query", default="")
    p_append.add_argument("--returned-ids", default="")
    p_append.add_argument("--rank-scores", default="")
    p_append.add_argument("--hook-observed-at", default=None)

    sub.add_parser("list", parents=[common])

    p_promote = sub.add_parser("promote", parents=[common])
    p_promote.add_argument("--ticket", required=True)
    p_promote.add_argument("--branch", required=True)
    p_promote.add_argument("--head-sha", required=True)
    p_promote.add_argument("--cwd", required=True)
    p_promote.add_argument("--now", default=None)

    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    workspace_root = Path(args.workspace_root).resolve()
    try:
        if args.command == "append":
            entry = append_pending(
                workspace_root,
                hook_observed_at=args.hook_observed_at or _utcnow_iso(),
                branch=args.branch,
                head_sha=args.head_sha,
                cwd=args.cwd,
                hook_time_resolved_ticket=args.resolved_ticket,
                query=args.query,
                returned_ids=_split_csv(args.returned_ids),
                rank_scores=_parse_rank_scores(args.rank_scores),
            )
            sys.stdout.write(json.dumps(entry, sort_keys=True) + "\n")
        elif args.command == "list":
            sys.stdout.write(json.dumps(list_pending(workspace_root), sort_keys=True) + "\n")
        else:
            promoted = promote_matching(
                workspace_root,
                ticket=args.ticket,
                branch=args.branch,
                head_sha=args.head_sha,
                cwd=args.cwd,
                now_iso=args.now or _utcnow_iso(),
            )
            sys.stdout.write(json.dumps(promoted, sort_keys=True) + "\n")
    except ValueError as exc:
        sys.stderr.write(f"recall-pending: invalid args: {exc}\n")
        return 3
    except LockContention as exc:
        sys.stderr.write(f"recall-pending: {exc}\n")
        return 2
    except OSError as exc:
        sys.stderr.write(f"recall-pending: I/O error: {exc}\n")
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "Runner",
    "append_pending",
    "cli_main",
    "compute_pending_id",
    "list_pending",
    "promote_matching",
    "recall_pending_path",
]
