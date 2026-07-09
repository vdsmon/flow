"""Contract tests for trace_mine.py, the read-only transcript-extract miner."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import trace_mine


def _slug(path: Path) -> str:
    """Independent re-derivation of the `~/.claude/projects/` slug convention
    (flatten `/` and `.` to `-`), so fixtures don't lean on trace_mine's own
    `_slugify` to build the paths trace_mine then re-derives and checks.
    """
    return str(path).replace("/", "-").replace(".", "-")


def _write_jsonl(path: Path, lines: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def _project_dir(tmp_path: Path, workspace_root: Path) -> Path:
    return tmp_path / "projects" / _slug(workspace_root)


def _run_extract(
    capsys: pytest.CaptureFixture[str], *extra_args: str
) -> tuple[int, dict[str, Any] | None]:
    rc = trace_mine.cli_main(["extract", *extra_args])
    out = capsys.readouterr().out
    payload = json.loads(out) if out.strip() else None
    return rc, payload


def _flow_intent_line(
    ticket: str, ts: str, session: str = "sess1", branch: str = "main"
) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": ts,
        "gitBranch": branch,
        "sessionId": session,
        "message": {
            "role": "user",
            "content": (
                "<command-message>flow:flow</command-message>\n"
                "<command-name>/flow:flow</command-name>\n"
                f"<command-args>{ticket}</command-args>"
            ),
        },
    }


def _tool_use_line(
    tool_id: str, name: str, ts: str, session: str = "sess1", branch: str = "main"
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": ts,
        "gitBranch": branch,
        "sessionId": session,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": {}}],
        },
    }


def _tool_error_line(
    tool_id: str, ts: str, body: str, session: str = "sess1", branch: str = "main"
) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": ts,
        "gitBranch": branch,
        "sessionId": session,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": True,
                    "content": f"<tool_use_error>{body}</tool_use_error>",
                }
            ],
        },
    }


def _tool_result_line(
    tool_id: str, ts: str, body: str = "ok", session: str = "sess1", branch: str = "main"
) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": ts,
        "gitBranch": branch,
        "sessionId": session,
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "is_error": False, "content": body}
            ],
        },
    }


def _keepalive_line(
    ts: str, kind: str = "queue-operation", branch: str | None = None, session: str = "sess1"
) -> dict[str, Any]:
    """A session-plumbing heartbeat (the kind a backgrounded run parked on CI
    emits): a bare typed line with a timestamp, no model/tool content.
    """
    line: dict[str, Any] = {"type": kind, "timestamp": ts, "sessionId": session}
    if branch is not None:
        line["gitBranch"] = branch
    return line


def _descriptor_line(
    ts: str,
    stage: str,
    *,
    finished_stage: str | None = None,
    ticket_dir: str = "/x/.flow/runs/flow-eia3",
    session: str = "sess1",
    branch: str = "main",
    **markers: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "done": False,
        "stage": stage,
        "head_sha": "abc123",
        "handler_type": "inline",
        "ticket_dir": ticket_dir,
        "timeout_min": 10,
        **markers,
    }
    if finished_stage is not None:
        payload["finished"] = {"stage": finished_stage, "status": "completed"}
    return {
        "type": "user",
        "timestamp": ts,
        "gitBranch": branch,
        "sessionId": session,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_dispatch",
                    "is_error": False,
                    "content": json.dumps(payload, indent=2, sort_keys=True),
                }
            ],
        },
    }


def _retry_line(ts: str, session: str = "sess1", subtype: str = "api_error") -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": subtype,
        "level": "error",
        "error": {"message": "Connection error."},
        "retryInMs": 522.3,
        "retryAttempt": 1,
        "maxRetries": 10,
        "timestamp": ts,
        "sessionId": session,
        "gitBranch": "main",
    }


def _extract_args(transcript: Path, workspace_root: Path, tmp_path: Path, ticket: str) -> list[str]:
    return [
        "--transcript",
        str(transcript),
        "--ticket",
        ticket,
        "--workspace-root",
        str(workspace_root),
        "--projects-root",
        str(tmp_path / "projects"),
    ]


# ─── slug convention (hardcoded, independent of _slugify's own logic) ──────


def test_slugify_matches_claude_projects_convention() -> None:
    assert trace_mine._slugify(Path("/a/b/.flow/worktrees/c")) == "-a-b--flow-worktrees-c"
    assert trace_mine._slugify(Path("/a/b")) == "-a-b"


# ─── the four extractors ────────────────────────────────────────────────────


def test_extract_tool_error_event(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    _write_jsonl(
        transcript,
        [
            _tool_use_line("toolu_1", "Bash", "2026-06-01T00:00:00.000Z"),
            _tool_error_line("toolu_1", "2026-06-01T00:00:01.000Z", "boom"),
            _descriptor_line("2026-06-01T00:00:02.000Z", "implement"),
        ],
    )
    rc, payload = _run_extract(
        capsys, *_extract_args(transcript, workspace_root, tmp_path, "flow-eia3")
    )
    assert rc == 0
    assert payload is not None
    events = payload["events"]
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "tool_error"
    assert ev["body"] == "boom"
    assert ev["detail"] == "Bash"
    assert ev["tool_use_id"] == "toolu_1"
    assert ev["ticket"] == "flow-eia3"
    assert ev["stage"] == "<pre-dispatch>"


def test_extract_silent_retry_event(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    # C1: subtype differs from the confirmed-live "api_error" -- predicate must
    # still fire off retryAttempt presence alone.
    _write_jsonl(
        transcript,
        [
            _retry_line("2026-06-01T00:00:00.000Z", subtype="something_else"),
            _descriptor_line("2026-06-01T00:00:01.000Z", "implement"),
        ],
    )
    rc, payload = _run_extract(
        capsys, *_extract_args(transcript, workspace_root, tmp_path, "flow-eia3")
    )
    assert rc == 0
    assert payload is not None
    events = [e for e in payload["events"] if e["kind"] == "silent_retry"]
    assert len(events) == 1
    ev = events[0]
    assert ev["body"] == "Connection error."
    detail = json.loads(ev["detail"])
    assert detail["subtype"] == "something_else"
    assert detail["max_retries"] == 10
    assert detail["message"] == "Connection error."


def test_extract_drift_marker_event(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    _write_jsonl(
        transcript,
        [
            _descriptor_line(
                "2026-06-01T00:00:00.000Z", "code_review", reconciled_drift="workspace_toml"
            ),
            _descriptor_line("2026-06-01T00:00:05.000Z", "e2e", state_recovered_from_backup=True),
        ],
    )
    rc, payload = _run_extract(
        capsys, *_extract_args(transcript, workspace_root, tmp_path, "flow-eia3")
    )
    assert rc == 0
    assert payload is not None
    events = payload["events"]
    assert [e["kind"] for e in events] == ["drift", "drift"]
    assert events[0]["detail"] == "reconciled_drift"
    assert events[0]["body"] == "reconciled_drift=workspace_toml"
    assert events[1]["detail"] == "state_recovered_from_backup"
    # extract itself reads/writes no friction; the friction cross-check now lives
    # in the `cluster` subcommand (child-2, which does import _memory_paths). What
    # stays invariant: extract never imports the friction WRITER, and an extract
    # run creates no friction file.
    source = Path(trace_mine.__file__).read_text(encoding="utf-8")
    assert "import flow_friction" not in source
    assert not list(tmp_path.rglob("friction.jsonl"))


def test_extract_stall_gap_event(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    _write_jsonl(
        transcript,
        [
            _descriptor_line("2026-06-01T00:00:00.000Z", "implement"),
            _tool_use_line("toolu_1", "Bash", "2026-06-01T00:00:01.000Z"),
            _tool_use_line("toolu_2", "Bash", "2026-06-01T00:10:01.000Z"),  # 600s dead air
        ],
    )
    rc, payload = _run_extract(
        capsys, *_extract_args(transcript, workspace_root, tmp_path, "flow-eia3")
    )
    assert rc == 0
    assert payload is not None
    gaps = [e for e in payload["events"] if e["kind"] == "stall_gap"]
    assert len(gaps) == 1
    assert gaps[0]["gap_secs"] == pytest.approx(600.0)
    assert gaps[0]["gap_start_ts"] == "2026-06-01T00:00:01.000Z"
    assert gaps[0]["gap_end_ts"] == "2026-06-01T00:10:01.000Z"


def test_stall_gap_below_threshold_emits_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    _write_jsonl(
        transcript,
        [
            _descriptor_line("2026-06-01T00:00:00.000Z", "implement"),
            _tool_use_line("toolu_1", "Bash", "2026-06-01T00:00:01.000Z"),
            _tool_use_line("toolu_2", "Bash", "2026-06-01T00:01:41.000Z"),  # 100s gap
        ],
    )
    rc, payload = _run_extract(
        capsys, *_extract_args(transcript, workspace_root, tmp_path, "flow-eia3")
    )
    assert rc == 0
    assert payload is not None
    assert [e for e in payload["events"] if e["kind"] == "stall_gap"] == []


def test_stall_gap_suppressed_by_in_flight_tool_op(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A long op genuinely in flight (a subagent Task dispatch) shows up as a
    # tool_use whose tool_result arrives well past the threshold. That is the
    # pipeline working, not dead air, so it must NOT emit a stall_gap; a genuine
    # dead-air gap of the same size still does. A backgrounded run parked on CI
    # is a different shape (no tool in flight, keepalive-bounded) covered by
    # test_stall_gap_suppressed_by_bg_ci_keepalives.
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    _write_jsonl(
        transcript,
        [
            _descriptor_line("2026-06-01T00:00:00.000Z", "implement"),
            _tool_use_line("toolu_task", "Task", "2026-06-01T00:00:01.000Z"),
            _tool_result_line("toolu_task", "2026-06-01T00:10:01.000Z", "done"),  # 600s in flight
            _tool_use_line("toolu_next", "Bash", "2026-06-01T00:20:01.000Z"),  # 600s dead air
        ],
    )
    rc, payload = _run_extract(
        capsys, *_extract_args(transcript, workspace_root, tmp_path, "flow-eia3")
    )
    assert rc == 0
    assert payload is not None
    gaps = [e for e in payload["events"] if e["kind"] == "stall_gap"]
    assert len(gaps) == 1
    assert gaps[0]["gap_start_ts"] == "2026-06-01T00:10:01.000Z"
    assert gaps[0]["gap_end_ts"] == "2026-06-01T00:20:01.000Z"


def test_stall_gap_suppressed_by_bg_ci_keepalives(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A backgrounded run parked on CI has no tool in flight: the merge-stage wait
    # is bounded by session-plumbing heartbeats (queue-operation/system, both
    # non-activity), not by a tool_use span. Below, each line is 1800s past the
    # last, so every consecutive gap (activity->keepalive, keepalive->keepalive,
    # keepalive->resume) clears the 300s threshold, yet all must be suppressed as
    # the pipeline waiting on CI, not a stall. The trailing tool_use keeps the
    # run's window open past the heartbeats so they are walked, not clipped.
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    _write_jsonl(
        transcript,
        [
            _flow_intent_line("flow-eia3", "2026-06-01T00:00:00.000Z"),
            _descriptor_line("2026-06-01T00:00:05.000Z", "merge"),
            _tool_use_line("toolu_1", "Bash", "2026-06-01T00:00:10.000Z"),  # last pre-park activity
            _keepalive_line("2026-06-01T00:30:10.000Z"),
            _keepalive_line("2026-06-01T01:00:10.000Z", kind="system"),
            _tool_use_line("toolu_2", "Bash", "2026-06-01T01:30:10.000Z"),  # run resumes post-CI
        ],
    )
    rc, payload = _run_extract(
        capsys, *_extract_args(transcript, workspace_root, tmp_path, "flow-eia3")
    )
    assert rc == 0
    assert payload is not None
    assert [e for e in payload["events"] if e["kind"] == "stall_gap"] == []


# ─── run-window scoping ─────────────────────────────────────────────────────


def test_multi_run_scoping_yields_only_target_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    _write_jsonl(
        transcript,
        [
            # day-old unrelated main content, before this run's intent
            _tool_use_line("toolu_old", "Bash", "2026-06-01T00:00:00.000Z"),
            _tool_error_line("toolu_old", "2026-06-01T00:00:01.000Z", "OLD UNRELATED ERROR"),
            # target run
            _flow_intent_line("flow-eia3", "2026-06-02T10:00:00.000Z"),
            _descriptor_line("2026-06-02T10:00:05.000Z", "implement"),
            _tool_use_line("toolu_r1", "Bash", "2026-06-02T10:00:06.000Z"),
            _tool_error_line("toolu_r1", "2026-06-02T10:00:07.000Z", "RUN1 ERROR"),
            # foreign-branch interleave inside the window -> dropped
            _tool_use_line("toolu_fb", "Bash", "2026-06-02T10:00:08.000Z", branch="worktree-other"),
            _tool_error_line(
                "toolu_fb",
                "2026-06-02T10:00:09.000Z",
                "FOREIGN BRANCH ERROR",
                branch="worktree-other",
            ),
            # next run's intent bounds the target window; its bootstrap + events
            # sit on the same main branch and must NOT leak in
            _flow_intent_line("flow-other", "2026-06-02T11:00:00.000Z"),
            _tool_use_line("toolu_r2b", "Bash", "2026-06-02T11:00:01.000Z"),
            _tool_error_line("toolu_r2b", "2026-06-02T11:00:02.000Z", "RUN2 BOOTSTRAP ERROR"),
            _descriptor_line(
                "2026-06-02T11:00:05.000Z", "implement", ticket_dir="/x/.flow/runs/flow-other"
            ),
            _tool_use_line("toolu_r2", "Bash", "2026-06-02T11:00:06.000Z"),
            _tool_error_line("toolu_r2", "2026-06-02T11:00:07.000Z", "RUN2 ERROR"),
        ],
    )
    rc, payload = _run_extract(
        capsys, *_extract_args(transcript, workspace_root, tmp_path, "flow-eia3")
    )
    assert rc == 0
    assert payload is not None
    assert payload["ticket"] == "flow-eia3"
    assert [e["body"] for e in payload["events"]] == ["RUN1 ERROR"]
    assert payload["stage_order"] == ["implement"]


def test_trailing_edge_drops_post_run_events_when_target_is_last_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The target run is the LAST flow run in the session, so no next intent bounds
    # it from above. The window must still end at the run's own last worktree-branch
    # activity: a post-run bootstrap error back on main and a branchless idle tail of
    # keepalive gaps are not this run's and must not attribute to it.
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    wt = "feat/flow-eia3"
    _write_jsonl(
        transcript,
        [
            _flow_intent_line("flow-eia3", "2026-06-02T10:00:00.000Z"),
            _descriptor_line("2026-06-02T10:00:05.000Z", "implement", branch=wt),
            _tool_use_line("toolu_r1", "Bash", "2026-06-02T10:00:06.000Z", branch=wt),
            _tool_error_line("toolu_r1", "2026-06-02T10:00:07.000Z", "IN RUN ERROR", branch=wt),
            # run finished; the session idles back on main and via branchless keepalives
            _tool_use_line("toolu_post", "Bash", "2026-06-02T14:48:00.000Z"),
            _tool_error_line("toolu_post", "2026-06-02T14:49:00.000Z", "POST RUN ERROR"),
            _keepalive_line("2026-06-02T16:00:00.000Z"),
            _keepalive_line("2026-06-02T18:30:00.000Z"),
        ],
    )
    rc, payload = _run_extract(
        capsys, *_extract_args(transcript, workspace_root, tmp_path, "flow-eia3")
    )
    assert rc == 0
    assert payload is not None
    assert [e["body"] for e in payload["events"]] == ["IN RUN ERROR"]
    assert [e for e in payload["events"] if e["kind"] == "stall_gap"] == []


def test_same_ticket_relaunch_scopes_to_last_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A ticket relaunched after a first run appears twice. A finished-run miner
    # wants the LAST run (the one that finished), so the earlier run's events must
    # not leak in.
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    wt = "feat/flow-eia3"
    _write_jsonl(
        transcript,
        [
            _flow_intent_line("flow-eia3", "2026-06-02T10:00:00.000Z"),
            _descriptor_line("2026-06-02T10:00:05.000Z", "implement", branch=wt),
            _tool_use_line("toolu_r1", "Bash", "2026-06-02T10:00:06.000Z", branch=wt),
            _tool_error_line("toolu_r1", "2026-06-02T10:00:07.000Z", "FIRST RUN ERROR", branch=wt),
            _flow_intent_line("flow-eia3", "2026-06-02T11:00:00.000Z"),
            _descriptor_line("2026-06-02T11:00:05.000Z", "code_review", branch=wt),
            _tool_use_line("toolu_r2", "Bash", "2026-06-02T11:00:06.000Z", branch=wt),
            _tool_error_line("toolu_r2", "2026-06-02T11:00:07.000Z", "SECOND RUN ERROR", branch=wt),
        ],
    )
    rc, payload = _run_extract(
        capsys, *_extract_args(transcript, workspace_root, tmp_path, "flow-eia3")
    )
    assert rc == 0
    assert payload is not None
    assert [e["body"] for e in payload["events"]] == ["SECOND RUN ERROR"]
    assert payload["stage_order"] == ["code_review"]


def test_no_dispatch_activity_exits_5(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    _write_jsonl(
        transcript,
        [
            _tool_use_line("toolu_1", "Bash", "2026-06-01T00:00:00.000Z"),
            _tool_error_line("toolu_1", "2026-06-01T00:00:01.000Z", "boom"),
            _descriptor_line(
                "2026-06-01T00:00:02.000Z", "implement", ticket_dir="/x/.flow/runs/flow-other"
            ),
        ],
    )
    rc, payload = _run_extract(
        capsys, *_extract_args(transcript, workspace_root, tmp_path, "flow-eia3")
    )
    assert rc == 5
    assert payload is None


def test_eventless_run_emits_empty_events(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    _write_jsonl(
        transcript,
        [
            _flow_intent_line("flow-eia3", "2026-06-01T00:00:00.000Z"),
            _descriptor_line("2026-06-01T00:00:01.000Z", "implement"),
            _descriptor_line("2026-06-01T00:00:02.000Z", "code_review", finished_stage="implement"),
        ],
    )
    rc, payload = _run_extract(
        capsys, *_extract_args(transcript, workspace_root, tmp_path, "flow-eia3")
    )
    assert rc == 0
    assert payload is not None
    assert payload["events"] == []
    assert payload["stage_order"] == ["implement", "code_review"]


# ─── stage bucketing ────────────────────────────────────────────────────────


def test_stage_bucketing_from_dispatch_descriptors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    _write_jsonl(
        transcript,
        [
            _tool_use_line("toolu_pre", "Bash", "2026-06-01T00:00:00.000Z"),
            _tool_error_line("toolu_pre", "2026-06-01T00:00:01.000Z", "pre-dispatch failure"),
            _descriptor_line("2026-06-01T00:00:02.000Z", "implement"),
            _tool_use_line("toolu_in", "Bash", "2026-06-01T00:00:03.000Z"),
            _tool_error_line("toolu_in", "2026-06-01T00:00:04.000Z", "implement failure"),
            _descriptor_line("2026-06-01T00:00:05.000Z", "code_review", finished_stage="implement"),
        ],
    )
    rc, payload = _run_extract(
        capsys, *_extract_args(transcript, workspace_root, tmp_path, "flow-eia3")
    )
    assert rc == 0
    assert payload is not None
    errors = [e for e in payload["events"] if e["kind"] == "tool_error"]
    assert len(errors) == 2
    assert errors[0]["stage"] == "<pre-dispatch>"
    assert errors[1]["stage"] == "implement"
    assert payload["stage_order"] == ["implement", "code_review"]


def test_tool_use_id_links_to_tool_name(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    _write_jsonl(
        transcript,
        [
            _descriptor_line("2026-06-01T00:00:00.000Z", "implement"),
            _tool_use_line("toolu_a", "Bash", "2026-06-01T00:00:01.000Z"),
            _tool_use_line("toolu_b", "Read", "2026-06-01T00:00:02.000Z"),
            _tool_error_line("toolu_a", "2026-06-01T00:00:03.000Z", "bash boom"),
            _tool_error_line("toolu_b", "2026-06-01T00:00:04.000Z", "read boom"),
        ],
    )
    rc, payload = _run_extract(
        capsys, *_extract_args(transcript, workspace_root, tmp_path, "flow-eia3")
    )
    assert rc == 0
    assert payload is not None
    by_id = {e["tool_use_id"]: e["detail"] for e in payload["events"] if e["kind"] == "tool_error"}
    assert by_id == {"toolu_a": "Bash", "toolu_b": "Read"}


def test_descriptor_with_prefixed_junk_is_parsed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A descriptor tool_result can carry a stderr prefix with a stray brace before the real JSON
    # object. The boundary (and its drift marker) must still be recovered by scanning past the first
    # non-parsing "{".
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    payload_obj = {
        "done": False,
        "stage": "implement",
        "head_sha": "abc123",
        "handler_type": "inline",
        "ticket_dir": "/x/.flow/runs/flow-eia3",
        "reconciled_drift": "workspace_toml",
    }
    text = "dispatch: auto-reconciled {oops not json}\n" + json.dumps(payload_obj, indent=2)
    line = {
        "type": "user",
        "timestamp": "2026-06-01T00:00:00.000Z",
        "gitBranch": "main",
        "sessionId": "sess1",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_d",
                    "is_error": False,
                    "content": text,
                }
            ],
        },
    }
    _write_jsonl(transcript, [line])
    rc, payload = _run_extract(
        capsys, *_extract_args(transcript, workspace_root, tmp_path, "flow-eia3")
    )
    assert rc == 0
    assert payload is not None
    assert payload["stage_order"] == ["implement"]
    drifts = [e for e in payload["events"] if e["kind"] == "drift"]
    assert len(drifts) == 1
    assert drifts[0]["stage"] == "implement"
    assert drifts[0]["detail"] == "reconciled_drift"


# ─── resilience ─────────────────────────────────────────────────────────────


def test_malformed_line_resilience(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    valid_desc = json.dumps(_descriptor_line("2026-06-01T00:00:00.000Z", "implement"))
    valid_use = json.dumps(_tool_use_line("toolu_1", "Bash", "2026-06-01T00:00:01.000Z"))
    valid_err = json.dumps(_tool_error_line("toolu_1", "2026-06-01T00:00:02.000Z", "boom"))
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        f"\n{{not json at all\n{valid_desc}\n{valid_use}\n"
        f'plain text, not even braces\n{{"truncated": \n{valid_err}\n',
        encoding="utf-8",
    )
    rc, payload = _run_extract(
        capsys, *_extract_args(transcript, workspace_root, tmp_path, "flow-eia3")
    )
    assert rc == 0
    assert payload is not None
    events = payload["events"]
    assert len(events) == 1
    assert events[0]["kind"] == "tool_error"
    assert events[0]["body"] == "boom"


def test_lenient_jsonl_tolerates_bad_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A single non-UTF-8 byte must not raise out of the OSError-only guard; the
    # bad line is dropped and valid lines still parse.
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    valid_desc = json.dumps(_descriptor_line("2026-06-01T00:00:00.000Z", "implement")).encode()
    valid_use = json.dumps(_tool_use_line("toolu_1", "Bash", "2026-06-01T00:00:01.000Z")).encode()
    valid_err = json.dumps(_tool_error_line("toolu_1", "2026-06-01T00:00:02.000Z", "boom")).encode()
    transcript.write_bytes(
        b"\xff\xfe not a utf-8 line\n" + valid_desc + b"\n" + valid_use + b"\n" + valid_err + b"\n"
    )
    rc, payload = _run_extract(
        capsys, *_extract_args(transcript, workspace_root, tmp_path, "flow-eia3")
    )
    assert rc == 0
    assert payload is not None
    events = payload["events"]
    assert len(events) == 1
    assert events[0]["kind"] == "tool_error"
    assert events[0]["body"] == "boom"


def test_missing_transcript_exits_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace_root = tmp_path / "repo"
    missing_transcript = _project_dir(tmp_path, workspace_root) / "missing.jsonl"
    rc, payload = _run_extract(
        capsys, *_extract_args(missing_transcript, workspace_root, tmp_path, "flow-eia3")
    )
    assert rc == 3
    assert payload is None


def test_empty_transcript_has_no_dispatch_activity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace_root = tmp_path / "repo"
    empty_transcript = _project_dir(tmp_path, workspace_root) / "empty.jsonl"
    empty_transcript.parent.mkdir(parents=True, exist_ok=True)
    empty_transcript.write_text("", encoding="utf-8")
    rc, payload = _run_extract(
        capsys, *_extract_args(empty_transcript, workspace_root, tmp_path, "flow-eia3")
    )
    assert rc == 5
    assert payload is None


# ─── self-target guard ──────────────────────────────────────────────────────


def test_self_target_rejection(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace_root = tmp_path / "repo"
    outside_transcript = tmp_path / "projects" / "-some-other-project" / "x.jsonl"
    rc, payload = _run_extract(
        capsys, *_extract_args(outside_transcript, workspace_root, tmp_path, "flow-eia3")
    )
    assert rc == 4
    assert payload is None


def test_self_target_accepts_parent_repo_dir_for_worktree_workspace_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A worktree workspace-root's own /flow session often files its transcript
    # under the main repo's project dir, not the worktree's own slug dir.
    repo_root = tmp_path / "repo"
    workspace_root = repo_root / ".flow" / "worktrees" / "feat-x"
    transcript = _project_dir(tmp_path, repo_root) / "t1.jsonl"
    _write_jsonl(transcript, [_descriptor_line("2026-06-01T00:00:00.000Z", "implement")])
    rc, payload = _run_extract(
        capsys, *_extract_args(transcript, workspace_root, tmp_path, "flow-eia3")
    )
    assert rc == 0
    assert payload is not None


def test_bad_args_requires_exactly_one_of_transcript_or_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, payload = _run_extract(
        capsys, "--ticket", "flow-eia3", "--workspace-root", str(tmp_path / "repo")
    )
    assert rc == 1
    assert payload is None


# ─── cluster: failure-signature clustering + friction dedup (child-2) ────────


def _seed_workspace(root: Path, namespace: str = "demo", *, maintainer: bool = False) -> None:
    """Seed a real workspace (`.flow/workspace.toml` + `.flow/<namespace>/`),
    mirroring test_friction_recurrence's pattern, so `cluster` self-resolves the
    friction log via `_memory_paths`. `maintainer=True` adds the `[maintainer]`
    marker (test_friction_escalate's seeding pattern), for file_signatures()
    tests."""
    flow = root / ".flow"
    (flow / namespace).mkdir(parents=True, exist_ok=True)
    marker = "[maintainer]\nself_target = true\n\n" if maintainer else ""
    (flow / "workspace.toml").write_text(
        f'{marker}[tracker]\nbackend = "jira"\n\n[memory]\nnamespace = "{namespace}"\n',
        encoding="utf-8",
    )


def _cev(
    *,
    id_: str,
    kind: str,
    stage: str,
    body: str,
    detail: str = "",
    ticket: str = "flow-eia3",
    run_id: str = "run-1",
    ts: str = "2026-06-01T00:00:00.000Z",
) -> dict[str, Any]:
    """A trace-mine extract event, minimal to the fields cluster reads."""
    event: dict[str, Any] = {
        "id": id_,
        "ts": ts,
        "run_id": run_id,
        "ticket": ticket,
        "stage": stage,
        "kind": kind,
        "body": body,
    }
    if detail:
        event["detail"] = detail
    return event


def _fr(
    *,
    id_: str,
    stage: str,
    body: str,
    detail: str = "",
    type_: str = "RETRY",
    ticket: str = "flow-eia3",
    run_id: str = "run-1",
    ts: str = "2026-06-01T00:00:00.000Z",
) -> dict[str, Any]:
    """A flow_friction.py log entry."""
    entry: dict[str, Any] = {
        "id": id_,
        "ts": ts,
        "run_id": run_id,
        "ticket": ticket,
        "stage": stage,
        "type": type_,
        "severity": "major",
        "body": body,
    }
    if detail:
        entry["detail"] = detail
    return entry


def _run_cluster(
    capsys: pytest.CaptureFixture[str], *args: str
) -> tuple[int, dict[str, Any] | None]:
    rc = trace_mine.cli_main(["cluster", *args])
    out = capsys.readouterr().out
    return rc, (json.loads(out) if out.strip() else None)


# --- library: cluster_signatures ---------------------------------------------


def test_cluster_groups_same_stage_kind_anchor() -> None:
    events = [
        _cev(
            id_="e1", kind="tool_error", stage="implement", body="boom in state.py", detail="Bash"
        ),
        _cev(
            id_="e2",
            kind="tool_error",
            stage="implement",
            body="state.py exploded again",
            detail="Bash",
            ts="2026-06-01T00:01:00.000Z",
            run_id="run-2",
        ),
    ]
    result = trace_mine.cluster_signatures(events, [])
    assert result["total_events"] == 2
    assert result["already_logged"] == 0
    assert result["missed"] == 1
    sig = result["signatures"][0]
    assert sig["event_count"] == 2
    assert sig["event_ids"] == ["e1", "e2"]
    assert sig["run_ids"] == ["run-1", "run-2"]
    assert sig["mechanism"]["stage"] == "implement"
    assert sig["mechanism"]["anchor"] == "state.py"
    assert sig["terminal_cause"]["kind"] == "tool_error"


def test_cluster_primary_anchor_prefers_file_over_snake() -> None:
    events = [
        _cev(id_="e1", kind="drift", stage="merge", body="reconciled_drift touched create_pr.py"),
    ]
    sig = trace_mine.cluster_signatures(events, [])["signatures"][0]
    assert sig["mechanism"]["anchor"] == "create_pr.py"
    assert "reconciled_drift" in sig["mechanism"]["related_anchors"]


def test_cluster_dedup_drops_on_overlapping_anchor_stage_ticket() -> None:
    events = [_cev(id_="e1", kind="drift", stage="implement", body="drift on state.py")]
    friction = [
        _fr(id_="f1", stage="implement", type_="DRIFT", body="state.py drift reconciled"),
    ]
    result = trace_mine.cluster_signatures(events, friction)
    assert result["already_logged"] == 1
    assert result["missed"] == 0
    assert result["signatures"] == []


def test_cluster_dedup_matches_on_run_id_when_ticket_differs() -> None:
    events = [
        _cev(id_="e1", kind="drift", stage="implement", body="drift on state.py", ticket="T-a")
    ]
    friction = [
        _fr(id_="f1", stage="implement", body="state.py drift", ticket="T-b", run_id="run-1"),
    ]
    # ticket differs but run_id matches -> still deduped.
    assert trace_mine.cluster_signatures(events, friction)["already_logged"] == 1


def test_cluster_dedup_requires_stage_match() -> None:
    events = [_cev(id_="e1", kind="drift", stage="implement", body="drift on state.py")]
    friction = [_fr(id_="f1", stage="code_review", body="state.py issue")]
    result = trace_mine.cluster_signatures(events, friction)
    assert result["already_logged"] == 0
    assert result["missed"] == 1


def test_cluster_stall_gap_always_surfaces() -> None:
    events = [_cev(id_="e1", kind="stall_gap", stage="implement", body="stall gap of 600.0s")]
    # a friction entry in the same stage/ticket cannot dedup an anchorless signature.
    friction = [_fr(id_="f1", stage="implement", body="unrelated state.py note")]
    result = trace_mine.cluster_signatures(events, friction)
    assert result["missed"] == 1
    sig = result["signatures"][0]
    assert sig["mechanism"]["anchor"] == ""
    assert sig["dedup_key"] == "no-anchor::stall_gap-implement"


def test_cluster_silent_retry_missed_by_loop_surfaces() -> None:
    events = [
        _cev(
            id_="e1",
            kind="silent_retry",
            stage="implement",
            body="Connection error.",
            detail='{"subtype": "api_error"}',
        )
    ]
    result = trace_mine.cluster_signatures(events, [])
    assert result["missed"] == 1
    assert result["signatures"][0]["terminal_cause"]["kind"] == "silent_retry"


def test_cluster_dedup_key_is_child3_partitionable() -> None:
    events = [
        _cev(id_="e1", kind="tool_error", stage="implement", body="fail in state.py"),
        _cev(id_="e2", kind="stall_gap", stage="merge", body="stall gap of 700s"),
    ]
    for sig in trace_mine.cluster_signatures(events, [])["signatures"]:
        key = sig["dedup_key"]
        file_part, sep, symptom = key.partition("::")
        assert sep == "::"
        assert file_part
        assert symptom
        assert key.count("::") == 1


def test_cluster_deterministic_ordering() -> None:
    events = [
        _cev(id_="a", kind="tool_error", stage="implement", body="x in aaa_mod.py"),
        _cev(id_="b", kind="drift", stage="merge", body="y in bbb_mod.py"),
        _cev(
            id_="c",
            kind="drift",
            stage="merge",
            body="y in bbb_mod.py",
            ts="2026-06-01T00:02:00.000Z",
        ),
    ]
    signatures = trace_mine.cluster_signatures(events, [])["signatures"]
    assert [s["event_count"] for s in signatures] == [2, 1]
    assert signatures[0]["mechanism"]["anchor"] == "bbb_mod.py"


def test_cluster_empty_events_yields_empty() -> None:
    assert trace_mine.cluster_signatures([], []) == {
        "signatures": [],
        "total_events": 0,
        "missed": 0,
        "already_logged": 0,
    }


# --- CLI: cluster ------------------------------------------------------------


def test_cluster_cli_reads_friction_via_memory_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    _write_jsonl(
        tmp_path / ".flow" / "demo" / "friction.jsonl",
        [_fr(id_="f1", stage="implement", body="state.py drift")],
    )
    payload = {
        "events": [
            _cev(id_="e1", kind="drift", stage="implement", body="drift on state.py"),
            _cev(id_="e2", kind="stall_gap", stage="merge", body="stall gap of 900s"),
        ]
    }
    events_file = tmp_path / "events.json"
    events_file.write_text(json.dumps(payload), encoding="utf-8")
    rc, result = _run_cluster(
        capsys, "--events-file", str(events_file), "--workspace-root", str(tmp_path)
    )
    assert rc == 0
    assert result is not None
    assert result["already_logged"] == 1
    assert result["missed"] == 1
    assert result["signatures"][0]["terminal_cause"]["kind"] == "stall_gap"


def test_cluster_cli_missing_friction_file_surfaces_all(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)  # no friction.jsonl written
    payload = {
        "events": [_cev(id_="e1", kind="drift", stage="implement", body="drift on state.py")]
    }
    events_file = tmp_path / "events.json"
    events_file.write_text(json.dumps(payload), encoding="utf-8")
    rc, result = _run_cluster(
        capsys, "--events-file", str(events_file), "--workspace-root", str(tmp_path)
    )
    assert rc == 0
    assert result is not None
    assert result["missed"] == 1
    assert result["already_logged"] == 0


def test_cluster_cli_missing_workspace_toml_exits_4(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    events_file = tmp_path / "events.json"
    events_file.write_text(json.dumps({"events": []}), encoding="utf-8")
    rc, result = _run_cluster(
        capsys, "--events-file", str(events_file), "--workspace-root", str(tmp_path)
    )
    assert rc == 4
    assert result is None


def test_cluster_cli_missing_events_file_exits_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    rc, result = _run_cluster(
        capsys, "--events-file", str(tmp_path / "nope.json"), "--workspace-root", str(tmp_path)
    )
    assert rc == 3
    assert result is None


def test_cluster_cli_reads_events_from_stdin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_workspace(tmp_path)
    payload = {
        "events": [_cev(id_="e1", kind="stall_gap", stage="merge", body="stall gap of 500s")]
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    rc, result = _run_cluster(capsys, "--events-file", "-", "--workspace-root", str(tmp_path))
    assert rc == 0
    assert result is not None
    assert result["missed"] == 1


def test_cluster_anchorless_key_distinct_from_stage_named_anchor():
    # code_review is itself an anchors() token: an anchorless drift group and a
    # code_review-anchored drift group in the same stage must not share a key.
    anchored = {
        "id": "e-1",
        "ts": "2026-06-01T00:00:00.000Z",
        "kind": "drift",
        "stage": "code_review",
        "run_id": "r1",
        "ticket": "T-1",
        "body": "code_review drifted on code_review",
        "detail": "",
    }
    anchorless = {
        "id": "e-2",
        "ts": "2026-06-01T00:01:00.000Z",
        "kind": "drift",
        "stage": "code_review",
        "run_id": "r1",
        "ticket": "T-1",
        "body": "???",
        "detail": "",
    }
    result = trace_mine.cluster_signatures([anchored, anchorless], [])
    keys = [s["dedup_key"] for s in result["signatures"]]
    assert len(keys) == len(set(keys)), keys
    assert all(k.split("::")[0] for k in keys)


# ─── file: propose-only bead filer (child-3) ────────────────────────────────

Recorder = list[tuple[list[str], Path]]


def _sig(
    dedup_key: str,
    summary: str,
    *,
    kind: str = "tool_error",
    stage: str = "implement",
    anchor: str = "",
    event_count: int = 1,
) -> dict[str, Any]:
    """A child-2 cluster signature dict, minimal to the fields file_signatures
    reads (mirrors _build_signature's shape)."""
    return {
        "dedup_key": dedup_key,
        "summary": summary,
        "terminal_cause": {"kind": kind, "body": f"{kind} body", "detail": ""},
        "mechanism": {"stage": stage, "anchor": anchor, "related_anchors": []},
        "anchors": [anchor] if anchor else [],
        "event_count": event_count,
        "event_ids": ["ev-1"],
        "run_ids": ["run-1"],
        "tickets": ["flow-eia3"],
        "ts_start": "2026-06-01T00:00:00.000Z",
        "ts_end": "2026-06-01T00:00:05.000Z",
    }


def _persisting_runner(
    seed_by_label: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[Callable[[list[str], Path], subprocess.CompletedProcess[str]], Recorder]:
    """Models create_bead's own dedup lookup across calls within one
    file_signatures() run: a `bd create` records the new bead under EVERY label
    it carries (crucially the evidfile: anchor label), so a later `bd list` for
    that label sees it. friction_escalate's `_runner` is non-persisting (fixed
    pre-seed, never records creates) and would make the ::-strip regression
    below vacuous, since that regression IS the fuzzy evidfile: pass.
    """
    by_label: dict[str, list[dict[str, Any]]] = {
        k: list(v) for k, v in (seed_by_label or {}).items()
    }
    calls: Recorder = []
    counter = [0]

    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append((args, cwd))
        if len(args) >= 2 and args[1] == "list":
            label = args[args.index("-l") + 1] if "-l" in args else ""
            return subprocess.CompletedProcess(args, 0, json.dumps(by_label.get(label, [])), "")
        counter[0] += 1
        uid = f"flow-new-{counter[0]}"
        title = next((a.split("=", 1)[1] for a in args if a.startswith("--title=")), "")
        labels_val = args[args.index("--labels") + 1] if "--labels" in args else ""
        for label in (s for s in labels_val.split(",") if s):
            by_label.setdefault(label, []).append({"id": uid, "title": title})
        return subprocess.CompletedProcess(args, 0, json.dumps({"id": uid}), "")

    return run, calls


def _create_labels(create_args: list[str]) -> list[str]:
    return create_args[create_args.index("--labels") + 1].split(",")


def test_file_signatures_happy_path_labels_and_shape(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, maintainer=True)
    sigs = [
        _sig("no-anchor::stall_gap-implement", "stall_gap in implement", kind="stall_gap"),
        _sig(
            "state.py::tool_error-implement",
            "tool_error in implement (state.py)",
            anchor="state.py",
        ),
    ]
    run, calls = _persisting_runner()

    result = trace_mine.file_signatures(tmp_path, sigs, runner=run)

    assert result["maintainer"] is True
    assert result["candidates"] == 2
    assert len(result["filed"]) == 2
    assert result["deduped"] == []
    assert result["errors"] == []

    create_calls = [c[0] for c in calls if c[0][:2] == ["bd", "create"]]
    assert len(create_calls) == 2
    for create_args in create_calls:
        stamped = set(_create_labels(create_args))
        assert {"evolve", "proposal", "trace-mined"} <= stamped
    assert {c[0][1] for c in calls} <= {"list", "create"}


def test_file_signatures_strip_prevents_anchorless_collision(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, maintainer=True)
    sigs = [
        _sig("no-anchor::stall_gap-implement", "stall_gap in implement", kind="stall_gap"),
        _sig(
            "no-anchor::stall_gap-commit",
            "stall_gap in commit",
            kind="stall_gap",
            stage="commit",
        ),
    ]
    run, calls = _persisting_runner()

    result = trace_mine.file_signatures(tmp_path, sigs, runner=run)

    assert len(result["filed"]) == 2
    assert result["deduped"] == []
    create_calls = [c[0] for c in calls if c[0][:2] == ["bd", "create"]]
    evid_labels = {
        label
        for args in create_calls
        for label in _create_labels(args)
        if label.startswith("evid:")
    }
    assert len(evid_labels) == 2


def test_file_signatures_strip_prevents_same_file_collision(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, maintainer=True)
    sigs = [
        _sig(
            "dispatch_stage.py::tool_error-implement",
            "tool_error in implement (dispatch_stage.py)",
            anchor="dispatch_stage.py",
        ),
        _sig(
            "dispatch_stage.py::drift-implement",
            "drift in implement (dispatch_stage.py)",
            kind="drift",
            anchor="dispatch_stage.py",
        ),
    ]
    run, calls = _persisting_runner()

    result = trace_mine.file_signatures(tmp_path, sigs, runner=run)

    assert len(result["filed"]) == 2
    assert result["deduped"] == []
    create_calls = [c[0] for c in calls if c[0][:2] == ["bd", "create"]]
    evid_labels = {
        label
        for args in create_calls
        for label in _create_labels(args)
        if label.startswith("evid:")
    }
    assert len(evid_labels) == 2


def test_file_signatures_duplicate_routes_to_deduped_no_create(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, maintainer=True)
    sig = _sig(
        "state.py::tool_error-implement", "tool_error in implement (state.py)", anchor="state.py"
    )
    stripped_key = sig["dedup_key"].replace("::", ":")

    import flow_beads_create as fbc

    evid = f"evid:{fbc.fingerprint(stripped_key)}"
    run, calls = _persisting_runner(
        seed_by_label={evid: [{"id": "flow-old", "title": sig["summary"]}]}
    )

    result = trace_mine.file_signatures(tmp_path, [sig], runner=run)

    assert result["filed"] == []
    assert result["deduped"] == [{"dedup_key": stripped_key, "existing_key": "flow-old"}]
    assert not any(c[0][:2] == ["bd", "create"] for c in calls)


def test_file_signatures_dormant_when_not_maintainer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("maintainer._global_config_path", lambda: tmp_path / "absent.toml")
    _seed_workspace(tmp_path, maintainer=False)
    sig = _sig(
        "state.py::tool_error-implement", "tool_error in implement (state.py)", anchor="state.py"
    )
    run, calls = _persisting_runner()

    result = trace_mine.file_signatures(tmp_path, [sig], runner=run)

    assert result == {
        "maintainer": False,
        "candidates": 0,
        "filed": [],
        "deduped": [],
        "errors": [],
    }
    assert calls == []


# --- CLI: file ----------------------------------------------------------------


def _run_file(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, dict[str, Any] | None]:
    rc = trace_mine.cli_main(["file", *args])
    out = capsys.readouterr().out
    return rc, (json.loads(out) if out.strip() else None)


def test_file_cli_reads_from_signatures_file_dormant(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("maintainer._global_config_path", lambda: tmp_path / "absent.toml")
    _seed_workspace(tmp_path, maintainer=False)
    payload = {"signatures": [_sig("no-anchor::stall_gap-implement", "stall_gap in implement")]}
    sig_file = tmp_path / "signatures.json"
    sig_file.write_text(json.dumps(payload), encoding="utf-8")

    rc, result = _run_file(
        capsys, "--signatures-file", str(sig_file), "--workspace-root", str(tmp_path)
    )
    assert rc == 0
    assert result is not None
    assert result["maintainer"] is False


def test_file_cli_reads_from_stdin_dormant(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("maintainer._global_config_path", lambda: tmp_path / "absent.toml")
    _seed_workspace(tmp_path, maintainer=False)
    payload = {"signatures": [_sig("no-anchor::stall_gap-implement", "stall_gap in implement")]}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    rc, result = _run_file(capsys, "--signatures-file", "-", "--workspace-root", str(tmp_path))
    assert rc == 0
    assert result is not None
    assert result["maintainer"] is False


def test_file_cli_missing_signatures_file_exits_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, result = _run_file(
        capsys, "--signatures-file", str(tmp_path / "nope.json"), "--workspace-root", str(tmp_path)
    )
    assert rc == 3
    assert result is None


def test_file_cli_non_json_signatures_file_exits_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    rc, result = _run_file(capsys, "--signatures-file", str(bad), "--workspace-root", str(tmp_path))
    assert rc == 3
    assert result is None


def test_file_cli_signatures_not_a_list_exits_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload_file = tmp_path / "signatures.json"
    payload_file.write_text(json.dumps({"signatures": {"not": "a list"}}), encoding="utf-8")
    rc, result = _run_file(
        capsys, "--signatures-file", str(payload_file), "--workspace-root", str(tmp_path)
    )
    assert rc == 3
    assert result is None


def test_file_signatures_bd_failure_routes_to_errors_batch_continues(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, maintainer=True)
    sigs = [
        _sig("no-anchor::stall_gap-implement", "stall_gap in implement", kind="stall_gap"),
        _sig(
            "state.py::tool_error-implement",
            "tool_error in implement (state.py)",
            anchor="state.py",
        ),
    ]
    inner, _calls = _persisting_runner()
    failed_first = {"done": False}

    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["bd", "create"] and not failed_first["done"]:
            failed_first["done"] = True
            return subprocess.CompletedProcess(args, 1, "", "dolt lock contention")
        return inner(args, cwd)

    result = trace_mine.file_signatures(tmp_path, sigs, runner=run)

    assert len(result["errors"]) == 1
    assert len(result["filed"]) == 1
    assert result["deduped"] == []


# ─── runs: recent finished-transcript enumeration (child-4) ────────────────


def _run_runs(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, list[tuple[str, str]]]:
    rc = trace_mine.cli_main(["runs", *args])
    out = capsys.readouterr().out
    pairs = [tuple(line.split("\t", 1)) for line in out.splitlines() if line.strip()]
    return rc, pairs


def _runs_args(workspace_root: Path, tmp_path: Path, *, since_hours: int = 48) -> list[str]:
    return [
        "--workspace-root",
        str(workspace_root),
        "--projects-root",
        str(tmp_path / "projects"),
        "--since-hours",
        str(since_hours),
    ]


def _minable_transcript(ticket: str) -> list[dict[str, Any]]:
    """A transcript with a genuinely extractable run: a dispatch descriptor plus
    a tool error inside its window (mirrors test_extract_tool_error_event)."""
    return [
        _tool_use_line("toolu_1", "Bash", "2026-06-01T00:00:00.000Z"),
        _tool_error_line("toolu_1", "2026-06-01T00:00:01.000Z", "boom"),
        _descriptor_line(
            "2026-06-01T00:00:02.000Z", "implement", ticket_dir=f"/x/.flow/runs/{ticket}"
        ),
    ]


def test_runs_lists_pair_for_recent_transcript(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    _write_jsonl(transcript, _minable_transcript("flow-eia3"))
    rc, pairs = _run_runs(capsys, *_runs_args(workspace_root, tmp_path))
    assert rc == 0
    assert pairs == [(str(transcript), "flow-eia3")]


def test_runs_emits_one_pair_per_distinct_ticket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    _write_jsonl(
        transcript,
        [
            _descriptor_line(
                "2026-06-01T00:00:00.000Z", "implement", ticket_dir="/x/.flow/runs/flow-aaaa"
            ),
            _descriptor_line(
                "2026-06-01T01:00:00.000Z", "implement", ticket_dir="/x/.flow/runs/flow-bbbb"
            ),
        ],
    )
    rc, pairs = _run_runs(capsys, *_runs_args(workspace_root, tmp_path))
    assert rc == 0
    assert pairs == [(str(transcript), "flow-aaaa"), (str(transcript), "flow-bbbb")]


def test_runs_excludes_transcript_older_than_window(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    _write_jsonl(transcript, _minable_transcript("flow-eia3"))
    old_mtime = time.time() - 49 * 3600  # past the 48h default window
    os.utime(transcript, (old_mtime, old_mtime))
    rc, pairs = _run_runs(capsys, *_runs_args(workspace_root, tmp_path, since_hours=48))
    assert rc == 0
    assert pairs == []


def test_runs_includes_worktree_sibling_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace_root = tmp_path / "repo"
    sibling_dir = tmp_path / "projects" / f"{_slug(workspace_root)}--flow-worktrees-feat-x"
    transcript = sibling_dir / "t1.jsonl"
    _write_jsonl(transcript, _minable_transcript("flow-eia3"))
    rc, pairs = _run_runs(capsys, *_runs_args(workspace_root, tmp_path))
    assert rc == 0
    assert pairs == [(str(transcript), "flow-eia3")]


def test_runs_transcript_without_dispatch_yields_no_pair(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    _write_jsonl(
        transcript,
        [
            _tool_use_line("toolu_1", "Bash", "2026-06-01T00:00:00.000Z"),
            _tool_error_line("toolu_1", "2026-06-01T00:00:01.000Z", "boom"),
        ],
    )
    rc, pairs = _run_runs(capsys, *_runs_args(workspace_root, tmp_path))
    assert rc == 0
    assert pairs == []


def test_runs_drops_descriptor_with_unresolvable_ticket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A descriptor with neither ticket_dir nor ticket: _descriptor_ticket
    # returns None for it, so it must contribute no pair.
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    payload = {"done": False, "stage": "implement", "head_sha": "abc123", "handler_type": "inline"}
    line = {
        "type": "user",
        "timestamp": "2026-06-01T00:00:00.000Z",
        "gitBranch": "main",
        "sessionId": "sess1",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_d",
                    "is_error": False,
                    "content": json.dumps(payload),
                }
            ],
        },
    }
    _write_jsonl(transcript, [line])
    rc, pairs = _run_runs(capsys, *_runs_args(workspace_root, tmp_path))
    assert rc == 0
    assert pairs == []


def test_runs_output_deterministically_sorted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace_root = tmp_path / "repo"
    transcript_b = _project_dir(tmp_path, workspace_root) / "b.jsonl"
    transcript_a = _project_dir(tmp_path, workspace_root) / "a.jsonl"
    _write_jsonl(transcript_b, _minable_transcript("flow-zz"))
    _write_jsonl(transcript_a, _minable_transcript("flow-aa"))
    rc, pairs = _run_runs(capsys, *_runs_args(workspace_root, tmp_path))
    assert rc == 0
    assert pairs == sorted(pairs)
    assert pairs == [(str(transcript_a), "flow-aa"), (str(transcript_b), "flow-zz")]


def test_runs_pairs_feed_extract_without_exit5(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The linchpin: a pair find_recent_runs emits must be one `extract` can
    # scope -- proves runs' ticket resolution matches extract's own contract.
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    _write_jsonl(transcript, _minable_transcript("flow-eia3"))
    pairs = trace_mine.find_recent_runs(tmp_path / "projects", workspace_root, 48)
    assert pairs == [(transcript, "flow-eia3")]
    tx, tk = pairs[0]
    rc, payload = _run_extract(capsys, *_extract_args(tx, workspace_root, tmp_path, tk))
    assert rc == 0
    assert payload is not None


def test_runs_empty_projects_dir_exits_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace_root = tmp_path / "repo"
    rc, pairs = _run_runs(capsys, *_runs_args(workspace_root, tmp_path))
    assert rc == 0
    assert pairs == []
