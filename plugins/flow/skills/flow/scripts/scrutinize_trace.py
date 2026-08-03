"""Transcript trace miner for the scrutinize sweep.

Library + thin CLI behind the `scrutinize-trace` facade command. The seat's sweep reads
session transcripts under `~/.claude/projects/<workspace-slug>/` for the performance and
nudge lenses; before this script every seat rebuilt the same ad-hoc miner from scratch
(witnessed 2026-08-03: a fresh ~90-line one-off, doing what the previous sweep's one-off
did). This is that miner, made durable: one incremental pass per session file, never
loading a transcript whole, emitting the four signal families the charter's lenses read:

  - the dispatch spine: facade calls (`.flow/runtime/flow <sub>`) with timestamps;
  - human messages (the nudge lens reads mid-run ones; Skill invocations render as user
    turns too, so each message carries an `is_skill` flag rather than being dropped);
  - agent spawns, joined against `<session>/subagents/agent-*.jsonl` spans when present
    (per-stage wall clock lives there, not in the driver transcript);
  - tool errors (`is_error` results), with the originating command snippet.

Read-only over files outside any workspace; writes nothing anywhere.

CLI:
  scrutinize_trace.py --transcript-dir <dir> [--since <iso>] [--session <id>]... [--json]

Exit codes:
  0 = mined (possibly zero sessions in window)
  2 = transcript dir missing or unreadable
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_FLOW_CALL = re.compile(
    r"\.flow/runtime/flow[\"']?\s+(?:--workspace-root\s+\S+\s+)?([a-z][a-z0-9-]*)"
)
_WS = re.compile(r"\s+")

_SKILL_MARKERS = ("<command-message>", "Base directory for this skill:")


def _short(text: str, limit: int) -> str:
    return _WS.sub(" ", text or "").strip()[:limit]


def _iter_events(path: Path):
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _content_list(event: dict[str, Any]) -> list[Any]:
    content = (event.get("message") or {}).get("content")
    return content if isinstance(content, list) else []


def _mine_subagents(session_dir: Path) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    sub_dir = session_dir / "subagents"
    if not sub_dir.is_dir():
        return spans
    for transcript in sorted(sub_dir.glob("agent-*.jsonl")):
        first = last = None
        lines = 0
        for event in _iter_events(transcript):
            lines += 1
            stamp = event.get("timestamp")
            if stamp:
                first = first or stamp
                last = stamp
        description = ""
        meta_path = transcript.with_name(transcript.name.replace(".jsonl", ".meta.json"))
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                description = _short(str(meta.get("description") or ""), 80)
            except ValueError:
                pass
        spans.append(
            {
                "agent": transcript.stem,
                "description": description,
                "first": first,
                "last": last,
                "lines": lines,
            }
        )
    return spans


def mine_session(path: Path, *, since: str | None = None) -> dict[str, Any]:
    """One incremental pass over a session transcript; returns the signal families."""
    report: dict[str, Any] = {
        "session": path.stem,
        "first": None,
        "last": None,
        "lines": 0,
        "user_messages": [],
        "flow_calls": [],
        "agent_spawns": [],
        "tool_errors": [],
    }
    pending: dict[str, tuple[str, str, str]] = {}
    for event in _iter_events(path):
        report["lines"] += 1
        stamp = event.get("timestamp") or ""
        if stamp:
            report["first"] = report["first"] or stamp
            report["last"] = stamp
        in_window = not since or not stamp or stamp >= since
        is_user = event.get("type") == "user"
        raw_content = (event.get("message") or {}).get("content")
        if is_user and in_window and isinstance(raw_content, str) and raw_content.strip():
            report["user_messages"].append(
                {
                    "ts": stamp,
                    "is_skill": any(marker in raw_content[:200] for marker in _SKILL_MARKERS),
                    "text": _short(raw_content, 200),
                }
            )
        for item in _content_list(event):
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "text" and is_user and in_window:
                text = item.get("text") or ""
                report["user_messages"].append(
                    {
                        "ts": stamp,
                        "is_skill": any(marker in text[:200] for marker in _SKILL_MARKERS),
                        "text": _short(text, 200),
                    }
                )
            elif kind == "tool_use":
                name = item.get("name") or ""
                tool_input = item.get("input") or {}
                snippet = ""
                if name == "Bash":
                    snippet = str(tool_input.get("command") or "")
                    match = _FLOW_CALL.search(snippet)
                    if match and in_window:
                        report["flow_calls"].append(
                            {"ts": stamp, "sub": match.group(1), "command": _short(snippet, 140)}
                        )
                elif name in ("Task", "Agent"):
                    snippet = str(tool_input.get("description") or "")
                    if in_window:
                        report["agent_spawns"].append(
                            {
                                "ts": stamp,
                                "type": str(tool_input.get("subagent_type") or ""),
                                "description": _short(snippet, 80),
                            }
                        )
                else:
                    snippet = json.dumps(tool_input, default=str)
                pending[str(item.get("id"))] = (stamp, name, _short(snippet, 140))
            elif kind == "tool_result" and item.get("is_error"):
                spawn_ts, name, snippet = pending.get(str(item.get("tool_use_id")), ("", "?", ""))
                if not in_window:
                    continue
                body = item.get("content")
                if isinstance(body, list):
                    body = " ".join(
                        str(part.get("text", "")) for part in body if isinstance(part, dict)
                    )
                report["tool_errors"].append(
                    {
                        "ts": stamp or spawn_ts,
                        "tool": name,
                        "command": snippet,
                        "error": _short(str(body), 240),
                    }
                )
    report["subagents"] = _mine_subagents(path.parent / path.stem)
    return report


def mine_dir(
    transcript_dir: Path, *, since: str | None = None, sessions: list[str] | None = None
) -> list[dict[str, Any]]:
    """Mine every session transcript in the window, newest last."""
    wanted = set(sessions or [])
    reports = []
    for path in sorted(transcript_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime):
        if wanted and path.stem not in wanted:
            continue
        report = mine_session(path, since=since)
        if since and report["last"] and report["last"] < since:
            continue
        reports.append(report)
    return reports


def _render(reports: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for report in reports:
        lines.append(
            f"== {report['session']} span={report['first']} .. {report['last']}"
            f" lines={report['lines']}"
        )
        for label, key, fields in (
            ("user", "user_messages", ("ts", "is_skill", "text")),
            ("flow", "flow_calls", ("ts", "sub", "command")),
            ("agent", "agent_spawns", ("ts", "type", "description")),
            ("error", "tool_errors", ("ts", "tool", "error")),
            ("span", "subagents", ("first", "last", "description")),
        ):
            for row in report.get(key, []):
                rendered = " | ".join(str(row.get(field, "")) for field in fields)
                lines.append(f"  {label}: {rendered}")
    return "\n".join(lines)


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Mine session transcripts for the sweep.")
    parser.add_argument("--transcript-dir", required=True)
    parser.add_argument("--since", default=None, help="ISO lower bound, e.g. the sweep cursor")
    parser.add_argument(
        "--session", action="append", default=None, help="limit to this session id (repeatable)"
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    transcript_dir = Path(args.transcript_dir).expanduser()
    if not transcript_dir.is_dir():
        print(f"scrutinize-trace: no such transcript dir: {transcript_dir}", file=sys.stderr)
        return 2
    reports = mine_dir(transcript_dir, since=args.since, sessions=args.session)
    if args.json:
        print(json.dumps(reports, indent=2))
    else:
        print(_render(reports))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["cli_main", "mine_dir", "mine_session"]
