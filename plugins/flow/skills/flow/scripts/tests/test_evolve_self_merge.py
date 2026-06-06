from __future__ import annotations

import json
import subprocess

import evolve_self_merge as esm

# ─── decide() — the pure gate ────────────────────────────────────────────────


def test_skip_when_not_maintainer():
    d = esm.decide(["evolve"], is_maintainer=False, auto_merge_hot=True, ci_status="green")
    assert d["action"] == "skip"
    assert "maintainer" in d["reason"]


def test_skip_when_not_evolve_bead():
    d = esm.decide(["chore"], is_maintainer=True, auto_merge_hot=True, ci_status="green")
    assert d["action"] == "skip"
    assert "evolve" in d["reason"]


def test_skip_when_ci_not_green():
    d = esm.decide(["evolve"], is_maintainer=True, auto_merge_hot=True, ci_status="pending")
    assert d["action"] == "skip"
    assert "green" in d["reason"]


def test_skip_proposal_bead():
    d = esm.decide(
        ["evolve", "proposal"], is_maintainer=True, auto_merge_hot=True, ci_status="green"
    )
    assert d["action"] == "skip"
    assert "proposal" in d["reason"]


def test_merge_leaf_when_green_maintainer():
    d = esm.decide(["evolve"], is_maintainer=True, auto_merge_hot=False, ci_status="green")
    assert d["action"] == "merge"
    assert d["is_hot"] is False


def test_merge_hot_when_auto_merge_hot_on():
    d = esm.decide(["evolve", "hot"], is_maintainer=True, auto_merge_hot=True, ci_status="green")
    assert d["action"] == "merge"
    assert d["is_hot"] is True


def test_skip_hot_when_auto_merge_hot_off():
    d = esm.decide(["evolve", "hot"], is_maintainer=True, auto_merge_hot=False, ci_status="green")
    assert d["action"] == "skip"
    assert d["is_hot"] is True
    assert "auto_merge_hot" in d["reason"]


# ─── CLI (injected runner + tmp workspace) ───────────────────────────────────


def _ws(tmp_path, *, self_target=True, auto_merge_hot=True):
    (tmp_path / ".flow").mkdir()
    body = '[tracker]\nbackend = "beads"\n[tracker.beads]\nprefix = "flow"\n'
    if self_target:
        body += "[maintainer]\nself_target = true\n"
    body += f"[evolve]\nauto_merge_hot = {str(auto_merge_hot).lower()}\n"
    (tmp_path / ".flow" / "workspace.toml").write_text(body, encoding="utf-8")
    return tmp_path


def _runner(labels):
    def run(args):
        if args[:2] == ["bd", "show"]:
            # bd show --json returns a LIST with one element (the bead), not a dict.
            return subprocess.CompletedProcess(args, 0, json.dumps([{"labels": labels}]), "")
        return subprocess.CompletedProcess(args, 1, "", "unexpected")

    return run


def test_cli_merge_for_hot_in_self_target(tmp_path, capsys):
    ws = _ws(tmp_path, self_target=True, auto_merge_hot=True)
    rc = esm.cli_main(
        ["--workspace-root", str(ws), "--key", "flow-x", "--ci-status", "green"],
        runner=_runner(["evolve", "hot"]),
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"action": "merge", "is_hot": True, "reason": "eligible"}


def test_cli_skip_when_not_self_target(tmp_path, capsys):
    ws = _ws(tmp_path, self_target=False)
    rc = esm.cli_main(
        ["--workspace-root", str(ws), "--key", "flow-x", "--ci-status", "green"],
        runner=_runner(["evolve"]),
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["action"] == "skip"
