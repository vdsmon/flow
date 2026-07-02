"""Read-only miner for a flow-dogfood run transcript (`~/.claude` session JSONL).

The `extract` subcommand reads one finished transcript and emits a single
structured-JSON object of per-stage events: tool errors, silent retries, drift
markers, and stall gaps, bucketed by dispatch-stage boundaries (child-1 of
flow-eia3). Pure extractor: it never clusters, never reads `friction.jsonl`,
never files beads, never calls `claude agents --json`. Filesystem-scan only.

Self-target guard: a `--transcript` is accepted only under this workspace's
own `~/.claude/projects/<slug>/` tree (or a worktree-variant sibling), so the
tool can never be pointed at another project's telemetry.

Exit codes:
  0 = ok (including a valid eventless transcript -> events: []).
  1 = bad args (neither/both of --transcript/--session given).
  3 = transcript missing/unreadable.
  4 = self-target guard rejection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from _timeutil import parse_iso

DEFAULT_STALL_THRESHOLD_SECS = 300
_PRE_DISPATCH = "<pre-dispatch>"
_DESCRIPTOR_KEYS = frozenset({"stage", "done", "head_sha", "handler_type"})
_DRIFT_MARKERS = ("reconciled_drift", "state_recovered_from_backup", "engine_reanchored")
_WORKTREE_MARKER = "/.flow/worktrees/"
_BODY_SNIPPET_LEN = 500


class _SelfTargetRejected(Exception):
    """--transcript resolves outside this workspace's own project tree."""


# ─── lenient reader (transcripts are foreign; never rewritten) ──────────────


def _lenient_jsonl(path: Path) -> list[dict[str, Any]]:
    """Per-line json.loads, skipping blank/malformed lines and non-object rows.

    Modeled on reflect_inputs.py's `_lenient_jsonl`: read-only, no quarantine
    sidecar. A transcript is foreign input; this never rewrites it.
    """
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


# ─── self-target guard ───────────────────────────────────────────────────────


def _slugify(path: Path) -> str:
    """Flatten an absolute path into its `~/.claude/projects/` dir-name slug."""
    return str(path).replace("/", "-").replace(".", "-")


def _accepted_project_dirs(projects_root: Path, workspace_root: Path) -> list[Path]:
    """Project dirs a self-target transcript for workspace_root may live under.

    Own slug dir, plus a `<slug>--flow-worktrees-*` sibling (covers a worktree
    run's transcript when workspace_root is the repo root). When workspace_root
    is itself a worktree (`<repo>/.flow/worktrees/<name>`), also accept the
    parent repo's slug dir and its sibling pattern: an in-worktree /flow session
    often keeps its transcript filed under the main repo's project dir.
    """
    roots = [workspace_root]
    idx = str(workspace_root).find(_WORKTREE_MARKER)
    if idx != -1:
        roots.append(Path(str(workspace_root)[:idx]))

    dirs: list[Path] = []
    for root in roots:
        slug = _slugify(root)
        dirs.append(projects_root / slug)
        dirs.extend(sorted(projects_root.glob(f"{slug}--flow-worktrees-*")))
    return dirs


def _check_self_target(transcript: Path, projects_root: Path, workspace_root: Path) -> None:
    resolved = transcript.resolve()
    for candidate in _accepted_project_dirs(projects_root, workspace_root):
        if resolved.is_relative_to(candidate.resolve()):
            return
    raise _SelfTargetRejected(
        f"{transcript} is not a self-target transcript under {projects_root} "
        f"for workspace-root {workspace_root}"
    )


# ─── event construction ──────────────────────────────────────────────────────


def _make_id(kind: str, discriminator: str, ts: str) -> str:
    digest = hashlib.sha256(f"{kind}|{discriminator}|{ts}".encode()).hexdigest()
    return digest[:12]


def _snippet(text: str) -> str:
    text = text.strip()
    if len(text) <= _BODY_SNIPPET_LEN:
        return text
    return text[:_BODY_SNIPPET_LEN] + "…"


def _new_event(
    kind: str,
    discriminator: str,
    ts: str | None,
    stage: str,
    run_id: str,
    ticket: str,
    body: str,
    *,
    detail: str | None = None,
    git_branch: str = "",
    session_id: str = "",
    **provenance: Any,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "id": _make_id(kind, discriminator, ts or ""),
        "ts": ts or "",
        "run_id": run_id,
        "ticket": ticket,
        "stage": stage,
        "kind": kind,
        "body": body,
        "git_branch": git_branch,
        "session_id": session_id,
    }
    if detail:
        event["detail"] = detail
    for key, value in provenance.items():
        if value is not None:
            event[key] = value
    return event


def _message_content(obj: dict[str, Any]) -> list[Any]:
    message = obj.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    return content if isinstance(content, list) else []


def _content_text(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = [
            block["text"]
            for block in raw
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        return "\n".join(parts)
    return ""


def _strip_tool_use_error(text: str) -> str:
    return text.replace("<tool_use_error>", "").replace("</tool_use_error>", "")


def _try_parse_json_object(text: str) -> dict[str, Any] | None:
    """Second json.loads of a tool_result's own text, for a dispatch descriptor
    riding inside it. The descriptor is pretty-printed JSON that may follow a
    stderr line (e.g. `dispatch: auto-reconciled ...\\n{...}`); find the first
    `{` and decode from there, tolerating trailing whitespace after the object.
    """
    idx = text.find("{")
    if idx == -1:
        return None
    try:
        parsed, _end = json.JSONDecoder().raw_decode(text, idx)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _looks_like_descriptor(parsed: dict[str, Any]) -> bool:
    return parsed.keys() >= _DESCRIPTOR_KEYS


# ─── the four extractors + stage bucketing (single top-to-bottom pass) ─────


@dataclass
class _Cursor:
    """Mutable walk state threaded through the per-line-type handlers below."""

    id_to_name: dict[str, str] = field(default_factory=dict)
    stage: str = _PRE_DISPATCH
    run_id: str = ""
    ticket: str = ""
    stage_order: list[str] = field(default_factory=list)
    session_id: str = ""
    last_ts_dt: datetime | None = None
    last_ts_raw: str | None = None


def _record_gap(
    cursor: _Cursor,
    i: int,
    ts_raw: str | None,
    ts_dt: datetime | None,
    git_branch: str,
    session_id: str,
    stall_threshold_secs: int,
) -> dict[str, Any] | None:
    """Emit a stall_gap when this line's timestamp exceeds the threshold past the
    last one seen, then advance the cursor's last-timestamp regardless.
    """
    event: dict[str, Any] | None = None
    if ts_dt is not None and cursor.last_ts_dt is not None:
        gap = (ts_dt - cursor.last_ts_dt).total_seconds()
        if gap > stall_threshold_secs:
            event = _new_event(
                "stall_gap",
                str(i),
                ts_raw,
                cursor.stage,
                cursor.run_id,
                cursor.ticket,
                f"stall gap of {gap:.1f}s",
                git_branch=git_branch,
                session_id=session_id,
                gap_secs=gap,
                gap_start_ts=cursor.last_ts_raw,
                gap_end_ts=ts_raw,
            )
    if ts_dt is not None:
        cursor.last_ts_dt = ts_dt
        cursor.last_ts_raw = ts_raw
    return event


def _handle_assistant_line(obj: dict[str, Any], cursor: _Cursor) -> None:
    for block in _message_content(obj):
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tool_id = block.get("id")
            if isinstance(tool_id, str):
                cursor.id_to_name[tool_id] = block.get("name") or ""


def _handle_descriptor(
    parsed: dict[str, Any],
    i: int,
    ts_raw: str | None,
    cursor: _Cursor,
    git_branch: str,
    session_id: str,
) -> list[dict[str, Any]]:
    """Advance the stage cursor off a dispatch descriptor and emit its drift
    markers, tagged with the NEW stage (the descriptor's own `stage`): a marker
    riding a `next`/`advance` payload belongs to the stage being entered, per
    dispatch_stage.py's drift-gate-before-dispatch ordering.
    """
    new_stage = parsed.get("stage")
    if isinstance(new_stage, str) and new_stage:
        if not cursor.stage_order or cursor.stage_order[-1] != new_stage:
            cursor.stage_order.append(new_stage)
        cursor.stage = new_stage
    ticket_dir = parsed.get("ticket_dir")
    if not cursor.ticket and isinstance(ticket_dir, str) and ticket_dir:
        cursor.ticket = Path(ticket_dir).name

    events: list[dict[str, Any]] = []
    for marker in _DRIFT_MARKERS:
        if marker not in parsed:
            continue
        events.append(
            _new_event(
                "drift",
                f"{i}:{marker}",
                ts_raw,
                cursor.stage,
                cursor.run_id,
                cursor.ticket,
                f"{marker}={parsed[marker]}",
                detail=marker,
                git_branch=git_branch,
                session_id=session_id,
            )
        )
    return events


def _handle_user_line(
    obj: dict[str, Any],
    i: int,
    ts_raw: str | None,
    cursor: _Cursor,
    git_branch: str,
    session_id: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in _message_content(obj):
        if not (isinstance(block, dict) and block.get("type") == "tool_result"):
            continue
        tool_use_id = block.get("tool_use_id") or ""
        text = _content_text(block.get("content"))

        if block.get("is_error"):
            name = cursor.id_to_name.get(tool_use_id, "")
            events.append(
                _new_event(
                    "tool_error",
                    tool_use_id or str(i),
                    ts_raw,
                    cursor.stage,
                    cursor.run_id,
                    cursor.ticket,
                    _snippet(_strip_tool_use_error(text)),
                    detail=name,
                    git_branch=git_branch,
                    session_id=session_id,
                    tool_use_id=tool_use_id or None,
                )
            )

        parsed = _try_parse_json_object(text)
        if not isinstance(parsed, dict):
            continue
        run_id_val = parsed.get("run_id")
        if isinstance(run_id_val, str) and run_id_val:
            cursor.run_id = run_id_val
        ticket_val = parsed.get("ticket")
        if isinstance(ticket_val, str) and ticket_val:
            cursor.ticket = ticket_val

        if _looks_like_descriptor(parsed):
            events.extend(_handle_descriptor(parsed, i, ts_raw, cursor, git_branch, session_id))
    return events


def _handle_system_line(
    obj: dict[str, Any],
    i: int,
    ts_raw: str | None,
    cursor: _Cursor,
    git_branch: str,
    session_id: str,
) -> dict[str, Any] | None:
    # C1: gate on the PRESENCE of retryAttempt alone -- a subtype rename
    # (subtype:"api_error") must not zero-match this predicate.
    if "retryAttempt" not in obj:
        return None
    error = obj.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    detail = json.dumps(
        {
            "subtype": obj.get("subtype"),
            "message": message,
            "max_retries": obj.get("maxRetries"),
            "retry_in_ms": obj.get("retryInMs"),
        },
        sort_keys=True,
    )
    return _new_event(
        "silent_retry",
        str(i),
        ts_raw,
        cursor.stage,
        cursor.run_id,
        cursor.ticket,
        message or f"silent retry (attempt {obj.get('retryAttempt')})",
        detail=detail,
        git_branch=git_branch,
        session_id=session_id,
    )


def extract_events(
    lines: list[dict[str, Any]],
    *,
    stall_threshold_secs: int = DEFAULT_STALL_THRESHOLD_SECS,
) -> tuple[list[dict[str, Any]], list[str], str]:
    """Walk parsed transcript lines in file order. Returns (events, stage_order,
    session_id). Events before the first dispatch descriptor bucket into the
    `<pre-dispatch>` sentinel stage; main-thread and `isSidechain` lines are
    bucketed identically off the same current-stage cursor.
    """
    cursor = _Cursor()
    events: list[dict[str, Any]] = []

    for i, obj in enumerate(lines):
        line_session = obj.get("sessionId")
        if isinstance(line_session, str) and line_session and not cursor.session_id:
            cursor.session_id = line_session
        git_branch = obj.get("gitBranch") or ""
        event_session = (
            line_session if isinstance(line_session, str) and line_session else cursor.session_id
        )

        ts_raw = obj.get("timestamp")
        ts_dt = parse_iso(ts_raw) if isinstance(ts_raw, str) else None

        gap_event = _record_gap(
            cursor, i, ts_raw, ts_dt, git_branch, event_session, stall_threshold_secs
        )
        if gap_event is not None:
            events.append(gap_event)

        line_type = obj.get("type")
        if line_type == "assistant":
            _handle_assistant_line(obj, cursor)
        elif line_type == "user":
            events.extend(_handle_user_line(obj, i, ts_raw, cursor, git_branch, event_session))
        elif line_type == "system":
            retry_event = _handle_system_line(obj, i, ts_raw, cursor, git_branch, event_session)
            if retry_event is not None:
                events.append(retry_event)

    return events, cursor.stage_order, cursor.session_id


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mine a flow-dogfood run transcript.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "extract",
        help="Extract tool-error/silent-retry/drift/stall-gap events from one transcript.",
    )
    p.add_argument("--transcript", default=None, help="Path to a session JSONL transcript.")
    p.add_argument(
        "--session",
        default=None,
        help="Session UUID; resolved under --projects-root/<slug>/<uuid>.jsonl.",
    )
    p.add_argument("--workspace-root", default=".")
    p.add_argument("--projects-root", default=None, help="override (default ~/.claude/projects).")
    p.add_argument("--stall-threshold-secs", type=int, default=DEFAULT_STALL_THRESHOLD_SECS)
    return parser.parse_args(argv)


def _run_extract(args: argparse.Namespace) -> int:
    if bool(args.transcript) == bool(args.session):
        sys.stderr.write("trace-mine: exactly one of --transcript or --session is required\n")
        return 1

    workspace_root = Path(args.workspace_root).resolve()
    projects_root = (
        Path(args.projects_root).expanduser().resolve()
        if args.projects_root
        else Path.home() / ".claude" / "projects"
    )

    if args.session:
        transcript = projects_root / _slugify(workspace_root) / f"{args.session}.jsonl"
    else:
        transcript = Path(args.transcript).expanduser()

    try:
        _check_self_target(transcript, projects_root, workspace_root)
    except _SelfTargetRejected as exc:
        sys.stderr.write(f"trace-mine: {exc}\n")
        return 4

    if not transcript.is_file():
        sys.stderr.write(f"trace-mine: transcript not found or unreadable: {transcript}\n")
        return 3

    try:
        lines = _lenient_jsonl(transcript)
    except OSError as exc:
        sys.stderr.write(f"trace-mine: I/O error reading transcript: {exc}\n")
        return 3

    events, stage_order, session_id = extract_events(
        lines, stall_threshold_secs=args.stall_threshold_secs
    )
    payload = {
        "transcript": str(transcript),
        "session_id": session_id or transcript.stem,
        "stage_order": stage_order,
        "events": events,
    }
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    if args.command == "extract":
        return _run_extract(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["cli_main", "extract_events"]
