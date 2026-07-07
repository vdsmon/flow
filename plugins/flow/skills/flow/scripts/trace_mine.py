"""Read-only miner for a flow-dogfood run transcript (`~/.claude` session JSONL).

The `extract` subcommand reads one finished transcript and emits a single
structured-JSON object of per-stage events: tool errors, silent retries, drift
markers, and stall gaps, bucketed by dispatch-stage boundaries (child-1 of
flow-eia3). Pure extractor: it never clusters, never reads `friction.jsonl`,
never files beads, never calls `claude agents --json`. Filesystem-scan only.

The `cluster` subcommand (child-2, flow-zfpe) groups those extracted events into
Self-Harness-style failure signatures — one `(stage, kind, primary-anchor)` group
each, carrying a terminal cause (kind + representative body) and a reusable
mechanism (stage + anchor) — and dedups them against the already-logged
`flow_friction.py` entries, surfacing only the friction the in-flight logger
MISSED. It resolves the friction log through `_memory_paths` (like
`friction_recurrence`) and reuses that module's `anchors` model, so both sides of
the dedup are anchored by the same function. Each surfaced signature carries a
`dedup_key` the child-3 `flow_beads_create` seam partitions on `::`.

The `file` subcommand (child-3) takes cluster's surfaced signatures and files each
as a deduped PROPOSAL bead through `flow_beads_create.create_bead`, labelled
`["evolve", "proposal", "trace-mined"]`: `evolve` makes it a candidate in
`bd ready -l evolve`; `proposal` makes `evolve_select.active()` exclude it from the
launchable set, so it never auto-implements. The `dedup_key`'s `::` is stripped to
a single `:` before filing, so only the exact `evid:` fingerprint net fires, never
the fuzzy `evidfile:` same-file pass (`fingerprint()` collapses non-alphanumeric
runs identically either way). Auto-dormant outside maintainer mode.

The `runs` subcommand (child-4) enumerates this workspace's own recent finished
transcripts (mtime-windowed by `--since-hours`, over `_accepted_project_dirs`)
and resolves each to its distinct ticket set through the same
`_line_descriptor`/`_descriptor_ticket` pair `extract`'s `_derive_window`
consumes, emitting one `<transcript>\t<ticket>` line per distinct ticket so a
scheduler can drive `extract` unattended. Pure read-only enumeration; ungated,
since self-target is already enforced structurally by `_accepted_project_dirs`.

Run-window scoping: a real session file spans many runs across days. `extract`
requires a `--ticket` and clips output to that ticket's run window: from the
run's `/flow` intent invocation (or, headless, the contiguous same-branch
prefix) through the run's own last activity on its worktree branch (a branchless
idle tail or a post-run slice on the bootstrap branch is dropped). A relaunched
ticket scopes to its last run. Events on a foreign git branch inside the window
are dropped. A transcript carrying no dispatch activity for `--ticket` exits 5
rather than emitting unattributable events.

Stall gaps are suppressed when a long tool op spanned the gap or when the gap is
bounded by session plumbing (a backgrounded run parked on CI emits only
heartbeat lines); a genuine stall is bounded by model/tool activity.

Self-target guard: a `--transcript` is accepted only under the project tree of
the CLAIMED `--workspace-root` (`~/.claude/projects/<slug>/`, or a worktree
sibling). That root is caller-controlled, so the guard blocks an accidental
cross-project read, not a determined one.

Known limitation: a dispatch descriptor re-emitted inside an `isMeta` text
block (seen on resumed runs) is not parsed; only tool_result-carried
descriptors bound stages.

Exit codes (extract):
  0 = ok (including a valid eventless run -> events: []).
  1 = bad args (neither/both of --transcript/--session given).
  3 = transcript missing/unreadable.
  4 = self-target guard rejection.
  5 = no dispatch activity for --ticket in the transcript.
Exit codes (cluster):
  0 = ok.
  1 = bad args.
  3 = events source missing/unreadable/unparseable.
  4 = workspace.toml missing/invalid (_MemoryConfigError).
Exit codes (file):
  0 = ok (including a dormant, non-maintainer run).
  3 = signatures source missing/unreadable/unparseable.
Exit codes (runs):
  0 = ok, always (including zero pairs).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import _memory_paths
import flow_beads_create
from _jsonl import read_jsonl_lenient
from _runner import Runner
from _timeutil import parse_iso
from friction_recurrence import anchors

DEFAULT_STALL_THRESHOLD_SECS = 300
_PRE_DISPATCH = "<pre-dispatch>"
_DESCRIPTOR_KEYS = frozenset({"stage", "done", "head_sha", "handler_type"})
_DRIFT_MARKERS = ("reconciled_drift", "state_recovered_from_backup", "engine_reanchored")
_WORKTREE_MARKER = "/.flow/worktrees/"
_BODY_SNIPPET_LEN = 500
_JSON_OBJECT_SCAN_LIMIT = 10
_FLOW_COMMAND_RE = re.compile(r"<command-name>[^<]*flow", re.IGNORECASE)
# Model/tool activity, as opposed to session plumbing (system, queue-operation,
# worktree-state and other heartbeat/metadata lines a backgrounded run emits
# while parked on CI). A genuine stall is bounded by activity on both ends.
_ACTIVITY_TYPES = frozenset({"assistant", "user", "attachment"})


class _SelfTargetRejected(Exception):
    """--transcript resolves outside this workspace's own project tree."""


class _NoDispatchActivity(Exception):
    """No dispatch descriptor for the target ticket appears in the transcript."""


# ─── lenient reader (transcripts are foreign; never rewritten) ──────────────


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
    event.update({key: value for key, value in provenance.items() if value is not None})
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

    Runs are delimited by `/flow` intent lines. This is a finished-run miner, so
    a relaunched ticket scopes to its LAST run: the window spans from the intent
    preceding that run's dispatch up to the next intent (which starts the
    following run), so neither an earlier run of the same ticket nor the next
    run's bootstrap on a shared branch leaks in.

    The end is bounded at the run's OWN last activity on its worktree branch. A
    live descriptor carries `done`, but a `done: true` terminal descriptor is
    not emitted to the transcript in practice, so the operative bound is the last
    line on a run branch: a branchless idle tail (keepalive/system heartbeats) or
    a post-run slice on the bootstrap branch after that is not this run's.

    Returns None when no descriptor names the target ticket.
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

    run_branches = {
        branch
        for i in target
        for branch in [lines[i].get("gitBranch")]
        if isinstance(branch, str) and branch
    }
    allowed = set(run_branches)

    last_target = target[-1]
    prior_intents = [j for j in intents if j <= last_target]
    later_intents = [j for j in intents if j > last_target]
    span_end = later_intents[0] if later_intents else len(lines)

    if prior_intents:
        start = prior_intents[-1]
        branch = lines[start].get("gitBranch")
        if isinstance(branch, str) and branch:
            allowed.add(branch)
    else:
        start = _contiguous_prefix_start(lines, target[0], run_branches)

    in_span = [i for i in target if start <= i < span_end] or target
    last_dispatch = max(in_span)
    foreign_after = [i for i, tk in descriptors if last_dispatch < i < span_end and tk != ticket]
    if foreign_after:
        span_end = min(span_end, foreign_after[0])

    run_activity = [
        i
        for i in range(last_dispatch, span_end)
        if isinstance(lines[i], dict) and lines[i].get("gitBranch") in run_branches
    ]
    end = run_activity[-1] + 1 if run_activity else span_end

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
    last_is_activity: bool = False


def _spans_open_tool_use(
    obj: dict[str, Any], id_to_start_ts: dict[str, datetime], gap_start_dt: datetime | None
) -> bool:
    """True when this user line closes a tool_use that was already in flight at
    the gap start: a long op (a subagent Task dispatch) covered the whole gap, so
    the dead-air reading is spurious. A backgrounded run parked on CI has no tool
    in flight; that case is caught by the plumbing-bound test in extract_events.
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
    cur_is_activity: bool,
) -> dict[str, Any] | None:
    """Emit a stall_gap when this line's timestamp exceeds the threshold past the
    last one seen, then advance the cursor's last-timestamp regardless. A
    suppressed gap (a long in-flight tool op, or a plumbing-bound bg CI park)
    advances the clock without emitting.
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
        cursor.last_is_activity = cur_is_activity
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

        line_type = obj.get("type")
        cur_is_activity = line_type in _ACTIVITY_TYPES

        branch = obj.get("gitBranch") or ""
        if branch and branch not in window.allowed_branches:
            # foreign-branch interleave inside the window: keep the clock moving
            # so a genuine in-run line after it isn't read as dead air, emit
            # nothing.
            if ts_dt is not None:
                cursor.last_ts_dt = ts_dt
                cursor.last_ts_raw = ts_raw
                cursor.last_is_activity = cur_is_activity
            continue

        event_session = (
            line_session if isinstance(line_session, str) and line_session else cursor.session_id
        )

        # Suppress a stall gap that a long tool op spanned, or one bounded by
        # session plumbing on either end: a backgrounded run parked on CI emits
        # only system/queue-operation heartbeats, which is the pipeline waiting,
        # not a stall. A genuine stall is bounded by model/tool activity.
        plumbing_bound = not (cursor.last_is_activity and cur_is_activity)
        suppress = plumbing_bound or _spans_open_tool_use(
            obj, cursor.id_to_start_ts, cursor.last_ts_dt
        )
        gap_event = _record_gap(
            cursor,
            i,
            ts_raw,
            ts_dt,
            branch,
            event_session,
            stall_threshold_secs,
            suppress,
            cur_is_activity,
        )
        if gap_event is not None:
            events.append(gap_event)

        if line_type == "assistant":
            _handle_assistant_line(obj, cursor, ts_dt)
        elif line_type == "user":
            events.extend(_handle_user_line(obj, i, ts_raw, cursor, branch, event_session, ticket))
        elif line_type == "system":
            retry_event = _handle_system_line(obj, i, ts_raw, cursor, branch, event_session)
            if retry_event is not None:
                events.append(retry_event)

    return events, cursor.stage_order, cursor.session_id


# ─── cluster: failure-signature clustering + friction dedup (child-2) ────────


def _str_field(entry: dict[str, Any], key: str) -> str:
    value = entry.get(key)
    return value if isinstance(value, str) else ""


def _text_anchors(entry: dict[str, Any]) -> set[str]:
    """Distinctive anchors of an event or friction entry, from body + detail.

    Reuses friction_recurrence.anchors so both sides of the friction dedup are
    anchored by the same function.
    """
    return anchors(f"{_str_field(entry, 'body')} {_str_field(entry, 'detail')}")


def _primary_anchor(anchor_set: set[str]) -> str:
    """Deterministic representative anchor: a file anchor (.py/.md/.toml) wins,
    else the lexicographically-first token, else "".

    Uses only the public anchors() output via a suffix test, not
    friction_recurrence's private file-anchor regex.
    """
    if not anchor_set:
        return ""
    return sorted(anchor_set, key=lambda a: (0 if a.endswith((".py", ".md", ".toml")) else 1, a))[0]


def _build_signature(
    stage: str, kind: str, primary: str, members: list[dict[str, Any]]
) -> dict[str, Any]:
    """One failure signature from a (stage, kind, primary-anchor) group."""
    union: set[str] = set()
    for event in members:
        union |= _text_anchors(event)
    # ts is Z-suffixed UTC-ms ISO8601, so lexicographic compare == chronological
    # (the same assumption friction_recurrence documents in-code).
    representative = min(members, key=lambda e: _str_field(e, "ts"))
    ts_values = [t for t in (_str_field(e, "ts") for e in members) if t]
    related = sorted(union - {primary}) if primary else sorted(union)
    return {
        # An anchorless group must not collide with a group whose anchor IS the
        # stage name (code_review/create_pr/review_loop are all real anchors()
        # tokens). "no-anchor" contains a hyphen, which anchors() never emits,
        # so the marker is collision-free and the left part stays non-empty.
        "dedup_key": f"{primary or 'no-anchor'}::{kind}-{stage}",
        "summary": f"{kind} in {stage}" + (f" ({primary})" if primary else ""),
        "terminal_cause": {
            "kind": kind,
            "body": _str_field(representative, "body"),
            "detail": _str_field(representative, "detail"),
        },
        "mechanism": {"stage": stage, "anchor": primary, "related_anchors": related},
        "anchors": sorted(union),
        "event_count": len(members),
        "event_ids": sorted(_str_field(e, "id") for e in members),
        "run_ids": sorted({r for r in (_str_field(e, "run_id") for e in members) if r}),
        "tickets": sorted({t for t in (_str_field(e, "ticket") for e in members) if t}),
        "ts_start": min(ts_values) if ts_values else "",
        "ts_end": max(ts_values) if ts_values else "",
    }


def _already_logged(
    signature: dict[str, Any], friction: list[tuple[dict[str, Any], set[str]]]
) -> bool:
    """A signature is already-logged (drop it) iff some friction entry shares its
    run/ticket AND its stage AND at least one distinctive anchor.

    Deliberately kind-agnostic: it matches on stage + anchor overlap, not on a
    friction-type <-> event-kind map (the coupling this producer avoids). The
    cost is coarseness — a logged friction can suppress a genuinely-missed event
    of a DIFFERENT kind that shares an incidental anchor in the same stage
    (likeliest on a hot file such as dispatch_stage.py). Accepted for a
    maintainer-gated proposal producer: child-3 re-dedups the filed beads, and an
    anchorless event (a stall_gap, an anchorless silent_retry) never overlaps, so
    it always surfaces — the friction class the in-flight logger misses by
    construction.
    """
    sig_anchors = set(signature["anchors"])
    if not sig_anchors:
        return False
    stage = signature["mechanism"]["stage"]
    tickets = set(signature["tickets"])
    run_ids = set(signature["run_ids"])
    for entry, entry_anchors in friction:
        if _str_field(entry, "stage") != stage:
            continue
        if (
            _str_field(entry, "ticket") not in tickets
            and _str_field(entry, "run_id") not in run_ids
        ):
            continue
        if entry_anchors & sig_anchors:
            return True
    return False


def cluster_signatures(
    events: list[dict[str, Any]], friction: list[dict[str, Any]]
) -> dict[str, Any]:
    """Group extract events into failure signatures and drop those a friction
    entry already logged, surfacing only the MISSED friction.

    Pure function of the two lists (no wall clock, no I/O), mirroring
    friction_recurrence's clustering shape. Returns
    {signatures, total_events, missed, already_logged}; `signatures` holds the
    surfaced (missed) classes only, sorted by (-event_count, dedup_key). The
    library takes flat lists so a caller can concatenate several runs' events;
    the CLI clusters one extract payload per invocation.
    """
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        primary = _primary_anchor(_text_anchors(event))
        key = (_str_field(event, "stage"), _str_field(event, "kind"), primary)
        groups.setdefault(key, []).append(event)

    friction_anchored = [
        (entry, _text_anchors(entry)) for entry in friction if isinstance(entry, dict)
    ]

    surfaced: list[dict[str, Any]] = []
    already_logged = 0
    for (stage, kind, primary), members in groups.items():
        signature = _build_signature(stage, kind, primary, members)
        if _already_logged(signature, friction_anchored):
            already_logged += 1
        else:
            surfaced.append(signature)

    surfaced.sort(key=lambda s: (-s["event_count"], s["dedup_key"]))
    return {
        "signatures": surfaced,
        "total_events": len(events),
        "missed": len(surfaced),
        "already_logged": already_logged,
    }


# ─── file: propose-only bead filer (child-3) ────────────────────────────────


def _describe_signature(sig: dict[str, Any]) -> str:
    """Multi-line description mirroring friction_escalate._describe, built from
    a child-2 signature dict's terminal cause, mechanism, and provenance."""
    cause = sig.get("terminal_cause", {})
    mechanism = sig.get("mechanism", {})
    body = cause.get("body") or "n/a"
    detail = cause.get("detail")
    lines = [
        f"{cause.get('kind', '')} signature ({sig.get('event_count', 0)}x) in stage "
        f"`{mechanism.get('stage', '')}`.",
        f"Terminal cause: {body}" + (f" ({detail})" if detail else ""),
        f"Anchor: {mechanism.get('anchor') or 'n/a'}. "
        f"Related anchors: {', '.join(mechanism.get('related_anchors', [])) or 'n/a'}.",
        f"All anchors: {', '.join(sig.get('anchors', [])) or 'n/a'}.",
        f"Run ids: {', '.join(sig.get('run_ids', [])) or 'n/a'}.",
        f"Event ids: {', '.join(sig.get('event_ids', [])) or 'n/a'}.",
        f"Tickets: {', '.join(sig.get('tickets', [])) or 'n/a'}.",
        f"Window: {sig.get('ts_start', '')} to {sig.get('ts_end', '')}.",
        "",
        "Propose-only: informational evidence for the maintainer, carries the "
        "`proposal` label so evolve_select excludes it from auto-drain. Never "
        "auto-implemented; the maintainer runs it via `/flow <key>`.",
    ]
    return "\n".join(lines)


def file_signatures(
    workspace_root: Path, signatures: list[dict[str, Any]], runner: Runner | None = None
) -> dict[str, Any]:
    """File one deduped PROPOSAL bead per child-2 failure signature.

    Dormant outside maintainer mode, checked BEFORE any per-signature work (a
    normal user run touches nothing here). The `dedup_key`'s `::` is stripped to
    a single `:` before it reaches `create_bead`: `fingerprint()` collapses both
    forms identically, so the exact `evid:` net is unaffected, but the stripped
    key has no `::` for `create_bead`'s `partition("::")` to find, so the fuzzy
    `evidfile:` same-file pass never fires. That pass wrongly collapses distinct
    signatures sharing an anchor (an anchorless stall_gap in two different
    stages, or two different kinds on the same hot file).

    Under-notification tradeoff (same as friction_escalate): the evid net
    matches every status, so one bead per signature EVER — a signature
    recurring after its bead closed routes to `deduped` silently, never a
    fresh bead. The safe direction for a propose-only producer.
    """
    result: dict[str, Any] = {
        "maintainer": False,
        "candidates": 0,
        "filed": [],
        "deduped": [],
        "errors": [],
    }
    if flow_beads_create.resolve_maintainer_repo(workspace_root) is None:
        return result

    result["maintainer"] = True
    result["candidates"] = len(signatures)

    for sig in signatures:
        dedup_key = sig["dedup_key"].replace("::", ":")
        try:
            key = flow_beads_create.create_bead(
                workspace_root,
                sig["summary"],
                _describe_signature(sig),
                type="task",
                labels=["evolve", "proposal", "trace-mined"],
                dedup_key=dedup_key,
                runner=runner,
            )
            result["filed"].append(
                {"dedup_key": dedup_key, "key": key, "event_count": sig["event_count"]}
            )
        except flow_beads_create.DuplicateBead as exc:
            result["deduped"].append({"dedup_key": dedup_key, "existing_key": exc.existing_key})
        except flow_beads_create.BeadCreateError as exc:
            result["errors"].append({"dedup_key": dedup_key, "error": str(exc)})
    return result


# ─── runs: recent finished-transcript enumeration (child-4) ────────────────


def find_recent_runs(
    projects_root: Path, workspace_root: Path, since_hours: int
) -> list[tuple[Path, str]]:
    """One `(transcript, ticket)` pair per distinct ticket dispatched in a
    recent transcript under this workspace's own project dirs.

    Enumerates `_accepted_project_dirs(projects_root, workspace_root)`, globs
    `*.jsonl`, and keeps files whose mtime falls within the last `since_hours`.
    Each kept transcript's ticket set is resolved through the exact
    `_line_descriptor` + `_descriptor_ticket` pair `_derive_window` consumes
    inside `extract`, so every pair this emits is one `extract` can scope
    (dispatch activity guaranteed by construction, never exit 5). A transcript
    with no dispatch descriptors (an audit/drain session) yields no pair.
    Sorted by (path, ticket) for determinism.
    """
    cutoff = time.time() - since_hours * 3600
    pairs: list[tuple[Path, str]] = []
    for project_dir in _accepted_project_dirs(projects_root, workspace_root):
        if not project_dir.is_dir():
            continue
        for transcript in project_dir.glob("*.jsonl"):
            try:
                if transcript.stat().st_mtime < cutoff:
                    continue
                lines = read_jsonl_lenient(transcript, replace_errors=True)
            except OSError:
                continue
            tickets = {
                tk
                for obj in lines
                if (desc := _line_descriptor(obj)) is not None
                and (tk := _descriptor_ticket(desc)) is not None
            }
            pairs.extend((transcript, ticket) for ticket in tickets)
    pairs.sort(key=lambda pair: (str(pair[0]), pair[1]))
    return pairs


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

    c = sub.add_parser(
        "cluster",
        help="Cluster extract events into failure signatures; surface only MISSED friction.",
    )
    c.add_argument(
        "--events-file",
        default="-",
        help="An `extract` result JSON; '-' (the default) reads stdin.",
    )
    c.add_argument("--workspace-root", default=".")

    f = sub.add_parser(
        "file",
        help="File each cluster signature as a deduped proposal bead (maintainer-only).",
    )
    f.add_argument(
        "--signatures-file",
        default="-",
        help="A `cluster` result JSON; '-' (the default) reads stdin.",
    )
    f.add_argument("--workspace-root", default=".")

    r = sub.add_parser(
        "runs",
        help="List (transcript, ticket) pairs for recent finished dogfood-run transcripts.",
    )
    r.add_argument("--since-hours", type=int, default=48)
    r.add_argument("--workspace-root", default=".")
    r.add_argument("--projects-root", default=None, help="override (default ~/.claude/projects).")
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
        lines = read_jsonl_lenient(transcript, replace_errors=True)
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


def _read_events_source(events_file: str) -> str | None:
    """Raw extract-payload text: stdin for '-', else the named file (None on an
    unreadable path)."""
    if events_file == "-":
        return sys.stdin.read()
    try:
        return Path(events_file).expanduser().read_text(encoding="utf-8")
    except OSError:
        return None


def _run_cluster(args: argparse.Namespace) -> int:
    raw = _read_events_source(args.events_file)
    if raw is None:
        sys.stderr.write(f"trace-mine: events source not found or unreadable: {args.events_file}\n")
        return 3
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"trace-mine: could not parse events JSON: {exc}\n")
        return 3
    events = payload.get("events") if isinstance(payload, dict) else payload
    if not isinstance(events, list):
        sys.stderr.write("trace-mine: events payload has no 'events' list\n")
        return 3

    workspace_root = Path(args.workspace_root).resolve()
    try:
        namespace = _memory_paths.resolve_namespace(workspace_root)
    except _memory_paths._MemoryConfigError as exc:
        sys.stderr.write(f"trace-mine: {exc}\n")
        return 4

    fpath = _memory_paths.friction_path(workspace_root, namespace)
    try:
        friction = read_jsonl_lenient(fpath, replace_errors=True)
    except OSError as exc:
        sys.stderr.write(f"trace-mine: I/O error reading friction log: {exc}\n")
        return 3

    result = cluster_signatures(events, friction)
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


def _run_file(args: argparse.Namespace) -> int:
    raw = _read_events_source(args.signatures_file)
    if raw is None:
        sys.stderr.write(
            f"trace-mine: signatures source not found or unreadable: {args.signatures_file}\n"
        )
        return 3
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"trace-mine: could not parse signatures JSON: {exc}\n")
        return 3
    signatures = payload.get("signatures") if isinstance(payload, dict) else payload
    if not isinstance(signatures, list):
        sys.stderr.write("trace-mine: signatures payload has no 'signatures' list\n")
        return 3

    workspace_root = Path(args.workspace_root).resolve()
    result = file_signatures(workspace_root, signatures)
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


def _run_runs(args: argparse.Namespace) -> int:
    workspace_root = Path(args.workspace_root).resolve()
    projects_root = (
        Path(args.projects_root).expanduser().resolve()
        if args.projects_root
        else Path.home() / ".claude" / "projects"
    )
    for transcript, ticket in find_recent_runs(projects_root, workspace_root, args.since_hours):
        sys.stdout.write(f"{transcript}\t{ticket}\n")
    return 0


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    if args.command == "extract":
        return _run_extract(args)
    if args.command == "cluster":
        return _run_cluster(args)
    if args.command == "file":
        return _run_file(args)
    if args.command == "runs":
        return _run_runs(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "cli_main",
    "cluster_signatures",
    "extract_events",
    "file_signatures",
    "find_recent_runs",
]
