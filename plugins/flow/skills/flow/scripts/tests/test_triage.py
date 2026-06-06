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
    runner = _FakeRunner([_version_ok(), _cp(stdout="[]")])
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
    runner = _FakeRunner([_version_ok(), _cp(stdout=list_json), _show(issue)])
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
    runner = _FakeRunner([_version_ok(), _cp(stdout=list_json), _show(issue)])
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
    runner = _FakeRunner([_version_ok(), _cp(stdout=list_json), _show(issue)])
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
    runner = _FakeRunner([_version_ok(), _cp(stdout=list_json), _show(issue)])
    code, out, _ = _run(["--workspace-root", str(tmp_path)], runner)
    assert code == 0
    assert "which backend should win the tie" in out


def test_zero_comment_deferred_shows_placeholder(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    list_json = json.dumps([{"id": "flow-z", "title": "Quiet bead"}])
    issue = {"id": "flow-z", "title": "Quiet bead", "status": "deferred", "comments": []}
    runner = _FakeRunner([_version_ok(), _cp(stdout=list_json), _show(issue)])
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
    runner = _FakeRunner([_version_ok(), _cp(stdout=list_bare), _show(issue_a), _show(issue_b)])
    code, out, _ = _run(["--workspace-root", str(tmp_path)], runner)
    assert code == 0
    assert "flow-a" in out
    assert "flow-b" in out

    # wrapper {"issues": [...]} shape
    list_wrap = json.dumps({"issues": [{"id": "flow-a", "title": "Alpha"}]})
    runner2 = _FakeRunner([_version_ok(), _cp(stdout=list_wrap), _show(issue_a)])
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
    runner = _FakeRunner([_version_ok(), _cp(stdout=list_json), _show(issue)])
    code, out, _ = _run(["--workspace-root", str(tmp_path), "--json"], runner)
    assert code == 0
    payload = json.loads(out)
    assert payload[0]["key"] == "flow-j"
    assert payload[0]["title"] == "JSON bead"
    assert "which backend" in payload[0]["open_question"]


def test_workspace_not_initialized_exits_1(tmp_path: Path) -> None:
    runner = _FakeRunner([])
    code, _, err = _run(["--workspace-root", str(tmp_path)], runner)
    assert code == 1
    assert "/flow init" in err
    assert runner.calls == []


def test_render_table_empty_is_pure_sentinel() -> None:
    assert triage.render_table([]) == "(no deferred tickets)"
