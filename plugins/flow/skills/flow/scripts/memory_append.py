"""Single-writer append to `.flow/<namespace>/knowledge.jsonl`.

Library + thin CLI. Stdlib-only.

Idempotency key formula (canonical for cross-run stability):

    id = sha256(namespace + ticket + type + normalized_body)[:16]
    normalize(body) = NFKC + lowercase + collapse-whitespace + strip-trailing-punct

The `ts` field is NOT in the formula so `/flow recover` reruns produce the
same id, letting the dedup scan suppress re-writes. `--id <override>` exists
for entries bound to specific intents (ship-event anchors) where the
formula's inputs aren't sufficient.

Quarantine semantics (sidecar — main file untouched):
- Malformed lines encountered during scan are APPENDED to
  `<file>.quarantine.<ts>` (one sidecar per invocation).
- Main `knowledge.jsonl` is NEVER rewritten — append-only invariant holds.
- Scan continues with remaining valid lines. Never crash.

Exit codes:
  0 = appended.
  1 = duplicate id (no-op).
  2 = lock contention.
  3 = invalid type.
  4 = I/O error, or workspace memory config missing/invalid.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

import _memory_paths
from _jsonl import iter_jsonl
from _locking import LockContention, flock_retry

VALID_TYPES: tuple[str, ...] = (
    "LEARNED",
    "DECISION",
    "FACT",
    "PATTERN",
    "INVESTIGATION",
    "DEVIATION",
)

_WS_RE = re.compile(r"\s+")
_TRAILING_PUNCT_RE = re.compile(r"[\.\,\;\:\!\?\-\—\s]+$")


# ─── Errors ──────────────────────────────────────────────────────────────────


class _InvalidType(Exception):
    """Type not in VALID_TYPES."""


class _DuplicateId(Exception):
    """Entry with this id already present."""


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _utcnow_iso_ms() -> str:
    """UTC ISO8601 with millisecond precision + Z suffix."""
    t = time.time()
    secs = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))
    ms = int((t - int(t)) * 1000)
    return f"{secs}.{ms:03d}Z"


def _ts_token() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _normalize_body(body: str) -> str:
    normalized = unicodedata.normalize("NFKC", body).lower()
    collapsed = _WS_RE.sub(" ", normalized).strip()
    return _TRAILING_PUNCT_RE.sub("", collapsed)


def compute_id(namespace: str, ticket: str, type_: str, body: str) -> str:
    src = namespace + ticket + type_ + _normalize_body(body)
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]


def _scan_for_id(
    knowledge_path: Path,
    target_id: str,
    quarantine_sidecar: Path,
) -> bool:
    """Returns True if target_id present. Malformed lines → sidecar."""
    for entry in iter_jsonl(knowledge_path, quarantine_sidecar):
        if entry.get("id") == target_id:
            return True
    return False


# ─── Public API ──────────────────────────────────────────────────────────────


def append(
    workspace_root: Path,
    type_: str,
    body: str,
    branch: str,
    ticket: str,
    id_override: str | None = None,
) -> dict[str, Any]:
    """Append one entry to knowledge.jsonl. Returns the entry.

    Raises:
        _InvalidType
        _DuplicateId
        LockContention
        _memory_paths._MemoryConfigError
        OSError
    """
    if type_ not in VALID_TYPES:
        raise _InvalidType(f"type {type_!r} not in {VALID_TYPES}")
    namespace = _memory_paths.resolve_namespace(workspace_root)
    kpath = _memory_paths.knowledge_path(workspace_root, namespace)
    lpath = _memory_paths.knowledge_lock_path(workspace_root, namespace)
    entry_id = id_override or compute_id(namespace, ticket, type_, body)
    quarantine_sidecar = kpath.with_name(f"{kpath.name}.quarantine.{_ts_token()}")

    with flock_retry(lpath):
        if _scan_for_id(kpath, entry_id, quarantine_sidecar):
            raise _DuplicateId(entry_id)
        entry = {
            "id": entry_id,
            "ts": _utcnow_iso_ms(),
            "type": type_,
            "namespace": namespace,
            "branch": branch,
            "ticket": ticket,
            "body": body,
        }
        kpath.parent.mkdir(parents=True, exist_ok=True)
        with kpath.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    return entry


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Single-writer append to .flow/<namespace>/knowledge.jsonl."
    )
    parser.add_argument("--type", dest="type_", required=True)
    parser.add_argument("--text", required=True, help="entry body (raw text).")
    parser.add_argument("--branch", required=True)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--id", dest="id_override", default=None)
    parser.add_argument("--workspace-root", default=".")
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    workspace_root = Path(args.workspace_root).resolve()
    try:
        entry = append(
            workspace_root=workspace_root,
            type_=args.type_,
            body=args.text,
            branch=args.branch,
            ticket=args.ticket,
            id_override=args.id_override,
        )
    except _InvalidType as exc:
        sys.stderr.write(f"memory-append: {exc}\n")
        return 3
    except _DuplicateId as exc:
        sys.stderr.write(f"memory-append: duplicate id {exc}; no-op\n")
        return 1
    except LockContention as exc:
        sys.stderr.write(f"memory-append: {exc}\n")
        return 2
    except _memory_paths._MemoryConfigError as exc:
        sys.stderr.write(f"memory-append: {exc}\n")
        return 4
    except OSError as exc:
        sys.stderr.write(f"memory-append: I/O error: {exc}\n")
        return 4
    sys.stdout.write(json.dumps(entry, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["VALID_TYPES", "append", "cli_main", "compute_id"]
