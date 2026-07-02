"""Read-only miner for a flow-dogfood run transcript (`~/.claude` session JSONL).

The `extract` subcommand reads one finished transcript and emits a single
structured-JSON object of per-stage events: tool errors, silent retries, drift
markers, and stall gaps, bucketed by dispatch-stage boundaries (child-1 of
flow-eia3). Pure extractor: it never clusters, never reads `friction.jsonl`,
never files beads, never calls `claude agents --json`. Filesystem-scan only.

Run-window scoping: a real session file spans many runs across days. `extract`
requires a `--ticket` and clips output to that ticket's run window: from the
run's `/flow` intent invocation (or, headless, the contiguous same-branch
prefix) through its last dispatch activity, bounded above by the next run's
intent. Events on a foreign git branch inside that window are dropped. A
transcript carrying no dispatch activity for `--ticket` exits 5 rather than
emitting unattributable events.

Self-target guard: a `--transcript` is accepted only under the project tree of
the CLAIMED `--workspace-root` (`~/.claude/projects/<slug>/`, or a worktree
sibling). That root is caller-controlled, so the guard blocks an accidental
cross-project read, not a determined one.

Known limitation: a dispatch descriptor re-emitted inside an `isMeta` text
block (seen on resumed runs) is not parsed; only tool_result-carried
descriptors bound stages.

Exit codes:
  0 = ok (including a valid eventless run -> events: []).
  1 = bad args (neither/both of --transcript/--session given).
  3 = transcript missing/unreadable.
  4 = self-target guard rejection.
  5 = no dispatch activity for --ticket in the transcript.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
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
_JSON_OBJECT_SCAN_LIMIT = 10
_FLOW_COMMAND_RE = re.compile(r"<command-name>[^<]*flow", re.IGNORECASE)


class _SelfTargetRejected(Exception):
    """--transcript resolves outside this workspace's own project tree."""


class _NoDispatchActivity(Exception):
    """No dispatch descriptor for the target ticket appears in the transcript."""


# ─── lenient reader (transcripts are foreign; never rewritten) ──────────────


def _lenient_jsonl(path: Path) -> list[dict[str, Any]]:
    """Per-line json.loads, skipping blank/malformed lines and non-object rows.

    Modeled on reflect_inputs.py's `_lenient_jsonl`: read-only, no quarantine
    sidecar. A transcript is foreign input; this never rewrites it. `errors=
    "replace"` keeps the lenient promise on a stray bad byte (a raised
    UnicodeDecodeError would escape the caller's OSError-only guard).
    """
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
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
    stderr line (e.g. `dispatch: auto-reconciled ...\\n{...}`). Scan successive
    `{` offsets (bounded) until one decodes to an object, so a stray brace in a
    leading stderr blob doesn't drop the boundary; trailing text after the
    object is tolerated.
    """
    decoder = json.JSONDecoder()
    idx = text.find("{")
    scanned = 0
    while idx != -1 and scanned < _JSON_OBJECT_SCAN_LIMIT:
        try:
            parsed, _end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        idx = text.find("{", idx + 1)
        scanned += 1
    return None


def _looks_like_descriptor(parsed: dict[str, Any]) -> bool:
    return parsed.keys() >= _DESCRIPTOR_KEYS


def _descriptor_ticket(parsed: dict[str, Any]) -> str | None:
    """The ticket a dispatch descriptor belongs to: `ticket_dir`'s basename
    (the reliable field in live descriptors), falling back to a `ticket` key.
    """
    ticket_dir = parsed.get("ticket_dir")
    if isinstance(ticket_dir, str) and ticket_dir:
        return Path(ticket_dir).name
    ticket = parsed.get("ticket")
    if isinstance(ticket, str) and ticket:
        return ticket
    return None


# ─── run-window scoping ──────────────────────────────────────────────────────


@dataclass
class _Window:
    """The target run's slice of a multi-run transcript (end is exclusive)."""

    start: int
    end: int
    allowed_branches: frozenset[str]


def _line_descriptor(obj: dict[str, Any]) -> dict[str, Any] | None:
    """The dispatch descriptor carried by a user tool_result line, else None."""
    if not isinstance(obj, dict) or obj.get("type") != "user":
        return None
    for block in _message_content(obj):
        if isinstance(block, dict) and block.get("type") == "tool_result":
            parsed = _try_parse_json_object(_content_text(block.get("content")))
            if isinstance(parsed, dict) and _looks_like_descriptor(parsed):
                return parsed
    return None


def _is_flow_command(obj: dict[str, Any]) -> bool:
    """True for a `/flow` slash-command invocation line (a run delimiter)."""
    if not isinstance(obj, dict) or obj.get("type") != "user":
        return False
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return isinstance(content, str) and bool(_FLOW_COMMAND_RE.search(content))


def _contiguous_prefix_start(
    lines: list[dict[str, Any]], first_dispatch: int, run_branches: set[str]
) -> int:
    """Headless-run fallback (no `/flow` intent line): the earliest index of the
    contiguous run-branch (or branchless) block ending just before the first
    dispatch, so a day-old preamble on a foreign branch is not swept in.
    """
    start = first_dispatch
    for i in range(first_dispatch - 1, -1, -1):
        obj = lines[i]
        branch = obj.get("gitBranch") if isinstance(obj, dict) else None
        if isinstance(branch, str) and branch and branch not in run_branches:
            break
        start = i
    return start


def _derive_window(lines: list[dict[str, Any]], ticket: str) -> _Window | None:
    """Scope the transcript to the run that dispatched `ticket`.

    Runs are delimited by `/flow` intent lines: the target run spans from the
    intent preceding its first dispatch descriptor up to the next intent (which
    starts the following run), so neither a prior run's tail nor the next run's
    bootstrap on a shared branch leaks in. Same-ticket-twice scopes to the run
    holding the FIRST target descriptor. Returns None when no descriptor names
    the target ticket.
    """
    intents = [i for i, obj in enumerate(lines) if _is_flow_command(obj)]
    descriptors = [
        (i, _descriptor_ticket(desc))
        for i, obj in enumerate(lines)
        if (desc := _line_descriptor(obj)) is not None
    ]
    target = [i for i, tk in descriptors if tk == ticket]
    if not target:
        return None
    first_dispatch = target[0]

    run_branches = {
        branch
        for i in target
        for branch in [lines[i].get("gitBranch")]
        if isinstance(branch, str) and branch
    }
    allowed = set(run_branches)

    prior_intents = [j for j in intents if j <= first_dispatch]
    if prior_intents:
        start = prior_intents[-1]
        branch = lines[start].get("gitBranch")
        if isinstance(branch, str) and branch:
            allowed.add(branch)
    else:
        start = _contiguous_prefix_start(lines, first_dispatch, run_branches)

    later_intents = [j for j in intents if j > first_dispatch]
    span_end = later_intents[0] if later_intents else len(lines)

    in_span = [i for i in target if start <= i < span_end]
    last_dispatch = max(in_span) if in_span else first_dispatch
    foreign_after = [i for i, tk in descriptors if last_dispatch < i < span_end and tk != ticket]
    end = foreign_after[0] if foreign_after else span_end

    return _Window(start=start, end=end, allowed_branches=frozenset(allowed))


# ─── the four extractors + stage bucketing (single top-to-bottom pass) ─────


@dataclass
class _Cursor:
    """Mutable walk state threaded through the per-line-type handlers below."""

    ticket: str = ""
    id_to_name: dict[str, str] = field(default_factory=dict)
    id_to_start_ts: dict[str, datetime] = field(default_factory=dict)
    stage: str = _PRE_DISPATCH
    run_id: str = ""
    stage_order: list[str] = field(default_factory=list)
    session_id: str = ""
    last_ts_dt: datetime | None = None
    last_ts_raw: str | None = None


def _spans_open_tool_use(
    obj: dict[str, Any], id_to_start_ts: dict[str, datetime], gap_start_dt: datetime | None
) -> bool:
    """True when this user line closes a tool_use that was already in flight at
    the gap start: a long op (subagent Task, ~30min merge-stage CI wait) covered
    the whole gap, so the dead-air reading is spurious.
    """
    if gap_start_dt is None or obj.get("type") != "user":
        return False
    for block in _message_content(obj):
        if isinstance(block, dict) and block.get("type") == "tool_result":
            start = id_to_start_ts.get(block.get("tool_use_id") or "")
            if start is not None and start <= gap_start_dt:
                return True
    return False


def _record_gap(
    cursor: _Cursor,
    i: int,
    ts_raw: str | None,
    ts_dt: datetime | None,
    git_branch: str,
    session_id: str,
    stall_threshold_secs: int,
    suppress: bool,
) -> dict[str, Any] | None:
    """Emit a stall_gap when this line's timestamp exceeds the threshold past the
    last one seen, then advance the cursor's last-timestamp regardless. A gap a
    long in-flight tool op spanned (`suppress`) advances the clock without
    emitting.
    """
    event: dict[str, Any] | None = None
    if not suppress and ts_dt is not None and cursor.last_ts_dt is not None:
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


def _handle_assistant_line(obj: dict[str, Any], cursor: _Cursor, ts_dt: datetime | None) -> None:
    for block in _message_content(obj):
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tool_id = block.get("id")
            if isinstance(tool_id, str):
                cursor.id_to_name[tool_id] = block.get("name") or ""
                if ts_dt is not None:
                    cursor.id_to_start_ts[tool_id] = ts_dt


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
    target_ticket: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in _message_content(obj):
        if not (isinstance(block, dict) and block.get("type") == "tool_result"):
            continue
        tool_use_id = block.get("tool_use_id") or ""
        cursor.id_to_start_ts.pop(tool_use_id, None)
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

        if _looks_like_descriptor(parsed) and _descriptor_ticket(parsed) in (None, target_ticket):
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
    ticket: str,
    *,
    stall_threshold_secs: int = DEFAULT_STALL_THRESHOLD_SECS,
) -> tuple[list[dict[str, Any]], list[str], str]:
    """Walk the target run's window of the transcript in file order. Returns
    (events, stage_order, session_id). Events before the first dispatch
    descriptor bucket into the `<pre-dispatch>` sentinel stage; main-thread and
    `isSidechain` lines are bucketed off the same current-stage cursor. Raises
    `_NoDispatchActivity` when no descriptor names `ticket`.
    """
    window = _derive_window(lines, ticket)
    if window is None:
        raise _NoDispatchActivity(ticket)

    cursor = _Cursor(ticket=ticket)
    events: list[dict[str, Any]] = []

    for i in range(window.start, window.end):
        obj = lines[i]
        if not isinstance(obj, dict):
            continue

        line_session = obj.get("sessionId")
        if isinstance(line_session, str) and line_session and not cursor.session_id:
            cursor.session_id = line_session

        ts_raw = obj.get("timestamp")
        ts_dt = parse_iso(ts_raw) if isinstance(ts_raw, str) else None

        branch = obj.get("gitBranch") or ""
        if branch and branch not in window.allowed_branches:
            # foreign-branch interleave inside the window: keep the clock moving
            # so a genuine in-run line after it isn't read as dead air, emit
            # nothing.
            if ts_dt is not None:
                cursor.last_ts_dt = ts_dt
                cursor.last_ts_raw = ts_raw
            continue

        event_session = (
            line_session if isinstance(line_session, str) and line_session else cursor.session_id
        )

        suppress = _spans_open_tool_use(obj, cursor.id_to_start_ts, cursor.last_ts_dt)
        gap_event = _record_gap(
            cursor, i, ts_raw, ts_dt, branch, event_session, stall_threshold_secs, suppress
        )
        if gap_event is not None:
            events.append(gap_event)

        line_type = obj.get("type")
        if line_type == "assistant":
            _handle_assistant_line(obj, cursor, ts_dt)
        elif line_type == "user":
            events.extend(_handle_user_line(obj, i, ts_raw, cursor, branch, event_session, ticket))
        elif line_type == "system":
            retry_event = _handle_system_line(obj, i, ts_raw, cursor, branch, event_session)
            if retry_event is not None:
                events.append(retry_event)

    return events, cursor.stage_order, cursor.session_id


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mine a flow-dogfood run transcript.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "extract",
        help="Extract tool-error/silent-retry/drift/stall-gap events for one run.",
    )
    p.add_argument("--transcript", default=None, help="Path to a session JSONL transcript.")
    p.add_argument(
        "--session",
        default=None,
        help="Session UUID; resolved under --projects-root/<slug>/<uuid>.jsonl.",
    )
    p.add_argument("--ticket", required=True, help="Target ticket key; scopes events to its run.")
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

    try:
        events, stage_order, session_id = extract_events(
            lines, args.ticket, stall_threshold_secs=args.stall_threshold_secs
        )
    except _NoDispatchActivity:
        sys.stderr.write(
            f"trace-mine: no dispatch activity for ticket {args.ticket!r} in {transcript}\n"
        )
        return 5

    payload = {
        "transcript": str(transcript),
        "ticket": args.ticket,
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
