"""Post-hoc stage hung detection (read-only inspection library).

Library + thin CLI. Stdlib-only.

There is no live poller and no producer. If a `<ticket_dir>/<stage>.progress`
JSON file exists, this module reads it and classifies a stalled stage as hung,
wedged, or making no progress, AFTER the fact. It never writes a progress file
and never watches a running process; the do-loop is stage-granular and emits no
per-op heartbeats, so detection only fires when some external writer drops a
file (none does today).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from _timeutil import parse_iso

DEFAULT_HEARTBEAT_INTERVAL_S = 60
DEFAULT_MAX_NO_PROGRESS_MIN = 10

# detection verdicts.
OK = "ok"
HUNG = "hung"
WEDGED = "wedged"
NO_PROGRESS = "no_progress"


# ─── Types ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Progress:
    run_id: str
    stage: str
    ticket: str
    seq: int
    current_op: str
    last_artifact: dict[str, Any] | None
    wrote_at: str


# ─── Paths ───────────────────────────────────────────────────────────────────


def progress_path(ticket_dir: Path, stage: str) -> Path:
    return ticket_dir / f"{stage}.progress"


# ─── Serialization ───────────────────────────────────────────────────────────


def _serialize(progress: Progress) -> str:
    return json.dumps(asdict(progress), indent=2, sort_keys=True) + "\n"


def _deserialize(raw: str) -> Progress:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("progress root is not an object")
    artifact = data.get("last_artifact")
    if artifact is not None and not isinstance(artifact, dict):
        raise ValueError("last_artifact is not an object or null")
    return Progress(
        run_id=str(data["run_id"]),
        stage=str(data["stage"]),
        ticket=str(data["ticket"]),
        seq=int(data["seq"]),
        current_op=str(data["current_op"]),
        last_artifact=artifact,
        wrote_at=str(data["wrote_at"]),
    )


# ─── Public API ──────────────────────────────────────────────────────────────


def read_progress(ticket_dir: Path, stage: str) -> Progress | None:
    """Read the progress file. None if absent or malformed; never raises on content.

    Malformed JSON or a structurally wrong record returns None so a corrupt
    heartbeat degrades detection to "no data" rather than crashing recovery.
    """
    path = progress_path(ticket_dir, stage)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        return _deserialize(raw)
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None


def detect_hung(
    progress: Progress,
    now_iso: str,
    *,
    heartbeat_interval_s: int = DEFAULT_HEARTBEAT_INTERVAL_S,
    max_no_progress_min: int = DEFAULT_MAX_NO_PROGRESS_MIN,
    prev: Progress | None = None,
) -> str:
    """Classify a (possibly stalled) stage from its progress file.

    Returns one of: ok | hung | wedged | no_progress.

    Precedence (checked in this order):
      1. hung      — wrote_at older than 3 * heartbeat_interval_s before now.
      2. wedged    — prev given and prev.seq == progress.seq (seq did not advance).
      3. no_progress — prev given, artifact and current_op unchanged, and the
                       wrote_at-to-wrote_at gap exceeds max_no_progress_min.
      4. ok.

    The no_progress gap is measured between the two heartbeats
    (progress.wrote_at - prev.wrote_at), not against now: it asks whether real
    time passed while the work stayed frozen, which is why prev is required.
    """
    now = parse_iso(now_iso)
    wrote_at = parse_iso(progress.wrote_at)
    if (
        now is not None
        and wrote_at is not None
        and (now - wrote_at).total_seconds() > 3 * heartbeat_interval_s
    ):
        return HUNG

    if prev is not None:
        if prev.seq == progress.seq:
            return WEDGED
        prev_wrote = parse_iso(prev.wrote_at)
        if (
            prev.last_artifact == progress.last_artifact
            and prev.current_op == progress.current_op
            and prev_wrote is not None
            and wrote_at is not None
            and (wrote_at - prev_wrote).total_seconds() > max_no_progress_min * 60
        ):
            return NO_PROGRESS

    return OK


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ticket-dir", required=True)
    common.add_argument("--stage", required=True)

    parser = argparse.ArgumentParser(description="Post-hoc stage hung detection.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("read", parents=[common])

    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    ticket_dir = Path(args.ticket_dir).resolve()

    if args.command == "read":
        progress = read_progress(ticket_dir, args.stage)
        if progress is None:
            sys.stdout.write("{}\n")
            return 0
        sys.stdout.write(_serialize(progress))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_S",
    "DEFAULT_MAX_NO_PROGRESS_MIN",
    "HUNG",
    "NO_PROGRESS",
    "OK",
    "WEDGED",
    "Progress",
    "cli_main",
    "detect_hung",
    "progress_path",
    "read_progress",
]
