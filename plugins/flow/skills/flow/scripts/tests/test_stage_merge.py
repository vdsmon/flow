from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import stage_merge as sm

_PR_ID = "123"


# ─── call-matching helpers (exact for git/gh, token-containment for scripts) ──


def _cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def _script_name(args):
    if len(args) >= 2 and args[0] == sys.executable:
        return Path(args[1]).name
    return args[0] if args else ""


def _is_git(*rest):
    def pred(args):
        return list(args) == ["git", *rest]

    return pred


def _is_gh(*rest):
    def pred(args):
        return list(args) == ["gh", *rest]

    return pred


def _is_bd(*tokens):
    def pred(args):
        return args[:1] == ["bd"] and all(t in args for t in tokens)

    return pred


def _is_script(name, *tokens):
    def pred(args):
        return _script_name(args) == name and all(t in args for t in tokens)

    return pred


class Recorder:
    """Records every call; dispatches to the first matching rule. Unmatched
    calls fall back to a bland 0/""/"" response."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self._rules: list[tuple] = []

    def when(self, predicate, response):
        self._rules.append((predicate, response))
        return self

    def __call__(self, args):
        self.calls.append(list(args))
        for predicate, response in self._rules:
            if predicate(args):
                return response
        return _cp(0, "")


# ─── fixtures ──────────────────────────────────────────────────────────────


def _create_pr_out(ticket_dir: Path, pr_id: str = _PR_ID) -> None:
    stages = ticket_dir / "stages"
    stages.mkdir(parents=True, exist_ok=True)
    (stages / "create_pr.out").write_text(
        f"PR_URL=https://github.com/acme/flow/pull/{pr_id}\n", encoding="utf-8"
    )


def _ticket_with_covers(ws: Path, key: str, covers: list[str]) -> None:
    tickets = ws / ".flow" / "tickets"
    tickets.mkdir(parents=True, exist_ok=True)
    covers_str = ", ".join(f'"{c}"' for c in covers)
    body = f'+++\nticket = "{key}"\nstatus = "in_progress"\ncovers = [{covers_str}]\n+++\nbody\n'
    (tickets / f"{key}.md").write_text(body, encoding="utf-8")


def _enable_review_brief(ws: Path) -> None:
    flow_dir = ws / ".flow"
    flow_dir.mkdir(parents=True, exist_ok=True)
    (flow_dir / "workspace.toml").write_text(
        '[pipeline]\nstages = ["review_brief"]\n\n[pipeline.handlers]\nreview_brief = "inline"\n',
        encoding="utf-8",
    )


def _disable_review_brief(ws: Path) -> None:
    _enable_review_brief(ws)
    path = ws / ".flow" / "workspace.toml"
    path.write_text(
        path.read_text(encoding="utf-8") + '\n[review_brief]\nmode = "off"\n',
        encoding="utf-8",
    )


def _probe_recorder(
    *,
    pr_id=_PR_ID,
    pr_state="OPEN",
    pr_state_rc=0,
    ci_status="green",
    ci_rc=0,
    changed_files=(),
    harness_rc=0,
    harness_stdout=None,
    main_ci_status="green",
    main_ci_rc=0,
    gate_result=None,
    gate_rc=0,
    guard_diff_stdout="diff --git a/x b/x\n",
) -> Recorder:
    if harness_stdout is None:
        harness_stdout = json.dumps(
            {
                "cases": 0,
                "splits": {"held_in": {"regressed": []}, "held_out": {"regressed": []}},
                "non_regression": True,
            }
        )
    if gate_result is None:
        gate_result = {"action": "merge", "is_hot": False, "reason": "eligible"}
    rec = Recorder()
    rec.when(
        _is_gh("pr", "view", pr_id, "--json", "state", "-q", ".state"),
        _cp(pr_state_rc, pr_state + "\n"),
    )
    rec.when(
        _is_gh("pr", "diff", pr_id, "--name-only"),
        _cp(0, "\n".join(changed_files) + ("\n" if changed_files else "")),
    )
    rec.when(_is_gh("pr", "diff", pr_id), _cp(0, guard_diff_stdout))
    rec.when(_is_script("forge_cli.py", "ci-rollup"), _cp(ci_rc, json.dumps({"status": ci_status})))
    rec.when(_is_script("harness_eval.py", "score"), _cp(harness_rc, harness_stdout))
    rec.when(
        _is_script("main_ci_health.py", "probe"),
        _cp(main_ci_rc, json.dumps({"status": main_ci_status, "sha": "abc", "failing_checks": []})),
    )
    rec.when(_is_script("evolve_self_merge.py"), _cp(gate_rc, json.dumps(gate_result)))
    return rec


def _execute_recorder(
    *,
    pr_id=_PR_ID,
    branch="feat-flow-x",
    status_porcelain="",
    local_sha="abc123",
    remote_sha="abc123",
    remote_rc=0,
    merge_state="CLEAN",
    merge_rc=0,
    mark_ready_rc=0,
    delete_rc=0,
    bd_close_rc=0,
    covers_rc=0,
) -> Recorder:
    rec = Recorder()
    rec.when(_is_git("rev-parse", "--abbrev-ref", "HEAD"), _cp(0, branch + "\n"))
    rec.when(_is_git("fetch", "--quiet", "origin", branch), _cp(0, ""))
    rec.when(_is_git("status", "--porcelain", "--untracked-files=no"), _cp(0, status_porcelain))
    rec.when(_is_git("rev-parse", "HEAD"), _cp(0, local_sha + "\n"))
    rec.when(_is_git("rev-parse", f"origin/{branch}"), _cp(remote_rc, remote_sha + "\n"))
    rec.when(
        _is_gh("pr", "view", pr_id, "--json", "mergeStateStatus", "-q", ".mergeStateStatus"),
        _cp(0, merge_state + "\n"),
    )
    rec.when(_is_script("forge_cli.py", "mark-ready"), _cp(mark_ready_rc, ""))
    rec.when(_is_script("forge_cli.py", "merge"), _cp(merge_rc, ""))
    rec.when(_is_script("forge_cli.py", "delete-branch"), _cp(delete_rc, ""))
    rec.when(_is_bd("close"), _cp(bd_close_rc, ""))
    rec.when(_is_script("tracker_cli.py", "comment"), _cp(covers_rc, ""))
    rec.when(_is_script("tracker_cli.py", "transition"), _cp(covers_rc, ""))
    rec.when(_is_bd("dep", "remove"), _cp(covers_rc, ""))
    return rec


def _first_idx(calls, pred):
    return next(i for i, c in enumerate(calls) if pred(c))


# ─── parse_pr_id ───────────────────────────────────────────────────────────


def test_parse_pr_id_from_create_pr_out(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir, pr_id="456")
    assert sm.parse_pr_id(ticket_dir / "stages" / "create_pr.out") == "456"


def test_parse_pr_id_missing_pr_url_line(tmp_path):
    ticket_dir = tmp_path / "run"
    stages = ticket_dir / "stages"
    stages.mkdir(parents=True)
    (stages / "create_pr.out").write_text("no url here\n", encoding="utf-8")
    assert sm.parse_pr_id(stages / "create_pr.out") is None


def test_parse_pr_id_missing_file(tmp_path):
    assert sm.parse_pr_id(tmp_path / "nope" / "create_pr.out") is None


# ─── probe: MERGED short-circuit / MERGED-only scoping ────────────────────


def test_probe_merged_short_circuits(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    rec = _probe_recorder(pr_state="MERGED")
    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    assert result["already_merged"] is True
    assert result["pr_id"] == _PR_ID
    # nothing past the state check ran
    assert not any(_is_script("forge_cli.py", "ci-rollup")(c) for c in rec.calls)


def test_probe_closed_not_merged_falls_through(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    rec = _probe_recorder(pr_state="CLOSED")
    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    assert result["already_merged"] is False
    assert any(_is_script("forge_cli.py", "ci-rollup")(c) for c in rec.calls)


def test_probe_blocks_stale_review_brief_before_other_merge_gates(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    _enable_review_brief(tmp_path)
    rec = _probe_recorder()
    rec.when(
        _is_script("review_brief.py", "freshness"),
        _cp(
            0,
            json.dumps(
                {
                    "status": "stale",
                    "reason": "latest brief targets aaaaaaa, not bbbbbbb",
                    "html_path": "/tmp/old.html",
                }
            ),
        ),
    )

    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)

    assert result["action"] == "refresh_review_brief"
    assert result["review_brief_status"] == "stale"
    assert result["review_brief_path"] == "/tmp/old.html"
    assert "latest brief targets" in result["reason"]
    assert not any(_is_script("forge_cli.py", "ci-rollup")(call) for call in rec.calls)


def test_probe_does_not_require_brief_when_workspace_mode_is_off(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    _disable_review_brief(tmp_path)
    rec = _probe_recorder()

    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)

    assert result["review_brief_status"] == "disabled"
    assert any(_is_script("forge_cli.py", "ci-rollup")(call) for call in rec.calls)
    assert not any(_is_script("review_brief.py", "freshness")(call) for call in rec.calls)


def test_probe_blocks_attended_canonical_skip_conflict(tmp_path):
    # review_brief.freshness() returns blocking "missing" when an attended run's receipt
    # carries the canonical unattended skip; the merge probe must still refresh, not merge.
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    _enable_review_brief(tmp_path)
    rec = _probe_recorder()
    rec.when(
        _is_script("review_brief.py", "freshness"),
        _cp(
            0,
            json.dumps(
                {
                    "status": "missing",
                    "reason": "review_brief was skipped but the run is not authorized "
                    "as unattended; the brief is required",
                    "html_path": None,
                }
            ),
        ),
    )

    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)

    assert result["action"] == "refresh_review_brief"
    assert result["review_brief_status"] == "missing"
    assert not any(_is_script("forge_cli.py", "ci-rollup")(call) for call in rec.calls)


def test_probe_authorized_unattended_skip_is_non_blocking(tmp_path):
    # An authorized unattended skip reports "disabled" from freshness() itself (distinct from
    # the mode=off bypass above, which never calls freshness at all); the probe must continue
    # through the remaining eligibility gates rather than refresh the brief.
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    _enable_review_brief(tmp_path)
    rec = _probe_recorder()
    rec.when(
        _is_script("review_brief.py", "freshness"),
        _cp(
            0,
            json.dumps(
                {
                    "status": "disabled",
                    "reason": "unattended run authorized the canonical review-brief skip",
                    "html_path": None,
                }
            ),
        ),
    )

    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)

    assert result["action"] == "merge"
    assert result["review_brief_status"] == "disabled"
    assert any(_is_script("forge_cli.py", "ci-rollup")(call) for call in rec.calls)


def test_probe_attended_current_review_brief_continues(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    _enable_review_brief(tmp_path)
    rec = _probe_recorder()
    rec.when(
        _is_script("review_brief.py", "freshness"),
        _cp(
            0,
            json.dumps(
                {
                    "status": "current",
                    "reason": "brief matches local and PR heads",
                    "html_path": "/tmp/brief.html",
                }
            ),
        ),
    )

    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)

    assert result["action"] == "merge"
    assert result["review_brief_status"] == "current"
    assert any(_is_script("forge_cli.py", "ci-rollup")(call) for call in rec.calls)


def test_probe_gh_view_state_failure_treated_as_not_merged(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    rec = _probe_recorder(pr_state_rc=1, pr_state="")
    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    assert result["already_merged"] is False


# ─── probe: CI re-read ──────────────────────────────────────────────────────


def test_probe_ci_status_green_passthrough(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    rec = _probe_recorder(ci_status="green")
    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    assert result["ci_status"] == "green"


def test_probe_ci_status_pending_passthrough(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    rec = _probe_recorder(ci_status="pending")
    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    assert result["ci_status"] == "pending"


def test_probe_ci_rollup_nonzero_surfaced_as_error(tmp_path):
    # flow-vmzu: a non-zero ci-rollup exit must be surfaced, not silently read
    # as pending forever.
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    rec = _probe_recorder(ci_rc=1)
    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    assert result["ci_status"] == "error"


# ─── probe: harness eval trigger ───────────────────────────────────────────


def test_probe_scripts_diff_triggers_harness_eval(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    rec = _probe_recorder(changed_files=["plugins/flow/skills/flow/scripts/recall.py"])
    sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    assert any(_is_script("harness_eval.py", "score")(c) for c in rec.calls)


def test_probe_non_scripts_diff_skips_harness_eval(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    rec = _probe_recorder(changed_files=["plugins/flow/skills/flow/references/stage-merge.md"])
    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    assert not any(_is_script("harness_eval.py", "score")(c) for c in rec.calls)
    assert result["eval_status"] is None


def test_probe_harness_eval_exit_3_regressed_with_case_ids(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    stdout = json.dumps(
        {
            "cases": 2,
            "splits": {
                "held_in": {"regressed": ["case-a"]},
                "held_out": {"regressed": ["case-b"]},
            },
            "non_regression": False,
        }
    )
    rec = _probe_recorder(
        changed_files=["plugins/flow/skills/flow/scripts/recall.py"],
        harness_rc=3,
        harness_stdout=stdout,
    )
    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    assert result["eval_status"] == "regressed"
    assert sorted(result["regressed_cases"]) == ["case-a", "case-b"]


def test_probe_harness_eval_other_nonzero_is_error(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    rec = _probe_recorder(
        changed_files=["plugins/flow/skills/flow/scripts/recall.py"],
        harness_rc=2,
        harness_stdout="",
    )
    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    assert result["eval_status"] == "error"
    assert result["regressed_cases"] == []


def test_probe_harness_eval_writes_json_file(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    stdout = json.dumps({"cases": 0, "splits": {}, "non_regression": True})
    rec = _probe_recorder(
        changed_files=["plugins/flow/skills/flow/scripts/recall.py"],
        harness_rc=0,
        harness_stdout=stdout,
    )
    sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    written = (ticket_dir / "stages" / "harness_eval.json").read_text(encoding="utf-8")
    assert written == stdout


# ─── probe: main-CI health gate ────────────────────────────────────────────


def test_probe_main_ci_failed_flows_to_gate_reason(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    gate_result = {
        "action": "skip",
        "is_hot": False,
        "reason": "main CI red — auto-merge paused this turn",
    }
    rec = _probe_recorder(main_ci_status="failed", gate_result=gate_result)
    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    assert result["action"] == "skip"
    assert "main CI red" in result["reason"]


def test_probe_main_ci_error_resumes(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    rec = _probe_recorder(
        main_ci_status="error",
        gate_result={"action": "merge", "is_hot": False, "reason": "eligible"},
    )
    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    assert result["action"] == "merge"


# ─── probe: gate argv (exact-argv passthrough) ─────────────────────────────


def test_probe_gate_argv_ci_status_present(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    rec = _probe_recorder(ci_status="pending")
    sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    gate_call = next(c for c in rec.calls if _script_name(c) == "evolve_self_merge.py")
    assert "--ci-status" in gate_call
    assert gate_call[gate_call.index("--ci-status") + 1] == "pending"


def test_probe_gate_argv_main_ci_status_always_present(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    rec = _probe_recorder(main_ci_status="pending")
    sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    gate_call = next(c for c in rec.calls if _script_name(c) == "evolve_self_merge.py")
    assert "--main-ci-status" in gate_call
    assert gate_call[gate_call.index("--main-ci-status") + 1] == "pending"


def test_probe_gate_argv_eval_status_absent_when_not_run(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    rec = _probe_recorder(changed_files=["plugins/flow/skills/flow/references/stage-merge.md"])
    sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    gate_call = next(c for c in rec.calls if _script_name(c) == "evolve_self_merge.py")
    assert "--eval-status" not in gate_call


def test_probe_gate_argv_eval_status_present_when_run(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    rec = _probe_recorder(
        changed_files=["plugins/flow/skills/flow/scripts/recall.py"], harness_rc=0
    )
    sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    gate_call = next(c for c in rec.calls if _script_name(c) == "evolve_self_merge.py")
    assert "--eval-status" in gate_call
    assert gate_call[gate_call.index("--eval-status") + 1] == "pass"


def test_probe_gate_argv_changed_files_absent_when_empty(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    rec = _probe_recorder(changed_files=())
    sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    gate_call = next(c for c in rec.calls if _script_name(c) == "evolve_self_merge.py")
    assert "--changed-files" not in gate_call


def test_probe_gate_argv_changed_files_present_comma_joined(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    files = [
        "plugins/flow/skills/flow/references/foo.md",
        "plugins/flow/skills/flow/references/bar.md",
    ]
    rec = _probe_recorder(changed_files=files)
    sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    gate_call = next(c for c in rec.calls if _script_name(c) == "evolve_self_merge.py")
    assert "--changed-files" in gate_call
    assert gate_call[gate_call.index("--changed-files") + 1] == ",".join(files)


# ─── probe: verdict passthrough ────────────────────────────────────────────


def test_probe_verdict_action_is_hot_reason_verbatim_from_gate(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    gate_result = {"action": "merge", "is_hot": False, "reason": "eligible"}
    rec = _probe_recorder(gate_result=gate_result)
    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    assert result["action"] == "merge"
    assert result["is_hot"] is False
    assert result["reason"] == "eligible"


def test_probe_gate_skip_hot_passthrough(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    gate_result = {"action": "skip", "is_hot": True, "reason": "hot bead and auto_merge_hot is off"}
    rec = _probe_recorder(gate_result=gate_result)
    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    assert result["action"] == "skip"
    assert result["is_hot"] is True


# ─── probe: guard diff ──────────────────────────────────────────────────────


def test_probe_guard_diff_written_when_hot_merge(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    gate_result = {"action": "merge", "is_hot": True, "reason": "eligible"}
    rec = _probe_recorder(gate_result=gate_result, guard_diff_stdout="the-full-diff\n")
    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    expected_path = ticket_dir / "stages" / "merge_guard_diff.txt"
    assert result["guard_diff_path"] == str(expected_path)
    assert expected_path.read_text(encoding="utf-8") == "the-full-diff\n"


def test_probe_guard_diff_not_written_when_not_hot(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    gate_result = {"action": "merge", "is_hot": False, "reason": "eligible"}
    rec = _probe_recorder(gate_result=gate_result)
    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    assert result["guard_diff_path"] is None
    assert not (ticket_dir / "stages" / "merge_guard_diff.txt").exists()


def test_probe_guard_diff_not_written_when_skip(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    gate_result = {"action": "skip", "is_hot": True, "reason": "hot bead and auto_merge_hot is off"}
    rec = _probe_recorder(gate_result=gate_result)
    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    assert result["guard_diff_path"] is None


# ─── probe: default runner / candidate absolutization ──────────────────────


def test_default_runner_cwd_is_workspace_root(tmp_path, monkeypatch):
    captured = {}

    def fake_run(args, cwd=None, **kwargs):
        captured["cwd"] = cwd
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = sm._default_runner(tmp_path)
    runner(["echo", "hi"])
    assert captured["cwd"] == str(tmp_path)


def test_probe_harness_eval_candidate_absolutized(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    rec = _probe_recorder(changed_files=["plugins/flow/skills/flow/scripts/recall.py"])
    sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    harness_call = next(c for c in rec.calls if _script_name(c) == "harness_eval.py")
    expected = str((tmp_path / "plugins" / "flow" / "skills" / "flow" / "scripts").resolve())
    assert harness_call[harness_call.index("--candidate") + 1] == expected


# ─── probe: verdict JSON shape ──────────────────────────────────────────────

_VERDICT_KEYS = {
    "already_merged",
    "pr_id",
    "action",
    "is_hot",
    "reason",
    "ci_status",
    "eval_status",
    "regressed_cases",
    "changed_files",
    "guard_diff_path",
    "review_brief_status",
    "review_brief_reason",
    "review_brief_path",
}


def test_probe_verdict_shape_already_merged(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    rec = _probe_recorder(pr_state="MERGED")
    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    assert set(result.keys()) == _VERDICT_KEYS


def test_probe_verdict_shape_normal(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    rec = _probe_recorder()
    result = sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
    assert set(result.keys()) == _VERDICT_KEYS


def test_probe_pr_id_absent_raises(tmp_path):
    ticket_dir = tmp_path / "run"
    stages = ticket_dir / "stages"
    stages.mkdir(parents=True)
    (stages / "create_pr.out").write_text("nothing useful\n", encoding="utf-8")
    rec = _probe_recorder()
    import pytest

    with pytest.raises(sm.StageMergeError):
        sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)


# ─── execute: push-state guard (§3) ─────────────────────────────────────────


def test_execute_dirty_tracked_skips(tmp_path):
    rec = _execute_recorder(status_porcelain=" M some_file.py\n")
    result = sm.execute(tmp_path, _PR_ID, "flow-x", runner=rec)
    assert result["merged"] is False
    assert not any(_is_script("forge_cli.py", "merge")(c) for c in rec.calls)


def test_execute_unpushed_skips(tmp_path):
    rec = _execute_recorder(local_sha="aaa", remote_sha="bbb")
    result = sm.execute(tmp_path, _PR_ID, "flow-x", runner=rec)
    assert result["merged"] is False
    assert not any(_is_script("forge_cli.py", "merge")(c) for c in rec.calls)


def test_execute_status_call_excludes_untracked(tmp_path):
    rec = _execute_recorder()
    sm.execute(tmp_path, _PR_ID, "flow-x", runner=rec)
    assert any(_is_git("status", "--porcelain", "--untracked-files=no")(c) for c in rec.calls)


def test_execute_fetch_before_rev_parse_origin(tmp_path):
    rec = _execute_recorder(branch="feat-flow-x")
    sm.execute(tmp_path, _PR_ID, "flow-x", runner=rec)
    i_fetch = _first_idx(rec.calls, _is_git("fetch", "--quiet", "origin", "feat-flow-x"))
    i_rev = _first_idx(rec.calls, _is_git("rev-parse", "origin/feat-flow-x"))
    assert i_fetch < i_rev


def test_execute_deleted_remote_branch_skips_not_closes(tmp_path):
    # the reap already deleted the remote branch: rev-parse origin/<branch> fails.
    rec = _execute_recorder(remote_rc=1)
    result = sm.execute(tmp_path, _PR_ID, "flow-x", runner=rec)
    assert result["merged"] is False
    assert not any(_is_bd("close")(c) for c in rec.calls)


# ─── execute: mergeStateStatus branch ──────────────────────────────────────


def test_execute_dirty_merge_state_leaves_for_human(tmp_path):
    rec = _execute_recorder(merge_state="DIRTY")
    result = sm.execute(tmp_path, _PR_ID, "flow-x", runner=rec)
    assert result["merged"] is False
    assert not any(_is_script("forge_cli.py", "mark-ready")(c) for c in rec.calls)
    assert not any(_is_script("forge_cli.py", "merge")(c) for c in rec.calls)


def test_execute_clean_merges_full_sequence_in_order(tmp_path):
    rec = _execute_recorder(merge_state="CLEAN")
    result = sm.execute(tmp_path, _PR_ID, "flow-x", runner=rec)
    assert result == {"status": "completed", "merged": True}
    i_mark = _first_idx(rec.calls, _is_script("forge_cli.py", "mark-ready"))
    i_merge = _first_idx(rec.calls, _is_script("forge_cli.py", "merge"))
    i_close = _first_idx(rec.calls, _is_bd("close"))
    i_delete = _first_idx(rec.calls, _is_script("forge_cli.py", "delete-branch"))
    assert i_mark < i_merge < i_close < i_delete


def test_execute_draft_merge_state_also_merges(tmp_path):
    rec = _execute_recorder(merge_state="DRAFT")
    result = sm.execute(tmp_path, _PR_ID, "flow-x", runner=rec)
    assert result == {"status": "completed", "merged": True}


# ─── execute: merge failure ─────────────────────────────────────────────────


def test_execute_merge_failure_no_close_no_delete_status_failed(tmp_path):
    rec = _execute_recorder(merge_rc=1)
    result = sm.execute(tmp_path, _PR_ID, "flow-x", runner=rec)
    assert result["status"] == "failed"
    assert result["merged"] is False
    assert not any(_is_bd("close")(c) for c in rec.calls)
    assert not any(_is_script("forge_cli.py", "delete-branch")(c) for c in rec.calls)


# ─── execute: post-merge best-effort ────────────────────────────────────────


def test_execute_post_merge_close_hiccup_still_completed(tmp_path):
    rec = _execute_recorder(bd_close_rc=1)
    result = sm.execute(tmp_path, _PR_ID, "flow-x", runner=rec)
    assert result == {"status": "completed", "merged": True}


def test_execute_post_merge_delete_hiccup_still_completed(tmp_path):
    rec = _execute_recorder(delete_rc=1)
    result = sm.execute(tmp_path, _PR_ID, "flow-x", runner=rec)
    assert result == {"status": "completed", "merged": True}


def test_execute_post_merge_cover_hiccup_still_completed(tmp_path):
    _ticket_with_covers(tmp_path, "flow-x", ["flow-y"])
    rec = _execute_recorder(covers_rc=1)
    result = sm.execute(tmp_path, _PR_ID, "flow-x", runner=rec)
    assert result == {"status": "completed", "merged": True}


# ─── execute: reap-defer (no local teardown, ever) ─────────────────────────


def test_execute_never_issues_local_teardown_commands(tmp_path):
    rec = _execute_recorder()
    sm.execute(tmp_path, _PR_ID, "flow-x", runner=rec)
    for call in rec.calls:
        assert "worktree" not in call
        assert not any("flow_worktree" in tok for tok in call)
        is_git_branch_delete = (
            call[:1] == ["git"] and "branch" in call and ("-d" in call or "-D" in call)
        )
        assert not is_git_branch_delete


# ─── execute: already-merged ────────────────────────────────────────────────


def test_execute_already_merged_closes_and_covers_no_merge(tmp_path):
    _ticket_with_covers(tmp_path, "flow-x", ["flow-y"])
    rec = _execute_recorder()
    result = sm.execute(tmp_path, _PR_ID, "flow-x", runner=rec, already_merged=True)
    assert result == {"status": "completed", "merged": False, "already_merged": True}
    assert not any(_is_script("forge_cli.py", "merge")(c) for c in rec.calls)
    assert not any(_is_script("forge_cli.py", "mark-ready")(c) for c in rec.calls)
    assert any(_is_bd("close")(c) for c in rec.calls)
    assert any(_is_script("tracker_cli.py", "comment")(c) for c in rec.calls)


def test_execute_already_merged_bd_close_nonzero_tolerated(tmp_path):
    rec = _execute_recorder(bd_close_rc=1)
    result = sm.execute(tmp_path, _PR_ID, "flow-x", runner=rec, already_merged=True)
    assert result == {"status": "completed", "merged": False, "already_merged": True}


def test_execute_already_merged_reason_string(tmp_path):
    rec = _execute_recorder()
    sm.execute(tmp_path, _PR_ID, "flow-x", runner=rec, already_merged=True)
    close_call = next(c for c in rec.calls if _is_bd("close")(c))
    assert close_call == ["bd", "close", "flow-x", "--reason", f"PR #{_PR_ID} already merged"]


def test_execute_merge_success_close_reason_string(tmp_path):
    rec = _execute_recorder()
    sm.execute(tmp_path, _PR_ID, "flow-x", runner=rec)
    close_call = next(c for c in rec.calls if _is_bd("close")(c))
    assert close_call == ["bd", "close", "flow-x", "--reason", f"self-merged via PR #{_PR_ID}"]


# ─── cover-close ─────────────────────────────────────────────────────────────


def test_cover_close_order_per_cover(tmp_path):
    _ticket_with_covers(tmp_path, "flow-x", ["flow-y"])
    rec = _execute_recorder()
    sm._cover_close(tmp_path, "flow-x", _PR_ID, rec)
    kinds = []
    for c in rec.calls:
        if _is_script("tracker_cli.py", "comment")(c):
            kinds.append("comment")
        elif _is_script("tracker_cli.py", "transition")(c):
            kinds.append("transition")
        elif _is_bd("dep", "remove")(c):
            kinds.append("dep-remove")
    assert kinds == ["comment", "transition", "dep-remove"]


def test_cover_close_hiccup_non_fatal_continues(tmp_path):
    _ticket_with_covers(tmp_path, "flow-x", ["flow-y", "flow-z"])
    rec = _execute_recorder(covers_rc=1)
    sm._cover_close(tmp_path, "flow-x", _PR_ID, rec)  # must not raise
    comment_calls = [c for c in rec.calls if _is_script("tracker_cli.py", "comment")(c)]
    transition_calls = [c for c in rec.calls if _is_script("tracker_cli.py", "transition")(c)]
    dep_calls = [c for c in rec.calls if _is_bd("dep", "remove")(c)]
    assert len(comment_calls) == 2
    assert len(transition_calls) == 2
    assert len(dep_calls) == 2


def test_cover_close_empty_covers_closes_nothing(tmp_path):
    rec = _execute_recorder()
    sm._cover_close(tmp_path, "flow-x", _PR_ID, rec)
    assert rec.calls == []


def test_cover_close_multiple_covers_all_processed(tmp_path):
    _ticket_with_covers(tmp_path, "flow-x", ["flow-y", "flow-z"])
    rec = _execute_recorder()
    sm._cover_close(tmp_path, "flow-x", _PR_ID, rec)
    assert len(rec.calls) == 6
    keys_touched = {c[c.index("--key") + 1] for c in rec.calls if "--key" in c}
    assert keys_touched == {"flow-y", "flow-z"}


def test_cover_close_comment_text_and_dep_remove_argv(tmp_path):
    _ticket_with_covers(tmp_path, "flow-x", ["flow-y"])
    rec = _execute_recorder()
    sm._cover_close(tmp_path, "flow-x", _PR_ID, rec)
    comment_call = next(c for c in rec.calls if _is_script("tracker_cli.py", "comment")(c))
    assert (
        comment_call[comment_call.index("--text") + 1] == f"co-delivered by flow-x via PR #{_PR_ID}"
    )
    dep_call = next(c for c in rec.calls if _is_bd("dep", "remove")(c))
    assert dep_call == ["bd", "dep", "remove", "flow-y", "flow-x"]


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_probe_prints_json(tmp_path, capsys):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    rec = _probe_recorder()
    rc = sm.cli_main(
        [
            "probe",
            "--workspace-root",
            str(tmp_path),
            "--ticket-dir",
            str(ticket_dir),
            "--key",
            "flow-x",
        ],
        runner=rec,
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "merge"


def test_cli_probe_missing_pr_id_returns_1(tmp_path, capsys):
    ticket_dir = tmp_path / "run"
    stages = ticket_dir / "stages"
    stages.mkdir(parents=True)
    (stages / "create_pr.out").write_text("nothing useful\n", encoding="utf-8")
    rec = _probe_recorder()
    rc = sm.cli_main(
        [
            "probe",
            "--workspace-root",
            str(tmp_path),
            "--ticket-dir",
            str(ticket_dir),
            "--key",
            "flow-x",
        ],
        runner=rec,
    )
    assert rc == 1


def test_cli_execute_already_merged_flag(tmp_path, capsys):
    rec = _execute_recorder()
    rc = sm.cli_main(
        [
            "execute",
            "--workspace-root",
            str(tmp_path),
            "--pr",
            _PR_ID,
            "--key",
            "flow-x",
            "--already-merged",
        ],
        runner=rec,
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["already_merged"] is True
    assert not any(_is_script("forge_cli.py", "merge")(c) for c in rec.calls)


def test_cli_execute_failed_merge_returns_1(tmp_path, capsys):
    rec = _execute_recorder(merge_rc=1)
    rc = sm.cli_main(
        ["execute", "--workspace-root", str(tmp_path), "--pr", _PR_ID, "--key", "flow-x"],
        runner=rec,
    )
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "failed"


# ─── review pins: probe is side-effect-free; shelled argv matches argparse ─


def test_probe_never_merges_closes_or_deletes(tmp_path):
    ticket_dir = tmp_path / "run"
    _create_pr_out(ticket_dir)
    for state in ("OPEN", "MERGED", "CLOSED"):
        rec = _probe_recorder(pr_state=state)
        sm.probe(tmp_path, ticket_dir, "flow-x", runner=rec)
        assert not any(_is_script("forge_cli.py", "merge")(c) for c in rec.calls)
        assert not any(_is_script("forge_cli.py", "delete-branch")(c) for c in rec.calls)
        assert not any(c[:2] == ["bd", "close"] for c in rec.calls)
        assert not any(_is_script("tracker_cli.py", "transition")(c) for c in rec.calls)


@pytest.mark.parametrize(
    ("script", "argv"),
    [
        ("forge_cli.py", ["--workspace-root", ".", "merge", "--pr", "1", "--squash"]),
        ("forge_cli.py", ["--workspace-root", ".", "delete-branch", "--branch", "feat/x"]),
        ("forge_cli.py", ["--workspace-root", ".", "ci-rollup", "--pr", "1"]),
        ("forge_cli.py", ["--workspace-root", ".", "pr-info", "--pr", "1"]),
        ("main_ci_health.py", ["probe", "--workspace-root", "."]),
        ("harness_eval.py", ["score", "--candidate", "."]),
        (
            "tracker_cli.py",
            ["--workspace-root", ".", "transition", "--key", "k", "--to-state", "closed"],
        ),
    ],
)
def test_shelled_sibling_argv_matches_argparse(script, argv):
    # seam_check covers prose only and the unit suite mocks the Runner, so a
    # sibling flag rename would otherwise pass every gate and break at runtime.
    # Parse the exact argv shapes stage_merge constructs against the real
    # argparse surfaces (parse only, nothing executed).
    import importlib

    mod = importlib.import_module(script[:-3])
    parser_fn = getattr(mod, "_parse_args", None)
    try:
        if parser_fn is not None:
            parser_fn(argv)
        else:
            # main_ci_health builds its parser inside cli_main, which takes an
            # injectable runner: drive the REAL entry with a stub so only the
            # argv parse is exercised (a parse rejection raises SystemExit
            # before the stub result matters).
            def stub(args):
                return subprocess.CompletedProcess(args, 1, "", "stub")

            mod.cli_main(argv, runner=stub)
    except SystemExit as exc:
        pytest.fail(f"{script} rejected argv {argv}: exit {exc.code}")
