"""/flow group persistence: durably record a cover set between propose and act.

`group` proposes a lead + covers, but if you do not run `spec --covers` right
away the decision lived only in the proposal. This persists it where it survives
sessions, machines, and the review wait, and is re-derivable, using only the
mandatory, cross-backend tracker ops (`comment` to write, `get` to read).

A cover set is recorded as a marker COMMENT on the lead ticket:

    flow-group covers: FT-1207, FT-1208, FT-1209

`derive` reads it back from `get(lead)` (no search, no label-merge, portable
across jira and beads). `spec <lead>` with no `--covers` consumes it to auto-fill
the run. Persisting is an explicit act, so `group` itself stays read-only.

Duplicate verdicts are NOT handled here: a dup is a one-time terminal mutation
(a `duplicates` link + close), left as an explicit human action in the proposal.

Exit codes:
  0 = ok (JSON on stdout)
  1 = tracker error (network / auth)
  2 = workspace config invalid
  3 = invalid CLI args (no lead, or persist with no covers)
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

_MARKER = "flow-group covers:"
_MARKER_RE = re.compile(r"^flow-group covers:\s*(.*)$", re.IGNORECASE)


def format_marker(covers: list[str]) -> str:
    return f"{_MARKER} {', '.join(covers)}"


def parse_marker(text: str) -> list[str] | None:
    """Covers from a marker line, or None when the text is not a marker."""
    m = _MARKER_RE.match(text.strip())
    if m is None:
        return None
    return [c.strip() for c in m.group(1).split(",") if c.strip()]


def _comment_text(comment: dict[str, Any]) -> str:
    body = comment.get("body")
    if isinstance(body, dict):
        return str(body.get("body", ""))
    return str(body or "")


def latest_covers(comments: list[dict[str, Any]]) -> list[str] | None:
    """The cover set from the most recent marker comment, or None if absent.

    Ordered by `created_at` so a re-persist (a newer marker) wins over an older
    one; comments without a timestamp keep their input order as a fallback.
    """
    markers = [
        (c.get("created_at", ""), parsed)
        for c in comments
        if (parsed := parse_marker(_comment_text(c))) is not None
    ]
    if not markers:
        return None
    markers.sort(key=lambda m: m[0])
    return markers[-1][1]


def persist(tracker: Any, lead: str, covers: list[str]) -> dict[str, Any]:
    """Write the cover marker on the lead, unless the latest one already matches."""
    existing = latest_covers(tracker.get(lead).get("comments", []))
    if existing == covers:
        return {"persisted": False, "reason": "unchanged", "lead": lead, "covers": covers}
    tracker.comment(lead, {"body": format_marker(covers), "fmt": "plain"})
    return {"persisted": True, "lead": lead, "covers": covers}


def derive(tracker: Any, lead: str) -> dict[str, Any]:
    covers = latest_covers(tracker.get(lead).get("comments", [])) or []
    return {"lead": lead, "covers": covers}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Persist / derive a /flow cover set.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("persist", help="record the cover set as a marker comment on the lead")
    p.add_argument("--lead", required=True)
    p.add_argument("--covers", required=True, help="comma-separated cover keys")
    p.add_argument("--workspace-root", default=".")
    d = sub.add_parser("derive", help="read the cover set back from the lead's marker comment")
    d.add_argument("--lead", required=True)
    d.add_argument("--workspace-root", default=".")
    return parser.parse_args(argv)


def _tracker_for(workspace_root: str) -> Any:
    config = _read_tracker_config(Path(workspace_root))
    return make_tracker(config)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        tracker = _tracker_for(args.workspace_root)
    except _WorkspaceConfigError as exc:
        sys.stderr.write(f"group-persist: {exc}\n")
        return 2
    try:
        if args.cmd == "persist":
            covers = [c.strip() for c in args.covers.split(",") if c.strip()]
            if not covers:
                sys.stderr.write("group-persist: --covers resolved to nothing\n")
                return 3
            result = persist(tracker, args.lead, covers)
        else:
            result = derive(tracker, args.lead)
    except TrackerError as exc:
        sys.stderr.write(f"group-persist: tracker op failed: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["cli_main", "derive", "format_marker", "latest_covers", "parse_marker", "persist"]
