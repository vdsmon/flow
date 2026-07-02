from __future__ import annotations

import json
import subprocess

import pytest

import evolve_self_merge as esm

# ─── decide(), the pure gate ─────────────────────────────────────────────────


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


# ─── decide(), the observed-diff hotness input (changed_files) ───────────────


def test_changed_files_guard_file_raises_is_hot_with_clean_plan():
    # the flow-sdkk blind spot: a guard file entered the PR during review-loop
    # fixes, so it is in the observed diff but planned_files never gained it.
    d = esm.decide(
        ["evolve"],
        is_maintainer=True,
        auto_merge_hot=True,
        ci_status="green",
        planned_files=["plugins/flow/skills/flow/scripts/recall.py"],
        changed_files=[
            "plugins/flow/skills/flow/scripts/recall.py",
            "plugins/flow/skills/flow/SKILL.md",
        ],
    )
    assert d["is_hot"] is True


def test_changed_files_hot_with_auto_merge_hot_off_skips():
    # the failure scenario end-to-end: without changed_files this returned
    # {action: merge, is_hot: false} and bypassed both §2 and the hot hold.
    d = esm.decide(
        ["evolve"],
        is_maintainer=True,
        auto_merge_hot=False,
        ci_status="green",
        planned_files=["plugins/flow/skills/flow/scripts/recall.py"],
        changed_files=["plugins/flow/skills/flow/scripts/lease.py"],
    )
    assert d == {
        "action": "skip",
        "is_hot": True,
        "reason": "hot bead and auto_merge_hot is off",
    }


def test_changed_files_clean_never_lowers_hotness():
    # observed files only RAISE is_hot: a clean diff leaves label/plan hotness alone.
    d = esm.decide(
        ["evolve", "hot"],
        is_maintainer=True,
        auto_merge_hot=True,
        ci_status="green",
        changed_files=["plugins/flow/skills/flow/scripts/recall.py"],
    )
    assert d["is_hot"] is True
    d = esm.decide(
        ["evolve"],
        is_maintainer=True,
        auto_merge_hot=True,
        ci_status="green",
        planned_files=["plugins/flow/skills/flow/scripts/snapshot.py"],
        changed_files=["plugins/flow/skills/flow/scripts/recall.py"],
    )
    assert d["is_hot"] is True


def test_changed_files_absent_is_byte_for_byte_legacy():
    d = esm.decide(["evolve"], is_maintainer=True, auto_merge_hot=False, ci_status="green")
    assert d == {"action": "merge", "is_hot": False, "reason": "eligible"}


def test_changed_files_empty_list_is_noop():
    d = esm.decide(
        ["evolve"],
        is_maintainer=True,
        auto_merge_hot=False,
        ci_status="green",
        changed_files=[],
    )
    assert d == {"action": "merge", "is_hot": False, "reason": "eligible"}


# ─── decide(), the harness-eval gate ─────────────────────────────────────────


def test_eval_regressed_blocks_merge():
    d = esm.decide(
        ["evolve"],
        is_maintainer=True,
        auto_merge_hot=False,
        ci_status="green",
        eval_status="regressed",
    )
    assert d["action"] == "skip"
    assert "regress" in d["reason"]


def test_eval_error_blocks_merge():
    d = esm.decide(
        ["evolve"],
        is_maintainer=True,
        auto_merge_hot=False,
        ci_status="green",
        eval_status="error",
    )
    assert d["action"] == "skip"
    assert "no non-regression evidence" in d["reason"]


def test_eval_unexpected_value_blocks():
    d = esm.decide(
        ["evolve"],
        is_maintainer=True,
        auto_merge_hot=False,
        ci_status="green",
        eval_status="garbage",
    )
    assert d["action"] == "skip"
    assert "no non-regression evidence" in d["reason"]


def test_eval_pass_merges():
    d = esm.decide(
        ["evolve"],
        is_maintainer=True,
        auto_merge_hot=False,
        ci_status="green",
        eval_status="pass",
    )
    assert d["action"] == "merge"


def test_eval_none_is_noop():
    d = esm.decide(["evolve"], is_maintainer=True, auto_merge_hot=False, ci_status="green")
    assert d == {"action": "merge", "is_hot": False, "reason": "eligible"}


def test_eval_gate_sits_after_ci_gate():
    d = esm.decide(
        ["evolve"],
        is_maintainer=True,
        auto_merge_hot=False,
        ci_status="pending",
        eval_status="regressed",
    )
    assert d["action"] == "skip"
    assert "green" in d["reason"]


def test_eval_gate_sits_before_hot_gate():
    d = esm.decide(
        ["evolve", "hot"],
        is_maintainer=True,
        auto_merge_hot=False,
        ci_status="green",
        eval_status="regressed",
    )
    assert d["action"] == "skip"
    assert "regress" in d["reason"]
    assert d["is_hot"] is True


# ─── decide(), the main-CI health gate (flow-a1ti.3) ─────────────────────────


def test_main_ci_failed_skips_even_when_otherwise_eligible():
    # maintainer + evolve + green + auto_merge_hot, otherwise a clean merge: a red
    # main pauses it for the turn.
    d = esm.decide(
        ["evolve", "hot"],
        is_maintainer=True,
        auto_merge_hot=True,
        ci_status="green",
        main_ci_status="failed",
    )
    assert d["action"] == "skip"
    assert d["reason"] == "main CI red — auto-merge paused this turn"
    assert d["is_hot"] is True


def test_main_ci_green_is_noop():
    d = esm.decide(
        ["evolve"],
        is_maintainer=True,
        auto_merge_hot=False,
        ci_status="green",
        main_ci_status="green",
    )
    assert d == {"action": "merge", "is_hot": False, "reason": "eligible"}


def test_main_ci_pending_is_noop():
    d = esm.decide(
        ["evolve"],
        is_maintainer=True,
        auto_merge_hot=False,
        ci_status="green",
        main_ci_status="pending",
    )
    assert d["action"] == "merge"


def test_main_ci_error_is_noop_resumes():
    # a transient probe error must RESUME (never pause): non-"failed" is a no-op.
    d = esm.decide(
        ["evolve"],
        is_maintainer=True,
        auto_merge_hot=False,
        ci_status="green",
        main_ci_status="error",
    )
    assert d["action"] == "merge"


def test_main_ci_none_is_byte_for_byte_legacy():
    d = esm.decide(["evolve"], is_maintainer=True, auto_merge_hot=False, ci_status="green")
    assert d == {"action": "merge", "is_hot": False, "reason": "eligible"}


def test_main_ci_gate_sits_after_ci_gate():
    # CI-not-green still wins the reason (the main gate sits after the PR-CI gate).
    d = esm.decide(
        ["evolve"],
        is_maintainer=True,
        auto_merge_hot=False,
        ci_status="pending",
        main_ci_status="failed",
    )
    assert d["action"] == "skip"
    assert "green" in d["reason"]


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


def test_cli_skip_when_not_self_target(tmp_path, capsys, monkeypatch):
    import maintainer

    monkeypatch.setattr(
        maintainer, "_global_config_path", lambda: tmp_path / "no-global" / "config.toml"
    )
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


def test_cli_eval_status_flows_to_decide(tmp_path, capsys):
    ws = _ws(tmp_path, self_target=True, auto_merge_hot=True)
    rc = esm.cli_main(
        [
            "--workspace-root",
            str(ws),
            "--key",
            "flow-x",
            "--ci-status",
            "green",
            "--eval-status",
            "regressed",
        ],
        runner=_runner(["evolve"]),
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "skip"
    assert "regress" in out["reason"]


def test_cli_omitted_eval_flag_unchanged(tmp_path, capsys):
    ws = _ws(tmp_path, self_target=True, auto_merge_hot=True)
    rc = esm.cli_main(
        ["--workspace-root", str(ws), "--key", "flow-x", "--ci-status", "green"],
        runner=_runner(["evolve"]),
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["action"] == "merge"


def test_cli_main_ci_status_flows_to_decide(tmp_path, capsys):
    # threading guard, sibling of test_cli_eval_status_flows_to_decide: dropping
    # `main_ci_status=args.main_ci_status` from cli_main leaves the pure tests
    # green while a red main silently stops pausing auto-merge.
    ws = _ws(tmp_path, self_target=True, auto_merge_hot=True)
    rc = esm.cli_main(
        [
            "--workspace-root",
            str(ws),
            "--key",
            "flow-x",
            "--ci-status",
            "green",
            "--main-ci-status",
            "failed",
        ],
        runner=_runner(["evolve"]),
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "skip"
    assert "main CI red" in out["reason"]


def test_cli_changed_files_flows_to_decide(tmp_path, capsys):
    # observed-diff threading: a guard file in --changed-files raises is_hot even
    # though the ticket frontmatter's planned_files are clean.
    ws = _ws(tmp_path, self_target=True, auto_merge_hot=True)
    _ticket(ws, "flow-x", ["plugins/flow/skills/flow/scripts/recall.py"])
    rc = esm.cli_main(
        [
            "--workspace-root",
            str(ws),
            "--key",
            "flow-x",
            "--ci-status",
            "green",
            "--changed-files",
            "plugins/flow/skills/flow/scripts/recall.py,plugins/flow/skills/flow/scripts/lease.py",
        ],
        runner=_runner(["evolve"]),
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["is_hot"] is True


def test_cli_omitted_changed_files_unchanged(tmp_path, capsys):
    ws = _ws(tmp_path, self_target=True, auto_merge_hot=True)
    _ticket(ws, "flow-x", ["plugins/flow/skills/flow/scripts/recall.py"])
    rc = esm.cli_main(
        ["--workspace-root", str(ws), "--key", "flow-x", "--ci-status", "green"],
        runner=_runner(["evolve"]),
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"action": "merge", "is_hot": False, "reason": "eligible"}


def test_cli_changed_files_tolerates_blank_segments(tmp_path, capsys):
    # the prose builds the csv with tr '\n' ','; stray empty segments must not
    # trip the guard-file match or crash the parse
    ws = _ws(tmp_path, self_target=True, auto_merge_hot=True)
    rc = esm.cli_main(
        [
            "--workspace-root",
            str(ws),
            "--key",
            "flow-x",
            "--ci-status",
            "green",
            "--changed-files",
            ",plugins/flow/skills/flow/scripts/recall.py,,",
        ],
        runner=_runner(["evolve"]),
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["is_hot"] is False


def test_cli_rejects_unknown_eval_status(tmp_path):
    ws = _ws(tmp_path, self_target=True, auto_merge_hot=True)
    with pytest.raises(SystemExit) as exc:
        esm.cli_main(
            [
                "--workspace-root",
                str(ws),
                "--key",
                "flow-x",
                "--ci-status",
                "green",
                "--eval-status",
                "garbage",
            ],
            runner=_runner(["evolve"]),
        )
    assert exc.value.code == 2


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
