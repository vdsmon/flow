"""Tests for the dep-edge link seam: tracker_cli.py `link` + adapter kind maps.

Three layers, each with its own fakes (test files here are standalone):
- CLI: injected `tracker_factory` records the link() call (test_tracker_cli.py pattern).
- Beads adapter: `_FakeRunner` returns sequenced CompletedProcess (test_beads_adapter.py).
- Jira adapter: `_Response`/`_FakeHttp` fake http (test_jira_adapter.py).

The direction contract these pin: `link(from, to, "blocks")` = from is blocked by
to. Beads renders it `bd dep add <from> <to> --type blocks`; Jira renders it
`inwardIssue=from` (the blocked issue), `outwardIssue=to` (the blocker).
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, cast, override

import pytest

import tracker as t
import tracker_beads as tb
import tracker_cli
import tracker_jira as tj
from tracker import TrackerError

# ─── CLI layer ───────────────────────────────────────────────────────────────


def _seed_workspace(root: Path, backend: str = "jira") -> None:
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


class _FakeTracker:
    """Records link() calls for the CLI dispatch tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def link(self, from_key: str, to_key: str, kind: str) -> None:
        self.calls.append((from_key, to_key, kind))


class _FailingTracker(_FakeTracker):
    @override
    def link(self, from_key: str, to_key: str, kind: str) -> None:
        raise TrackerError(f"issueLink 400 for {from_key}->{to_key}")


def _factory(tracker_obj: _FakeTracker):
    def make(_config):
        return tracker_obj

    return make


def test_link_default_kind_is_blocks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    tk = _FakeTracker()
    rc = tracker_cli.cli_main(
        ["--workspace-root", str(tmp_path), "link", "--from-key", "A", "--to-key", "B"],
        tracker_factory=_factory(tk),
    )
    assert rc == 0
    assert tk.calls == [("A", "B", "blocks")]
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": True, "from_key": "A", "to_key": "B", "kind": "blocks"}


def test_link_explicit_kind_forwarded(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    tk = _FakeTracker()
    rc = tracker_cli.cli_main(
        [
            "--workspace-root",
            str(tmp_path),
            "link",
            "--from-key",
            "A",
            "--to-key",
            "B",
            "--kind",
            "relates",
        ],
        tracker_factory=_factory(tk),
    )
    assert rc == 0
    assert tk.calls == [("A", "B", "relates")]


def test_link_tracker_error_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    rc = tracker_cli.cli_main(
        ["--workspace-root", str(tmp_path), "link", "--from-key", "A", "--to-key", "B"],
        tracker_factory=_factory(_FailingTracker()),
    )
    assert rc == 1
    assert "tracker error" in capsys.readouterr().err


def test_link_missing_to_key_is_argparse_error(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    with pytest.raises(SystemExit):
        tracker_cli.cli_main(
            ["--workspace-root", str(tmp_path), "link", "--from-key", "A"],
            tracker_factory=_factory(_FakeTracker()),
        )


# ─── Beads adapter layer ─────────────────────────────────────────────────────


def _cp(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class _FakeRunner:
    def __init__(self, responses: list[subprocess.CompletedProcess[str]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        if not self._responses:
            raise AssertionError(f"FakeRunner ran out of responses; got args={args!r}")
        return self._responses.pop(0)


def _build_beads(
    extra: list[subprocess.CompletedProcess[str]],
) -> tuple[tb.BeadsAdapter, _FakeRunner]:
    runner = _FakeRunner([_cp(stdout="bd version 1.0.4 (Homebrew)\n"), *extra])
    adapter = tb.BeadsAdapter({"prefix": "bd"}, runner=runner)
    runner.calls.clear()
    return adapter, runner


@pytest.mark.parametrize(
    ("kind", "expected_type"),
    [("blocks", "blocks"), ("depends_on", "blocks"), ("relates", "related")],
)
def test_beads_link_maps_kind(kind: str, expected_type: str) -> None:
    adapter, runner = _build_beads([_cp()])
    adapter.link("bd-2", "bd-1", kind)
    args = runner.calls[-1][0]
    assert args == ["bd", "dep", "add", "bd-2", "bd-1", "--type", expected_type]


def test_beads_link_unknown_kind_passes_through() -> None:
    adapter, runner = _build_beads([_cp()])
    adapter.link("bd-2", "bd-1", "parent-child")
    args = runner.calls[-1][0]
    assert args[-2:] == ["--type", "parent-child"]


def test_beads_link_nonzero_raises() -> None:
    adapter, _ = _build_beads([_cp(returncode=1, stderr="Error: no such issue\n")])
    with pytest.raises(TrackerError):
        adapter.link("bd-2", "bd-1", "blocks")


# ─── Jira adapter layer ──────────────────────────────────────────────────────


class _Response:
    def __init__(self, body: dict[str, Any] | list[Any] | None, status: int = 200) -> None:
        self.status = status
        self._payload = b"" if body is None else json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._payload


class _FakeHttp:
    def __init__(self, responses: list[Any]) -> None:
        self._iter = iter(responses)
        self.calls: list[urllib.request.Request] = []

    def __call__(self, req: urllib.request.Request) -> _Response:
        self.calls.append(req)
        try:
            entry = next(self._iter)
        except StopIteration as e:
            raise AssertionError(f"unexpected extra request: {req.method} {req.full_url}") from e
        if isinstance(entry, BaseException):
            raise entry
        return entry


def _body_dict(req: urllib.request.Request) -> dict[str, Any]:
    if req.data is None:
        return {}
    return cast("dict[str, Any]", json.loads(cast("bytes", req.data)))


def _make_jira(monkeypatch: pytest.MonkeyPatch, http: tj.HttpFn) -> tj.JiraAdapter:
    monkeypatch.setenv("ATLASSIAN_EMAIL", "you@example.com")
    monkeypatch.setenv("ATLASSIAN_API_TOKEN", "tok")
    cfg: dict[str, Any] = {"backend": "jira", "cloud_id": "cloud-xyz", "project_key": "FT"}
    return tj.JiraAdapter(cfg, http=http)


@pytest.mark.parametrize(
    ("kind", "expected_name"),
    [("blocks", "Blocks"), ("depends_on", "Blocks"), ("relates", "Relates")],
)
def test_jira_link_maps_kind(
    monkeypatch: pytest.MonkeyPatch, kind: str, expected_name: str
) -> None:
    http = _FakeHttp([_Response(None)])
    adapter = _make_jira(monkeypatch, http)
    adapter.link("FT-2", "FT-1", kind)
    sent = http.calls[0]
    assert sent.method == "POST"
    assert sent.full_url.endswith("/rest/api/3/issueLink")
    assert _body_dict(sent) == {
        "type": {"name": expected_name},
        "inwardIssue": {"key": "FT-2"},
        "outwardIssue": {"key": "FT-1"},
    }


def test_jira_link_unknown_kind_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _FakeHttp([_Response(None)])
    adapter = _make_jira(monkeypatch, http)
    adapter.link("FT-2", "FT-1", "Cloners")
    assert _body_dict(http.calls[0])["type"] == {"name": "Cloners"}


# ─── Cross-backend direction agreement ───────────────────────────────────────


def test_cross_backend_blocks_direction_agrees(monkeypatch: pytest.MonkeyPatch) -> None:
    # link(A, B, "blocks") = A is blocked by B. Beads puts A first in `dep add`;
    # Jira puts A as inwardIssue (the blocked issue). Both name A the dependent.
    beads, runner = _build_beads([_cp()])
    beads.link("A", "B", "blocks")
    assert runner.calls[-1][0] == ["bd", "dep", "add", "A", "B", "--type", "blocks"]

    http = _FakeHttp([_Response(None)])
    jira = _make_jira(monkeypatch, http)
    jira.link("A", "B", "blocks")
    body = _body_dict(http.calls[0])
    assert body["inwardIssue"] == {"key": "A"}
    assert body["outwardIssue"] == {"key": "B"}


def test_cross_backend_read_direction_agrees(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cross-backend read direction: from_key=dependent/blocked, to_key=blocker.
    beads_issue = {
        "id": "A",
        "title": "sample",
        "status": "open",
        "issue_type": "task",
        "priority": 2,
        "dependencies": [{"type": "blocks", "target": "B"}],
        "comments": [],
    }
    beads, _ = _build_beads([_cp(stdout=json.dumps([beads_issue]))])
    beads_ticket = beads.get("A")
    assert {"kind": "blocks", "from_key": "A", "to_key": "B"} in beads_ticket["links"]

    jira_payload = {
        "key": "A",
        "fields": {
            "summary": "sample",
            "description": {"type": "doc", "content": []},
            "status": {"name": "Open", "statusCategory": {"key": "new", "name": "To Do"}},
            "issuetype": {"name": "Task"},
            "priority": {"name": "Medium"},
            "assignee": None,
            "comment": {"comments": []},
            "parent": None,
            "attachment": [],
            "labels": [],
            "resolution": None,
            "issuelinks": [{"type": {"name": "Blocks"}, "inwardIssue": {"key": "B"}}],
        },
    }
    http = _FakeHttp([_Response(jira_payload), _Response([])])
    jira = _make_jira(monkeypatch, http)
    jira_ticket = jira.get("A")
    assert {"kind": "blocks", "from_key": "A", "to_key": "B"} in jira_ticket["links"]


def test_structural_import_ok() -> None:
    # Guards against an accidental import break across the three modules.
    assert callable(tracker_cli.cli_main)
    assert hasattr(tb.BeadsAdapter, "link")
    assert hasattr(tj.JiraAdapter, "link")
    assert t.Tracker is not None
