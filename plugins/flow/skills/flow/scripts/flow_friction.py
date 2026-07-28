"""Append-only in-flight friction log: `.flow/<namespace>/friction.jsonl`.

The do-verb loop appends one entry whenever the driver hits a snag the run
worked around (a retry, a missing tool, config drift, a lost lease, a planned-file
reconcile, a failed stage). The reflect stage synthesizes these into the
machinery-lens findings (`MACHINERY:` knowledge entries) instead of reconstructing
friction postmortem from state.json, which is lossy.

Friction is operational telemetry, not recall knowledge: it lives in a SEPARATE
file from knowledge.jsonl, is high-cardinality and time-ordered, and is never
deduplicated (each entry is a distinct event, keyed by a uuid4).

Each entry carries a self-read `plugin_version` stamp (the flow plugin version at
append time; '' if the version can't be read).

The CLI also echoes the live knowledge entries that describe the same snag
(`recall.similar_entries`), because the driver that logs friction is about to
brief the next stage agent and the corpus often already holds the answer. The
echo is best-effort: it runs after the durable write, and no failure of it can
change the exit code, the record, or the first stdout line. The library
`append()` stays a pure writer, so `recover.py`'s in-process call is unaffected.

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
import uuid
from pathlib import Path
from typing import Any

import _memory_paths
import recall
from _locking import LockContention, flock_retry
from _timeutil import utcnow_iso_ms
from _workspace import plugin_version

VALID_TYPES: tuple[str, ...] = (
    "BLOCKER",
    "RETRY",
    "MISSING_TOOL",
    "DRIFT",
    "LEASE_LOSS",
    "RECONCILE",
    "STAGE_FAILED",
    "STATE_ROLLBACK",
)

VALID_SEVERITIES: tuple[str, ...] = ("major", "minor")

# Floor for the related-knowledge echo, calibrated by replaying real friction appends against the
# real corpus. A friction body is short and operational and a knowledge entry is long and analytic,
# so their cosines top out near 0.92 rather than the 0.93+ two entries reach, which makes
# `recall_usage.MISS_SIMILARITY` (0.90) the wrong bar here: it would admit 1 of 8 known true
# positives.
RELATED_SIMILARITY = 0.82
RELATED_TOP_N = 3
# Corpus bodies have a median of 728 characters and a 90th percentile of 1242, but the largest is
# 52732. This cap is inert for 99.2% of live entries and bounds what an unasked echo can push into
# the reader's context.
RELATED_BODY_CHARS = 2000


class _InvalidType(Exception):
    """Type not in VALID_TYPES, or severity not in VALID_SEVERITIES."""


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
        "ts": utcnow_iso_ms(),
        "run_id": run_id,
        "ticket": ticket,
        "stage": stage,
        "type": type_,
        "severity": severity,
        "body": body,
        "plugin_version": plugin_version(),
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


def _related_query(entry: dict[str, Any]) -> str:
    detail = entry.get("detail")
    body = str(entry.get("body") or "")
    return f"{body}\n{detail}" if detail else body


def _render_related(hits: list[dict[str, Any]]) -> str:
    lines = [
        f"related knowledge ({len(hits)}): the corpus already describes this snag. "
        "Read it before improvising a workaround, and carry what you use into the "
        "next stage prompt."
    ]
    for hit in hits:
        lines.append(f"[{hit.get('score')}] {hit.get('id')} {hit.get('ticket')} {hit.get('ts')}")
        body = str(hit.get("body") or "")
        if len(body) > RELATED_BODY_CHARS:
            # `memory search` has no id lookup, and its `--ticket` is a ranking boost that still
            # needs a query of its own, so the ticket key IS the query here and the id printed above
            # picks the entry out of a result that also carries its neighbours.
            body = (
                f"{body[:RELATED_BODY_CHARS]}\n"
                f"[truncated at {RELATED_BODY_CHARS} chars; full text: "
                f'FLOW memory search "{hit.get("ticket")}"]'
            )
        lines.append(body)
    return "\n".join(lines) + "\n"


def _emit_related(workspace_root: Path, entry: dict[str, Any]) -> None:
    hits = recall.similar_entries(
        workspace_root,
        _related_query(entry),
        top_n=RELATED_TOP_N,
        threshold=RELATED_SIMILARITY,
        exclude_ticket=str(entry.get("ticket") or "") or None,
    )
    if hits:
        sys.stdout.write(_render_related(hits))


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
    # The driver reads both streams merged (`... 2>&1 | cat`), and a piped stdout is block-buffered
    # where stderr is not, so an unflushed record lands AFTER the near-miss diagnostic the echo
    # writes to stderr.
    sys.stdout.flush()
    try:
        _emit_related(workspace_root, entry)
    except Exception as exc:  # the record is already durable; recall never fails an append
        sys.stderr.write(f"flow-friction: related-recall skipped: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "RELATED_BODY_CHARS",
    "RELATED_SIMILARITY",
    "RELATED_TOP_N",
    "VALID_SEVERITIES",
    "VALID_TYPES",
    "append",
    "cli_main",
]
