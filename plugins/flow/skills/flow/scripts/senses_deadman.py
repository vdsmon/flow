"""Nightly ship-event divergence check + folded loop-health digest.

Library + thin CLI. Stdlib-only.

The RUNS deadman (`maintainer_preflight.py` over `~/.flow-evolve/run-record.jsonl`) watches the
nightly loop fire from bare cockpit and maintenance preflight. This is the SENSES deadman: it
watches the ship-event sense itself going dark. It joins
the window's closed beads against the ship-event store, buckets each close as observed / missing /
covered / unmerged / within-lag / ignored, files ONE deduped P0 on divergence, and prints a health
digest (telemetry freshness incl. quarantine-sidecar growth, metric-trend deltas, loop liveness) as
the nightly evidence trail. A merged-and-closed bead with no frozen ship event is the blindspot this
turns from a slow "PRs stopped appearing" discovery into a one-night alarm.

is_shipped is read through the tracker seam and NEVER modified (PR#277's two-join measurement-
integrity gate is untouched; this path is a read-only consumer).

Normal alarm-producing runs refresh the default-branch ref and use the metric readers' existing
quarantine-on-malformed behavior. `--dry-run` is strictly read-only: it does not fetch, file an
alarm, or call any quarantine-on-malformed reader. Its trend section is marked unavailable rather
than weakening that guarantee.

CLI:
  senses_deadman.py --workspace-root <dir> [--window-days 7 --lag-hours 24 --min-missing 2
                    --max-gap 5 --json --dry-run --run-record <path>]

Exit codes:
  0 = healthy (no divergence)
  1 = divergence detected (P0 filed, already-open, or dry-run)
  2 = bd/git/tracker error (could not compute a reliable verdict)
  4 = not a maintainer setup (dormant; nothing checked)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import metric
from _jsonl import read_jsonl_lenient
from _memory_paths import resolve_namespace, ship_events_dir
from _runner import Runner, default_runner
from _timeutil import iso_z, parse_iso, utcnow_iso
from maintainer import resolve_maintainer_repo
from tracker import TrackerError, make_tracker
from tracker_cli import _read_tracker_config, _WorkspaceConfigError

_P0_STEM = "senses-deadman"
_TREND_WINDOW_DAYS = 14
_TREND_UNAVAILABLE_READONLY = "read-only dry-run omits quarantine-on-malformed metric readers"
_TREND_UNAVAILABLE_NO_PRODUCER = (
    "the scheduled nightly trend producer is not deployed or has never run"
)
_SHIP_EVENT_SKIP_INFIXES = (".dupe.", ".corrupt.", ".quarantine-intent.")
_PR_SUFFIX_RE = re.compile(r"\(#\d+\)")


class _GatherError(Exception):
    """A bd/git/tracker read failed such that the divergence verdict is unreliable (exit 2)."""


# ─── Pure core ───────────────────────────────────────────────────────────────


def _keys_in_body(body: str, candidates: set[str]) -> list[str]:
    """Candidate keys appearing as whole words in the commit body, sorted.

    Word-boundary match keeps a parent key (flow-a1ti) from false-matching inside a child
    (flow-a1ti.2); keys may themselves contain a dot, so each is re.escaped. Mirrors
    metric._keys_in_message, kept local rather than importing a private helper.
    """
    found = [k for k in candidates if re.search(rf"(?<![\w.-]){re.escape(k)}(?![\w.-])", body)]
    return sorted(found)


def classify_closes(
    closes: list[dict[str, Any]],
    *,
    now_iso: str,
    lag_hours: float,
    observed_keys: set[str],
    is_shipped_fn: Any,
    commit_body_fn: Any,
) -> dict[str, Any]:
    """Bucket each window close as observed / within-lag / missing / covered / unmerged / ignored.

    Order per close: a key with a primary ship-event file is `observed` (never probed). An
    unobserved close still inside `lag_hours` is `within_lag` (excluded from missing). Otherwise
    `is_shipped_fn` decides: `not_yet_observed` is a missing-candidate, `indeterminate` is
    `unmerged` (closed-unmerged never expects an event), anything else is `ignored`.

    Covers attribution: a missing-candidate whose is_shipped evidence `commit_sha` names ANOTHER key
    that itself holds a primary ship event is `covered`, not missing. The commit body comes from
    `commit_body_fn(sha)` (None on failure -> stays missing, never crashes); the named lead must be
    in `observed_keys` (an unobserved lead means the sense is genuinely dark, so both stay missing
    and the alarm fires).
    """
    now = parse_iso(now_iso)
    buckets: dict[str, Any] = {
        "observed": [],
        "within_lag": [],
        "missing": [],
        "covered": [],
        "unmerged": [],
        "ignored": [],
    }
    for close in closes:
        key = close.get("key")
        if not isinstance(key, str) or not key:
            continue
        if key in observed_keys:
            buckets["observed"].append(key)
            continue
        closed_at = parse_iso(close.get("closed_at"))
        if now is not None and closed_at is not None:
            age_hours = (now - closed_at).total_seconds() / 3600.0
            if age_hours < lag_hours:
                buckets["within_lag"].append(key)
                continue
        ship = is_shipped_fn(key)
        state = ship.get("state")
        if state == "indeterminate":
            buckets["unmerged"].append(key)
            continue
        if state != "not_yet_observed":
            buckets["ignored"].append(key)
            continue
        lead = _cover_lead(ship, key, observed_keys, commit_body_fn)
        if lead is not None:
            buckets["covered"].append({"key": key, "lead": lead})
        else:
            buckets["missing"].append(key)
    return buckets


def _cover_lead(
    ship: dict[str, Any], key: str, observed_keys: set[str], commit_body_fn: Any
) -> str | None:
    """The observed lead key that covers this missing-candidate, or None if uncovered."""
    evidence = ship.get("evidence") or {}
    sha = evidence.get("commit_sha")
    if not isinstance(sha, str) or not sha:
        return None
    body = commit_body_fn(sha)
    if not body:
        return None
    # a lead is always another key; classify_closes never routes an observed key here, and the
    # subtraction keeps that true if the caller's bucket order ever drifts
    leads = _keys_in_body(body, observed_keys - {key})
    return leads[0] if leads else None


def decide_alarm(observed: int, missing: int, *, min_missing: int, max_gap: int) -> bool:
    """Fire iff the sense is fully dark with enough closes, or partially dark past the gap.

    covered closes are already excluded from `missing` by classify_closes.
    """
    return (observed == 0 and missing >= min_missing) or missing >= max_gap


def run_record_summary(entries: list[dict[str, Any]], *, now_iso: str) -> dict[str, Any]:
    """Per-schedule latest start/end/outcome + age from `run-record.jsonl` lines.

    Empty input -> `{"armed": False}` (no schedule armed on this machine).
    """
    if not entries:
        return {"armed": False, "schedules": {}}
    now = parse_iso(now_iso)
    by_schedule: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        schedule = entry.get("schedule")
        if isinstance(schedule, str) and schedule:
            by_schedule.setdefault(schedule, []).append(entry)

    schedules: dict[str, Any] = {}
    for schedule, rows in by_schedule.items():
        latest_start = _latest_ts(rows, "start")
        latest_end_row = _latest_row(rows, "end")
        latest_ts = _latest_ts(rows, None)
        age_hours: float | None = None
        latest_dt = parse_iso(latest_ts) if latest_ts else None
        if now is not None and latest_dt is not None:
            age_hours = round((now - latest_dt).total_seconds() / 3600.0, 2)
        schedules[schedule] = {
            "latest_start": latest_start,
            "latest_end": latest_end_row.get("ts") if latest_end_row else None,
            "latest_outcome": latest_end_row.get("outcome") if latest_end_row else None,
            "age_hours": age_hours,
        }
    return {"armed": True, "schedules": schedules}


def _latest_row(rows: list[dict[str, Any]], phase: str | None) -> dict[str, Any] | None:
    candidates = [
        r
        for r in rows
        if (phase is None or r.get("phase") == phase) and isinstance(r.get("ts"), str)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: str(r.get("ts")))


def _latest_ts(rows: list[dict[str, Any]], phase: str | None) -> str | None:
    row = _latest_row(rows, phase)
    return str(row["ts"]) if row else None


# ─── Digest rendering ────────────────────────────────────────────────────────


def render_digest(digest: dict[str, Any]) -> str:
    """Render the machine digest dict as the markdown evidence trail (and the P0 description).

    An error digest (the exit-2 path) renders as the error line alone; zero-count sections would
    read as a healthy night in the log.
    """
    error = digest.get("error")
    if error:
        return f"## senses-deadman digest\n\n- ERROR: {error} (no verdict)\n"
    div = digest.get("divergence", {})
    fresh = digest.get("freshness", {})
    trend = digest.get("trend", {})
    live = digest.get("liveness", {})
    window = digest.get("window_days", "?")

    lines: list[str] = []
    lines.append(f"## senses-deadman digest ({window}d window)")
    lines.append("")
    lines.append("### Divergence")
    lines.append(f"- closes: {div.get('closes', 0)}")
    lines.append(f"- observed: {div.get('observed', 0)}")
    covered = [f"{c.get('key')} (lead {c.get('lead')})" for c in div.get("covered", [])]
    lines.append(f"- missing: {len(div.get('missing', []))} {div.get('missing', [])}")
    lines.append(f"- covered: {len(covered)} {covered}")
    lines.append(f"- unmerged: {len(div.get('unmerged', []))} {div.get('unmerged', [])}")
    lines.append(f"- within-lag: {len(div.get('within_lag', []))} {div.get('within_lag', [])}")
    lines.append(f"- merged-PR commits on default (informational): {div.get('merged_pr_count', 0)}")
    lines.append(f"- alarm: {div.get('alarm', False)}")
    lines.append("")
    lines.append("### Telemetry freshness")
    lines.append(f"- newest ship event: {fresh.get('newest_ship_event', 'absent')}")
    lines.append(f"- newest friction entry: {fresh.get('newest_friction', 'absent')}")
    lines.append(f"- newest knowledge entry: {fresh.get('newest_knowledge', 'absent')}")
    lines.append(f"- ship-events quarantine lines: {fresh.get('quarantine_lines', 'absent')}")
    lines.append("")
    lines.append("### Metric trend (current vs previous 14d)")
    if trend.get("unavailable"):
        lines.append(f"- unavailable: {trend['unavailable']}")
    else:
        for name in ("shipped", "time_to_pr", "friction_per_run", "recall_hit_rate"):
            measure = trend.get(name, {})
            lines.append(
                f"- {name}: current={measure.get('current')} previous={measure.get('previous')} "
                f"delta={measure.get('delta')}"
            )
    lines.append("")
    lines.append("### Loop liveness")
    if not live.get("armed"):
        lines.append("- not armed")
    else:
        for schedule, info in sorted(live.get("schedules", {}).items()):
            lines.append(
                f"- {schedule}: latest end {info.get('latest_end')} "
                f"({info.get('latest_outcome')}), age {info.get('age_hours')}h"
            )
    lines.append("")
    return "\n".join(lines)


# ─── Gather (I/O) ────────────────────────────────────────────────────────────


def _default_branch(run: Runner, repo: Path) -> str:
    """The default branch NAME (e.g. `main`) via origin/HEAD; falls back to `main`."""
    res = run(["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], repo)
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip().rsplit("/", 1)[-1]
    return "main"


def _closed_beads(run: Runner, repo: Path, since_iso: str) -> list[dict[str, Any]]:
    """Window closes via `bd list --status closed --closed-after <date>`, re-filtered by closed_at.

    `--closed-after` is day-granular, so the precise half-open `[since, now]` re-filter runs here.
    """
    since_dt = parse_iso(since_iso)
    since_date = since_dt.date().isoformat() if since_dt else since_iso[:10]
    argv = ["bd", "list", "--status", "closed", "--closed-after", since_date, "--limit", "0"]
    res = run([*argv, "--json"], repo)
    if res.returncode != 0:
        raise _GatherError(f"bd list closed failed (rc={res.returncode})")
    try:
        rows = json.loads(res.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise _GatherError(f"bd list closed emitted non-JSON: {exc}") from exc
    closes: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = row.get("id")
        closed_at = row.get("closed_at")
        closed_dt = parse_iso(closed_at)
        if since_dt is not None and (closed_dt is None or closed_dt < since_dt):
            continue
        if isinstance(key, str) and key:
            closes.append({"key": key, "closed_at": closed_at})
    return closes


def _observed_keys(repo: Path, namespace: str) -> set[str]:
    """Keys holding a primary `ship-events/<key>.json` (dupe/corrupt/intent files excluded)."""
    ship_dir = ship_events_dir(repo, namespace)
    if not ship_dir.is_dir():
        return set()
    keys: set[str] = set()
    for path in ship_dir.glob("*.json"):
        if any(infix in path.name for infix in _SHIP_EVENT_SKIP_INFIXES):
            continue
        keys.add(path.stem)
    return keys


def _git_show_body(run: Runner, repo: Path, sha: str) -> str | None:
    res = run(["git", "show", "-s", "--format=%B", sha], repo)
    return res.stdout if res.returncode == 0 else None


def _merged_pr_count(run: Runner, repo: Path, branch: str, since_iso: str) -> int:
    """Informational count of squash-merge commits (`(#N)` subject suffix) on the default branch."""
    res = run(
        ["git", "log", f"origin/{branch}", f"--since={since_iso}", "--format=%s"],
        repo,
    )
    if res.returncode != 0:
        return 0
    return sum(1 for line in res.stdout.splitlines() if _PR_SUFFIX_RE.search(line))


def _newest_iso(values: list[Any]) -> str | None:
    parsed = [(parse_iso(v), v) for v in values]
    dated = [(dt, raw) for dt, raw in parsed if dt is not None]
    if not dated:
        return None
    return str(max(dated, key=lambda p: p[0])[1])


def _read_ship_events_without_quarantine(repo: Path, namespace: str) -> list[dict[str, Any]]:
    """Read valid primary ship events without producing a quarantine sidecar."""

    ship_dir = ship_events_dir(repo, namespace)
    if not ship_dir.is_dir():
        return []
    events: list[dict[str, Any]] = []
    for path in sorted(ship_dir.glob("*.json")):
        if any(infix in path.name for infix in _SHIP_EVENT_SKIP_INFIXES):
            continue
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(event, dict) and isinstance(event.get("shipped_at"), str):
            events.append(event)
    return events


def _gather_freshness(repo: Path, namespace: str, *, read_only: bool = False) -> dict[str, Any]:
    ship_dir = ship_events_dir(repo, namespace)
    base = ship_dir.parent
    ship_events = (
        _read_ship_events_without_quarantine(repo, namespace)
        if read_only
        else metric.load_ship_events(repo, namespace)
    )
    newest_ship = _newest_iso([e.get("shipped_at") for e in ship_events])
    newest_friction = _newest_iso(
        [e.get("ts") for e in read_jsonl_lenient(base / "friction.jsonl")]
    )
    newest_knowledge = _newest_iso(
        [e.get("ts") for e in read_jsonl_lenient(base / "knowledge.jsonl")]
    )
    quarantine = base / "ship-events.quarantine"
    if quarantine.exists():
        text = quarantine.read_text(encoding="utf-8", errors="replace")
        quarantine_lines: Any = sum(1 for line in text.splitlines() if line.strip())
    else:
        quarantine_lines = "absent"
    return {
        "newest_ship_event": newest_ship or "absent",
        "newest_friction": newest_friction or "absent",
        "newest_knowledge": newest_knowledge or "absent",
        "quarantine_lines": quarantine_lines,
    }


def _safe_measure(fn: Any, field: str, **kwargs: Any) -> float | None:
    try:
        result = fn(**kwargs)
    except Exception:
        return None
    value = result.get(field)
    return value if isinstance(value, (int, float)) else None


def _gather_trend(repo: Path, namespace: str, now_iso: str) -> dict[str, Any]:
    """Current-vs-previous 14-day deltas for the four read-mostly trend measures.

    compute_revert_rate is DELIBERATELY absent: it emits durable revert events. The now_iso kwarg
    goes only to compute / compute_time_to_pr; the friction / recall computes do not take it.
    """
    now = parse_iso(now_iso)
    if now is None:
        return {}
    cur_until = now_iso
    cur_since = iso_z(now - timedelta(days=_TREND_WINDOW_DAYS))
    prev_until = cur_since
    prev_since = iso_z(now - timedelta(days=2 * _TREND_WINDOW_DAYS))

    def _delta(cur: float | None, prev: float | None) -> float | None:
        return round(cur - prev, 6) if cur is not None and prev is not None else None

    trend: dict[str, Any] = {}
    for name, fn, field, takes_now in (
        ("shipped", metric.compute, "shipped", True),
        ("time_to_pr", metric.compute_time_to_pr, "median_hours", True),
        ("friction_per_run", metric.compute_friction_per_run, "events_per_run", False),
        ("recall_hit_rate", metric.compute_recall_hit_rate, "hit_rate", False),
    ):
        cur_kw: dict[str, Any] = {
            "since_iso": cur_since,
            "until_iso": cur_until,
        }
        prev_kw: dict[str, Any] = {
            "since_iso": prev_since,
            "until_iso": prev_until,
        }
        if takes_now:
            cur_kw["now_iso"] = cur_until
            prev_kw["now_iso"] = prev_until
        cur = _safe_measure(fn, field, workspace_root=repo, namespace=namespace, **cur_kw)
        prev = _safe_measure(fn, field, workspace_root=repo, namespace=namespace, **prev_kw)
        trend[name] = {"current": cur, "previous": prev, "delta": _delta(cur, prev)}
    return trend


def _dry_run_trend_unavailable(liveness: dict[str, Any]) -> str:
    """Neutral read-only clause, plus the producer-absence clause iff no `nightly` schedule ran.

    Schedule presence alone is the signal: an existing `nightly` entry is sufficient evidence the
    producer was deployed or attempted, regardless of its age or outcome (maintainer_preflight owns
    stale/failed/hung/disarmed classification).
    """
    if "nightly" in liveness.get("schedules", {}):
        return _TREND_UNAVAILABLE_READONLY
    return f"{_TREND_UNAVAILABLE_READONLY}; {_TREND_UNAVAILABLE_NO_PRODUCER}"


# ─── P0 filing ───────────────────────────────────────────────────────────────


def _file_p0(
    run: Runner,
    repo: Path,
    *,
    n_missing: int,
    window_days: int,
    newest_event: str,
    description: str,
) -> dict[str, str]:
    """File ONE deduped P0 on divergence. At-most-one-OPEN via a title-stem scan.

    Mirrors evolve_reap._file_main_red_p0: file directly (not flow_beads_create, whose dedup is
    closed-inclusive and would never refile after a human closes the P0). A list failure skips
    filing rather than risk a duplicate.
    """
    try:
        listed = run(["bd", "list", "--status", "open", "--limit", "0", "--json"], repo)
    except Exception:
        return {"action": "skipped_list_error"}
    if listed.returncode != 0:
        return {"action": "skipped_list_error"}
    try:
        open_beads = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError:
        return {"action": "skipped_list_error"}
    for bead in open_beads:
        if isinstance(bead, dict) and _P0_STEM in str(bead.get("title", "")):
            return {"action": "skipped_open"}
    newest = newest_event if newest_event and newest_event != "absent" else "none"
    title = (
        f"{_P0_STEM}: {n_missing} merged closes unobserved over {window_days}d "
        f"(newest event {newest[:10]})"
    )
    try:
        created = run(["bd", "create", "-p", "P0", "--title", title, "-d", description], repo)
    except Exception:
        return {"action": "create_error"}
    if created.returncode != 0:
        return {"action": "create_error"}
    return {"action": "filed"}


# ─── Orchestration ───────────────────────────────────────────────────────────


def deadman(
    workspace_root: Path,
    *,
    runner: Runner | None = None,
    now_iso: str | None = None,
    window_days: int = 7,
    lag_hours: float = 24.0,
    min_missing: int = 2,
    max_gap: int = 5,
    run_record_path: Path | None = None,
    dry_run: bool = False,
) -> tuple[dict[str, Any], int]:
    """Compute the divergence verdict + digest for an already-resolved maintainer repo.

    Returns `(digest, exit_code)`. The caller (cli_main) owns the maintainer gate; this assumes
    `workspace_root` is the maintainer repo.
    """
    repo = Path(workspace_root)
    run = runner or default_runner()
    now = now_iso or utcnow_iso()
    now_dt = parse_iso(now)
    since_iso = iso_z(now_dt - timedelta(days=window_days)) if now_dt else now

    try:
        namespace = resolve_namespace(repo)
        branch = _default_branch(run, repo)
        if not dry_run:
            run(["git", "fetch", "--quiet", "origin", branch], repo)  # one writeful refresh
        closes = _closed_beads(run, repo, since_iso)
        observed = _observed_keys(repo, namespace)
        config = _read_tracker_config(repo)
        tracker = make_tracker(config)
    except (_WorkspaceConfigError, TrackerError, _GatherError) as exc:
        return {"error": str(exc)}, 2
    except Exception as exc:
        return {"error": f"unexpected: {exc}"}, 2

    try:
        buckets = classify_closes(
            closes,
            now_iso=now,
            lag_hours=lag_hours,
            observed_keys=observed,
            is_shipped_fn=tracker.is_shipped,
            commit_body_fn=lambda sha: _git_show_body(run, repo, sha),
        )
    except TrackerError as exc:
        return {"error": str(exc)}, 2
    except Exception as exc:
        return {"error": f"unexpected: {exc}"}, 2
    observed_count = len(buckets["observed"])
    missing_count = len(buckets["missing"])
    alarm = decide_alarm(observed_count, missing_count, min_missing=min_missing, max_gap=max_gap)

    freshness = _gather_freshness(repo, namespace, read_only=dry_run)
    record_path = run_record_path or (Path.home() / ".flow-evolve" / "run-record.jsonl")
    liveness = run_record_summary(read_jsonl_lenient(record_path, replace_errors=True), now_iso=now)
    digest: dict[str, Any] = {
        "window_days": window_days,
        "divergence": {
            "closes": len(closes),
            "observed": observed_count,
            "missing": buckets["missing"],
            "covered": buckets["covered"],
            "unmerged": buckets["unmerged"],
            "within_lag": buckets["within_lag"],
            "ignored": buckets["ignored"],
            "merged_pr_count": _merged_pr_count(run, repo, branch, since_iso),
            "alarm": alarm,
        },
        "freshness": freshness,
        "trend": (
            {"unavailable": _dry_run_trend_unavailable(liveness)}
            if dry_run
            else _gather_trend(repo, namespace, now)
        ),
        "liveness": liveness,
    }

    filed: dict[str, str] = {"action": "none"}
    if alarm and not dry_run:
        filed = _file_p0(
            run,
            repo,
            n_missing=missing_count,
            window_days=window_days,
            newest_event=str(freshness.get("newest_ship_event", "none")),
            description=render_digest(digest),
        )
    digest["filed"] = filed
    return digest, (1 if alarm else 0)


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Nightly ship-event divergence check + folded loop-health digest."
    )
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--window-days", type=int, default=7)
    parser.add_argument("--lag-hours", type=float, default=24.0)
    parser.add_argument("--min-missing", type=int, default=2)
    parser.add_argument("--max-gap", type=int, default=5)
    parser.add_argument("--json", action="store_true", help="emit the machine digest dict.")
    parser.add_argument("--dry-run", action="store_true", help="compute + print, never file.")
    parser.add_argument("--run-record", default=None, help="override the run-record.jsonl path.")
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    workspace_root = Path(args.workspace_root).resolve()
    repo = resolve_maintainer_repo(workspace_root)
    if repo is None:
        print("not a flow maintainer setup; senses-deadman is dormant", file=sys.stderr)
        return 4

    digest, code = deadman(
        repo,
        now_iso=None,
        window_days=args.window_days,
        lag_hours=args.lag_hours,
        min_missing=args.min_missing,
        max_gap=args.max_gap,
        run_record_path=Path(args.run_record) if args.run_record else None,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(digest, indent=2, default=str))
    else:
        print(render_digest(digest))
    return code


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "classify_closes",
    "cli_main",
    "deadman",
    "decide_alarm",
    "render_digest",
    "run_record_summary",
]
