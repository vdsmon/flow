"""Contract tests for trace_mine.py, the read-only transcript-extract miner."""

from __future__ import annotations

import json
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
    # never opens friction.jsonl (the "eaten" cross-check is child-2's job): no
    # import of the modules that would read/locate it.
    source = Path(trace_mine.__file__).read_text(encoding="utf-8")
    assert "import flow_friction" not in source
    assert "import _memory_paths" not in source
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
    # A descriptor tool_result can carry a stderr prefix with a stray brace
    # before the real JSON object. The boundary (and its drift marker) must
    # still be recovered by scanning past the first non-parsing "{".
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
        "\n".join(
            [
                "",
                "{not json at all",
                valid_desc,
                valid_use,
                "plain text, not even braces",
                '{"truncated": ',
                valid_err,
                "",
            ]
        ),
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
