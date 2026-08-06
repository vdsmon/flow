from __future__ import annotations

import json
from pathlib import Path

import scrutinize_trace as st


def _event(ts: str, typ: str, content) -> str:
    return json.dumps({"timestamp": ts, "type": typ, "message": {"content": content}})


def _tool_use(tool_id: str, name: str, tool_input: dict) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def _write_session(tmp_path: Path, name: str, lines: list[str]) -> Path:
    path = tmp_path / f"{name}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _sample_session(tmp_path: Path, name: str = "sess-1") -> Path:
    lines = [
        _event("2026-08-03T10:00:00Z", "user", [{"type": "text", "text": "please fix the thing"}]),
        _event(
            "2026-08-03T10:00:05Z",
            "user",
            [{"type": "text", "text": "<command-message>flow:flow</command-message> args"}],
        ),
        _event(
            "2026-08-03T10:01:00Z",
            "assistant",
            [
                _tool_use(
                    "t1",
                    "Bash",
                    {
                        "command": "FLOW_HARNESS=x ./.flow/runtime/flow "
                        "--workspace-root . status --json"
                    },
                )
            ],
        ),
        _event(
            "2026-08-03T10:02:00Z",
            "assistant",
            [
                _tool_use(
                    "t2",
                    "Task",
                    {"description": "Implement the fix", "subagent_type": "general-purpose"},
                )
            ],
        ),
        _event(
            "2026-08-03T10:03:00Z",
            "assistant",
            [_tool_use("t3", "Bash", {"command": "false"})],
        ),
        _event(
            "2026-08-03T10:03:10Z",
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "t3",
                    "is_error": True,
                    "content": [{"type": "text", "text": "Exit code 1  boom"}],
                }
            ],
        ),
    ]
    return _write_session(tmp_path, name, lines)


def test_mine_session_extracts_all_signal_families(tmp_path):
    path = _sample_session(tmp_path)
    report = st.mine_session(path)
    assert report["first"] == "2026-08-03T10:00:00Z"
    assert report["last"] == "2026-08-03T10:03:10Z"
    plain, skill = report["user_messages"]
    assert plain["is_skill"] is False
    assert skill["is_skill"] is True
    assert report["flow_calls"] == [
        {
            "ts": "2026-08-03T10:01:00Z",
            "sub": "status",
            "command": "FLOW_HARNESS=x ./.flow/runtime/flow --workspace-root . status --json",
        }
    ]
    assert report["agent_spawns"][0]["type"] == "general-purpose"
    (error,) = report["tool_errors"]
    assert error["tool"] == "Bash"
    assert error["command"] == "false"
    assert "boom" in error["error"]


def test_mine_session_reads_string_content_user_messages(tmp_path):
    path = _write_session(
        tmp_path,
        "plain",
        [
            json.dumps(
                {
                    "timestamp": "2026-08-03T19:03:36Z",
                    "type": "user",
                    "message": {"content": "merged"},
                }
            )
        ],
    )
    (msg,) = st.mine_session(path)["user_messages"]
    assert msg["text"] == "merged"
    assert msg["is_skill"] is False


def test_mine_session_since_filters_signals_but_keeps_span(tmp_path):
    path = _sample_session(tmp_path)
    report = st.mine_session(path, since="2026-08-03T10:02:30Z")
    assert report["first"] == "2026-08-03T10:00:00Z"
    assert report["user_messages"] == []
    assert report["flow_calls"] == []
    assert report["agent_spawns"] == []
    assert len(report["tool_errors"]) == 1


def test_mine_session_joins_subagent_spans(tmp_path):
    path = _sample_session(tmp_path)
    sub_dir = tmp_path / path.stem / "subagents"
    sub_dir.mkdir(parents=True)
    (sub_dir / "agent-a1.jsonl").write_text(
        _event("2026-08-03T10:02:01Z", "assistant", [])
        + "\n"
        + _event("2026-08-03T10:09:00Z", "assistant", [])
        + "\n",
        encoding="utf-8",
    )
    (sub_dir / "agent-a1.meta.json").write_text(
        json.dumps({"description": "Implement the fix"}), encoding="utf-8"
    )
    (span,) = st.mine_session(path)["subagents"]
    assert span["first"] == "2026-08-03T10:02:01Z"
    assert span["last"] == "2026-08-03T10:09:00Z"
    assert span["description"] == "Implement the fix"


def test_subagent_spans_carry_their_own_flow_calls_and_errors(tmp_path):
    path = _sample_session(tmp_path)
    sub_dir = tmp_path / path.stem / "subagents"
    sub_dir.mkdir(parents=True)
    lines = [
        _event(
            "2026-08-03T10:05:00Z",
            "assistant",
            [
                _tool_use(
                    "s1",
                    "Bash",
                    {"command": "FLOW_HARNESS=x ./.flow/runtime/flow preflight probe"},
                )
            ],
        ),
        _event(
            "2026-08-03T10:05:30Z",
            "assistant",
            [_tool_use("s2", "Bash", {"command": "false"})],
        ),
        _event(
            "2026-08-03T10:05:40Z",
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "s2",
                    "is_error": True,
                    "content": [{"type": "text", "text": "Exit code 1 stage boom"}],
                }
            ],
        ),
    ]
    (sub_dir / "agent-e2e.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (span,) = st.mine_session(path)["subagents"]
    assert [c["sub"] for c in span["flow_calls"]] == ["preflight"]
    assert len(span["tool_errors"]) == 1
    assert "stage boom" in span["tool_errors"][0]["error"]


def test_subagent_signals_respect_since_window(tmp_path):
    path = _sample_session(tmp_path)
    sub_dir = tmp_path / path.stem / "subagents"
    sub_dir.mkdir(parents=True)
    (sub_dir / "agent-old.jsonl").write_text(
        _event(
            "2026-08-01T09:00:00Z",
            "assistant",
            [
                _tool_use(
                    "o1",
                    "Bash",
                    {"command": "FLOW_HARNESS=x ./.flow/runtime/flow preflight probe"},
                )
            ],
        )
        + "\n",
        encoding="utf-8",
    )
    (span,) = st.mine_session(path, since="2026-08-03T00:00:00Z")["subagents"]
    assert span["flow_calls"] == []
    assert span["first"] == "2026-08-01T09:00:00Z"


def test_mine_dir_drops_sessions_entirely_before_window(tmp_path):
    _sample_session(tmp_path, "old")
    fresh = _write_session(
        tmp_path,
        "fresh",
        [_event("2026-08-04T09:00:00Z", "user", [{"type": "text", "text": "hi"}])],
    )
    reports = st.mine_dir(tmp_path, since="2026-08-04T00:00:00Z")
    assert [r["session"] for r in reports] == ["fresh"]
    assert fresh.exists()


def test_mine_dir_session_filter(tmp_path):
    _sample_session(tmp_path, "one")
    _sample_session(tmp_path, "two")
    reports = st.mine_dir(tmp_path, sessions=["two"])
    assert [r["session"] for r in reports] == ["two"]


def test_cli_missing_dir_exits_2_and_json_round_trips(tmp_path, capsys):
    assert st.cli_main(["--transcript-dir", str(tmp_path / "nope")]) == 2
    capsys.readouterr()
    _sample_session(tmp_path)
    assert st.cli_main(["--transcript-dir", str(tmp_path), "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed[0]["flow_calls"][0]["sub"] == "status"


def test_mine_dir_counts_forked_shared_prefix_once(tmp_path):
    # A forked session repeats its parent's transcript verbatim before diverging
    # (witnessed 2026-08-06: 693 shared lines, 10 double-counted errors).
    parent = _sample_session(tmp_path, "parent")
    shared = parent.read_text(encoding="utf-8")
    fork_tail = [
        _event(
            "2026-08-03T11:00:00Z",
            "assistant",
            [_tool_use("f1", "Bash", {"command": "true"})],
        ),
        _event(
            "2026-08-03T11:00:10Z",
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "f1",
                    "is_error": True,
                    "content": [{"type": "text", "text": "Exit code 1  fork-only"}],
                }
            ],
        ),
    ]
    fork = tmp_path / "fork.jsonl"
    fork.write_text(shared + "\n".join(fork_tail) + "\n", encoding="utf-8")
    import os
    import time

    now = time.time()
    os.utime(parent, (now - 100, now - 100))
    os.utime(fork, (now, now))
    reports = {r["session"]: r for r in st.mine_dir(tmp_path)}
    total_errors = sum(len(r["tool_errors"]) for r in reports.values())
    assert total_errors == 2
    assert len(reports["parent"]["tool_errors"]) == 1
    fork_report = reports["fork"]
    assert len(fork_report["tool_errors"]) == 1
    assert "fork-only" in fork_report["tool_errors"][0]["error"]
    assert fork_report["shared_prefix_lines"] == reports["parent"]["lines"]
    assert fork_report["first"] == "2026-08-03T11:00:00Z"


def test_mine_dir_fork_result_joins_prefix_tool_use(tmp_path):
    # The error result lands after the fork point but its tool_use sits in the
    # shared prefix; the join must survive the dedup.
    lines = [
        _event(
            "2026-08-03T10:00:00Z",
            "assistant",
            [_tool_use("x1", "Bash", {"command": "slowcmd"})],
        ),
    ]
    parent = _write_session(tmp_path, "p2", lines)
    fork = tmp_path / "f2.jsonl"
    fork.write_text(
        parent.read_text(encoding="utf-8")
        + _event(
            "2026-08-03T10:05:00Z",
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "x1",
                    "is_error": True,
                    "content": [{"type": "text", "text": "Exit code 1  late"}],
                }
            ],
        )
        + "\n",
        encoding="utf-8",
    )
    import os
    import time

    now = time.time()
    os.utime(parent, (now - 100, now - 100))
    os.utime(fork, (now, now))
    reports = {r["session"]: r for r in st.mine_dir(tmp_path)}
    (error,) = reports["f2"]["tool_errors"]
    assert error["command"] == "slowcmd"
