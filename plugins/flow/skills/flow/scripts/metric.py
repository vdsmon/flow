"""Tickets-per-week calculator behind the 14-day checkpoint.

Library + thin CLI. Stdlib-only.

Counts ship events whose frozen `shipped_at` falls in a half-open UTC window
`[since, until)`, and attributes each shipped ticket as either shipped through a
flow run (`shipped_via_flow`) or observed by the backend without flow
attribution (`shipped_backend_not_attributed`).

Ship events are one-JSON-object-per-file under `.flow/<namespace>/ship-events/`,
written by observe_ship_event.py. Each primary file is `<ticket>.json`; dupes,
corruptions, and intent logs use suffixed names and are skipped here. Files that
fail to parse or lack `shipped_at` are quarantined-skip (logged to a sidecar,
never counted) so a single bad file cannot abort the metric.

Attribution joins each ship event to its per-ticket state.json at
`.flow/runs/<ticket>/state.json`. A ticket is `shipped_via_flow` iff that state
exists, its `ticket` matches, its `run_id` matches the ship event's observing
run id (`observed_by_run_id`), and its `reflect` stage status is `completed`.

Window defaults: until = now; since = 14 days before now, floored to 00:00 UTC.

Checkpoint mode aggregates compute() across every checkpoint-manifest
participant whose `checkpoint_mode` matches `--mode`. Effective-interval
accounting for a participant that changed mode mid-window is deferred; this phase
includes a participant iff its mode matches and it was initialized at or before
`until`.

Exit codes:
  0 = ok
  1 = bad args (namespace required when not --checkpoint, bad date, bad mode)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import _memory_paths
from _jsonl import append_quarantine

ATTR_VIA_FLOW = "shipped_via_flow"
ATTR_NOT_ATTRIBUTED = "shipped_backend_not_attributed"

WINDOW_DAYS = 14

# ship-event file suffixes the sole writer (observe_ship_event.py) uses for
# non-primary records. A primary is `<ticket>.json`; these never count.
_SKIP_INFIXES: tuple[str, ...] = (".dupe.", ".corrupt.", ".quarantine-intent.")


# ─── Time helpers ────────────────────────────────────────────────────────────


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime | None:
    """Parse a UTC ISO8601 timestamp into a tz-aware datetime, or None on failure."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def default_window(now_iso: str) -> tuple[str, str]:
    """Return (since_iso, until_iso) defaults: until=now, since=14d-ago at 00:00 UTC."""
    now = _parse_iso(now_iso)
    if now is None:
        raise ValueError(f"now is not a UTC ISO8601 timestamp: {now_iso!r}")
    since_day = (now - timedelta(days=WINDOW_DAYS)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return _to_iso(since_day), _to_iso(now)


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── Ship-event loading ──────────────────────────────────────────────────────


def _is_primary_ship_event(path: Path) -> bool:
    """A primary ship event is `<ticket>.json` with no dupe/corrupt/intent infix."""
    if path.suffix != ".json":
        return False
    name = path.name
    return not any(infix in name for infix in _SKIP_INFIXES)


def load_ship_events(workspace_root: Path, namespace: str) -> list[dict[str, Any]]:
    """Read every primary `ship-events/<ticket>.json` as one JSON object each.

    Skips dupe/corrupt/intent-log files by name. A file that fails to parse, is
    not a JSON object, or lacks `shipped_at` is appended to a quarantine sidecar
    and skipped. Returns the parsed event dicts (order: sorted by filename).
    """
    ship_dir = _memory_paths.ship_events_dir(workspace_root, namespace)
    if not ship_dir.is_dir():
        return []
    quarantine = ship_dir.parent / "ship-events.quarantine"
    events: list[dict[str, Any]] = []
    for path in sorted(ship_dir.glob("*.json")):
        if not _is_primary_ship_event(path):
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            append_quarantine(quarantine, str(path), f"read: {exc}")
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            append_quarantine(quarantine, str(path), f"json: {exc}")
            continue
        if not isinstance(event, dict):
            append_quarantine(quarantine, str(path), "not an object")
            continue
        if not isinstance(event.get("shipped_at"), str):
            append_quarantine(quarantine, str(path), "missing shipped_at")
            continue
        events.append(event)
    return events


# ─── Attribution ─────────────────────────────────────────────────────────────


def _state_path(workspace_root: Path, ticket: str) -> Path:
    return workspace_root / ".flow" / "runs" / ticket / "state.json"


def classify_attribution(workspace_root: Path, ship_event: dict[str, Any]) -> str:
    """Attribute one ship event to flow or backend.

    Returns ATTR_VIA_FLOW iff `.flow/runs/<ticket>/state.json` exists AND its
    `ticket` matches the ship event's ticket AND its `run_id` matches the ship
    event's observing-run-id (`observed_by_run_id`) AND its `reflect` stage
    status is `completed`. Otherwise ATTR_NOT_ATTRIBUTED.

    A malformed or unreadable state.json yields ATTR_NOT_ATTRIBUTED (the metric
    never counts a ticket as flow-attributed without a clean join).
    """
    ticket = ship_event.get("ticket")
    if not isinstance(ticket, str) or not ticket:
        return ATTR_NOT_ATTRIBUTED
    state_path = _state_path(workspace_root, ticket)
    if not state_path.is_file():
        return ATTR_NOT_ATTRIBUTED
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ATTR_NOT_ATTRIBUTED
    if not isinstance(state, dict):
        return ATTR_NOT_ATTRIBUTED
    if state.get("ticket") != ticket:
        return ATTR_NOT_ATTRIBUTED
    if state.get("run_id") != ship_event.get("observed_by_run_id"):
        return ATTR_NOT_ATTRIBUTED
    stages = state.get("stages")
    if not isinstance(stages, dict):
        return ATTR_NOT_ATTRIBUTED
    reflect = stages.get("reflect")
    if not isinstance(reflect, dict) or reflect.get("status") != "completed":
        return ATTR_NOT_ATTRIBUTED
    return ATTR_VIA_FLOW


# ─── Compute ─────────────────────────────────────────────────────────────────


def compute(
    workspace_root: Path,
    namespace: str,
    *,
    since_iso: str,
    until_iso: str,
    now_iso: str,
) -> dict[str, Any]:
    """Compute tickets-per-week stats over the half-open window [since, until).

    A ship event counts iff its `shipped_at` parses and is in [since, until).
    Each counted event is attributed via classify_attribution. `now_iso` is
    accepted for symmetry with the CLI default-window derivation; the window here
    is taken from the explicit since/until.
    """
    since = _parse_iso(since_iso)
    until = _parse_iso(until_iso)
    if since is None:
        raise ValueError(f"since is not a UTC ISO8601 timestamp: {since_iso!r}")
    if until is None:
        raise ValueError(f"until is not a UTC ISO8601 timestamp: {until_iso!r}")

    shipped = 0
    via_flow = 0
    not_attributed = 0
    tickets: list[dict[str, Any]] = []

    for event in load_ship_events(workspace_root, namespace):
        shipped_at = _parse_iso(str(event.get("shipped_at")))
        if shipped_at is None or not (since <= shipped_at < until):
            continue
        attribution = classify_attribution(workspace_root, event)
        shipped += 1
        if attribution == ATTR_VIA_FLOW:
            via_flow += 1
        else:
            not_attributed += 1
        tickets.append(
            {
                "ticket": event.get("ticket"),
                "shipped_at": event.get("shipped_at"),
                "attribution": attribution,
            }
        )

    tickets.sort(key=lambda t: (str(t["shipped_at"]), str(t["ticket"])))
    return {
        "since": since_iso,
        "until": until_iso,
        "shipped": shipped,
        ATTR_VIA_FLOW: via_flow,
        ATTR_NOT_ATTRIBUTED: not_attributed,
        "tickets": tickets,
    }


# ─── Checkpoint manifest aggregation ─────────────────────────────────────────


def _default_checkpoint_manifest_path() -> Path:
    return Path.home() / ".config" / "flow" / "checkpoint-manifest.jsonl"


def _read_manifest(path: Path) -> list[dict[str, Any]]:
    """Read manifest entries (one JSON object per line); skip blank/malformed."""
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _participant_initialized_at(entry: dict[str, Any]) -> str | None:
    # init.py writes `ts`; the spec's checkpoint field is `initialized_at`. Read
    # the spec field first, fall back to the on-disk `ts`.
    for key in ("initialized_at", "ts"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _participant_workspace_root(entry: dict[str, Any]) -> str | None:
    for key in ("workspace_path", "workspace_root"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def compute_checkpoint(
    mode: str,
    *,
    until_iso: str,
    since_iso: str,
    now_iso: str,
    manifest_path: Path,
) -> dict[str, Any]:
    """Aggregate compute() across manifest participants whose mode matches.

    Effective-interval accounting for a participant that changed mode mid-window
    is deferred. This phase includes a participant iff its `checkpoint_mode`
    equals `mode` and its initialized_at parses and is <= until. The per-mode
    `shipped_via_flow` is summed across the included participants.
    """
    until = _parse_iso(until_iso)
    if until is None:
        raise ValueError(f"until is not a UTC ISO8601 timestamp: {until_iso!r}")

    participants: list[dict[str, Any]] = []
    total_shipped = 0
    total_via_flow = 0
    total_not_attributed = 0

    for entry in _read_manifest(manifest_path):
        if entry.get("checkpoint_mode") != mode:
            continue
        initialized_at = _participant_initialized_at(entry)
        init_dt = _parse_iso(initialized_at) if initialized_at else None
        if init_dt is None or init_dt > until:
            continue
        ws_root = _participant_workspace_root(entry)
        namespace = entry.get("namespace")
        if not ws_root or not isinstance(namespace, str) or not namespace:
            continue
        result = compute(
            Path(ws_root),
            namespace,
            since_iso=since_iso,
            until_iso=until_iso,
            now_iso=now_iso,
        )
        total_shipped += result["shipped"]
        total_via_flow += result[ATTR_VIA_FLOW]
        total_not_attributed += result[ATTR_NOT_ATTRIBUTED]
        participants.append(
            {
                "workspace_root": ws_root,
                "namespace": namespace,
                "initialized_at": initialized_at,
                "shipped": result["shipped"],
                ATTR_VIA_FLOW: result[ATTR_VIA_FLOW],
                ATTR_NOT_ATTRIBUTED: result[ATTR_NOT_ATTRIBUTED],
            }
        )

    return {
        "mode": mode,
        "since": since_iso,
        "until": until_iso,
        "participant_count": len(participants),
        "shipped": total_shipped,
        ATTR_VIA_FLOW: total_via_flow,
        ATTR_NOT_ATTRIBUTED: total_not_attributed,
        "participants": participants,
    }


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _resolve_window(args: argparse.Namespace, now_iso: str) -> tuple[str, str]:
    """Resolve (since, until) from --since/--until day flags, defaulting per now."""
    default_since, default_until = default_window(now_iso)
    until_iso = f"{args.until}T00:00:00Z" if args.until else default_until
    since_iso = f"{args.since}T00:00:00Z" if args.since else default_since
    if _parse_iso(until_iso) is None:
        raise ValueError(f"--until is not YYYY-MM-DD: {args.until!r}")
    if _parse_iso(since_iso) is None:
        raise ValueError(f"--since is not YYYY-MM-DD: {args.since!r}")
    return since_iso, until_iso


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tickets-per-week metric.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_tpw = sub.add_parser("tickets-per-week", help="Compute shipped tickets in a window.")
    p_tpw.add_argument("--namespace", default=None)
    p_tpw.add_argument("--workspace-root", default=".")
    p_tpw.add_argument("--since", default=None, help="YYYY-MM-DD (inclusive day start, UTC)")
    p_tpw.add_argument("--until", default=None, help="YYYY-MM-DD (exclusive day start, UTC)")
    p_tpw.add_argument("--checkpoint", action="store_true")
    p_tpw.add_argument("--mode", choices=("personal", "work"), default=None)
    p_tpw.add_argument("--manifest-path", default=None)
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    now_iso = _utcnow_iso()
    try:
        since_iso, until_iso = _resolve_window(args, now_iso)
    except ValueError as exc:
        sys.stderr.write(f"metric: {exc}\n")
        return 1

    if args.checkpoint:
        if args.mode is None:
            sys.stderr.write("metric: --checkpoint requires --mode personal|work\n")
            return 1
        manifest_path = (
            Path(args.manifest_path).expanduser()
            if args.manifest_path
            else _default_checkpoint_manifest_path()
        )
        try:
            result = compute_checkpoint(
                args.mode,
                since_iso=since_iso,
                until_iso=until_iso,
                now_iso=now_iso,
                manifest_path=manifest_path,
            )
        except ValueError as exc:
            sys.stderr.write(f"metric: {exc}\n")
            return 1
        sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
        return 0

    if not args.namespace:
        sys.stderr.write("metric: --namespace is required when not --checkpoint\n")
        return 1

    workspace_root = Path(args.workspace_root).resolve()
    try:
        result = compute(
            workspace_root,
            args.namespace,
            since_iso=since_iso,
            until_iso=until_iso,
            now_iso=now_iso,
        )
    except ValueError as exc:
        sys.stderr.write(f"metric: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "ATTR_NOT_ATTRIBUTED",
    "ATTR_VIA_FLOW",
    "classify_attribution",
    "cli_main",
    "compute",
    "compute_checkpoint",
    "default_window",
    "load_ship_events",
]
