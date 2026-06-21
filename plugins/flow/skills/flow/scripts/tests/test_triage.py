"""Tests for triage.py — surface the deferred queue + each bead's open question.

Offline, `_FakeRunner`-driven (mirrors test_beads_adapter.py): the runner returns
a sequence of `subprocess.CompletedProcess` objects, the first being the
`bd version` preflight response BeadsAdapter construction consumes. No live `bd`.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
from pathlib import Path
from typing import Any

import triage


def _cp(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class _FakeRunner:
    """Sequenced subprocess fake. Returns the next response per `run()` call."""

    def __init__(self, responses: list[subprocess.CompletedProcess[str]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        if not self._responses:
            raise AssertionError(f"FakeRunner ran out of responses; got call args={args!r}")
        return self._responses.pop(0)


def _version_ok() -> subprocess.CompletedProcess[str]:
    return _cp(stdout="bd version 1.0.4 (Homebrew)\n")


def _seed_workspace(root: Path, backend: str = "beads") -> None:
    flow = root / ".flow"
    flow.mkdir(parents=True, exist_ok=True)
    if backend == "jira":
        body = (
            '[tracker]\nbackend = "jira"\n\n'
            '[tracker.jira]\ncloud_id = "x"\nproject_key = "FT"\n\n'
            '[memory]\nnamespace = "demo"\n'
        )
    else:
        body = (
            '[tracker]\nbackend = "beads"\n\n'
            '[tracker.beads]\nprefix = "bd"\n\n'
            '[memory]\nnamespace = "demo"\n'
        )
    (flow / "workspace.toml").write_text(body, encoding="utf-8")


def _run(argv: list[str], runner: _FakeRunner) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = triage.cli_main(argv, runner=runner)
    return code, out.getvalue(), err.getvalue()


def _show(issue: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    # bd >=1.0 `show --json` wraps the issue in a single-element list.
    return _cp(stdout=json.dumps([issue]))


def _comment(body: str, created_at: str, *, cid: str = "c", author: str = "x") -> dict[str, Any]:
    return {"id": cid, "author": author, "body": body, "created_at": created_at}


def _decided_show(
    *,
    comments: list[dict[str, Any]] | None = None,
    labels: list[str] | None = None,
    key: str = "flow-x",
) -> subprocess.CompletedProcess[str]:
    """`bd show --include-comments --json` payload: comments keyed under `text`."""
    issue = {
        "id": key,
        "title": "T",
        "status": "open",
        "labels": labels or [],
        "comments": comments or [],
    }
    return _cp(stdout=json.dumps([issue]))


def _tc(text: str, created_at: str, *, cid: str = "c") -> dict[str, Any]:
    """A decided-probe comment (body under `text`)."""
    return {"id": cid, "author": "x", "text": text, "created_at": created_at}


_DEFER_REAL = (
    "flow --auto could not self-approve (HOT lease/mutex machinery): the plan "
    "needs a decision on whether to serialize the drain. To unstick: answer here."
)
_DEFER_COLON = (
    "flow --auto could not self-approve: which backend should win the tie. "
    "To unstick: answer here, reopen, re-run WITHOUT --auto."
)


def test_non_beads_backend_short_circuits(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="jira")
    runner = _FakeRunner([])  # never invoked: no preflight, no bd list
    code, out, _ = _run(["--workspace-root", str(tmp_path)], runner)
    assert code == 0
    assert "beads concept" in out
    assert runner.calls == []


def test_no_deferred_prints_sentinel(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    # deferred list empty, blocked list empty
    runner = _FakeRunner([_version_ok(), _cp(stdout="[]"), _cp(stdout="[]")])
    code, out, _ = _run(["--workspace-root", str(tmp_path)], runner)
    assert code == 0
    assert "(no deferred tickets)" in out


def test_one_deferred_surfaces_title_and_defer_comment(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    list_json = json.dumps([{"id": "flow-wo5", "title": "Serialize the drain", "summary": "WRONG"}])
    issue = {
        "id": "flow-wo5",
        "title": "Serialize the drain",
        "status": "deferred",
        "comments": [_comment(_DEFER_REAL, "2026-06-01T10:00:00Z")],
    }
    runner = _FakeRunner([_version_ok(), _cp(stdout=list_json), _cp(stdout="[]"), _show(issue)])
    code, out, _ = _run(["--workspace-root", str(tmp_path)], runner)
    assert code == 0
    assert "flow-wo5" in out
    assert "Serialize the drain" in out
    assert "HOT lease/mutex machinery" in out
    assert "WRONG" not in out  # title key read, not summary


def test_stem_match_beats_later_plain_human_comment(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    list_json = json.dumps([{"id": "flow-x", "title": "T"}])
    issue = {
        "id": "flow-x",
        "title": "T",
        "status": "deferred",
        "comments": [
            _comment(_DEFER_REAL, "2026-06-01T10:00:00Z", cid="1"),
            _comment("just a human note, unrelated", "2026-06-02T10:00:00Z", cid="2"),
        ],
    }
    runner = _FakeRunner([_version_ok(), _cp(stdout=list_json), _cp(stdout="[]"), _show(issue)])
    code, out, _ = _run(["--workspace-root", str(tmp_path)], runner)
    assert code == 0
    assert "HOT lease/mutex machinery" in out
    assert "just a human note" not in out


def test_redefer_picks_last_stem_comment(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    list_json = json.dumps([{"id": "flow-x", "title": "T"}])
    issue = {
        "id": "flow-x",
        "title": "T",
        "status": "deferred",
        "comments": [
            _comment(
                "flow --auto could not self-approve: OLD question.",
                "2026-06-01T10:00:00Z",
                cid="1",
            ),
            _comment(
                "flow --auto could not self-approve: NEW question.",
                "2026-06-03T10:00:00Z",
                cid="2",
            ),
        ],
    }
    runner = _FakeRunner([_version_ok(), _cp(stdout=list_json), _cp(stdout="[]"), _show(issue)])
    code, out, _ = _run(["--workspace-root", str(tmp_path)], runner)
    assert code == 0
    assert "NEW question" in out
    assert "OLD question" not in out


def test_colon_format_matches(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    list_json = json.dumps([{"id": "flow-c", "title": "T"}])
    issue = {
        "id": "flow-c",
        "title": "T",
        "status": "deferred",
        "comments": [_comment(_DEFER_COLON, "2026-06-01T10:00:00Z")],
    }
    runner = _FakeRunner([_version_ok(), _cp(stdout=list_json), _cp(stdout="[]"), _show(issue)])
    code, out, _ = _run(["--workspace-root", str(tmp_path)], runner)
    assert code == 0
    assert "which backend should win the tie" in out


def test_zero_comment_deferred_shows_placeholder(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    list_json = json.dumps([{"id": "flow-z", "title": "Quiet bead"}])
    issue = {"id": "flow-z", "title": "Quiet bead", "status": "deferred", "comments": []}
    runner = _FakeRunner([_version_ok(), _cp(stdout=list_json), _cp(stdout="[]"), _show(issue)])
    code, out, _ = _run(["--workspace-root", str(tmp_path)], runner)
    assert code == 0
    assert "flow-z" in out
    assert "(no open-question comment)" in out


def test_two_deferred_both_json_shapes(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    # bare-list shape
    list_bare = json.dumps([{"id": "flow-a", "title": "Alpha"}, {"id": "flow-b", "title": "Beta"}])
    issue_a = {
        "id": "flow-a",
        "title": "Alpha",
        "status": "deferred",
        "comments": [_comment(_DEFER_COLON, "2026-06-01T10:00:00Z")],
    }
    issue_b = {"id": "flow-b", "title": "Beta", "status": "deferred", "comments": []}
    runner = _FakeRunner(
        [_version_ok(), _cp(stdout=list_bare), _cp(stdout="[]"), _show(issue_a), _show(issue_b)]
    )
    code, out, _ = _run(["--workspace-root", str(tmp_path)], runner)
    assert code == 0
    assert "flow-a" in out
    assert "flow-b" in out

    # wrapper {"issues": [...]} shape
    list_wrap = json.dumps({"issues": [{"id": "flow-a", "title": "Alpha"}]})
    runner2 = _FakeRunner([_version_ok(), _cp(stdout=list_wrap), _cp(stdout="[]"), _show(issue_a)])
    code2, out2, _ = _run(["--workspace-root", str(tmp_path)], runner2)
    assert code2 == 0
    assert "flow-a" in out2


def test_json_flag_emits_raw_structure(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    list_json = json.dumps([{"id": "flow-j", "title": "JSON bead"}])
    issue = {
        "id": "flow-j",
        "title": "JSON bead",
        "status": "deferred",
        "comments": [_comment(_DEFER_COLON, "2026-06-01T10:00:00Z")],
    }
    runner = _FakeRunner([_version_ok(), _cp(stdout=list_json), _cp(stdout="[]"), _show(issue)])
    code, out, _ = _run(["--workspace-root", str(tmp_path), "--json"], runner)
    assert code == 0
    payload = json.loads(out)
    assert payload[0]["key"] == "flow-j"
    assert payload[0]["title"] == "JSON bead"
    assert payload[0]["status"] == "deferred"
    assert "which backend" in payload[0]["open_question"]


def test_workspace_not_initialized_exits_1(tmp_path: Path) -> None:
    runner = _FakeRunner([])
    code, _, err = _run(["--workspace-root", str(tmp_path)], runner)
    assert code == 1
    assert "/flow init" in err
    assert runner.calls == []


def test_render_table_empty_is_pure_sentinel() -> None:
    assert triage.render_table([]) == "(no deferred tickets)"


# ─── list: subcommand back-compat + blocked-bead surfacing ───────────────────


def test_list_explicit_subcommand_matches_bare(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    list_json = json.dumps([{"id": "flow-wo5", "title": "Serialize the drain"}])
    issue = {
        "id": "flow-wo5",
        "title": "Serialize the drain",
        "status": "deferred",
        "comments": [_comment(_DEFER_REAL, "2026-06-01T10:00:00Z")],
    }
    runner = _FakeRunner([_version_ok(), _cp(stdout=list_json), _cp(stdout="[]"), _show(issue)])
    code, out, _ = _run(["list", "--workspace-root", str(tmp_path)], runner)
    assert code == 0
    assert "flow-wo5" in out
    assert "deferred" in out


def test_list_surfaces_blocked_with_stem_not_bare_blocked(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    deferred_json = json.dumps([{"id": "flow-d", "title": "A deferred bead"}])
    blocked_json = json.dumps(
        [
            {"id": "flow-hb", "title": "Hot block"},
            {"id": "flow-dag", "title": "Dependency hold"},
        ]
    )
    issue_d = {
        "id": "flow-d",
        "title": "A deferred bead",
        "status": "deferred",
        "comments": [_comment(_DEFER_COLON, "2026-06-01T10:00:00Z")],
    }
    issue_hb = {
        "id": "flow-hb",
        "title": "Hot block",
        "status": "blocked",
        "comments": [_tc(_DEFER_REAL, "2026-06-02T10:00:00Z")],
    }
    issue_dag = {
        "id": "flow-dag",
        "title": "Dependency hold",
        "status": "blocked",
        "comments": [_tc("blocked on flow-d (dependency)", "2026-06-02T10:00:00Z")],
    }
    runner = _FakeRunner(
        [
            _version_ok(),
            _cp(stdout=deferred_json),
            _cp(stdout=blocked_json),
            _show(issue_d),
            _show(issue_hb),
            _show(issue_dag),
        ]
    )
    code, out, _ = _run(["--workspace-root", str(tmp_path), "--json"], runner)
    assert code == 0
    payload = json.loads(out)
    keys = {row["key"]: row["status"] for row in payload}
    assert keys == {"flow-d": "deferred", "flow-hb": "blocked"}
    assert "flow-dag" not in keys  # bare dependency hold not surfaced


# ─── list: --ready opt-in + queue tagging ─────────────────────────────────────


def test_list_ready_adds_ready_rows_partitioned_by_queue(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    ready_json = json.dumps(
        [
            {"id": "flow-ev", "title": "Evolve bead", "labels": ["evolve"]},
            {"id": "flow-dj", "title": "Day job bead"},
            {"id": "flow-pr", "title": "Proposal bead", "labels": ["proposal"]},
        ]
    )
    runner = _FakeRunner(
        [_version_ok(), _cp(stdout="[]"), _cp(stdout="[]"), _cp(stdout=ready_json)]
    )
    code, out, _ = _run(["--workspace-root", str(tmp_path), "--ready", "--json"], runner)
    assert code == 0
    payload = json.loads(out)
    rows = {r["key"]: r for r in payload}
    assert set(rows) == {"flow-ev", "flow-dj", "flow-pr"}
    assert all(r["status"] == "ready" for r in payload)
    assert all(r["open_question"] == "" for r in payload)
    assert rows["flow-ev"]["queue"] == "evolve"
    assert rows["flow-dj"]["queue"] == "day-job"
    assert rows["flow-pr"]["queue"] == "day-job"
    assert ["bd", "ready", "--json"] in [c[0] for c in runner.calls]
    assert not any("show" in c[0] for c in runner.calls)  # ready rows get no per-bead show


def test_list_default_makes_no_ready_call(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    runner = _FakeRunner([_version_ok(), _cp(stdout="[]"), _cp(stdout="[]")])
    code, _, _ = _run(["--workspace-root", str(tmp_path)], runner)
    assert code == 0
    assert not any("ready" in c[0] for c in runner.calls)


def test_queue_field_on_deferred_and_blocked_rows(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    deferred_json = json.dumps([{"id": "flow-d", "title": "Deferred evolve", "labels": ["evolve"]}])
    blocked_json = json.dumps([{"id": "flow-hb", "title": "Hot block"}])
    issue_d = {
        "id": "flow-d",
        "title": "Deferred evolve",
        "status": "deferred",
        "comments": [_comment(_DEFER_COLON, "2026-06-01T10:00:00Z")],
    }
    issue_hb = {
        "id": "flow-hb",
        "title": "Hot block",
        "status": "blocked",
        "comments": [_tc(_DEFER_REAL, "2026-06-02T10:00:00Z")],
    }
    runner = _FakeRunner(
        [
            _version_ok(),
            _cp(stdout=deferred_json),
            _cp(stdout=blocked_json),
            _show(issue_d),
            _show(issue_hb),
        ]
    )
    code, out, _ = _run(["--workspace-root", str(tmp_path), "--json"], runner)
    assert code == 0
    payload = json.loads(out)
    queues = {row["key"]: row["queue"] for row in payload}
    assert queues == {"flow-d": "evolve", "flow-hb": "day-job"}


def test_list_ready_wrapper_shape(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    ready_wrap = json.dumps({"issues": [{"id": "flow-r", "title": "Ready", "labels": ["evolve"]}]})
    runner = _FakeRunner(
        [_version_ok(), _cp(stdout="[]"), _cp(stdout="[]"), _cp(stdout=ready_wrap)]
    )
    code, out, _ = _run(["--workspace-root", str(tmp_path), "--ready", "--json"], runner)
    assert code == 0
    payload = json.loads(out)
    assert len(payload) == 1
    assert payload[0]["key"] == "flow-r"
    assert payload[0]["status"] == "ready"
    assert payload[0]["queue"] == "evolve"


def test_render_table_has_queue_column() -> None:
    queueless = [{"key": "flow-a", "status": "deferred", "title": "T", "open_question": "q"}]
    table = triage.render_table(queueless)
    header = table.splitlines()[0]
    assert "QUEUE" in header
    assert header.index("STATUS") < header.index("QUEUE") < header.index("TITLE")
    tagged = [
        {"key": "flow-b", "status": "ready", "queue": "day-job", "title": "T", "open_question": ""}
    ]
    assert "day-job" in triage.render_table(tagged)


def test_ready_with_no_rows_keeps_sentinel(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    runner = _FakeRunner([_version_ok(), _cp(stdout="[]"), _cp(stdout="[]"), _cp(stdout="[]")])
    code, out, _ = _run(["--workspace-root", str(tmp_path), "--ready"], runner)
    assert code == 0
    assert "(no deferred tickets)" in out


# ─── decided probe ───────────────────────────────────────────────────────────


def _run_decided(
    tmp_path: Path, argv_tail: list[str], runner: _FakeRunner
) -> tuple[int, dict[str, Any]]:
    code, out, _ = _run(["decided", "--workspace-root", str(tmp_path), *argv_tail], runner)
    return code, json.loads(out)


def test_decided_legacy_decision_stem(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    comments = [
        _tc(_DEFER_REAL, "2026-06-01T10:00:00Z", cid="1"),
        _tc(
            "DECISION: FIX, branch-gated behind the drift guard; serialize via the existing lease.",
            "2026-06-03T10:00:00Z",
            cid="2",
        ),
    ]
    runner = _FakeRunner([_version_ok(), _decided_show(comments=comments, key="flow-2pp")])
    code, result = _run_decided(tmp_path, ["--key", "flow-2pp"], runner)
    assert code == 0
    assert result["decided"] is True
    assert result["answer"].startswith("FIX, branch-gated")
    assert "could not self-approve" not in result["answer"]


def test_decided_new_triage_decision_stem(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    comments = [_tc("TRIAGE-DECISION: build it the simple way.", "2026-06-03T10:00:00Z")]
    runner = _FakeRunner([_version_ok(), _decided_show(comments=comments)])
    code, result = _run_decided(tmp_path, ["--key", "flow-x"], runner)
    assert code == 0
    assert result["decided"] is True
    assert result["answer"] == "build it the simple way."


def test_decided_start_anchored_no_false_positive(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    comments = [
        _tc(
            "flow --auto could not self-approve: please record the decision on scope.",
            "2026-06-01T10:00:00Z",
        )
    ]
    runner = _FakeRunner([_version_ok(), _decided_show(comments=comments)])
    code, result = _run_decided(tmp_path, ["--key", "flow-x"], runner)
    assert code == 0
    assert result["decided"] is False
    assert result["answer"] is None


def test_decided_newest_decision_wins(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    comments = [
        _tc("DECISION: old answer.", "2026-06-01T10:00:00Z", cid="1"),
        _tc("TRIAGE-DECISION: new answer.", "2026-06-05T10:00:00Z", cid="2"),
    ]
    runner = _FakeRunner([_version_ok(), _decided_show(comments=comments)])
    code, result = _run_decided(tmp_path, ["--key", "flow-x"], runner)
    assert code == 0
    assert result["answer"] == "new answer."


def test_decided_clean_files_no_hot_label(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    comments = [_tc("DECISION: ship it.", "2026-06-03T10:00:00Z")]
    runner = _FakeRunner([_version_ok(), _decided_show(comments=comments, labels=["proposal"])])
    code, result = _run_decided(tmp_path, ["--key", "flow-x", "--files", "recall.py"], runner)
    assert code == 0
    assert result["decided"] is True
    assert result["is_hot"] is False


def test_decided_hot_via_label(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    comments = [_tc("DECISION: ship it.", "2026-06-03T10:00:00Z")]
    runner = _FakeRunner(
        [_version_ok(), _decided_show(comments=comments, labels=["hot", "proposal"])]
    )
    code, result = _run_decided(tmp_path, ["--key", "flow-x", "--files", "recall.py"], runner)
    assert code == 0
    assert result["is_hot"] is True


def test_decided_hot_via_guard_set(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    comments = [_tc("DECISION: ship it.", "2026-06-03T10:00:00Z")]
    runner = _FakeRunner([_version_ok(), _decided_show(comments=comments, labels=["proposal"])])
    code, result = _run_decided(tmp_path, ["--key", "flow-x", "--files", "snapshot.py"], runner)
    assert code == 0
    assert result["is_hot"] is True


def test_decided_untriaged_no_decision(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    comments = [_tc(_DEFER_REAL, "2026-06-01T10:00:00Z")]
    runner = _FakeRunner([_version_ok(), _decided_show(comments=comments)])
    code, result = _run_decided(tmp_path, ["--key", "flow-x"], runner)
    assert code == 0
    assert result["decided"] is False
    assert result["answer"] is None


def test_decided_bd_read_fail_blocks(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    runner = _FakeRunner([_version_ok(), _cp(returncode=1, stderr="boom")])
    code, result = _run_decided(tmp_path, ["--key", "flow-x"], runner)
    assert code == 0
    assert result == {"decided": False, "answer": None, "is_hot": True}


def test_decided_hotness_indeterminate_blocks(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    comments = [_tc("DECISION: ship it.", "2026-06-03T10:00:00Z")]
    runner = _FakeRunner([_version_ok(), _decided_show(comments=comments, labels=["proposal"])])
    # no --files, no hot label -> default to hot
    code, result = _run_decided(tmp_path, ["--key", "flow-x"], runner)
    assert code == 0
    assert result["decided"] is True
    assert result["is_hot"] is True


def test_is_hot_change_unit() -> None:
    assert triage.is_hot_change(["scripts/lease.py"]) is True
    assert triage.is_hot_change(["lease.py"]) is True
    assert triage.is_hot_change(["recall.py"]) is False
    assert triage.is_hot_change([]) is False
    for guard in triage._GUARD_FILES:
        assert triage.is_hot_change([f"some/path/{guard}"]) is True


# ─── lane resolver (spec-time twin of flow_worktree._lane_for_bead) ───────────


def _run_lane(tmp_path: Path, key: str, runner: _FakeRunner) -> tuple[int, str]:
    code, out, _ = _run(["lane", "--workspace-root", str(tmp_path), "--key", key], runner)
    return code, out.strip()


def test_lane_trivial_resolves_express(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    runner = _FakeRunner([_version_ok(), _decided_show(labels=["evolve", "tier:trivial"])])
    code, out = _run_lane(tmp_path, "flow-x", runner)
    assert code == 0
    assert out == "express"


def test_lane_light_resolves_light(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    runner = _FakeRunner([_version_ok(), _decided_show(labels=["evolve", "tier:light"])])
    code, out = _run_lane(tmp_path, "flow-x", runner)
    assert code == 0
    assert out == "light"


def test_lane_untiered_resolves_full(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    runner = _FakeRunner([_version_ok(), _decided_show(labels=["evolve"])])
    code, out = _run_lane(tmp_path, "flow-x", runner)
    assert code == 0
    assert out == "full"


def test_lane_hot_overrides_tier(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    runner = _FakeRunner([_version_ok(), _decided_show(labels=["hot", "tier:trivial"])])
    code, out = _run_lane(tmp_path, "flow-x", runner)
    assert code == 0
    assert out == "full"


def test_lane_bd_read_fail_is_full(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    runner = _FakeRunner([_version_ok(), _cp(returncode=1, stderr="boom")])
    code, out = _run_lane(tmp_path, "flow-x", runner)
    assert code == 0
    assert out == "full"  # fail-open: a flaky read never silently downshifts


def test_lane_non_beads_is_full(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="jira")
    runner = _FakeRunner([])  # never invoked: tiers are a beads concept
    code, out = _run_lane(tmp_path, "FT-1", runner)
    assert code == 0
    assert out == "full"
    assert runner.calls == []


# --- advisor_adjudicates flag + adjudicate-enabled CLI ------------------------


def _seed_evolve(root: Path, body: str) -> None:
    """Append an `[evolve]` section to the seeded workspace.toml."""
    path = root / ".flow" / "workspace.toml"
    path.write_text(path.read_text(encoding="utf-8") + body, encoding="utf-8")


def test_advisor_adjudicates_true_when_explicitly_true(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    _seed_evolve(tmp_path, "\n[evolve]\nadvisor_adjudicates = true\n")
    assert triage.advisor_adjudicates(tmp_path) is True


def test_advisor_adjudicates_default_on_when_key_absent(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    _seed_evolve(tmp_path, "\n[evolve]\nauto_merge_hot = true\n")
    assert triage.advisor_adjudicates(tmp_path) is True


def test_advisor_adjudicates_default_on_when_section_absent(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    assert triage.advisor_adjudicates(tmp_path) is True


def test_advisor_adjudicates_false_only_when_explicitly_false(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    _seed_evolve(tmp_path, "\n[evolve]\nadvisor_adjudicates = false\n")
    assert triage.advisor_adjudicates(tmp_path) is False


def test_advisor_adjudicates_default_on_when_no_workspace(tmp_path: Path) -> None:
    # absent workspace.toml -> WorkspaceConfigError -> default on
    assert triage.advisor_adjudicates(tmp_path) is True


def test_adjudicate_enabled_cli_prints_true_by_default(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")  # no [evolve] -> default on
    code, out, _ = _run(["adjudicate-enabled", "--workspace-root", str(tmp_path)], _FakeRunner([]))
    assert code == 0
    assert out.strip() == "true"


def test_adjudicate_enabled_cli_prints_false_when_opted_out(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    _seed_evolve(tmp_path, "\n[evolve]\nadvisor_adjudicates = false\n")
    code, out, _ = _run(["adjudicate-enabled", "--workspace-root", str(tmp_path)], _FakeRunner([]))
    assert code == 0
    assert out.strip() == "false"


# --- adjudicate_hot flag + adjudicate-hot-enabled CLI ------------------------


def test_adjudicate_hot_true_when_explicitly_true(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    _seed_evolve(tmp_path, "\n[evolve]\nadjudicate_hot = true\n")
    assert triage.adjudicate_hot(tmp_path) is True


def test_adjudicate_hot_default_off_when_key_absent(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    _seed_evolve(tmp_path, "\n[evolve]\nauto_merge_hot = true\n")
    assert triage.adjudicate_hot(tmp_path) is False


def test_adjudicate_hot_default_off_when_section_absent(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    assert triage.adjudicate_hot(tmp_path) is False


def test_adjudicate_hot_false_when_explicitly_false(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    _seed_evolve(tmp_path, "\n[evolve]\nadjudicate_hot = false\n")
    assert triage.adjudicate_hot(tmp_path) is False


def test_adjudicate_hot_default_off_when_no_workspace(tmp_path: Path) -> None:
    # absent workspace.toml -> WorkspaceConfigError -> default off
    assert triage.adjudicate_hot(tmp_path) is False


def test_adjudicate_hot_enabled_cli_prints_false_by_default(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")  # no [evolve] -> default off
    code, out, _ = _run(
        ["adjudicate-hot-enabled", "--workspace-root", str(tmp_path)], _FakeRunner([])
    )
    assert code == 0
    assert out.strip() == "false"


def test_adjudicate_hot_enabled_cli_prints_true_when_opted_in(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    _seed_evolve(tmp_path, "\n[evolve]\nadjudicate_hot = true\n")
    code, out, _ = _run(
        ["adjudicate-hot-enabled", "--workspace-root", str(tmp_path)], _FakeRunner([])
    )
    assert code == 0
    assert out.strip() == "true"


def test_render_table_tags_advisor_rows() -> None:
    rows = [
        {
            "key": "flow-a",
            "status": "blocked",
            "title": "T",
            "open_question": "flow --auto could not self-approve: advisor ruled X (advisor) blocked auto-ship",
        },
        {
            "key": "flow-b",
            "status": "deferred",
            "title": "T2",
            "open_question": "needs a human answer",
        },
    ]
    table = triage.render_table(rows)
    a_line = next(line for line in table.splitlines() if line.startswith("flow-a"))
    b_line = next(line for line in table.splitlines() if line.startswith("flow-b"))
    assert "blocked (advisor)" in a_line
    assert "(advisor)" not in b_line


def test_recorded_decision_accepts_advisor_stem() -> None:
    comments = [
        {
            "text": "DECISION: (advisor) ship option A; blast radius is contained",
            "created_at": "2026-06-08T10:00:00Z",
        }
    ]
    assert (
        triage._recorded_decision(comments) == "(advisor) ship option A; blast radius is contained"
    )


def test_recorded_decision_accepts_freeform_maintainer_stem() -> None:
    # flow-rvc: a freeform `MAINTAINER DECISION <date>:` comment now reads as a
    # recorded decision (was a false negative under the old startswith stems).
    comments = [
        {
            "text": "MAINTAINER DECISION 2026-06-10: ship the regex, gate the hot branch.",
            "created_at": "2026-06-10T10:00:00Z",
        }
    ]
    assert triage._recorded_decision(comments) == "ship the regex, gate the hot branch."


def test_recorded_decision_case_sensitive_lowercase_no_match() -> None:
    # case-sensitive: lowercase prose `decision:` must not match.
    comments = [
        {
            "text": "decision: this is prose, not a recorded decision",
            "created_at": "2026-06-10T10:00:00Z",
        }
    ]
    assert triage._recorded_decision(comments) is None


def test_decided_freeform_maintainer_stem(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    comments = [_tc("MAINTAINER DECISION 2026-06-10: build it.", "2026-06-10T10:00:00Z")]
    runner = _FakeRunner([_version_ok(), _decided_show(comments=comments)])
    code, result = _run_decided(tmp_path, ["--key", "flow-x"], runner)
    assert code == 0
    assert result["decided"] is True
    assert result["answer"] == "build it."
