"""Stage progress heartbeat + post-hoc hung detection.

Library + thin CLI. Stdlib-only.

There is no live poller. A subagent/skill MAY write a progress file while a stage
runs; /flow recover (and the post-stage path) reads it AFTER the fact to decide
whether a stalled stage is hung, wedged, or making no progress. This module is
the writer, the reader, the identity check, and the pure detection logic — none
of it watches a running process.

Progress file: `<ticket_dir>/<stage>.progress` (JSON). Written atomically via
_atomicio.atomic_write_text (temp + fsync + os.replace), so a reader sees
old-or-new, never a torn file.

Identity matters because a fresh run may inherit a ticket dir that still holds a
prior run's progress file. identity_ok gates on run_id/stage/ticket all matching
AND the progress being no older than the stage start; quarantine_stale moves a
mismatched file aside so detection never reads a foreign heartbeat.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from _atomicio import atomic_write_text

EXIT_INVALID_ARGS = 3

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


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


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


def write_progress(
    ticket_dir: Path,
    *,
    run_id: str,
    stage: str,
    ticket: str,
    seq: int,
    current_op: str,
    last_artifact: dict[str, Any] | None = None,
    now_iso: str,
) -> Progress:
    """Atomically write the progress file and return the record written."""
    progress = Progress(
        run_id=run_id,
        stage=stage,
        ticket=ticket,
        seq=seq,
        current_op=current_op,
        last_artifact=last_artifact,
        wrote_at=now_iso,
    )
    atomic_write_text(progress_path(ticket_dir, stage), _serialize(progress))
    return progress


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


def identity_ok(
    progress: Progress,
    *,
    run_id: str,
    stage: str,
    ticket: str,
    stage_started_at_iso: str,
) -> bool:
    """True iff run_id/stage/ticket all match AND wrote_at >= stage start.

    The wrote_at floor rejects a stale file from a prior run that happens to
    share the same identity triple. Timestamps are compared as parsed datetimes,
    not strings, so format quirks do not silently flip the result.
    """
    if progress.run_id != run_id or progress.stage != stage or progress.ticket != ticket:
        return False
    wrote_at = _parse_iso(progress.wrote_at)
    started = _parse_iso(stage_started_at_iso)
    if wrote_at is None or started is None:
        return False
    return wrote_at >= started


def quarantine_stale(
    ticket_dir: Path,
    stage: str,
    *,
    run_id: str,
    ticket: str,
    stage_started_at_iso: str,
) -> bool:
    """Move a mismatched progress file aside. True if a file was quarantined.

    Returns False when no progress file exists, when it is unreadable, or when it
    passes identity_ok. The quarantined name is `<stage>.progress.stale.<n>` with
    the next free index so an earlier quarantine is never clobbered.
    """
    progress = read_progress(ticket_dir, stage)
    if progress is None:
        return False
    if identity_ok(
        progress,
        run_id=run_id,
        stage=stage,
        ticket=ticket,
        stage_started_at_iso=stage_started_at_iso,
    ):
        return False
    src = progress_path(ticket_dir, stage)
    dst = _next_stale_path(ticket_dir, stage)
    src.replace(dst)
    return True


def _next_stale_path(ticket_dir: Path, stage: str) -> Path:
    base = progress_path(ticket_dir, stage)
    n = 0
    while True:
        candidate = base.with_name(f"{base.name}.stale.{n}")
        if not candidate.exists():
            return candidate
        n += 1


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
    now = _parse_iso(now_iso)
    wrote_at = _parse_iso(progress.wrote_at)
    if (
        now is not None
        and wrote_at is not None
        and (now - wrote_at).total_seconds() > 3 * heartbeat_interval_s
    ):
        return HUNG

    if prev is not None:
        if prev.seq == progress.seq:
            return WEDGED
        prev_wrote = _parse_iso(prev.wrote_at)
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

    parser = argparse.ArgumentParser(description="Stage progress heartbeat.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_write = sub.add_parser("write", parents=[common])
    p_write.add_argument("--run-id", required=True)
    p_write.add_argument("--ticket", required=True)
    p_write.add_argument("--seq", type=int, required=True)
    p_write.add_argument("--current-op", required=True)
    p_write.add_argument("--last-artifact", default=None, help="JSON object or null")
    p_write.add_argument("--now", default=None)

    sub.add_parser("read", parents=[common])

    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    ticket_dir = Path(args.ticket_dir).resolve()

    if args.command == "write":
        last_artifact: dict[str, Any] | None = None
        if args.last_artifact is not None:
            try:
                parsed = json.loads(args.last_artifact)
            except json.JSONDecodeError as exc:
                sys.stderr.write(f"heartbeat write: --last-artifact not JSON: {exc}\n")
                return EXIT_INVALID_ARGS
            if parsed is not None and not isinstance(parsed, dict):
                sys.stderr.write("heartbeat write: --last-artifact must be a JSON object or null\n")
                return EXIT_INVALID_ARGS
            last_artifact = parsed
        now_iso = args.now or _utcnow_iso()
        progress = write_progress(
            ticket_dir,
            run_id=args.run_id,
            stage=args.stage,
            ticket=args.ticket,
            seq=args.seq,
            current_op=args.current_op,
            last_artifact=last_artifact,
            now_iso=now_iso,
        )
        sys.stdout.write(_serialize(progress))
        return 0

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
    "EXIT_INVALID_ARGS",
    "HUNG",
    "NO_PROGRESS",
    "OK",
    "WEDGED",
    "Progress",
    "cli_main",
    "detect_hung",
    "identity_ok",
    "progress_path",
    "quarantine_stale",
    "read_progress",
    "write_progress",
]
