"""Append-only in-flight friction log: `.flow/<namespace>/friction.jsonl`.

The do-verb loop appends one entry whenever the orchestrator hits a snag the run
worked around (a retry, a missing tool, config drift, a lost lease, a planned-file
reconcile, a failed stage). The reflect stage synthesizes these into the
machinery-lens findings (`MACHINERY:` knowledge entries) instead of reconstructing
friction postmortem from state.json, which is lossy.

Friction is operational telemetry, not recall knowledge: it lives in a SEPARATE
file from knowledge.jsonl, is high-cardinality and time-ordered, and is never
deduplicated (each entry is a distinct event, keyed by a uuid4).

Exit codes:
  0 = appended.
  2 = lock contention.
  3 = invalid type or severity.
  4 = I/O error, or workspace memory config missing/invalid.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import _memory_paths
from _locking import LockContention, flock_retry

VALID_TYPES: tuple[str, ...] = (
    "BLOCKER",
    "RETRY",
    "MISSING_TOOL",
    "DRIFT",
    "LEASE_LOSS",
    "RECONCILE",
    "STAGE_FAILED",
)

VALID_SEVERITIES: tuple[str, ...] = ("major", "minor")


class _InvalidType(Exception):
    """Type not in VALID_TYPES, or severity not in VALID_SEVERITIES."""


def _utcnow_iso_ms() -> str:
    """UTC ISO8601 with millisecond precision + Z suffix."""
    t = time.time()
    secs = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))
    ms = int((t - int(t)) * 1000)
    return f"{secs}.{ms:03d}Z"


def append(
    workspace_root: Path,
    ticket: str,
    run_id: str,
    stage: str,
    type_: str,
    body: str,
    detail: str | None = None,
    severity: str = "major",
) -> dict[str, Any]:
    """Append one friction entry. Returns it.

    Raises:
        _InvalidType
        LockContention
        _memory_paths._MemoryConfigError
        OSError
    """
    if type_ not in VALID_TYPES:
        raise _InvalidType(f"type {type_!r} not in {VALID_TYPES}")
    if severity not in VALID_SEVERITIES:
        raise _InvalidType(f"severity {severity!r} not in {VALID_SEVERITIES}")
    namespace = _memory_paths.resolve_namespace(workspace_root)
    fpath = _memory_paths.friction_path(workspace_root, namespace)
    lpath = _memory_paths.friction_lock_path(workspace_root, namespace)

    entry: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "ts": _utcnow_iso_ms(),
        "run_id": run_id,
        "ticket": ticket,
        "stage": stage,
        "type": type_,
        "severity": severity,
        "body": body,
    }
    if detail:
        entry["detail"] = detail

    with flock_retry(lpath):
        fpath.parent.mkdir(parents=True, exist_ok=True)
        with fpath.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    return entry


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Append one entry to .flow/<namespace>/friction.jsonl."
    )
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--run-id", dest="run_id", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--type", dest="type_", required=True)
    parser.add_argument("--body", required=True)
    parser.add_argument("--detail", default=None)
    parser.add_argument("--severity", default="major")
    parser.add_argument("--workspace-root", default=".")
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    workspace_root = Path(args.workspace_root).resolve()
    try:
        entry = append(
            workspace_root=workspace_root,
            ticket=args.ticket,
            run_id=args.run_id,
            stage=args.stage,
            type_=args.type_,
            body=args.body,
            detail=args.detail,
            severity=args.severity,
        )
    except _InvalidType as exc:
        sys.stderr.write(f"flow-friction: {exc}\n")
        return 3
    except LockContention as exc:
        sys.stderr.write(f"flow-friction: {exc}\n")
        return 2
    except _memory_paths._MemoryConfigError as exc:
        sys.stderr.write(f"flow-friction: {exc}\n")
        return 4
    except OSError as exc:
        sys.stderr.write(f"flow-friction: I/O error: {exc}\n")
        return 4
    sys.stdout.write(json.dumps(entry, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["VALID_SEVERITIES", "VALID_TYPES", "append", "cli_main"]
