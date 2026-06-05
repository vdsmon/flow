"""/flow triage: surface the deferred queue + each bead's open-question comment.

Read-only. Lists every `deferred` bead (whole queue, unscoped by assignee) with
the last "could not self-approve" defer comment inline, so a human can answer it
and reopen via the tracker_cli seams (the reopen mutation lives in verb-triage.md,
not here). Deferred is a beads-native concept; non-beads backends short-circuit.

Stdlib-only. The `bd` transport is injectable (`runner=`) for offline tests.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from tracker_beads import BeadsAdapter
from tracker_cli import _read_tracker_config, _WorkspaceConfigError

# The defer comment stem written by the `--auto` path (verb-spec.md). Both the
# template form `... self-approve:` and the in-the-wild `... self-approve (HOT...`
# share this prefix, so we match on the stem and accept whatever follows.
_DEFER_STEM = "flow --auto could not self-approve"

_NO_COMMENT = "(no open-question comment)"


def _open_question(comments: list[Any]) -> str:
    if not comments:
        return _NO_COMMENT
    ordered = sorted(comments, key=lambda c: str(c.get("created_at", "")))
    chosen: dict[str, Any] | None = None
    for c in ordered:
        body = c.get("body") or {}
        text = body.get("body", "") if isinstance(body, dict) else str(body)
        if _DEFER_STEM in text:
            chosen = c
    if chosen is None:
        chosen = ordered[-1]
    body = chosen.get("body") or {}
    return body.get("body", "") if isinstance(body, dict) else str(body)


def collect(config: dict[str, Any], *, runner: Any = None) -> list[dict[str, Any]]:
    adapter = BeadsAdapter(config, runner=runner)
    raw = adapter._run_json(["list", "--status", "deferred"])
    items = (
        raw if isinstance(raw, list) else (raw.get("issues", []) if isinstance(raw, dict) else [])
    )
    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("id", ""))
        ticket = adapter.get(key)
        rows.append(
            {
                "key": key,
                "title": str(item.get("title", "")),
                "open_question": _open_question(ticket.get("comments") or []),
            }
        )
    rows.sort(key=lambda r: r["key"])
    return rows


def _truncate(text: str, width: int = 80) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= width else one_line[: width - 1] + "…"


def render_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no deferred tickets)"
    headers = ["KEY", "TITLE", "OPEN QUESTION"]
    table = [headers]
    for r in rows:
        table.append(
            [
                str(r["key"]),
                _truncate(str(r["title"]), 40),
                _truncate(str(r["open_question"])),
            ]
        )
    widths = [max(len(row[i]) for row in table) for i in range(len(headers))]
    return "\n".join(
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in table
    )


def cli_main(argv: list[str], runner: Any = None) -> int:
    parser = argparse.ArgumentParser(description="/flow triage: list deferred beads.")
    parser.add_argument("--workspace-root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    workspace_root = Path(args.workspace_root).expanduser().resolve()
    if not (workspace_root / ".flow").is_dir():
        sys.stderr.write("triage: workspace not initialized; run `/flow init`\n")
        return 1
    try:
        config = _read_tracker_config(workspace_root)
    except _WorkspaceConfigError as exc:
        sys.stderr.write(f"triage: {exc}\n")
        return 2
    if config["backend"] != "beads":
        sys.stdout.write("deferred is a beads concept; nothing to triage\n")
        return 0
    rows = collect(config, runner=runner)
    if args.json:
        sys.stdout.write(json.dumps(rows, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_table(rows) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["cli_main", "collect", "render_table"]
