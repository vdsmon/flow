"""Pre-migration time-to-PR baseline: file format + statistics.

Library + thin CLI. Stdlib-only.

The work-mode quality gate compares each ticket's observed time-to-PR against
this baseline (the +/-30% comparison), so the baseline records what shipping a
ticket took BEFORE the flow pipeline existed.

LIVE COLLECTION IS OUT OF SCOPE HERE. Gathering the real samples means walking
the Jira changelog (status transitions) and Bitbucket PR history, both of which
need authenticated APIs; that lives outside this module. This module owns only
the on-disk format and the statistics (median / p90 / n). It accepts samples
from the caller and never reaches out to a network.

Baseline file (default ~/.config/flow/baseline-jira-workflow.json):
    {
      "collected_at": "<UTC ISO8601 Z>",
      "source": "manual",
      "samples": [{"ticket": "FT-1", "time_to_pr_hours": 12.5}, ...],
      "median_hours": <float>,
      "p90_hours": <float>,
      "n": <int>
    }

Exit codes:
  0 = ok
  1 = bad args / no samples / malformed samples JSON
  3 = I/O error
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from _atomicio import atomic_write_text

DEFAULT_PATH = Path.home() / ".config" / "flow" / "baseline-jira-workflow.json"


# ─── Errors ──────────────────────────────────────────────────────────────────


class _NoSamples(Exception):
    """Samples list is empty or absent."""


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── Statistics ────────────────────────────────────────────────────────────────


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (numpy default, C=1). pct in [0, 100].

    Empty input returns 0.0. Sorts a copy; the caller's list is untouched.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        return float(ordered[0])
    rank = (n - 1) * pct / 100.0
    lo = math.floor(rank)
    hi = math.ceil(rank)
    return ordered[lo] + (rank - lo) * (ordered[hi] - ordered[lo])


# ─── Public API ──────────────────────────────────────────────────────────────


def build_baseline(
    samples: list[dict[str, Any]],
    *,
    collected_at: str,
    source: str = "manual",
) -> dict[str, Any]:
    """Compute median_hours (p50), p90_hours, and n from samples.

    Each sample carries `time_to_pr_hours`. The samples list is stored verbatim.

    Raises:
        _NoSamples
    """
    if not samples:
        raise _NoSamples("no samples provided")
    hours = [float(s["time_to_pr_hours"]) for s in samples]
    return {
        "collected_at": collected_at,
        "source": source,
        "samples": samples,
        "median_hours": percentile(hours, 50.0),
        "p90_hours": percentile(hours, 90.0),
        "n": len(hours),
    }


def write_baseline(path: Path, baseline: dict[str, Any]) -> None:
    """Atomically write the baseline as JSON.

    Raises:
        OSError
    """
    atomic_write_text(path, json.dumps(baseline, sort_keys=True))


def read_baseline(path: Path) -> dict[str, Any] | None:
    """Read the stored baseline, or None if the file does not exist.

    Raises:
        OSError (other than missing file)
        ValueError (malformed JSON)
    """
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _load_samples_json(value: str) -> list[dict[str, Any]]:
    """Parse --samples-json as a file path or inline JSON list."""
    candidate = Path(value)
    raw = candidate.read_text(encoding="utf-8") if candidate.exists() else value
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError("samples JSON must be a list")
    return parsed


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pre-migration time-to-PR baseline: build / show.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build")
    p_build.add_argument(
        "--samples-json",
        required=True,
        help="file path or inline JSON list of {ticket, time_to_pr_hours}.",
    )
    p_build.add_argument("--path", default=None)
    p_build.add_argument("--source", default="manual")

    p_show = sub.add_parser("show")
    p_show.add_argument("--path", default=None)

    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    path = Path(args.path) if args.path else DEFAULT_PATH
    if args.command == "build":
        try:
            samples = _load_samples_json(args.samples_json)
        except (ValueError, OSError) as exc:
            sys.stderr.write(f"baseline-collect: invalid samples JSON: {exc}\n")
            return 1
        try:
            baseline = build_baseline(samples, collected_at=_utcnow_iso(), source=args.source)
        except _NoSamples as exc:
            sys.stderr.write(f"baseline-collect: {exc}\n")
            return 1
        try:
            write_baseline(path, baseline)
        except OSError as exc:
            sys.stderr.write(f"baseline-collect: I/O error: {exc}\n")
            return 3
        sys.stdout.write(json.dumps(baseline, sort_keys=True) + "\n")
        return 0

    try:
        baseline = read_baseline(path)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"baseline-collect: I/O error: {exc}\n")
        return 3
    if baseline is None:
        sys.stderr.write(f"baseline-collect: no baseline at {path}\n")
        return 3
    sys.stdout.write(json.dumps(baseline, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "DEFAULT_PATH",
    "build_baseline",
    "cli_main",
    "percentile",
    "read_baseline",
    "write_baseline",
]
