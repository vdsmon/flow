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


def test_merge_hot_via_guard_file_without_hot_label():
    d = esm.decide(
        ["evolve"],
        is_maintainer=True,
        auto_merge_hot=True,
        ci_status="green",
        planned_files=["plugins/flow/skills/flow/scripts/snapshot.py"],
    )
    assert d["is_hot"] is True
    assert d["action"] == "merge"


def test_non_guard_planned_files_leaves_is_hot_label_driven():
    d = esm.decide(
        ["evolve"],
        is_maintainer=True,
        auto_merge_hot=True,
        ci_status="green",
        planned_files=["plugins/flow/skills/flow/scripts/recall.py"],
    )
    assert d["is_hot"] is False


def test_planned_files_absent_follows_label():
    d = esm.decide(["evolve"], is_maintainer=True, auto_merge_hot=True, ci_status="green")
    assert d["is_hot"] is False


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


def _ticket(ws, key, planned_files):
    tickets = ws / ".flow" / "tickets"
    tickets.mkdir(parents=True, exist_ok=True)
    files = ", ".join(f'"{f}"' for f in planned_files)
    body = f'+++\nticket = "{key}"\nstatus = "in_progress"\nplanned_files = [{files}]\n+++\nbody\n'
    (tickets / f"{key}.md").write_text(body, encoding="utf-8")


def test_cli_hot_via_planned_files_guard_file(tmp_path, capsys):
    ws = _ws(tmp_path, self_target=True, auto_merge_hot=True)
    _ticket(ws, "flow-x", ["plugins/flow/skills/flow/scripts/snapshot.py"])
    rc = esm.cli_main(
        ["--workspace-root", str(ws), "--key", "flow-x", "--ci-status", "green"],
        runner=_runner(["evolve"]),
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["is_hot"] is True


def test_cli_no_ticket_file_follows_label(tmp_path, capsys):
    ws = _ws(tmp_path, self_target=True, auto_merge_hot=True)
    rc = esm.cli_main(
        ["--workspace-root", str(ws), "--key", "flow-x", "--ci-status", "green"],
        runner=_runner(["evolve"]),
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["is_hot"] is False


# ─── _bead_labels error branches ─────────────────────────────────────────────


def test_bead_labels_nonzero_returncode():
    def runner(args):
        return subprocess.CompletedProcess(args, 1, "", "bd error")

    assert esm._bead_labels("flow-x", runner) == []


def test_bead_labels_malformed_json():
    def runner(args):
        return subprocess.CompletedProcess(args, 0, "not valid json{{{", "")

    assert esm._bead_labels("flow-x", runner) == []


def test_bead_labels_empty_list():
    def runner(args):
        return subprocess.CompletedProcess(args, 0, "[]", "")

    assert esm._bead_labels("flow-x", runner) == []
