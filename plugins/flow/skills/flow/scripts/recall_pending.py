"""Recall-pending protocol: plan-phase recall appends, dispatcher promotes.

Library + thin CLI. Stdlib-only.

Two files, two roles:
- `<workspace_root>/.flow/recall-pending.jsonl`: the plan-phase `recall.py
  --record-pending` (the post-gate write, targeting the run worktree) is the SOLE
  writer, appending one entry per recall it observed. The dispatcher's `init`
  promotes matching entries from inside that worktree; its rewrite also moves
  stale entries to the `.stale` sidecar, so promotion is the compactor too.
- `<workspace_root>/.flow/runs/<ticket>/recall-log.jsonl`: promoted entries land here, dispatcher-
  stamped with `recalled_at`.

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

Per-entry three-way partition (stale checked FIRST): older than 24h or a
missing/unparseable hook_observed_at -> stale; else all five rules pass ->
promote; else -> keep.

Exit codes:
  0 = ok
  2 = lock contention
  3 = invalid args
  4 = I/O error
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from _atomicio import atomic_write_text
from _jsonl import iter_jsonl
from _locking import flock_retry
from _runner import Runner
from _runner import default_runner as _default_runner
from _timeutil import parse_iso

_WINDOW = timedelta(hours=24)
_STALE_CAP = 500  # ring-buffer bound on the write-only .stale forensic sidecar


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


def _stale_quarantine_path(workspace_root: Path) -> Path:
    p = _stale_path(workspace_root)
    return p.with_name(p.name + ".quarantine")


def _recall_log_path(workspace_root: Path, ticket: str) -> Path:
    return workspace_root / ".flow" / "runs" / ticket / "recall-log.jsonl"


# ─── Helpers ───────────────────────────────────────────────────────────────────


def compute_pending_id(hook_observed_at: str, branch: str, head_sha: str, cwd: str) -> str:
    src = hook_observed_at + branch + head_sha + cwd
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]


def _append_line(path: Path, entry: dict[str, Any]) -> None:
    """Append one JSON line, fsynced. Caller holds any required lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _atomic_rewrite(path: Path, entries: list[dict[str, Any]]) -> None:
    """Replace `path` with `entries` (one JSON line each) atomically, mode-preserved."""
    atomic_write_text(path, "".join(json.dumps(e, sort_keys=True) + "\n" for e in entries))


def _append_stale_capped(workspace_root: Path, entries: list[dict[str, Any]]) -> None:
    """Append evicted entries to the write-only .stale sidecar, then cap it to
    the most-recent _STALE_CAP entries. Nothing reads .stale back (promotion is
    the only metric path), so it is a bounded forensic trail, not unbounded
    growth. Caller holds the recall-pending flock."""
    if not entries:
        return
    stale_path = _stale_path(workspace_root)
    for entry in entries:
        _append_line(stale_path, entry)
    existing = list(iter_jsonl(stale_path, _stale_quarantine_path(workspace_root)))
    if len(existing) > _STALE_CAP:
        _atomic_rewrite(stale_path, existing[-_STALE_CAP:])


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


def _is_stale(entry: dict[str, Any], cutoff: datetime) -> bool:
    """An entry is stale if its hook_observed_at is missing/unparseable or older
    than `cutoff` (24h before now). Single source of the stale partition."""
    observed = parse_iso(str(entry.get("hook_observed_at", "")))
    return observed is None or observed < cutoff


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
    partitioned: older than 24h or missing/unparseable hook_observed_at ->
    stale; else all five rules pass -> promoted
    (stamped recalled_at=now_iso); else -> kept. Durability order under the lock:
    append promoted, append stale, then atomic-rewrite the pending file to the
    kept set. Returns the promoted entries (each with recalled_at).

    Raises:
        LockContention
        OSError
    """
    runner = runner or _default_runner()
    cwd_path = Path(cwd)
    now = parse_iso(now_iso) or datetime.now(UTC)
    cutoff = now - _WINDOW

    path = recall_pending_path(workspace_root)
    quarantine = _quarantine_path(workspace_root)

    promoted: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []

    with flock_retry(_lock_path(workspace_root)):
        entries = list(iter_jsonl(path, quarantine))
        for entry in entries:
            if _is_stale(entry, cutoff):
                stale.append(entry)
                continue
            matches = (
                entry.get("branch") == branch
                and entry.get("cwd") == cwd
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
        _append_stale_capped(workspace_root, stale)
        _atomic_rewrite(path, kept)

    return promoted


__all__ = [
    "Runner",
    "append_pending",
    "compute_pending_id",
    "list_pending",
    "promote_matching",
    "recall_pending_path",
]
