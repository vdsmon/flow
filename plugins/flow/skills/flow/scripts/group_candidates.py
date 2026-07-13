"""FLOW ticket group: candidate fetch + duplicate hints for run-level grouping.

Read-only. Resolves a candidate ticket set (explicit keys, or the
assigned-open selector), enriches each via ``tracker.get``, and emits a JSON
bundle that the ticket-group prose clusters into lead+covers proposals.

The clustering itself is judgment (shared files / deps / subsystem) and lives
in the reference doc. This script owns only the deterministic half: fetch,
normalize each ticket to a compact record (key, summary, status, type, parent,
links, whether the body is empty), and surface duplicate HINTS (an empty-body
ticket whose title strongly overlaps a sibling, the FT-1190 pattern). Hints
are suggestions the prose confirms, never verdicts.

Exit codes:
  0 = ok (bundle on stdout)
  1 = tracker read error (network / auth / retryable)
  2 = workspace config invalid (no workspace.toml, malformed, missing block)
  3 = invalid CLI args (no keys and no selector resolved nothing)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from tracker import TrackerError, make_tracker
from tracker_cli import _read_tracker_config, _WorkspaceConfigError

# Short, summary-noise stopwords dropped before the title-overlap test. The
# domain words (form names, sheet numbers, client) are what carry the signal.
_STOPWORDS = frozenset({"the", "and", "for", "with", "from", "into"})
_DUP_JACCARD = 0.6


def _summary_tokens(summary: str) -> set[str]:
    """Lowercase alphanumeric tokens (len >= 2) of a summary, minus stopwords.

    Punctuation is dropped, so "[AR 2083 - Rappi] - Sheet 3 - Arca" and
    "[AR 2083 - Rappi / Sheet 3 - ARCA]" tokenize identically. That is exactly
    the empty-body title-twin (FT-1190 vs FT-1207) the dup hint is meant to catch.
    """
    raw = re.findall(r"[a-z0-9]{2,}", summary.lower())
    return {t for t in raw if t not in _STOPWORDS}


def _normalize(ticket: dict[str, Any]) -> dict[str, Any]:
    links = [
        {"kind": link.get("kind"), "to_key": link.get("to_key")}
        for link in ticket.get("links", [])
        if isinstance(link, dict)
    ]
    return {
        "key": ticket.get("key"),
        "summary": ticket.get("summary", ""),
        "status": ticket.get("status", ""),
        "type": ticket.get("type", ""),
        "parent": ticket.get("parent"),
        "links": links,
        "body_empty": not str(ticket.get("description") or "").strip(),
    }


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _dup_hints(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Empty-body tickets whose title strongly overlaps a sibling (possible dups).

    Directional: only an EMPTY-body ticket is flagged as the duplicate (the
    sibling with real content is the keeper). The prose adjudicates; this only
    narrows where to look.
    """
    tokens = {r["key"]: _summary_tokens(r["summary"]) for r in records}
    hints: list[dict[str, Any]] = []
    for rec in records:
        if not rec["body_empty"]:
            continue
        best: tuple[str, float] | None = None
        for other in records:
            if other["key"] == rec["key"]:
                continue
            score = _jaccard(tokens[rec["key"]], tokens[other["key"]])
            if score >= _DUP_JACCARD and (best is None or score > best[1]):
                best = (other["key"], score)
        if best is not None:
            hints.append(
                {"key": rec["key"], "duplicate_of": best[0], "title_overlap": round(best[1], 2)}
            )
    return hints


def collect(tracker: Any, keys: list[str], selector_filter: str | None) -> dict[str, Any]:
    """Fetch + normalize candidates and compute dup hints.

    `keys` (explicit) take precedence; otherwise the `selector_filter` drives
    `list_assigned`, and each ref is enriched via `get` so parent/links/body are
    present for clustering.
    """
    if keys:
        tickets = [tracker.get(k) for k in keys]
    else:
        refs = tracker.list_assigned(selector_filter or "open")
        tickets = [tracker.get(r["key"]) for r in refs]
    records = [_normalize(t) for t in tickets]
    return {"candidates": records, "dup_hints": _dup_hints(records)}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch + normalize grouping candidates.")
    parser.add_argument("keys", nargs="*", help="explicit ticket keys to consider")
    parser.add_argument(
        "--mine",
        action="store_true",
        help="selector: candidates = your assigned tickets matching --filter (default open)",
    )
    parser.add_argument("--filter", default="open", help="list_assigned filter (default: open)")
    parser.add_argument("--workspace-root", default=".")
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    if not args.keys and not args.mine:
        sys.stderr.write("group-candidates: pass ticket keys or --mine\n")
        return 3
    try:
        config = _read_tracker_config(Path(args.workspace_root))
    except _WorkspaceConfigError as exc:
        sys.stderr.write(f"group-candidates: {exc}\n")
        return 2
    try:
        bundle = collect(make_tracker(config), args.keys, None if args.keys else args.filter)
    except TrackerError as exc:
        sys.stderr.write(f"group-candidates: tracker read failed: {exc}\n")
        return 1
    if not bundle["candidates"]:
        sys.stderr.write("group-candidates: no candidates resolved\n")
        return 3
    sys.stdout.write(json.dumps(bundle, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["cli_main", "collect"]
