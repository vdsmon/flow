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
    (per-stage wall clock lives there, not in the driver transcript), each span carrying
    the subagent's own facade calls and tool errors mined by the same rules;
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
                yield line, json.loads(line)
            except ValueError:
                continue


def _content_list(event: dict[str, Any]) -> list[Any]:
    content = (event.get("message") or {}).get("content")
    return content if isinstance(content, list) else []


def _record_tool_item(
    item: dict[str, Any],
    stamp: str,
    in_window: bool,
    pending: dict[str, tuple[str, str, str]],
    flow_calls: list[dict[str, Any]],
    tool_errors: list[dict[str, Any]],
    agent_spawns: list[dict[str, Any]] | None = None,
) -> None:
    """One tool_use/tool_result content item into the shared signal shapes.

    Used by the driver pass and the per-subagent pass so stage-side facade calls and
    errors mine identically; span-only subagent records left the env lens unable to
    verify the pre-e2e credential probe (2026-08-04 sweep)."""
    kind = item.get("type")
    if kind == "tool_use":
        name = item.get("name") or ""
        tool_input = item.get("input") or {}
        snippet = ""
        if name == "Bash":
            snippet = str(tool_input.get("command") or "")
            match = _FLOW_CALL.search(snippet)
            if match and in_window:
                flow_calls.append(
                    {"ts": stamp, "sub": match.group(1), "command": _short(snippet, 140)}
                )
        elif name in ("Task", "Agent"):
            snippet = str(tool_input.get("description") or "")
            if agent_spawns is not None and in_window:
                agent_spawns.append(
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
            return
        body = item.get("content")
        if isinstance(body, list):
            body = " ".join(str(part.get("text", "")) for part in body if isinstance(part, dict))
        tool_errors.append(
            {
                "ts": stamp or spawn_ts,
                "tool": name,
                "command": snippet,
                "error": _short(str(body), 240),
            }
        )


def _mine_subagents(session_dir: Path, *, since: str | None = None) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    sub_dir = session_dir / "subagents"
    if not sub_dir.is_dir():
        return spans
    for transcript in sorted(sub_dir.glob("agent-*.jsonl")):
        first = last = None
        lines = 0
        pending: dict[str, tuple[str, str, str]] = {}
        flow_calls: list[dict[str, Any]] = []
        tool_errors: list[dict[str, Any]] = []
        for _line, event in _iter_events(transcript):
            lines += 1
            stamp = event.get("timestamp")
            if stamp:
                first = first or stamp
                last = stamp
            in_window = not since or not stamp or stamp >= since
            for item in _content_list(event):
                if isinstance(item, dict):
                    _record_tool_item(
                        item, stamp or "", in_window, pending, flow_calls, tool_errors
                    )
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
                "flow_calls": flow_calls,
                "tool_errors": tool_errors,
            }
        )
    return spans


def mine_session(
    path: Path, *, since: str | None = None, seen: set[int] | None = None
) -> dict[str, Any]:
    """One incremental pass over a session transcript; returns the signal families.

    `seen` dedups byte-identical lines across files: a forked session's transcript
    repeats its parent's full prefix verbatim (witnessed 2026-08-06: two brinta files
    sharing a 693-line, 10-error prefix under one embedded sessionId, inflating the
    census 27 errors vs 17 unique). A repeated line still feeds `pending` so tool
    use/result joins survive the fork boundary, but contributes no signals and no
    span, so a shared line counts once, attributed to whichever file is mined first."""
    report: dict[str, Any] = {
        "session": path.stem,
        "first": None,
        "last": None,
        "lines": 0,
        "shared_prefix_lines": 0,
        "user_messages": [],
        "flow_calls": [],
        "agent_spawns": [],
        "tool_errors": [],
    }
    pending: dict[str, tuple[str, str, str]] = {}
    for line, event in _iter_events(path):
        report["lines"] += 1
        duplicate = False
        if seen is not None:
            key = hash(line)
            duplicate = key in seen
            seen.add(key)
            if duplicate:
                report["shared_prefix_lines"] += 1
        stamp = event.get("timestamp") or ""
        if stamp and not duplicate:
            report["first"] = report["first"] or stamp
            report["last"] = stamp
        in_window = (not since or not stamp or stamp >= since) and not duplicate
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
            else:
                _record_tool_item(
                    item,
                    stamp,
                    in_window,
                    pending,
                    report["flow_calls"],
                    report["tool_errors"],
                    agent_spawns=report["agent_spawns"],
                )
    report["subagents"] = _mine_subagents(path.parent / path.stem, since=since)
    return report


def mine_dir(
    transcript_dir: Path, *, since: str | None = None, sessions: list[str] | None = None
) -> list[dict[str, Any]]:
    """Mine every session transcript in the window, newest last."""
    wanted = set(sessions or [])
    reports = []
    seen: set[int] = set()
    for path in sorted(transcript_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime):
        if wanted and path.stem not in wanted:
            continue
        report = mine_session(path, since=since, seen=seen)
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
