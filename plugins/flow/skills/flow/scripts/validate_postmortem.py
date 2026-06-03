"""Validate `.flow/postmortem.md` frontmatter for the 14-day checkpoint stop-path.

Library + thin CLI. Stdlib-only.

Reads the postmortem's `+++`-delimited TOML frontmatter via
`ticket_frontmatter.read()` (same convention as ticket files; we do not invent a
second frontmatter format).

Required fields + closed enums:
  - root_cause in ROOT_CAUSES
  - evidence: a list with >= 2 entries
  - next_action in NEXT_ACTIONS

Conditional on next_action:
  - "continue_as_is" requires a `trend_evidence` block AND the week-over-week
    shipped count must be non-decreasing (current >= prior). Counts come from
    immutable ship-events at `.flow/<namespace>/ship-events/*.json` (their
    top-level `shipped_at`), NOT from the trend_evidence block. The block is a
    presence requirement only.
  - "extend_dogfood_one_more_week" requires a `blocker_fixed_evidence` block
    carrying both `commit_sha` and `summary`.

Exit codes:
  0 = valid
  1 = schema violation (stderr lists violations)
  2 = trend assertion failed (schema clean, but counts went down)

The CLI separates exit 1 from exit 2 by tagging the trend-assertion violation
with `TREND_PREFIX`. Schema violations dominate: a run with any schema violation
exits 1 even if the trend also failed.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any

import ticket_frontmatter
from _memory_paths import ship_events_dir

TREND_PREFIX = "trend: "
WINDOW_DAYS = 7

ROOT_CAUSES: frozenset[str] = frozenset(
    {
        "plan_execution_failure",
        "doctrine_wrong",
        "tracker_friction",
        "memory_underperformance",
        "external_dependency_issue",
        "insufficient_calendar_time",
        "tool_tourism_resumed",
        "scope_too_ambitious",
    }
)

NEXT_ACTIONS: frozenset[str] = frozenset(
    {
        "continue_as_is",
        "extend_dogfood_one_more_week",
        "rollback_to_jira_workflow",
        "swap_tracker",
        "doctrine_change",
        "archive_flow",
    }
)


# ─── Time helpers ────────────────────────────────────────────────────────────


def _parse_iso(value: str) -> datetime.datetime | None:
    """Parse a UTC ISO8601 Z timestamp. Returns None if it does not parse."""
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        return datetime.datetime.fromisoformat(value[:-1]).replace(tzinfo=datetime.UTC)
    except ValueError:
        return None


# ─── Trend from ship-events ──────────────────────────────────────────────────


def _shipped_timestamps(workspace_root: Path, namespace: str) -> list[datetime.datetime]:
    """Top-level `shipped_at` of every primary ship-event file.

    Skips `.dupe.<n>.json` (would double-count a single ship) and any file
    without a parseable top-level `shipped_at` (e.g. `.quarantine-intent.*`
    files, whose shipped_at is nested under `record`).
    """
    out: list[datetime.datetime] = []
    events_dir = ship_events_dir(workspace_root, namespace)
    if not events_dir.is_dir():
        return out
    for path in sorted(events_dir.glob("*.json")):
        if ".dupe." in path.name:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        ts = _parse_iso(data.get("shipped_at", ""))
        if ts is not None:
            out.append(ts)
    return out


def _window_counts(timestamps: list[datetime.datetime], now: datetime.datetime) -> tuple[int, int]:
    """Return (prior_count, current_count) over two adjacent 7-day windows.

    current  = (now - 7d, now]
    prior    = (now - 14d, now - 7d]
    """
    one = now - datetime.timedelta(days=WINDOW_DAYS)
    two = now - datetime.timedelta(days=2 * WINDOW_DAYS)
    current = sum(1 for ts in timestamps if one < ts <= now)
    prior = sum(1 for ts in timestamps if two < ts <= one)
    return prior, current


# ─── Field checks ────────────────────────────────────────────────────────────


def _check_required(fm: dict[str, Any]) -> list[str]:
    violations: list[str] = []

    root_cause = fm.get("root_cause")
    if "root_cause" not in fm:
        violations.append("root_cause: missing")
    elif root_cause not in ROOT_CAUSES:
        violations.append(f"root_cause: {root_cause!r} not in enum")

    evidence = fm.get("evidence")
    if "evidence" not in fm:
        violations.append("evidence: missing")
    elif not isinstance(evidence, list):
        violations.append("evidence: not a list")
    elif len(evidence) < 2:
        violations.append("evidence: fewer than 2 entries")

    next_action = fm.get("next_action")
    if "next_action" not in fm:
        violations.append("next_action: missing")
    elif next_action not in NEXT_ACTIONS:
        violations.append(f"next_action: {next_action!r} not in enum")

    return violations


def _check_extend_dogfood(fm: dict[str, Any]) -> list[str]:
    block = fm.get("blocker_fixed_evidence")
    if not isinstance(block, dict):
        return ["blocker_fixed_evidence: missing or not a block"]
    violations: list[str] = []
    for key in ("commit_sha", "summary"):
        value = block.get(key)
        if not isinstance(value, str) or not value:
            violations.append(f"blocker_fixed_evidence.{key}: missing or empty")
    return violations


# ─── Public API ──────────────────────────────────────────────────────────────


def validate(
    postmortem_path: Path,
    *,
    workspace_root: Path,
    namespace: str,
    now_iso: str,
) -> list[str]:
    """Return violation strings; empty list = valid.

    The single trend-assertion violation (if any) is prefixed with
    `TREND_PREFIX` so the CLI can map it to exit 2. All other strings are schema
    violations (exit 1).
    """
    fm = ticket_frontmatter.read(postmortem_path)
    violations = _check_required(fm)

    next_action = fm.get("next_action")

    if next_action == "extend_dogfood_one_more_week":
        violations.extend(_check_extend_dogfood(fm))

    if next_action == "continue_as_is":
        if not isinstance(fm.get("trend_evidence"), dict):
            violations.append("trend_evidence: missing or not a block")
        else:
            now = _parse_iso(now_iso)
            if now is None:
                violations.append(f"now_iso: {now_iso!r} not UTC ISO8601 Z")
            else:
                prior, current = _window_counts(_shipped_timestamps(workspace_root, namespace), now)
                if current < prior:
                    violations.append(
                        f"{TREND_PREFIX}shipped count decreased "
                        f"({prior} -> {current}) but next_action is continue_as_is"
                    )

    return violations


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _exit_code(violations: list[str]) -> int:
    if not violations:
        return 0
    schema = [v for v in violations if not v.startswith(TREND_PREFIX)]
    if schema:
        return 1
    return 2


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate .flow/postmortem.md frontmatter for the 14-day checkpoint."
    )
    parser.add_argument("--path", required=True, help="path to postmortem.md.")
    parser.add_argument("--workspace-root", default=".", help="workspace dir containing .flow/.")
    parser.add_argument("--namespace", required=True, help="memory namespace.")
    parser.add_argument(
        "--now",
        default=None,
        help="UTC ISO8601 Z timestamp for the trend window (defaults to now).",
    )
    return parser.parse_args(argv)


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    now_iso = args.now if args.now is not None else _utcnow_iso()
    violations = validate(
        Path(args.path).resolve(),
        workspace_root=Path(args.workspace_root).resolve(),
        namespace=args.namespace,
        now_iso=now_iso,
    )
    for v in violations:
        sys.stderr.write(v + "\n")
    return _exit_code(violations)


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "NEXT_ACTIONS",
    "ROOT_CAUSES",
    "TREND_PREFIX",
    "cli_main",
    "validate",
]
