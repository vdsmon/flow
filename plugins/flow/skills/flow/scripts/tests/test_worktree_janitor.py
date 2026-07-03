from __future__ import annotations

import json
import subprocess

import worktree_janitor as wj


def _cp(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _porcelain(entries: list[tuple[str, str | None, str]]) -> str:
    """Render `git worktree list --porcelain` from (path, branch, head_sha) triples.

    Distinct head shas per entry: tip==head_oid equality is the reap safety gate.
    None branch -> detached.
    """
    blocks = []
    for path, branch, head in entries:
        lines = [f"worktree {path}", f"HEAD {head}"]
        lines.append("detached" if branch is None else f"branch refs/heads/{branch}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


class _Runner:
    """Contract-C fake answering git worktree list / gh pr list --head / bd show."""

    def __init__(self, *, porcelain="", merged=None, bead_status=None, bd_error=(), gh_error=False):
        self.porcelain = porcelain
        self.merged = dict(merged or {})  # branch -> {"number","headRefOid"}
        self.bead_status = dict(bead_status or {})  # key -> raw status
        self.bd_error = set(bd_error)  # keys whose `bd show` returns rc!=0
        self.gh_error = gh_error
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:4] == ["git", "worktree", "list", "--porcelain"]:
            return _cp(self.porcelain)
        if args[:3] == ["gh", "pr", "list"]:
            if self.gh_error:
                return _cp("boom", returncode=1)
            head = args[args.index("--head") + 1]
            pr = self.merged.get(head)
            return _cp(json.dumps([pr] if pr else []))
        if args[:2] == ["bd", "show"]:
            key = args[2]
            if key in self.bd_error:
                return _cp("boom", returncode=1)
            return _cp(json.dumps({"id": key, "status": self.bead_status.get(key, "closed")}))
        raise AssertionError(f"unexpected tool call: {args}")


class _LiveProbe:
    def __init__(self, live: bool):
        self.live = live
        self.calls: list[tuple] = []

    def __call__(self, ws, key):
        self.calls.append((ws, key))
        return self.live


def _wire(monkeypatch, tmp_path, runner, *, live_probe):
    repo = tmp_path / "flow"
    repo.mkdir()
    reap_calls: list[dict] = []

    def fake_reap(*, ticket, main_root, branch):
        reap_calls.append({"ticket": ticket, "main_root": main_root, "branch": branch})
        return {"ticket": ticket, "branch": branch, "worktree_removed": True}

    monkeypatch.setattr(wj, "resolve_maintainer_repo", lambda ws: repo)
    monkeypatch.setattr(wj, "_default_runner", lambda r: runner)
    monkeypatch.setattr(wj.fleet, "is_live", live_probe)
    monkeypatch.setattr(wj, "reap_worktree", fake_reap)
    return repo, reap_calls


def _run(monkeypatch, tmp_path, runner, capsys, *, live=False, dry_run=False):
    probe = _LiveProbe(live)
    repo, reap_calls = _wire(monkeypatch, tmp_path, runner, live_probe=probe)
    argv = ["sweep", "--workspace-root", str(tmp_path)]
    if dry_run:
        argv.append("--dry-run")
    rc = wj.cli_main(argv)
    out = json.loads(capsys.readouterr().out) if rc == 0 else None
    return repo, rc, reap_calls, probe, out


# --- pure classify_orphans ---------------------------------------------------


def test_classify_orphans_match_is_reapable():
    wts = [{"key": "flow-a", "branch": "feat/flow-a-x", "worktree": "/wt/a", "tip": "sha1"}]
    out = wj.classify_orphans(wts, {"feat/flow-a-x": {"pr": 7, "head_oid": "sha1"}})
    assert [e["key"] for e in out["reapable"]] == ["flow-a"]
    assert out["reapable"][0]["pr"] == 7
    assert out["skipped_ahead"] == []
    assert out["no_merged_pr"] == []


def test_classify_orphans_tip_ahead_is_skipped():
    # local tip past the merged head (e.g. unpushed reflect commit) -> never reap
    wts = [{"key": "flow-a", "branch": "feat/flow-a-x", "worktree": "/wt/a", "tip": "sha2"}]
    out = wj.classify_orphans(wts, {"feat/flow-a-x": {"pr": 7, "head_oid": "sha1"}})
    assert out["reapable"] == []
    assert [e["key"] for e in out["skipped_ahead"]] == ["flow-a"]


def test_classify_orphans_no_merged_pr():
    wts = [{"key": "flow-a", "branch": "feat/flow-a-x", "worktree": "/wt/a", "tip": "sha1"}]
    out = wj.classify_orphans(wts, {})
    assert [e["key"] for e in out["no_merged_pr"]] == ["flow-a"]
    assert out["reapable"] == []


def test_classify_orphans_mixed():
    wts = [
        {"key": "flow-a", "branch": "feat/flow-a-x", "worktree": "/wt/a", "tip": "sha1"},
        {"key": "flow-b", "branch": "feat/flow-b-y", "worktree": "/wt/b", "tip": "ahead"},
        {"key": "flow-c", "branch": "feat/flow-c-z", "worktree": "/wt/c", "tip": "sha3"},
    ]
    merged = {
        "feat/flow-a-x": {"pr": 1, "head_oid": "sha1"},
        "feat/flow-b-y": {"pr": 2, "head_oid": "sha2"},
    }
    out = wj.classify_orphans(wts, merged)
    assert [e["key"] for e in out["reapable"]] == ["flow-a"]
    assert [e["key"] for e in out["skipped_ahead"]] == ["flow-b"]
    assert [e["key"] for e in out["no_merged_pr"]] == ["flow-c"]


# --- _enumerate_worktrees ----------------------------------------------------


def test_enumerate_keeps_only_flow_branches():
    blob = _porcelain(
        [
            ("/main", "main", "m0"),
            ("/wt/a", "feat/flow-a-x", "sha1"),
            ("/wt/d", None, "d0"),
        ]
    )
    entries = wj._enumerate_worktrees(blob)
    assert entries == [
        {"key": "flow-a", "branch": "feat/flow-a-x", "worktree": "/wt/a", "tip": "sha1"}
    ]


# --- CLI sweep ---------------------------------------------------------------


def test_sweep_reaps_terminal_nonlive_match(monkeypatch, tmp_path, capsys):
    runner = _Runner(
        porcelain=_porcelain([("/wt/a", "feat/flow-a-x", "sha1")]),
        merged={"feat/flow-a-x": {"number": 7, "headRefOid": "sha1"}},
        bead_status={"flow-a": "closed"},  # RAW status beads emits for a closed bead
    )
    repo, rc, reap_calls, _probe, out = _run(monkeypatch, tmp_path, runner, capsys)
    assert rc == 0
    assert [e["key"] for e in out["reaped"]] == ["flow-a"]
    assert reap_calls == [{"ticket": "flow-a", "main_root": repo, "branch": "feat/flow-a-x"}]


def test_sweep_active_bead_in_progress_skipped(monkeypatch, tmp_path, capsys):
    runner = _Runner(
        porcelain=_porcelain([("/wt/a", "feat/flow-a-x", "sha1")]),
        merged={"feat/flow-a-x": {"number": 7, "headRefOid": "sha1"}},
        bead_status={"flow-a": "in_progress"},
    )
    _repo, rc, reap_calls, _probe, out = _run(monkeypatch, tmp_path, runner, capsys)
    assert rc == 0
    assert reap_calls == []
    assert [e["key"] for e in out["skipped_active_bead"]] == ["flow-a"]
    assert out["reaped"] == []


def test_sweep_active_bead_open_skipped(monkeypatch, tmp_path, capsys):
    runner = _Runner(
        porcelain=_porcelain([("/wt/a", "feat/flow-a-x", "sha1")]),
        merged={"feat/flow-a-x": {"number": 7, "headRefOid": "sha1"}},
        bead_status={"flow-a": "open"},
    )
    _repo, rc, reap_calls, _probe, out = _run(monkeypatch, tmp_path, runner, capsys)
    assert rc == 0
    assert reap_calls == []
    assert [e["key"] for e in out["skipped_active_bead"]] == ["flow-a"]


def test_sweep_bd_read_error_treated_active(monkeypatch, tmp_path, capsys):
    # a bd show failure is fail-safe: treat active -> skip, never reap
    runner = _Runner(
        porcelain=_porcelain([("/wt/a", "feat/flow-a-x", "sha1")]),
        merged={"feat/flow-a-x": {"number": 7, "headRefOid": "sha1"}},
        bd_error=["flow-a"],
    )
    _repo, rc, reap_calls, _probe, out = _run(monkeypatch, tmp_path, runner, capsys)
    assert rc == 0
    assert reap_calls == []
    assert [e["key"] for e in out["skipped_active_bead"]] == ["flow-a"]


def test_sweep_live_lease_skipped(monkeypatch, tmp_path, capsys):
    runner = _Runner(
        porcelain=_porcelain([("/wt/a", "feat/flow-a-x", "sha1")]),
        merged={"feat/flow-a-x": {"number": 7, "headRefOid": "sha1"}},
        bead_status={"flow-a": "closed"},
    )
    repo, rc, reap_calls, probe, out = _run(monkeypatch, tmp_path, runner, capsys, live=True)
    assert rc == 0
    assert reap_calls == []
    assert [e["key"] for e in out["skipped_live"]] == ["flow-a"]
    # is_live must be called with the RAW workspace-root, not the resolved repo
    assert probe.calls == [(tmp_path, "flow-a")]
    assert probe.calls[0][0] != repo


def test_sweep_dry_run_reports_without_reaping(monkeypatch, tmp_path, capsys):
    # closed + non-live so the entry reaches the would-reap branch
    runner = _Runner(
        porcelain=_porcelain([("/wt/a", "feat/flow-a-x", "sha1")]),
        merged={"feat/flow-a-x": {"number": 7, "headRefOid": "sha1"}},
        bead_status={"flow-a": "closed"},
    )
    _repo, rc, reap_calls, _probe, out = _run(monkeypatch, tmp_path, runner, capsys, dry_run=True)
    assert rc == 0
    assert reap_calls == []
    assert out["dry_run"] is True
    assert [e["key"] for e in out["reaped"]] == ["flow-a"]
    assert out["reaped"][0]["receipt"] is None


def test_sweep_tip_mismatch_end_to_end_skipped_ahead(monkeypatch, tmp_path, capsys):
    runner = _Runner(
        porcelain=_porcelain([("/wt/a", "feat/flow-a-x", "local-ahead")]),
        merged={"feat/flow-a-x": {"number": 7, "headRefOid": "merged-head"}},
        bead_status={"flow-a": "closed"},
    )
    _repo, rc, reap_calls, _probe, out = _run(monkeypatch, tmp_path, runner, capsys)
    assert rc == 0
    assert reap_calls == []
    assert [e["key"] for e in out["skipped_ahead"]] == ["flow-a"]
    # a skipped_ahead worktree is never bd-probed (gated before the bead read)
    assert not any(c[:2] == ["bd", "show"] for c in runner.calls)


def test_sweep_no_merged_pr_bucketed(monkeypatch, tmp_path, capsys):
    runner = _Runner(porcelain=_porcelain([("/wt/a", "feat/flow-a-x", "sha1")]))
    _repo, rc, reap_calls, _probe, out = _run(monkeypatch, tmp_path, runner, capsys)
    assert rc == 0
    assert reap_calls == []
    assert [e["key"] for e in out["no_merged_pr"]] == ["flow-a"]


def test_sweep_not_maintainer_exit_4(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(wj, "resolve_maintainer_repo", lambda ws: None)
    rc = wj.cli_main(["sweep", "--workspace-root", str(tmp_path)])
    assert rc == 4
    assert "dormant" in capsys.readouterr().err


def test_sweep_gh_error_exit_2(monkeypatch, tmp_path, capsys):
    runner = _Runner(
        porcelain=_porcelain([("/wt/a", "feat/flow-a-x", "sha1")]),
        gh_error=True,
    )
    _repo, rc, reap_calls, _probe, _out = _run(monkeypatch, tmp_path, runner, capsys)
    assert rc == 2
    assert reap_calls == []
    assert "gh pr list" in capsys.readouterr().err


def test_sweep_reap_failure_isolated(monkeypatch, tmp_path, capsys):
    # a mid-sweep reap failure buckets into reap_failed; the loop keeps going and
    # the JSON audit trail of the reaps already done still prints (exit 0).
    runner = _Runner(
        porcelain=_porcelain(
            [("/wt/a", "feat/flow-a-x", "sha1"), ("/wt/b", "feat/flow-b-y", "sha2")]
        ),
        merged={
            "feat/flow-a-x": {"number": 1, "headRefOid": "sha1"},
            "feat/flow-b-y": {"number": 2, "headRefOid": "sha2"},
        },
        bead_status={"flow-a": "closed", "flow-b": "closed"},
    )
    repo = tmp_path / "flow"
    repo.mkdir()

    def raising_reap(*, ticket, main_root, branch):
        if ticket == "flow-a":
            raise RuntimeError("worktree remove exploded")
        return {"ticket": ticket, "branch": branch, "worktree_removed": True}

    monkeypatch.setattr(wj, "resolve_maintainer_repo", lambda ws: repo)
    monkeypatch.setattr(wj, "_default_runner", lambda r: runner)
    monkeypatch.setattr(wj.fleet, "is_live", lambda ws, key: False)
    monkeypatch.setattr(wj, "reap_worktree", raising_reap)

    rc = wj.cli_main(["sweep", "--workspace-root", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [e["key"] for e in out["reap_failed"]] == ["flow-a"]
    assert "worktree remove exploded" in out["reap_failed"][0]["reap_error"]
    assert [e["key"] for e in out["reaped"]] == ["flow-b"]
