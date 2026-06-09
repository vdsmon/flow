"""Sole writer of `.flow/<namespace>/ship-events/<ticket>.json`.

Library + thin CLI. Stdlib-only.

Once `<ticket>.json` exists, it is IMMUTABLE. Subsequent attempts write to
`<ticket>.json.dupe.<n>.json` instead (monotonic n). `/flow recover` in phase
8c handles dupe reconciliation. On I/O error during write, an intent log is
left at `<ticket>.json.quarantine-intent.<ts>.json` so recover can replay.

Atomicity: O_EXCL on create. No temp+rename (that would allow overwrite).

CLI:
  observe_ship_event.py --ticket <key> --evidence-json '<json>'
                        --run-id <16-hex> [--workspace-root <dir>]

Evidence JSON validation rejects with exit 1 if:
- not a JSON object at top level
- `ticket` missing / not str / mismatches --ticket arg
- `shipped_at` missing / fails UTC ISO8601 Z regex
- `evidence` missing / not dict
- any extra top-level key present (script owns observed_at / observed_by_run_id /
  flow_attribution)

When a coherent live run state.json is found at ship time (matching run_id, with
both plan.started_at_iso and create_pr.finished_at_iso present), an owned
`flow_attribution` block is stamped onto the durable record so metric.py can
attribute the ship after the run's worktree (and its state.json) is reaped.
Backend-only observations (no live state) carry no stamp. Forward-only: tickets
shipped before this lands have no stamp and a reaped state, so they stay
backend-not-attributed.

Exit codes:
  0 = primary write succeeded
  1 = evidence JSON invalid, or run_id not 16 hex chars
  2 = EEXIST — wrote .dupe.<n>.json instead (informational, not error)
  3 = I/O error, lock contention, or workspace memory config missing/invalid
      (intent log written; surfaces for /flow recover)
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import _memory_paths
from _locking import LockContention, flock_retry

_SHIPPED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_RUN_ID_RE = re.compile(r"^[0-9a-f]{16}$")

_ALLOWED_TOP_KEYS: frozenset[str] = frozenset({"ticket", "shipped_at", "evidence"})


# ─── Errors ──────────────────────────────────────────────────────────────────


class _EvidenceInvalid(Exception):
    """Evidence JSON fails validation. Exit code 1."""


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _utcnow_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ts_token() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def validate_evidence(payload: Any, ticket: str) -> dict[str, Any]:
    """Returns the validated dict or raises `_EvidenceInvalid`."""
    if not isinstance(payload, dict):
        raise _EvidenceInvalid("evidence JSON top level is not an object")
    extras = set(payload.keys()) - _ALLOWED_TOP_KEYS
    if extras:
        raise _EvidenceInvalid(f"extra top-level keys not allowed: {sorted(extras)}")
    p_ticket = payload.get("ticket")
    if not isinstance(p_ticket, str):
        raise _EvidenceInvalid("ticket missing or not a string")
    if p_ticket != ticket:
        raise _EvidenceInvalid(f"ticket {p_ticket!r} mismatches --ticket {ticket!r}")
    shipped_at = payload.get("shipped_at")
    if not isinstance(shipped_at, str) or not _SHIPPED_AT_RE.match(shipped_at):
        raise _EvidenceInvalid("shipped_at missing or not UTC ISO8601 Z")
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        raise _EvidenceInvalid("evidence field missing or not an object")
    return payload


def _serialize(record: dict[str, Any]) -> str:
    return json.dumps(record, indent=2, sort_keys=True) + "\n"


def _write_o_excl(path: Path, content: str) -> None:
    """O_EXCL create + write + fsync + fsync parent dir.

    Raises FileExistsError on EEXIST, other OSError otherwise.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(str(path), flags, 0o644)
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    # fsync parent dir to make the rename visible.
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _next_dupe_path(primary: Path) -> Path:
    """Pick `<primary>.dupe.<n>.json` for next monotonic n (max + 1, or 1)."""
    pattern = primary.name + ".dupe."
    max_n = 0
    for sibling in primary.parent.glob(f"{primary.name}.dupe.*.json"):
        suffix = sibling.name[len(pattern) :]
        # suffix is `<n>.json`
        n_str = suffix[: -len(".json")] if suffix.endswith(".json") else suffix
        try:
            n = int(n_str)
        except ValueError:
            continue
        max_n = max(max_n, n)
    return primary.parent / f"{primary.name}.dupe.{max_n + 1}.json"


def _intent_log_path(primary: Path) -> Path:
    return primary.parent / f"{primary.name}.quarantine-intent.{_ts_token()}.json"


def _write_intent_log(primary: Path, record: dict[str, Any], err: str) -> None:
    """Best-effort intent log write. Never raises."""
    payload = {
        "primary_path": str(primary),
        "ts": _utcnow_iso(),
        "error": err,
        "record": record,
    }
    log_path = _intent_log_path(primary)
    with contextlib.suppress(OSError):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


# ─── Attribution stamp ───────────────────────────────────────────────────────


def _attribution_stamp(workspace_root: Path, ticket: str, run_id: str) -> dict[str, str] | None:
    """Read the live run state.json and return durable attribution timestamps.

    Returns `{"plan_started_at_iso": ..., "create_pr_finished_at_iso": ...}` ONLY
    when the live state is coherent: its `run_id` equals `run_id` AND both
    `stages.plan.started_at_iso` and `stages.create_pr.finished_at_iso` are
    non-empty strings. Otherwise None.

    Fully guarded: any OSError / json.JSONDecodeError / non-dict shape yields None,
    never raises. This stamps the durable ship-event while state.json is alive
    (pre-reap), since metric.py can no longer join to the reaped worktree path.
    """
    state_path = workspace_root / ".flow" / "runs" / ticket / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict) or state.get("run_id") != run_id:
        return None
    stages = state.get("stages")
    if not isinstance(stages, dict):
        return None
    plan = stages.get("plan")
    create_pr = stages.get("create_pr")
    if not isinstance(plan, dict) or not isinstance(create_pr, dict):
        return None
    plan_started = plan.get("started_at_iso")
    create_pr_finished = create_pr.get("finished_at_iso")
    if not isinstance(plan_started, str) or not plan_started:
        return None
    if not isinstance(create_pr_finished, str) or not create_pr_finished:
        return None
    return {
        "plan_started_at_iso": plan_started,
        "create_pr_finished_at_iso": create_pr_finished,
    }


# ─── Public API ──────────────────────────────────────────────────────────────


def observe(
    workspace_root: Path,
    ticket: str,
    evidence_payload: dict[str, Any],
    run_id: str,
) -> tuple[Path, bool]:
    """Write a ship-event evidence file.

    Returns `(path_written, is_dupe)`.

    Raises `_EvidenceInvalid` if payload fails validation.
    Raises `_memory_paths._MemoryConfigError` if namespace can't resolve.
    Raises `OSError` on non-EEXIST I/O errors (intent log written first).
    Raises `LockContention` if the dupe lock can't be acquired (intent log written first).
    """
    validated = validate_evidence(evidence_payload, ticket)
    if not _RUN_ID_RE.match(run_id):
        raise _EvidenceInvalid(f"run_id {run_id!r} not 16 hex chars")
    namespace = _memory_paths.resolve_namespace(workspace_root)
    primary = _memory_paths.ship_event_path(workspace_root, namespace, ticket)
    record: dict[str, Any] = dict(validated)
    record["observed_at"] = _utcnow_iso()
    record["observed_by_run_id"] = run_id
    stamp = _attribution_stamp(workspace_root, ticket, run_id)
    if stamp is not None:
        record["flow_attribution"] = stamp
    content = _serialize(record)

    try:
        _write_o_excl(primary, content)
        return primary, False
    except FileExistsError:
        pass
    except OSError as exc:
        _write_intent_log(primary, record, f"{exc.errno}: {exc.strerror}")
        raise

    # EEXIST → dupe path under lock.
    dupe_lock = primary.parent / f"{primary.name}.dupe.lock"
    try:
        with flock_retry(dupe_lock):
            dupe_path = _next_dupe_path(primary)
            record["superseded_by_dupe"] = False
            try:
                _write_o_excl(dupe_path, _serialize(record))
            except FileExistsError:
                # Extremely unlikely under flock; surface as I/O error.
                raise OSError(errno.EEXIST, "dupe path collision under flock") from None
    except (OSError, LockContention) as exc:
        _write_intent_log(primary, record, f"dupe write failed: {exc}")
        raise
    return dupe_path, True


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sole writer of ship-event evidence files.")
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--evidence-json", required=True, help="JSON string.")
    parser.add_argument("--run-id", required=True, help="16-hex run_id from dispatcher.")
    parser.add_argument("--workspace-root", default=".")
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    workspace_root = Path(args.workspace_root).resolve()
    try:
        payload = json.loads(args.evidence_json)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"observe-ship-event: --evidence-json not JSON: {exc}\n")
        return 1
    try:
        path, is_dupe = observe(workspace_root, args.ticket, payload, args.run_id)
    except _EvidenceInvalid as exc:
        sys.stderr.write(f"observe-ship-event: {exc}\n")
        return 1
    except _memory_paths._MemoryConfigError as exc:
        sys.stderr.write(f"observe-ship-event: {exc}\n")
        return 3
    except LockContention as exc:
        sys.stderr.write(f"observe-ship-event: I/O error: {exc}\n")
        return 3
    except OSError as exc:
        sys.stderr.write(f"observe-ship-event: I/O error: {exc}\n")
        return 3
    sys.stdout.write(json.dumps({"path": str(path), "is_dupe": is_dupe}) + "\n")
    return 2 if is_dupe else 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["cli_main", "observe", "validate_evidence"]
