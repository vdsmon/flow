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

Attribution is stamp-first. observe_ship_event.py stamps an owned
`flow_attribution` block (plan_started + create_pr_finished iso timestamps) onto
the durable ship event at observe time, while the run's state.json is still
alive. A ship event carrying a well-formed stamp is `shipped_via_flow` directly;
the run's worktree (and its state.json) is reaped after merge, so the legacy
join below cannot resolve a recently-shipped ticket. Forward-only: tickets
shipped before stamping landed have no stamp and a reaped state, so they stay
`shipped_backend_not_attributed`.

Legacy fallback (no stamp): joins each ship event to its per-ticket state.json at
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
import contextlib
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import _memory_paths
import _workspace
import friction_recurrence
import observe_ship_event
import recall
import recall_usage
from _jsonl import append_quarantine, iter_jsonl
from _timeutil import iso_z, parse_iso, utcnow_iso
from baseline_collect import percentile

ATTR_VIA_FLOW = "shipped_via_flow"
ATTR_NOT_ATTRIBUTED = "shipped_backend_not_attributed"

WINDOW_DAYS = 14

# ship-event file suffixes the sole writer (observe_ship_event.py) uses for
# non-primary records. A primary is `<ticket>.json`; these never count.
_SKIP_INFIXES: tuple[str, ...] = (".dupe.", ".corrupt.", ".quarantine-intent.")


# ─── Time helpers ────────────────────────────────────────────────────────────


def default_window(now_iso: str) -> tuple[str, str]:
    """Return (since_iso, until_iso) defaults: until=now, since=14d-ago at 00:00 UTC."""
    now = parse_iso(now_iso)
    if now is None:
        raise ValueError(f"now is not a UTC ISO8601 timestamp: {now_iso!r}")
    since_day = (now - timedelta(days=WINDOW_DAYS)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return iso_z(since_day), iso_z(now)


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


def _read_stamp(ship_event: dict[str, Any]) -> tuple[datetime, datetime] | None:
    """Parse a ship event's `flow_attribution` stamp into (plan_started, create_pr_finished).

    Returns the two parsed datetimes iff `flow_attribution` is a dict and BOTH
    iso fields parse. Otherwise None. The single predicate both classify and
    time-to-pr share, so a stamped event is never routed to the reaped-state read.
    """
    stamp = ship_event.get("flow_attribution")
    if not isinstance(stamp, dict):
        return None
    plan_started = parse_iso(stamp.get("plan_started_at_iso"))
    create_pr_finished = parse_iso(stamp.get("create_pr_finished_at_iso"))
    if plan_started is None or create_pr_finished is None:
        return None
    return plan_started, create_pr_finished


def classify_attribution(workspace_root: Path, ship_event: dict[str, Any]) -> str:
    """Attribute one ship event to flow or backend.

    Stamp-first: a WELL-FORMED `flow_attribution` block (a dict whose two iso
    fields both parse) is direct evidence a flow run observed this ship at reflect
    time and returns ATTR_VIA_FLOW. The stamp supersedes the reaped-state.json
    proxy; the run's worktree is torn down after merge, so the legacy join below
    cannot resolve a recently-shipped ticket.

    Legacy fallback (no stamp / malformed stamp): ATTR_VIA_FLOW iff
    `.flow/runs/<ticket>/state.json` exists AND its `ticket` matches the ship
    event's ticket AND its `run_id` matches the ship event's observing-run-id
    (`observed_by_run_id`) AND its `reflect` stage status is `completed`.
    Otherwise ATTR_NOT_ATTRIBUTED. A malformed or unreadable state.json yields
    ATTR_NOT_ATTRIBUTED (never count flow-attribution without a clean join).

    Forward-only: tickets shipped before stamping landed have no stamp and a
    reaped state, so they stay ATTR_NOT_ATTRIBUTED.
    """
    ticket = ship_event.get("ticket")
    if not isinstance(ticket, str) or not ticket:
        return ATTR_NOT_ATTRIBUTED
    if _read_stamp(ship_event) is not None:
        return ATTR_VIA_FLOW
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
    since = parse_iso(since_iso)
    until = parse_iso(until_iso)
    if since is None:
        raise ValueError(f"since is not a UTC ISO8601 timestamp: {since_iso!r}")
    if until is None:
        raise ValueError(f"until is not a UTC ISO8601 timestamp: {until_iso!r}")

    shipped = 0
    via_flow = 0
    not_attributed = 0
    tickets: list[dict[str, Any]] = []

    for event in load_ship_events(workspace_root, namespace):
        shipped_at = parse_iso(str(event.get("shipped_at")))
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


# ─── Time-to-PR ──────────────────────────────────────────────────────────────


def compute_time_to_pr(
    workspace_root: Path,
    namespace: str,
    *,
    since_iso: str,
    until_iso: str,
    now_iso: str,
) -> dict[str, Any]:
    """Compute observed time-to-PR over the half-open window [since, until).

    Enumerates flow-attributed ship events whose `shipped_at` is in window. A
    STAMPED event reads `plan_started`/`create_pr_finished` from its
    `flow_attribution` block and never touches state.json (the run's worktree is
    reaped after merge). A LEGACY (no-stamp) event reads those timestamps from
    `.flow/runs/<ticket>/state.json` via a GUARDED read; a missing/unreadable/
    non-dict state is skip-and-recorded, never a crash. Tickets with a
    missing/None/unparseable timestamp or a negative duration are skip-and-
    recorded (counted in `n_skipped`, never fed to the percentiles). `now_iso` is
    accepted for symmetry with compute(); the window is the explicit since/until.
    """
    since = parse_iso(since_iso)
    until = parse_iso(until_iso)
    if since is None:
        raise ValueError(f"since is not a UTC ISO8601 timestamp: {since_iso!r}")
    if until is None:
        raise ValueError(f"until is not a UTC ISO8601 timestamp: {until_iso!r}")

    hours: list[float] = []
    tickets: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for event in load_ship_events(workspace_root, namespace):
        shipped_at = parse_iso(str(event.get("shipped_at")))
        if shipped_at is None or not (since <= shipped_at < until):
            continue
        if classify_attribution(workspace_root, event) != ATTR_VIA_FLOW:
            continue
        ticket = event.get("ticket")
        stamp = _read_stamp(event)
        if stamp is not None:
            plan_started, create_pr_finished = stamp
            plan_started_raw = event["flow_attribution"]["plan_started_at_iso"]
            create_pr_finished_raw = event["flow_attribution"]["create_pr_finished_at_iso"]
        else:
            try:
                state = json.loads(
                    _state_path(workspace_root, str(ticket)).read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                skipped.append({"ticket": ticket, "reason": "unreadable state.json"})
                continue
            if not isinstance(state, dict):
                skipped.append({"ticket": ticket, "reason": "unreadable state.json"})
                continue
            stages = state.get("stages", {})
            plan_stage = stages.get("plan") if isinstance(stages.get("plan"), dict) else {}
            create_pr_stage = (
                stages.get("create_pr") if isinstance(stages.get("create_pr"), dict) else {}
            )
            plan_started_raw = plan_stage.get("started_at_iso")
            create_pr_finished_raw = create_pr_stage.get("finished_at_iso")
            plan_started = (
                parse_iso(plan_started_raw) if isinstance(plan_started_raw, str) else None
            )
            create_pr_finished = (
                parse_iso(create_pr_finished_raw)
                if isinstance(create_pr_finished_raw, str)
                else None
            )
        if plan_started is None:
            skipped.append({"ticket": ticket, "reason": "missing plan.started_at_iso"})
            continue
        if create_pr_finished is None:
            skipped.append({"ticket": ticket, "reason": "missing create_pr.finished_at_iso"})
            continue
        duration = (create_pr_finished - plan_started).total_seconds() / 3600.0
        if duration < 0:
            skipped.append({"ticket": ticket, "reason": "negative duration"})
            continue
        hours.append(duration)
        tickets.append(
            {
                "ticket": ticket,
                "time_to_pr_hours": duration,
                "plan_started_at": plan_started_raw,
                "create_pr_finished_at": create_pr_finished_raw,
            }
        )

    tickets.sort(key=lambda t: (t["time_to_pr_hours"], str(t["ticket"])))
    return {
        "since": since_iso,
        "until": until_iso,
        "n_measured": len(hours),
        "n_skipped": len(skipped),
        "median_hours": percentile(hours, 50.0),
        "p90_hours": percentile(hours, 90.0),
        "tickets": tickets,
        "skipped": skipped,
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
    until = parse_iso(until_iso)
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
        init_dt = parse_iso(initialized_at) if initialized_at else None
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


# ─── Friction per run ────────────────────────────────────────────────────────


def _ts_token() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def compute_friction_per_run(
    workspace_root: Path,
    namespace: str,
    *,
    since_iso: str,
    until_iso: str,
) -> dict[str, Any]:
    """Compute friction-events-per-run over the half-open window [since, until).

    Reads `.flow/<namespace>/friction.jsonl` (one JSON object per line) via the
    quarantine-on-malformed reader. An entry counts iff its `ts` parses and is in
    [since, until); entries with missing/unparseable `ts` are skipped. Among the
    windowed entries, distinct string `run_id` values give the run count; a
    missing or non-string `run_id` is not added to the set. events_per_run is
    total_events / runs, or 0 when runs == 0.
    """
    since = parse_iso(since_iso)
    until = parse_iso(until_iso)
    if since is None:
        raise ValueError(f"since is not a UTC ISO8601 timestamp: {since_iso!r}")
    if until is None:
        raise ValueError(f"until is not a UTC ISO8601 timestamp: {until_iso!r}")

    path = _memory_paths.friction_path(workspace_root, namespace)
    sidecar = path.with_name(f"{path.name}.quarantine.{_ts_token()}")

    total_events = 0
    runs: set[str] = set()
    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}

    for entry in iter_jsonl(path, sidecar):
        ts = parse_iso(entry.get("ts"))
        if ts is None or not (since <= ts < until):
            continue
        total_events += 1
        run_id = entry.get("run_id")
        if isinstance(run_id, str):
            runs.add(run_id)
        entry_type = entry.get("type")
        if isinstance(entry_type, str):
            by_type[entry_type] = by_type.get(entry_type, 0) + 1
        severity = entry.get("severity")
        if isinstance(severity, str):
            by_severity[severity] = by_severity.get(severity, 0) + 1

    run_count = len(runs)
    events_per_run = round(total_events / run_count, 6) if run_count > 0 else 0
    return {
        "since": since_iso,
        "until": until_iso,
        "total_events": total_events,
        "runs": run_count,
        "events_per_run": events_per_run,
        "by_type": by_type,
        "by_severity": by_severity,
    }


def compute_corpus_health(
    workspace_root: Path,
    namespace: str,
    *,
    since_iso: str,
    until_iso: str,
    now_iso: str,
) -> dict[str, Any]:
    """Report knowledge.jsonl corpus health: live-vs-superseded counts + DECISION age.

    Reads `.flow/<namespace>/knowledge.jsonl` via the quarantine-on-malformed
    reader. An entry is superseded iff its `id` is named by some other entry's
    `supersedes` (the tombstone). supersession_rate is superseded/total.
    supersedes_in_window counts tombstones (entries with a non-empty `supersedes`)
    whose `ts` parses and falls in the half-open window [since, until), the
    over-time axis. The DECISION breakdown counts `type == "DECISION"` entries and,
    among the live (non-superseded) ones with a parseable `ts`, surfaces the oldest
    as {id, ts, age_days} measured against now_iso (None when there are none).
    """
    since = parse_iso(since_iso)
    until = parse_iso(until_iso)
    if since is None:
        raise ValueError(f"since is not a UTC ISO8601 timestamp: {since_iso!r}")
    if until is None:
        raise ValueError(f"until is not a UTC ISO8601 timestamp: {until_iso!r}")
    now = parse_iso(now_iso)
    if now is None:
        raise ValueError(f"now is not a UTC ISO8601 timestamp: {now_iso!r}")

    kpath = _memory_paths.knowledge_path(workspace_root, namespace)
    if not kpath.exists():
        entries: list[Any] = []
    else:
        sidecar = kpath.with_name(f"{kpath.name}.quarantine.{_ts_token()}")
        entries = list(iter_jsonl(kpath, sidecar))

    dead = recall.superseded_ids([e for e in entries if isinstance(e, dict)])

    total = len(entries)
    superseded = sum(1 for e in entries if isinstance(e, dict) and e.get("id") in dead)
    live = total - superseded
    supersession_rate = round(superseded / total, 4) if total else 0.0

    supersedes_in_window = 0
    for e in entries:
        if not isinstance(e, dict) or not e.get("supersedes"):
            continue
        ts = parse_iso(e.get("ts"))
        if ts is not None and since <= ts < until:
            supersedes_in_window += 1

    decisions_total = 0
    live_decisions: list[tuple[datetime, dict[str, Any]]] = []
    for e in entries:
        if not isinstance(e, dict) or e.get("type") != "DECISION":
            continue
        decisions_total += 1
        if e.get("id") in dead:
            continue
        ts = parse_iso(e.get("ts"))
        if ts is not None:
            live_decisions.append((ts, e))

    decisions_live = sum(
        1
        for e in entries
        if isinstance(e, dict) and e.get("type") == "DECISION" and e.get("id") not in dead
    )

    oldest_live_decision: dict[str, Any] | None = None
    if live_decisions:
        ts, entry = min(live_decisions, key=lambda pair: pair[0])
        oldest_live_decision = {
            "id": entry.get("id"),
            "ts": entry.get("ts"),
            "age_days": round((now - ts).total_seconds() / 86400, 2),
        }

    return {
        "total_entries": total,
        "live_entries": live,
        "superseded_entries": superseded,
        "supersession_rate": supersession_rate,
        "supersedes_in_window": supersedes_in_window,
        "decisions_total": decisions_total,
        "decisions_live": decisions_live,
        "oldest_live_decision": oldest_live_decision,
        "since": since_iso,
        "until": until_iso,
    }


def compute_recall_hit_rate(
    workspace_root: Path,
    namespace: str,
    *,
    since_iso: str,
    until_iso: str,
) -> dict[str, Any]:
    """Recall precision + miss count over the half-open window [since, until).

    Reads `.flow/<namespace>/recall-usage.jsonl` (usage + miss records, written by
    `recall_usage.py`) via the quarantine-on-malformed reader. A record counts iff
    its `ts` parses and is in [since, until). Among `kind=="usage"` records,
    hit_rate = used / surfaced (0.0 when surfaced == 0), the precision of what
    recall put in front of the run. `kind=="miss"` records (a known fact re-learned
    without being recalled) are counted separately as the false-negative proxy.
    runs is the distinct run_id count across both kinds. Neither half is
    ground-truth recall, but both are valid for RELATIVE config comparison (e.g.
    bge vs potion via `arm-compare`).
    """
    since = parse_iso(since_iso)
    until = parse_iso(until_iso)
    if since is None:
        raise ValueError(f"since is not a UTC ISO8601 timestamp: {since_iso!r}")
    if until is None:
        raise ValueError(f"until is not a UTC ISO8601 timestamp: {until_iso!r}")

    path = recall_usage.recall_usage_path(workspace_root, namespace)
    surfaced = 0
    used = 0
    misses = 0
    runs: set[str] = set()

    if path.exists():
        sidecar = path.with_name(f"{path.name}.quarantine.{_ts_token()}")
        for rec in iter_jsonl(path, sidecar):
            ts = parse_iso(rec.get("ts"))
            if ts is None or not (since <= ts < until):
                continue
            run_id = rec.get("run_id")
            if isinstance(run_id, str):
                runs.add(run_id)
            kind = rec.get("kind")
            if kind == "usage":
                surfaced += 1
                if rec.get("used") is True:
                    used += 1
            elif kind == "miss":
                misses += 1

    hit_rate = round(used / surfaced, 4) if surfaced else 0.0
    return {
        "since": since_iso,
        "until": until_iso,
        "surfaced": surfaced,
        "used": used,
        "hit_rate": hit_rate,
        "misses": misses,
        "runs": len(runs),
    }


# ─── Fix efficacy ────────────────────────────────────────────────────────────


def _entry_anchors(entry: dict[str, Any]) -> set[str]:
    """Replicates friction_recurrence._entry_anchors (private there, needed here)."""
    body = entry.get("body")
    detail = entry.get("detail")
    text = f"{body if isinstance(body, str) else ''} {detail if isinstance(detail, str) else ''}"
    return friction_recurrence.anchors(text)


def _fix_efficacy_recurrence_dicts(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (
            {
                "id": e.get("id", ""),
                "run_id": e.get("run_id", ""),
                "ticket": e.get("ticket", ""),
                "ts": e.get("ts", ""),
                "stage": e.get("stage", ""),
                "type": e.get("type", ""),
            }
            for e in entries
        ),
        key=lambda x: (x["ts"], x["id"]),
    )


def compute_fix_efficacy(workspace_root: Path, namespace: str) -> dict[str, Any]:
    """Per closed MACHINERY-fix bead, did the friction class it claimed to fix recur?

    Mirrors friction_recurrence.analyze()'s read and distinctive-anchor selection,
    then joins per BEAD (`ticket`) instead of per anchor class: analyze()'s
    class-level `earliest_fix_ts` would smear one bead's recurrence across every
    other bead sharing that anchor. Lifetime metric (no time window); "closed" is
    read as "a MACHINERY entry exists for this ticket", not a live tracker check.
    """
    fpath = _memory_paths.friction_path(workspace_root, namespace)
    fsidecar = fpath.with_name(f"{fpath.name}.quarantine.{_ts_token()}")
    friction_entries = [e for e in iter_jsonl(fpath, fsidecar) if isinstance(e, dict)]

    kpath = _memory_paths.knowledge_path(workspace_root, namespace)
    ksidecar = kpath.with_name(f"{kpath.name}.quarantine.{_ts_token()}")
    knowledge_entries = [e for e in iter_jsonl(kpath, ksidecar) if isinstance(e, dict)]

    machinery_entries = [
        e
        for e in knowledge_entries
        if isinstance(e.get("body"), str)
        and e["body"].startswith(friction_recurrence.MACHINERY_PREFIX)
    ]

    friction_anchored = [(f, _entry_anchors(f)) for f in friction_entries]
    machinery_anchored = [(m, _entry_anchors(m)) for m in machinery_entries]

    df = friction_recurrence.document_frequencies(
        [a for _, a in friction_anchored] + [a for _, a in machinery_anchored]
    )
    machinery_tokens: set[str] = set()
    for _, a in machinery_anchored:
        machinery_tokens |= a
    distinct = friction_recurrence.distinctive_anchors(df, machinery_tokens)

    groups: dict[str, list[tuple[dict[str, Any], set[str]]]] = {}
    for m, ma in machinery_anchored:
        ticket = m.get("ticket")
        if isinstance(ticket, str) and ticket:
            groups.setdefault(ticket, []).append((m, ma))

    beads: list[dict[str, Any]] = []
    for ticket, entries in groups.items():
        claimed_anchors: set[str] = set()
        for _, ma in entries:
            claimed_anchors |= ma & distinct

        fix_ts_values = [m["ts"] for m, _ in entries if isinstance(m.get("ts"), str) and m["ts"]]
        fix_ts = min(fix_ts_values) if fix_ts_values else None
        # an empty/missing fix_ts must never forward-join: `ts > None` raises, and
        # `ts > ""` would be true for every friction entry (false positives).
        measurable = bool(claimed_anchors) and fix_ts is not None

        recurring: list[dict[str, Any]] = []
        if measurable:
            for f, fa in friction_anchored:
                f_ts = f.get("ts")
                if isinstance(f_ts, str) and f_ts > fix_ts and (fa & claimed_anchors):
                    recurring.append(f)

        recurrences = _fix_efficacy_recurrence_dicts(recurring)
        fix_shas = sorted(
            {
                sha
                for m, _ in entries
                if (sha := friction_recurrence.fix_sha(m, workspace_root, namespace)) is not None
            }
        )
        beads.append(
            {
                "ticket": ticket,
                "verdict": "recurred" if recurring else "clean",
                "measurable": measurable,
                "fix_ts": fix_ts,
                "claimed_anchors": sorted(claimed_anchors),
                "post_fix_count": len(recurring),
                "recurrence_run_ids": sorted({r["run_id"] for r in recurrences}),
                "stages": sorted({r["stage"] for r in recurrences}),
                "types": sorted({r["type"] for r in recurrences}),
                "recurrences": recurrences,
                "fix_shas": fix_shas,
            }
        )

    beads.sort(
        key=lambda b: (0 if b["verdict"] == "recurred" else 1, -b["post_fix_count"], b["ticket"])
    )

    fix_beads = len(beads)
    recurred = sum(1 for b in beads if b["verdict"] == "recurred")
    clean = fix_beads - recurred
    unmeasurable = sum(1 for b in beads if not b["measurable"])
    recurrence_rate = round(recurred / fix_beads, 6) if fix_beads else 0

    return {
        "beads": beads,
        "totals": {
            "fix_beads": fix_beads,
            "recurred": recurred,
            "clean": clean,
            "unmeasurable": unmeasurable,
            "recurrence_rate": recurrence_rate,
        },
    }


# ─── Revert rate ─────────────────────────────────────────────────────────────

_REOPEN_STATES = frozenset({"open", "in_progress", "blocked"})


def _status_history(
    workspace_root: Path, namespace: str, ticket: str
) -> list[tuple[datetime, str]] | None:
    """Read a bead's status timeline via `bd history <ticket> --json --limit 0`.

    Returns [(commit_date, status)] with unparseable dates dropped, sorted
    ascending. Returns None on ANY failure (non-zero rc, bad JSON, bd absent) so
    the caller skip-and-records rather than crashing. The thin subprocess wrapper
    is also the monkeypatch seam for tests; BeadsAdapter is avoided (no history
    method, and its constructor runs a bd-version preflight that raises).
    `namespace` is unused (the bead key is global) but kept for call-site symmetry
    with the other tracker-coupled seams.
    """
    try:
        proc = subprocess.run(
            ["bd", "history", ticket, "--json", "--limit", "0"],
            cwd=workspace_root,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        rows = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(rows, list):
        return None
    out: list[tuple[datetime, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        issue = row.get("Issue")
        status = issue.get("status") if isinstance(issue, dict) else None
        when = parse_iso(row.get("CommitDate"))
        if when is None or not isinstance(status, str):
            continue
        out.append((when, status))
    out.sort(key=lambda p: p[0])
    return out


def _collapse(timeline: list[tuple[datetime, str]]) -> list[tuple[datetime, str]]:
    """Drop consecutive-equal statuses (bd history is global-commit-granular)."""
    collapsed: list[tuple[datetime, str]] = []
    for when, status in timeline:
        if not collapsed or collapsed[-1][1] != status:
            collapsed.append((when, status))
    return collapsed


def _classify_revert(
    timeline: list[tuple[datetime, str]], shipped_at: datetime
) -> tuple[bool, str | None, str | None, bool]:
    """Decide reverted vs not from a collapsed timeline restricted to date > shipped_at.

    A revert is a reopen (non-closed status) followed by a subsequent re-close.
    Returns (reverted, reopened_at_iso, reclosed_at_iso, reopened_not_yet_reclosed).
    reopened_not_yet_reclosed flags the in-flight case (reopen seen, no re-close).
    """
    post = [(when, status) for when, status in _collapse(timeline) if when > shipped_at]
    reopened_at: datetime | None = None
    for when, status in post:
        if reopened_at is None:
            if status in _REOPEN_STATES:
                reopened_at = when
        elif status == "closed":
            return True, reopened_at.isoformat(), when.isoformat(), False
    if reopened_at is not None:
        return False, None, None, True
    return False, None, None, False


class RevertScanError(Exception):
    """Signals workspace_root is not a git repo so the git revert scan cannot run.

    Loud-fail (h8s7 cwd-silent guard) for the new git-source path. cli_main maps it
    to a non-zero exit naming the resolved root, mirroring ArmCompareEmpty.
    """


_REVERTS_COMMIT_RE = re.compile(r"This reverts commit ([0-9a-f]{7,40})")

_MAIN_REF_CANDIDATES: tuple[str, ...] = (
    "origin/main",
    "origin/master",
    "main",
    "master",
    "HEAD",
)


def _git_out(workspace_root: Path, args: list[str]) -> str | None:
    """Run `git -C <root> <args>`; return stripped stdout, or None on any failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace_root), *args],
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _resolve_main_ref(workspace_root: Path) -> str:
    """Pick the default-branch ref to scan: origin/HEAD target first, then fallbacks."""
    sym = _git_out(workspace_root, ["symbolic-ref", "refs/remotes/origin/HEAD"])
    if sym:
        ref = sym.replace("refs/remotes/", "", 1)
        if _git_out(workspace_root, ["rev-parse", "--verify", "--quiet", ref]) is not None:
            return ref
    for ref in _MAIN_REF_CANDIDATES:
        if _git_out(workspace_root, ["rev-parse", "--verify", "--quiet", ref]) is not None:
            return ref
    return "HEAD"


def _scan_main_reverts(workspace_root: Path) -> list[dict[str, Any]]:
    """Scan the default branch git log for revert commits.

    Returns one dict per candidate that names a reverted commit (parsed from the
    `This reverts commit <sha>` body line). Each dict carries the reverting commit
    metadata plus the reverted commit's full message (resolved separately, "" if
    unknown). NOT bounded by date; window semantics live in compute_revert_rate.

    Raises RevertScanError iff workspace_root is not a git repo (the h8s7 guard).
    An empty repo or a repo with zero reverts returns [] without raising.
    """
    if _git_out(workspace_root, ["rev-parse", "--git-dir"]) is None:
        raise RevertScanError(f"not a git repo: {workspace_root}")

    ref = _resolve_main_ref(workspace_root)
    shas_out = _git_out(workspace_root, ["log", ref, "--grep=revert", "-i", "--format=%H"])
    if not shas_out:
        return []

    out: list[dict[str, Any]] = []
    for sha in shas_out.splitlines():
        sha = sha.strip()
        if not sha:
            continue
        body = _git_out(workspace_root, ["log", "-1", "--format=%B", sha])
        if not body:
            continue
        m = _REVERTS_COMMIT_RE.search(body)
        if m is None:
            continue
        reverted_sha = m.group(1)
        reverted_msg = _git_out(workspace_root, ["log", "-1", "--format=%B", reverted_sha]) or ""
        subject = body.splitlines()[0] if body else ""
        committed = _git_out(workspace_root, ["log", "-1", "--format=%cI", sha]) or ""
        out.append(
            {
                "reverting_commit_sha": sha,
                "reverting_subject": subject,
                "reverting_committed_at": _normalize_iso_z(committed),
                "reverted_commit_sha": reverted_sha,
                "reverted_message": reverted_msg,
            }
        )
    return out


def _normalize_iso_z(committed: str) -> str:
    """Normalize a git %cI timestamp to a `...Z` UTC string; passthrough on failure."""
    dt = parse_iso(committed)
    return iso_z(dt) if dt is not None else committed


def _keys_in_message(message: str, candidate_keys: set[str]) -> list[str]:
    """Return candidate keys appearing as whole words in message.

    Word-boundary match guards against a parent key (flow-a1ti) false-matching
    inside a child (flow-a1ti.2): the trailing lookahead forbids a following dot.
    Keys themselves may contain a dot (e.g. flow-a1ti.2), so re.escape the key.
    """
    found: list[str] = []
    for key in candidate_keys:
        pat = rf"(?<![\w.-]){re.escape(key)}(?![\w.-])"
        if re.search(pat, message):
            found.append(key)
    return found


def _emit_git_revert_event(
    workspace_root: Path, namespace: str, tickets: list[str], rev: dict[str, Any]
) -> None:
    """Best-effort durable revert event. Never raises (readout is the point).

    One record per reverting sha carrying every matched ticket: observe_revert
    keys the file by sha alone and treats EEXIST as a no-op, so a per-ticket emit
    would silently drop every key after the first.
    """
    record = {
        "kind": "revert",
        "ticket": tickets[0],
        "tickets": tickets,
        "reverted_commit_sha": rev.get("reverted_commit_sha"),
        "reverting_commit_sha": rev.get("reverting_commit_sha"),
        "reverting_subject": rev.get("reverting_subject"),
        "reverting_committed_at": rev.get("reverting_committed_at"),
        "source": "git",
    }
    with contextlib.suppress(OSError, ValueError, _memory_paths._MemoryConfigError):
        observe_ship_event.observe_revert(workspace_root, namespace, record)


def compute_revert_rate(
    workspace_root: Path,
    namespace: str,
    *,
    since_iso: str,
    until_iso: str,
) -> dict[str, Any]:
    """Compute the revert rate over the half-open window [since, until).

    Denominator = every in-window ship-event that is DECIDABLE (clean-no-reopen or
    reopened-and-reclosed); each lands in `tickets[]` and is counted in `shipped`.
    A revert is a shipped bead reopened and re-closed AFTER its `shipped_at`,
    detected by joining the ship-event to its tracker status history. Each revert
    is attributed via classify_attribution so the count splits by flow attribution.

    Undecidable / unmeasurable events are skip-and-recorded (not counted in
    `shipped`): `history_unavailable` (bd read failed), `tracker_unsupported`
    (non-beads backend short-circuit, no bd invocation), `reopened_not_yet_reclosed`
    (in-flight reopen, the DECISION requires reopen AND re-close).
    """
    since = parse_iso(since_iso)
    until = parse_iso(until_iso)
    if since is None:
        raise ValueError(f"since is not a UTC ISO8601 timestamp: {since_iso!r}")
    if until is None:
        raise ValueError(f"until is not a UTC ISO8601 timestamp: {until_iso!r}")

    backend = _tracker_backend(workspace_root)

    # scan first so the not-a-git-repo guard fires deterministically, regardless
    # of ship-event presence or tracker backend (git reverts count for jira too).
    git_reverts_scanned = _scan_main_reverts(workspace_root)

    shipped = 0
    n_reverts = 0
    reverts_via_flow = 0
    reverts_not_attributed = 0
    tickets: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    in_window_keys: set[str] = set()

    for event in load_ship_events(workspace_root, namespace):
        shipped_at_dt = parse_iso(str(event.get("shipped_at")))
        if shipped_at_dt is None or not (since <= shipped_at_dt < until):
            continue
        ticket = event.get("ticket")
        if isinstance(ticket, str) and ticket:
            in_window_keys.add(ticket)

        if backend != "beads":
            skipped.append({"ticket": ticket, "reason": "tracker_unsupported"})
            continue

        timeline = _status_history(workspace_root, namespace, str(ticket))
        if timeline is None:
            skipped.append({"ticket": ticket, "reason": "history_unavailable"})
            continue

        reverted, reopened_at, reclosed_at, in_flight = _classify_revert(timeline, shipped_at_dt)
        if in_flight:
            skipped.append({"ticket": ticket, "reason": "reopened_not_yet_reclosed"})
            continue

        attribution = classify_attribution(workspace_root, event)
        shipped += 1
        if reverted:
            n_reverts += 1
            if attribution == ATTR_VIA_FLOW:
                reverts_via_flow += 1
            else:
                reverts_not_attributed += 1
        tickets.append(
            {
                "ticket": ticket,
                "shipped_at": event.get("shipped_at"),
                "attribution": attribution,
                "reverted": reverted,
                "reopened_at": reopened_at,
                "reclosed_at": reclosed_at,
            }
        )

    tickets.sort(key=lambda t: (str(t["shipped_at"]), str(t["ticket"])))
    revert_rate = round(n_reverts / shipped, 6) if shipped > 0 else 0

    # git layer: join scanned main reverts to in-window shipped ticket keys. NOT
    # window-bounded on the revert side; a ticket shipped in-window but reverted
    # later still counts. A durable revert event is emitted per matched revert
    # (idempotent on reverting_commit_sha); the emit is best-effort.
    git_reverts: list[dict[str, Any]] = []
    git_reverted_keys: set[str] = set()
    for rev in git_reverts_scanned:
        msg = str(rev.get("reverted_message") or "")
        keys = sorted(_keys_in_message(msg, in_window_keys))
        for key in keys:
            git_reverted_keys.add(key)
            git_reverts.append(
                {
                    "ticket": key,
                    "reverting_commit_sha": rev.get("reverting_commit_sha"),
                    "reverting_subject": rev.get("reverting_subject"),
                    "reverted_commit_sha": rev.get("reverted_commit_sha"),
                }
            )
        if keys:
            _emit_git_revert_event(workspace_root, namespace, keys, rev)
    git_reverts.sort(key=lambda r: (str(r["ticket"]), str(r["reverting_commit_sha"])))

    return {
        "since": since_iso,
        "until": until_iso,
        "shipped": shipped,
        "n_reverts": n_reverts,
        "revert_rate": revert_rate,
        "reverts_via_flow": reverts_via_flow,
        "reverts_not_attributed": reverts_not_attributed,
        "tickets": tickets,
        "skipped": skipped,
        "n_skipped": len(skipped),
        "reverts_by_source": {"tracker": n_reverts, "git": len(git_reverted_keys)},
        "git_reverts": git_reverts,
    }


def _tracker_backend(workspace_root: Path) -> str | None:
    """Read [tracker].backend from workspace.toml; None if absent/unreadable."""
    try:
        data = _workspace.load_workspace_toml(workspace_root)
    except _workspace.WorkspaceConfigError:
        return None
    tracker = data.get("tracker")
    if not isinstance(tracker, dict):
        return None
    backend = tracker.get("backend")
    return backend if isinstance(backend, str) else None


# ─── Arm compare ─────────────────────────────────────────────────────────────


class ArmCompareEmpty(Exception):
    """Signals an empty in-window corpus so cli_main can fail loud (h8s7 guard)."""


def _arm_time_to_pr_hours(event: dict[str, Any]) -> tuple[float | None, str | None]:
    """Resolve one event's time-to-PR hours. Returns (hours, skip_reason).

    Stamp first (flow_attribution plan_started -> create_pr_finished); else
    evidence.start_ts -> evidence.pr_ts. Missing/unparseable/negative -> (None, reason).
    """
    stamp = _read_stamp(event)
    if stamp is not None:
        start, finish = stamp
    else:
        evidence = event.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        start = parse_iso(evidence.get("start_ts"))
        finish = parse_iso(evidence.get("pr_ts"))
        if start is None or finish is None:
            return None, "missing_start_or_pr_ts"
    duration = (finish - start).total_seconds() / 3600.0
    if duration < 0:
        return None, "negative_duration"
    return duration, None


def _arm_revert(
    workspace_root: Path,
    namespace: str,
    backend: str | None,
    ticket: Any,
    shipped_at: datetime,
) -> tuple[bool, str | None]:
    """Per-arm revert decision. Returns (is_revert, skip_reason).

    skip_reason mirrors compute_revert_rate's undecidable buckets
    (tracker_unsupported / history_unavailable / reopened_not_yet_reclosed); when
    it is None the event is decidable and is_revert is authoritative.
    """
    if backend != "beads":
        return False, "tracker_unsupported"
    timeline = _status_history(workspace_root, namespace, str(ticket))
    if timeline is None:
        return False, "history_unavailable"
    reverted, _, _, in_flight = _classify_revert(timeline, shipped_at)
    if in_flight:
        return False, "reopened_not_yet_reclosed"
    return reverted, None


def _arm_axis(flow_val: Any, control_val: Any, *, flow_better_lt: bool) -> str | None:
    """Score one axis: "flow" / "control", or None when undecidable for either arm."""
    if flow_val is None or control_val is None:
        return None
    if flow_better_lt:
        return "flow" if flow_val < control_val else "control"
    return "flow" if flow_val > control_val else "control"


def _arm_verdict(flow_block: dict[str, Any], control_block: dict[str, Any]) -> dict[str, Any]:
    """Render the pre-registered verdict from the two per-arm blocks.

    flow wins iff it takes >= 2 of the three axes; a flow-arm revert with zero
    control-arm reverts forces flow_wins false (GUARD).
    """
    axis_ttp = _arm_axis(
        flow_block["median_time_to_pr_hours"],
        control_block["median_time_to_pr_hours"],
        flow_better_lt=True,
    )
    axis_iv = _arm_axis(
        flow_block["interventions_per_pr"],
        control_block["interventions_per_pr"],
        flow_better_lt=True,
    )
    axis_cr = _arm_axis(
        flow_block["completion_rate"],
        control_block["completion_rate"],
        flow_better_lt=False,
    )
    favored_flow_count = sum(1 for a in (axis_ttp, axis_iv, axis_cr) if a == "flow")
    flow_wins = favored_flow_count >= 2
    guard_triggered = flow_block["reverts"] > 0 and control_block["reverts"] == 0
    if guard_triggered:
        flow_wins = False
    return {
        "time_to_pr": axis_ttp,
        "interventions_per_pr": axis_iv,
        "completion_rate": axis_cr,
        "favored_flow_count": favored_flow_count,
        "flow_wins": flow_wins,
        "guard_triggered": guard_triggered,
    }


def compute_arm_compare(
    workspace_root: Path,
    namespace: str,
    *,
    since_iso: str,
    until_iso: str,
) -> dict[str, Any]:
    """Compare flow-arm vs control-arm ship-events over [since, until).

    Partitions in-window events on `event["arm"]` (absent -> "flow"; legacy events
    read as flow). Per arm computes median_time_to_pr_hours, interventions_per_pr,
    completion_rate, and reverts, then a pre-registered verdict (flow wins iff it
    takes >=2 of the three axes), with a GUARD override: any flow-arm revert with
    zero control-arm reverts forces flow_wins=false.

    Raises ArmCompareEmpty when the in-window corpus is empty (h8s7 guard); the CLI
    maps it to a loud non-zero exit rather than an all-zeros verdict at exit 0.
    """
    since = parse_iso(since_iso)
    until = parse_iso(until_iso)
    if since is None:
        raise ValueError(f"since is not a UTC ISO8601 timestamp: {since_iso!r}")
    if until is None:
        raise ValueError(f"until is not a UTC ISO8601 timestamp: {until_iso!r}")

    backend = _tracker_backend(workspace_root)

    arms = ("flow", "control")
    hours: dict[str, list[float]] = {a: [] for a in arms}
    ttp_skipped: dict[str, list[dict[str, Any]]] = {a: [] for a in arms}
    interventions: dict[str, list[int]] = {a: [] for a in arms}
    outcomes: dict[str, list[str]] = {a: [] for a in arms}
    reverts: dict[str, int] = {a: 0 for a in arms}
    reverts_skipped: dict[str, list[dict[str, Any]]] = {a: [] for a in arms}
    n_events: dict[str, int] = {a: 0 for a in arms}

    total = 0
    for event in load_ship_events(workspace_root, namespace):
        shipped_at = parse_iso(str(event.get("shipped_at")))
        if shipped_at is None or not (since <= shipped_at < until):
            continue
        arm = event.get("arm", "flow")
        if arm not in arms:
            arm = "flow"
        total += 1
        n_events[arm] += 1
        ticket = event.get("ticket")

        dur, reason = _arm_time_to_pr_hours(event)
        if dur is None:
            ttp_skipped[arm].append({"ticket": ticket, "reason": reason})
        else:
            hours[arm].append(dur)

        evidence = event.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        iv = evidence.get("interventions")
        if isinstance(iv, int) and not isinstance(iv, bool):
            interventions[arm].append(iv)
        outcome = evidence.get("outcome")
        if outcome in ("merged", "abandoned"):
            outcomes[arm].append(outcome)

        is_revert, skip_reason = _arm_revert(workspace_root, namespace, backend, ticket, shipped_at)
        if skip_reason is not None:
            reverts_skipped[arm].append({"ticket": ticket, "reason": skip_reason})
        elif is_revert:
            reverts[arm] += 1

    if total == 0:
        raise ArmCompareEmpty(str(_memory_paths.ship_events_dir(workspace_root, namespace)))

    def _arm_block(arm: str) -> dict[str, Any]:
        ivs = interventions[arm]
        outs = outcomes[arm]
        merged = sum(1 for o in outs if o == "merged")
        return {
            "n_events": n_events[arm],
            "median_time_to_pr_hours": (percentile(hours[arm], 50.0) if hours[arm] else None),
            "interventions_per_pr": (sum(ivs) / len(ivs) if ivs else None),
            "completion_rate": (merged / len(outs) if outs else None),
            "reverts": reverts[arm],
            "time_to_pr_skipped": ttp_skipped[arm],
            "reverts_skipped": reverts_skipped[arm],
        }

    flow_block = _arm_block("flow")
    control_block = _arm_block("control")
    return {
        "since": since_iso,
        "until": until_iso,
        "resolved_workspace_root": str(workspace_root),
        "total_ship_events": total,
        "flow": flow_block,
        "control": control_block,
        "verdict": _arm_verdict(flow_block, control_block),
    }


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _resolve_window(args: argparse.Namespace, now_iso: str) -> tuple[str, str]:
    """Resolve (since, until) from --since/--until day flags, defaulting per now."""
    default_since, default_until = default_window(now_iso)
    until_iso = f"{args.until}T00:00:00Z" if args.until else default_until
    since_iso = f"{args.since}T00:00:00Z" if args.since else default_since
    if parse_iso(until_iso) is None:
        raise ValueError(f"--until is not YYYY-MM-DD: {args.until!r}")
    if parse_iso(since_iso) is None:
        raise ValueError(f"--since is not YYYY-MM-DD: {args.since!r}")
    return since_iso, until_iso


def _check_flow_dir(workspace_root: Path) -> str | None:
    """Return an error string if workspace_root is not an initialized flow workspace, else None."""
    if not (workspace_root / ".flow" / ".initialized").exists():
        return f"metric: not a flow workspace (no .flow/.initialized): {workspace_root}\n"
    return None


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--workspace-root", default=".")
    parser.add_argument("--since", default=None, help="YYYY-MM-DD (inclusive day start, UTC)")
    parser.add_argument("--until", default=None, help="YYYY-MM-DD (exclusive day start, UTC)")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tickets-per-week metric.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_tpw = sub.add_parser("tickets-per-week", help="Compute shipped tickets in a window.")
    _add_common_args(p_tpw)
    p_tpw.add_argument("--checkpoint", action="store_true")
    p_tpw.add_argument("--mode", choices=("personal", "work"), default=None)
    p_tpw.add_argument("--manifest-path", default=None)

    p_ttp = sub.add_parser("time-to-pr", help="Compute observed time-to-PR in a window.")
    _add_common_args(p_ttp)

    p_fpr = sub.add_parser(
        "friction-per-run", help="Compute friction events per distinct run in a window."
    )
    _add_common_args(p_fpr)

    p_rev = sub.add_parser(
        "revert-rate", help="Compute the revert rate of shipped tickets in a window."
    )
    _add_common_args(p_rev)

    p_arm = sub.add_parser(
        "arm-compare", help="Compare flow-arm vs control-arm ship-events in a window."
    )
    _add_common_args(p_arm)

    p_trend = sub.add_parser("trend", help="Roll up all five window measures.")
    _add_common_args(p_trend)
    p_trend.add_argument("--json", action="store_true")

    p_ch = sub.add_parser("corpus-health", help="Report knowledge.jsonl live-vs-superseded health.")
    _add_common_args(p_ch)

    p_rhr = sub.add_parser(
        "recall-hit-rate", help="Recall precision (used/surfaced) + miss count in a window."
    )
    _add_common_args(p_rhr)

    p_fe = sub.add_parser(
        "fix-efficacy",
        help="Per closed MACHINERY-fix bead, did the friction class it claimed to fix recur?",
    )
    _add_common_args(p_fe)
    p_fe.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _run_arm_compare(args: argparse.Namespace, since_iso: str, until_iso: str) -> int:
    if not args.namespace:
        sys.stderr.write("metric: --namespace is required\n")
        return 1
    workspace_root = Path(args.workspace_root).resolve()
    err = _check_flow_dir(workspace_root)
    if err:
        sys.stderr.write(err)
        return 1
    try:
        result = compute_arm_compare(
            workspace_root,
            args.namespace,
            since_iso=since_iso,
            until_iso=until_iso,
        )
    except ArmCompareEmpty as exc:
        sys.stderr.write(f"metric: arm-compare found no in-window ship-events under {exc}\n")
        return 1
    except ValueError as exc:
        sys.stderr.write(f"metric: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


def _run_time_to_pr(args: argparse.Namespace, since_iso: str, until_iso: str, now_iso: str) -> int:
    if not args.namespace:
        sys.stderr.write("metric: --namespace is required when not --checkpoint\n")
        return 1
    workspace_root = Path(args.workspace_root).resolve()
    err = _check_flow_dir(workspace_root)
    if err:
        sys.stderr.write(err)
        return 1
    try:
        result = compute_time_to_pr(
            workspace_root,
            args.namespace,
            since_iso=since_iso,
            until_iso=until_iso,
            now_iso=now_iso,
        )
    except ValueError as exc:
        sys.stderr.write(f"metric: {exc}\n")
        return 1
    out = dict(result)
    out["resolved_workspace_root"] = str(workspace_root)
    sys.stdout.write(json.dumps(out, indent=2, sort_keys=True) + "\n")
    return 0


def _run_friction_per_run(args: argparse.Namespace, since_iso: str, until_iso: str) -> int:
    if not args.namespace:
        sys.stderr.write("metric: --namespace is required\n")
        return 1
    workspace_root = Path(args.workspace_root).resolve()
    err = _check_flow_dir(workspace_root)
    if err:
        sys.stderr.write(err)
        return 1
    result = compute_friction_per_run(
        workspace_root,
        args.namespace,
        since_iso=since_iso,
        until_iso=until_iso,
    )
    out = dict(result)
    out["resolved_workspace_root"] = str(workspace_root)
    sys.stdout.write(json.dumps(out, indent=2, sort_keys=True) + "\n")
    return 0


def _run_corpus_health(
    args: argparse.Namespace, since_iso: str, until_iso: str, now_iso: str
) -> int:
    if not args.namespace:
        sys.stderr.write("metric: --namespace is required\n")
        return 1
    workspace_root = Path(args.workspace_root).resolve()
    err = _check_flow_dir(workspace_root)
    if err:
        sys.stderr.write(err)
        return 1
    try:
        result = compute_corpus_health(
            workspace_root,
            args.namespace,
            since_iso=since_iso,
            until_iso=until_iso,
            now_iso=now_iso,
        )
    except ValueError as exc:
        sys.stderr.write(f"metric: {exc}\n")
        return 1
    out = dict(result)
    out["resolved_workspace_root"] = str(workspace_root)
    sys.stdout.write(json.dumps(out, indent=2, sort_keys=True) + "\n")
    return 0


def _run_recall_hit_rate(args: argparse.Namespace, since_iso: str, until_iso: str) -> int:
    workspace_root = Path(args.workspace_root).resolve()
    namespace = args.namespace
    if not namespace:
        # concrete for the reflect 3f surface step: no placeholder namespace needed.
        try:
            namespace = _memory_paths.resolve_namespace(workspace_root)
        except _memory_paths._MemoryConfigError as exc:
            sys.stderr.write(f"metric: {exc}\n")
            return 1
    err = _check_flow_dir(workspace_root)
    if err:
        sys.stderr.write(err)
        return 1
    try:
        result = compute_recall_hit_rate(
            workspace_root,
            namespace,
            since_iso=since_iso,
            until_iso=until_iso,
        )
    except ValueError as exc:
        sys.stderr.write(f"metric: {exc}\n")
        return 1
    out = dict(result)
    out["resolved_workspace_root"] = str(workspace_root)
    sys.stdout.write(json.dumps(out, indent=2, sort_keys=True) + "\n")
    return 0


def _run_revert_rate(args: argparse.Namespace, since_iso: str, until_iso: str) -> int:
    if not args.namespace:
        sys.stderr.write("metric: --namespace is required\n")
        return 1
    workspace_root = Path(args.workspace_root).resolve()
    err = _check_flow_dir(workspace_root)
    if err:
        sys.stderr.write(err)
        return 1
    try:
        result = compute_revert_rate(
            workspace_root,
            args.namespace,
            since_iso=since_iso,
            until_iso=until_iso,
        )
    except RevertScanError as exc:
        sys.stderr.write(f"metric: revert-rate git scan failed: {exc}\n")
        return 1
    out = dict(result)
    out["resolved_workspace_root"] = str(workspace_root)
    sys.stdout.write(json.dumps(out, indent=2, sort_keys=True) + "\n")
    return 0


def _fmt_num(value: Any) -> str:
    """Render a measure number for the table; None -> 'n/a'."""
    if value is None:
        return "n/a"
    return str(value)


def _render_trend_table(rollup: dict[str, Any]) -> str:
    tpw = rollup["tickets-per-week"]
    ttp = rollup["time-to-pr"]
    fpr = rollup["friction-per-run"]
    rev = rollup["revert-rate"]
    rhr = rollup["recall-hit-rate"]
    by_source = rev["reverts_by_source"]
    lines = [
        f"metric trend  window [{rollup['since']}, {rollup['until']})",
        f"  workspace: {rollup['resolved_workspace_root']}",
        "",
        f"  tickets-per-week  : shipped={_fmt_num(tpw['shipped'])} "
        f"via_flow={_fmt_num(tpw[ATTR_VIA_FLOW])} "
        f"not_attributed={_fmt_num(tpw[ATTR_NOT_ATTRIBUTED])}",
        f"  time-to-pr        : n_measured={_fmt_num(ttp['n_measured'])} "
        f"median_hours={_fmt_num(ttp['median_hours'])} "
        f"p90_hours={_fmt_num(ttp['p90_hours'])}",
        f"  friction-per-run  : total_events={_fmt_num(fpr['total_events'])} "
        f"runs={_fmt_num(fpr['runs'])} "
        f"events_per_run={_fmt_num(fpr['events_per_run'])}",
        f"  revert-rate       : shipped={_fmt_num(rev['shipped'])} "
        f"n_reverts={_fmt_num(rev['n_reverts'])} "
        f"revert_rate={_fmt_num(rev['revert_rate'])} "
        f"by_source(tracker={_fmt_num(by_source['tracker'])} "
        f"git={_fmt_num(by_source['git'])})",
        f"  recall-hit-rate   : surfaced={_fmt_num(rhr['surfaced'])} "
        f"used={_fmt_num(rhr['used'])} "
        f"hit_rate={_fmt_num(rhr['hit_rate'])} "
        f"misses={_fmt_num(rhr['misses'])}",
    ]
    return "\n".join(lines) + "\n"


def _run_trend(args: argparse.Namespace, since_iso: str, until_iso: str, now_iso: str) -> int:
    if not args.namespace:
        sys.stderr.write("metric: --namespace is required\n")
        return 1
    workspace_root = Path(args.workspace_root).resolve()
    err = _check_flow_dir(workspace_root)
    if err:
        sys.stderr.write(err)
        return 1
    try:
        tpw = compute(
            workspace_root,
            args.namespace,
            since_iso=since_iso,
            until_iso=until_iso,
            now_iso=now_iso,
        )
        ttp = compute_time_to_pr(
            workspace_root,
            args.namespace,
            since_iso=since_iso,
            until_iso=until_iso,
            now_iso=now_iso,
        )
        fpr = compute_friction_per_run(
            workspace_root,
            args.namespace,
            since_iso=since_iso,
            until_iso=until_iso,
        )
        rev = compute_revert_rate(
            workspace_root,
            args.namespace,
            since_iso=since_iso,
            until_iso=until_iso,
        )
        rhr = compute_recall_hit_rate(
            workspace_root,
            args.namespace,
            since_iso=since_iso,
            until_iso=until_iso,
        )
    except RevertScanError as exc:
        sys.stderr.write(f"metric: revert-rate git scan failed: {exc}\n")
        return 1
    except ValueError as exc:
        sys.stderr.write(f"metric: {exc}\n")
        return 1

    rollup = {
        "since": since_iso,
        "until": until_iso,
        "resolved_workspace_root": str(workspace_root),
        "tickets-per-week": tpw,
        "time-to-pr": ttp,
        "friction-per-run": fpr,
        "revert-rate": rev,
        "recall-hit-rate": rhr,
    }
    if args.json:
        sys.stdout.write(json.dumps(rollup, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(_render_trend_table(rollup))
    return 0


def _render_fix_efficacy_table(result: dict[str, Any]) -> str:
    totals = result["totals"]
    lines = [
        f"fix-efficacy  workspace: {result['resolved_workspace_root']}",
        "",
        f"  totals: fix_beads={totals['fix_beads']} recurred={totals['recurred']} "
        f"clean={totals['clean']} unmeasurable={totals['unmeasurable']} "
        f"recurrence_rate={totals['recurrence_rate']} (over all fix_beads)",
        "",
    ]
    for bead in result["beads"]:
        lines.append(
            f"  {bead['ticket']:<16} {bead['verdict']:<9} "
            f"post_fix_count={bead['post_fix_count']} "
            f"claimed_anchors={bead['claimed_anchors']} "
            f"fix_shas={bead['fix_shas']}"
        )
    return "\n".join(lines) + "\n"


def _run_fix_efficacy(args: argparse.Namespace) -> int:
    workspace_root = Path(args.workspace_root).resolve()
    namespace = args.namespace
    if not namespace:
        try:
            namespace = _memory_paths.resolve_namespace(workspace_root)
        except _memory_paths._MemoryConfigError as exc:
            sys.stderr.write(f"metric: {exc}\n")
            return 1
    err = _check_flow_dir(workspace_root)
    if err:
        sys.stderr.write(err)
        return 1
    result = compute_fix_efficacy(workspace_root, namespace)
    out = dict(result)
    out["resolved_workspace_root"] = str(workspace_root)
    if args.json:
        sys.stdout.write(json.dumps(out, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(_render_fix_efficacy_table(out))
    return 0


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    now_iso = utcnow_iso()
    try:
        since_iso, until_iso = _resolve_window(args, now_iso)
    except ValueError as exc:
        sys.stderr.write(f"metric: {exc}\n")
        return 1

    # explicit-command dispatch: a dict collapses what would otherwise be one
    # `if` per command (C901 complexity) into a single lookup + call.
    dispatchers: dict[str, Callable[[], int]] = {
        "time-to-pr": lambda: _run_time_to_pr(args, since_iso, until_iso, now_iso),
        "friction-per-run": lambda: _run_friction_per_run(args, since_iso, until_iso),
        "revert-rate": lambda: _run_revert_rate(args, since_iso, until_iso),
        "arm-compare": lambda: _run_arm_compare(args, since_iso, until_iso),
        "trend": lambda: _run_trend(args, since_iso, until_iso, now_iso),
        "corpus-health": lambda: _run_corpus_health(args, since_iso, until_iso, now_iso),
        "recall-hit-rate": lambda: _run_recall_hit_rate(args, since_iso, until_iso),
        "fix-efficacy": lambda: _run_fix_efficacy(args),
    }
    if args.command in dispatchers:
        return dispatchers[args.command]()

    if getattr(args, "checkpoint", False):
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
    err = _check_flow_dir(workspace_root)
    if err:
        sys.stderr.write(err)
        return 1
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
    out = dict(result)
    out["resolved_workspace_root"] = str(workspace_root)
    sys.stdout.write(json.dumps(out, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "ATTR_NOT_ATTRIBUTED",
    "ATTR_VIA_FLOW",
    "ArmCompareEmpty",
    "RevertScanError",
    "classify_attribution",
    "cli_main",
    "compute",
    "compute_arm_compare",
    "compute_checkpoint",
    "compute_corpus_health",
    "compute_fix_efficacy",
    "compute_friction_per_run",
    "compute_recall_hit_rate",
    "compute_revert_rate",
    "compute_time_to_pr",
    "default_window",
    "load_ship_events",
]
