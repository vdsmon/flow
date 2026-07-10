"""HITL label gate (flow-blh2): unattended machinery stays off decision-bound beads.

A `hitl` bead (human-in-the-loop, resolves only through a live exchange) is filtered
out of both drain selectors, refused early by the `--auto` bootstrap floor, and reads
its decision escape from `triage.decided`. Interactive runs never consult the label.

Offline: pure-function partition tests, a `_FakeRunner`-driven `triage.decided`, and
direct `_enforce_autonomy_floors` calls with `triage.decided` monkeypatched.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import evolve_select
import flow_worktree as fw
import queue_select
import triage


def _cp(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class _FakeRunner:
    """Sequenced subprocess fake: the first response is the `bd version` preflight
    BeadsAdapter construction consumes, then one per `_run_json` call."""

    def __init__(self, responses: list[subprocess.CompletedProcess[str]]) -> None:
        self._responses = list(responses)

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if not self._responses:
            raise AssertionError(f"FakeRunner ran out of responses; got args={args!r}")
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


def _show(
    *, labels: list[str] | None = None, comments: list[dict[str, Any]] | None = None
) -> subprocess.CompletedProcess[str]:
    issue = {"id": "flow-x", "labels": labels or [], "comments": comments or []}
    return _cp(stdout=json.dumps([issue]))


def _tc(text: str, created_at: str = "2026-06-01T10:00:00Z") -> dict[str, Any]:
    return {"id": "c", "author": "x", "text": text, "created_at": created_at}


# ─── selectors drop hitl from the auto-pickable set ───────────────────────────


def test_queue_select_excluded_labels_contains_hitl() -> None:
    assert "hitl" in queue_select._EXCLUDED_LABELS


def test_queue_select_partition_drops_hitl_keeps_twin() -> None:
    cands = [
        {"id": "flow-h", "labels": ["hitl"], "issue_type": "task"},
        {"id": "flow-a", "labels": [], "issue_type": "task"},
    ]
    result = queue_select.partition(cands, set(), 0)
    assert result["launch"] == ["flow-a"]


def test_evolve_select_partition_drops_hitl() -> None:
    cands = [
        {"id": "flow-e", "labels": ["evolve", "hitl"], "issue_type": "task"},
        {"id": "flow-b", "labels": ["evolve"], "issue_type": "task"},
    ]
    result = evolve_select.partition(cands, set(), False, 0)
    assert result["launch"] == ["flow-b"]


def test_evolve_select_hitl_excluded_even_with_include_proposals() -> None:
    # the hitl clause is unconditional: the dangerous proposal opt-in does not lift it.
    cands = [{"id": "flow-e", "labels": ["evolve", "hitl"], "issue_type": "task"}]
    result = evolve_select.partition(cands, set(), False, 0, include_proposals=True)
    assert result["launch"] == []


def test_evolve_select_hitl_inflight_accounting_unchanged() -> None:
    # a hitl bead already in-flight still reports in skipped_in_flight (the split
    # runs over all candidates); it simply never reaches launch.
    cands = [{"id": "flow-e", "labels": ["evolve", "hitl"], "issue_type": "task"}]
    result = evolve_select.partition(cands, {"flow-e"}, False, 0)
    assert result["skipped_in_flight"] == ["flow-e"]
    assert result["launch"] == []


# ─── triage.decided carries the hitl field ────────────────────────────────────


def _decided(
    tmp_path: Path,
    *,
    labels: list[str] | None = None,
    comments: list[dict[str, Any]] | None = None,
    files: list[str] | None = None,
) -> dict[str, Any]:
    _seed_workspace(tmp_path)
    config, code = triage._resolve_config(tmp_path)
    assert config is not None
    assert code == 0
    runner = _FakeRunner([_version_ok(), _show(labels=labels, comments=comments)])
    return triage.decided(config, "flow-x", files or [], runner=runner)


def test_decided_reports_hitl_true(tmp_path: Path) -> None:
    result = _decided(tmp_path, labels=["hitl"])
    assert result["hitl"] is True


def test_decided_reports_hitl_false_when_absent(tmp_path: Path) -> None:
    result = _decided(tmp_path, labels=["evolve"])
    assert result["hitl"] is False


def test_decided_hitl_alongside_decision(tmp_path: Path) -> None:
    result = _decided(
        tmp_path,
        labels=["hitl"],
        comments=[_tc("TRIAGE-DECISION: build it.")],
    )
    assert result["hitl"] is True
    assert result["decided"] is True


def test_decided_bd_read_fail_returns_hitl_false(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    config, _ = triage._resolve_config(tmp_path)
    assert config is not None
    runner = _FakeRunner([_version_ok(), _cp(returncode=1)])
    result = triage.decided(config, "flow-x", [], runner=runner)
    assert result == {"decided": False, "answer": None, "is_hot": True, "hitl": False}


def test_decided_non_dict_issue_returns_hitl_false(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    config, _ = triage._resolve_config(tmp_path)
    assert config is not None
    runner = _FakeRunner([_version_ok(), _cp(stdout=json.dumps(["not-a-dict"]))])
    result = triage.decided(config, "flow-x", [], runner=runner)
    assert result == {"decided": False, "answer": None, "is_hot": True, "hitl": False}


# ─── the autonomous hitl floor ────────────────────────────────────────────────


def _floor(
    tmp_path: Path,
    *,
    base: str = "main",
    auto: bool = True,
    planned: list[str] | None = None,
) -> None:
    fw._enforce_autonomy_floors(
        ticket="flow-x",
        base=base,
        auto=auto,
        planned_files=planned or [],
        main_root=tmp_path,
    )


def test_floor_refuses_hitl_undecided(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    monkeypatch.setattr(
        triage, "decided", lambda *a, **k: {"hitl": True, "decided": False, "is_hot": False}
    )
    with pytest.raises(fw._HitlBead):
        _floor(tmp_path, auto=True)


def test_floor_passes_hitl_decided(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    monkeypatch.setattr(
        triage, "decided", lambda *a, **k: {"hitl": True, "decided": True, "is_hot": False}
    )
    _floor(tmp_path, auto=True)


def test_floor_passes_unlabeled(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    monkeypatch.setattr(
        triage, "decided", lambda *a, **k: {"hitl": False, "decided": False, "is_hot": False}
    )
    _floor(tmp_path, auto=True)


def test_floor_noops_when_not_autonomous(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)

    def _boom(*a, **k):
        raise AssertionError("decided() must not run on the interactive path")

    monkeypatch.setattr(triage, "decided", _boom)
    _floor(tmp_path, base="main", auto=False)


def test_floor_noops_non_beads_backend(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path, backend="jira")

    def _boom(*a, **k):
        raise AssertionError("decided() must not run for a non-beads tracker")

    monkeypatch.setattr(triage, "decided", _boom)
    _floor(tmp_path, base="@default", auto=True)


def test_floor_hitl_fires_even_when_adjudicate_hot_on(tmp_path: Path, monkeypatch) -> None:
    # adjudicate_hot lifts only the hot half; a hitl bead still refuses.
    _seed_workspace(tmp_path)
    monkeypatch.setattr(
        triage, "decided", lambda *a, **k: {"hitl": True, "decided": False, "is_hot": True}
    )
    monkeypatch.setattr(triage, "adjudicate_hot", lambda *a, **k: True)
    with pytest.raises(fw._HitlBead):
        _floor(tmp_path, auto=True, planned=["lease.py"])


def test_floor_hitl_wins_over_hot(tmp_path: Path, monkeypatch) -> None:
    # a bead that is both hitl and hot defers (hitl), not blocks (hot).
    _seed_workspace(tmp_path)
    monkeypatch.setattr(
        triage, "decided", lambda *a, **k: {"hitl": True, "decided": False, "is_hot": True}
    )
    with pytest.raises(fw._HitlBead):
        _floor(tmp_path, auto=True, planned=["lease.py"])


def test_floor_hot_still_refuses_when_not_hitl(tmp_path: Path, monkeypatch) -> None:
    # the hot half is intact: a hot+undecided change with no hitl label still blocks.
    _seed_workspace(tmp_path)
    monkeypatch.setattr(
        triage, "decided", lambda *a, **k: {"hitl": False, "decided": False, "is_hot": True}
    )
    with pytest.raises(fw._ConfigError):
        _floor(tmp_path, auto=True, planned=["lease.py"])


def test_floor_shares_one_probe(tmp_path: Path, monkeypatch) -> None:
    # both floors read a single triage.decided probe (one bd show per bootstrap).
    _seed_workspace(tmp_path)
    calls: list[int] = []

    def _counting(*a, **k):
        calls.append(1)
        return {"hitl": False, "decided": False, "is_hot": False}

    monkeypatch.setattr(triage, "decided", _counting)
    _floor(tmp_path, auto=True, planned=["some_helper.py"])
    assert len(calls) == 1


def test_cli_maps_hitl_bead_to_exit_8(tmp_path: Path, monkeypatch) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text("Goal: x\n", encoding="utf-8")

    def _raise(*a, **k):
        raise fw._HitlBead("marked hitl")

    monkeypatch.setattr(fw, "bootstrap", _raise)
    code = fw.cli_main(
        [
            "create",
            "--ticket",
            "flow-x",
            "--plan-from",
            str(plan),
            "--base",
            "main",
            "--branch",
            "feat/flow-x-y",
            "--main-root",
            str(tmp_path),
        ]
    )
    assert code == 8
