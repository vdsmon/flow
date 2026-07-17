"""Single-writer append to `.flow/<namespace>/knowledge.jsonl`.

Library + thin CLI. Stdlib-only.

Idempotency key formula (canonical for cross-run stability):

    id = sha256(namespace + ticket + type + normalized_body)[:16]
    normalize(body) = NFKC + lowercase + collapse-whitespace + strip-trailing-punct

The `ts` field is NOT in the formula so `FLOW workspace repair` reruns produce the
same id, letting the dedup scan suppress re-writes.

The stored body is sanitized before it is written: terminal control sequences
(ANSI/C1 CSI, OSC) are stripped, then any remaining C0/C1 control bytes are
stripped too (this catches escapes the CSI/OSC regex misses, e.g. unterminated
or newline-split OSC), and the result is capped at `_MAX_BODY_LENGTH`
characters, with `_TRUNCATION_MARKER` appended when the cap is hit. `compute_id`
always runs on the raw caller input, never the sanitized/truncated storage form.

Quarantine semantics (sidecar, main file untouched):
- Malformed lines encountered during scan are APPENDED to
  `<file>.quarantine.<ts>` (one sidecar per invocation).
- Main `knowledge.jsonl` is NEVER rewritten, append-only invariant holds.
- Scan continues with remaining valid lines. Never crash.

Exit codes:
  0 = appended.
  1 = duplicate id (no-op).
  2 = lock contention.
  3 = invalid type.
  4 = I/O error, or workspace memory config missing/invalid.
  5 = unknown --supersedes target id (not present in knowledge.jsonl).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

import _memory_paths
from _jsonl import iter_jsonl
from _locking import LockContention, flock_retry
from _timeutil import ts_token, utcnow_iso_ms

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

_MAX_BODY_LENGTH = 5_120
_TRUNCATION_MARKER = "\n[truncated by memory_append]"

# Terminal control sequences stripped from stored bodies before the length cap is
# applied: standard ESC-bracket CSI, the single-byte C1 CSI (0x9b), and OSC (ESC ]
# or 0x9d) terminated by BEL (0x07) or ST (ESC \ or 0x9c).
_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI: ESC [ ... final byte
    r"|\x9b[0-?]*[ -/]*[@-~]"  # C1 CSI
    r"|(?:\x1b\]|\x9d).*?(?:\x07|\x1b\\|\x9c)"  # OSC ... BEL/ST
)

# Backstop for every escape/control byte _ANSI_RE doesn't catch (a lone ESC, RIS
# `\x1bc`, an unterminated or newline-split OSC, bare CR/BEL/BS, ...): any C0
# control byte except newline/tab, plus DEL and the C1 range. Runs after _ANSI_RE
# so it only ever removes leftover control bytes, never legitimate text.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


# ─── Errors ──────────────────────────────────────────────────────────────────


class _InvalidType(Exception):
    """Type not in VALID_TYPES."""


class _DuplicateId(Exception):
    """Entry with this id already present."""


class _UnknownSupersedeTarget(Exception):
    """--supersedes named an id not present in knowledge.jsonl."""


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _normalize_body(body: str) -> str:
    normalized = unicodedata.normalize("NFKC", body).lower()
    collapsed = _WS_RE.sub(" ", normalized).strip()
    return _TRAILING_PUNCT_RE.sub("", collapsed)


def compute_id(namespace: str, ticket: str, type_: str, body: str) -> str:
    src = namespace + ticket + type_ + _normalize_body(body)
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]


def _sanitize_body(body: str) -> str:
    """Storage representation of `body`: ANSI/C1/OSC stripped, then any leftover
    control bytes stripped, then capped at `_MAX_BODY_LENGTH`.
    Never fed to `compute_id`, which stays on raw input."""
    cleaned = _ANSI_RE.sub("", body)
    cleaned = _CONTROL_RE.sub("", cleaned)
    if len(cleaned) <= _MAX_BODY_LENGTH:
        return cleaned
    prefix_len = _MAX_BODY_LENGTH - len(_TRUNCATION_MARKER)
    return cleaned[:prefix_len] + _TRUNCATION_MARKER


def _scan_for_ids(
    knowledge_path: Path,
    target_ids: set[str],
    quarantine_sidecar: Path,
) -> set[str]:
    """One pass over knowledge.jsonl. Returns the subset of target_ids present.
    Malformed lines → sidecar."""
    if not target_ids:
        return set()
    found: set[str] = set()
    for entry in iter_jsonl(knowledge_path, quarantine_sidecar):
        eid = entry.get("id")
        if eid in target_ids:
            found.add(eid)
    return found


# ─── Public API ──────────────────────────────────────────────────────────────


def append(
    workspace_root: Path,
    type_: str,
    body: str,
    branch: str,
    ticket: str,
    supersedes: str | list[str] | None = None,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """Append one entry to knowledge.jsonl. Returns the entry.

    `supersedes` is a single target id, a list of target ids (a canonical entry
    consolidating a whole cluster), or None. Every target must already be present
    in knowledge.jsonl.

    `labels` is an optional `["facet:value", ...]` array (e.g. `form:iva_2083`)
    for `recall.py --label` cluster retrieval. Metadata like `ts`/`supersedes`,
    NOT a `compute_id` input.

    Raises:
        _InvalidType
        _DuplicateId
        _UnknownSupersedeTarget
        LockContention
        _memory_paths._MemoryConfigError
        OSError
    """
    if type_ not in VALID_TYPES:
        raise _InvalidType(f"type {type_!r} not in {VALID_TYPES}")
    namespace = _memory_paths.resolve_namespace(workspace_root)
    kpath = _memory_paths.knowledge_path(workspace_root, namespace)
    lpath = _memory_paths.knowledge_lock_path(workspace_root, namespace)
    entry_id = compute_id(namespace, ticket, type_, body)
    quarantine_sidecar = kpath.with_name(f"{kpath.name}.quarantine.{ts_token()}")

    if supersedes is None:
        targets: list[str] = []
    elif isinstance(supersedes, str):
        targets = [supersedes] if supersedes else []
    else:
        targets = list(supersedes)

    with flock_retry(lpath):
        present = _scan_for_ids(kpath, {entry_id, *targets}, quarantine_sidecar)
        if entry_id in present:
            raise _DuplicateId(entry_id)
        missing = set(targets) - present
        if missing:
            raise _UnknownSupersedeTarget(sorted(missing)[0])
        entry: dict[str, Any] = {
            "id": entry_id,
            "ts": utcnow_iso_ms(),
            "type": type_,
            "namespace": namespace,
            "branch": branch,
            "ticket": ticket,
            "body": _sanitize_body(body),
        }
        # supersedes is a tombstone pointer (metadata like ts), NOT a hash input,
        # so a superseding entry's id stays stable across recover reruns. Only
        # present when non-empty, to avoid churning every record with a null field.
        if supersedes:
            entry["supersedes"] = supersedes
        if labels:
            entry["labels"] = labels
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
    parser.add_argument(
        "--text",
        required=True,
        help="entry body (raw text); stored with terminal control sequences "
        f"stripped and capped at {_MAX_BODY_LENGTH} characters.",
    )
    parser.add_argument("--branch", required=True)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--supersedes", default=None)
    parser.add_argument(
        "--labels", default=None, help="comma-separated labels, e.g. form:iva_2083,area:vat"
    )
    parser.add_argument("--workspace-root", default=".")
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    workspace_root = Path(args.workspace_root).resolve()
    labels = [tok.strip() for tok in (args.labels or "").split(",") if tok.strip()]
    try:
        entry = append(
            workspace_root=workspace_root,
            type_=args.type_,
            body=args.text,
            branch=args.branch,
            ticket=args.ticket,
            supersedes=args.supersedes,
            labels=labels or None,
        )
    except _InvalidType as exc:
        sys.stderr.write(f"memory-append: {exc}\n")
        return 3
    except _DuplicateId as exc:
        sys.stderr.write(f"memory-append: duplicate id {exc}; no-op\n")
        return 1
    except _UnknownSupersedeTarget as exc:
        sys.stderr.write(f"memory-append: unknown supersedes target id {exc}\n")
        return 5
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
