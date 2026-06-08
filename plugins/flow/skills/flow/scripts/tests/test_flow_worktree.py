"""Tests for flow_worktree.py — the post-approval worktree bootstrap.

git/mise are injected via a fake runner; the worktree dir is materialized by the
fake `git worktree add` (simulating a checkout where .flow is gitignored, so the
bootstrap must copy config in).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import flow_worktree as fw
import state
import triage


def _main_checkout(
    tmp: Path,
    *,
    with_mise: bool = False,
    stages: list[str] | None = None,
    maintainer: bool = False,
) -> Path:
    stages = stages or ["ticket", "plan", "implement", "commit", "reflect"]
    main = tmp / "main"
    flow = main / ".flow"
    flow.mkdir(parents=True)
    (flow / ".initialized").touch()
    lines = [
        "[tracker]",
        'backend = "jira"',
        "[tracker.jira]",
        'cloud_id = "x"',
        'project_key = "FT"',
        "[pipeline]",
        "stages = [" + ", ".join(f'"{s}"' for s in stages) + "]",
        "[memory]",
        'namespace = "FT"',
        "compounding = true",
    ]
    if maintainer:
        lines += ["[maintainer]", "self_target = true"]
    (flow / "workspace.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (main / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (main / ".claude").mkdir()
    (main / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    if with_mise:
        (main / "mise.toml").write_text("[tools]\npython = '3.12'\n", encoding="utf-8")
    return main


def _fake_runner(
    *,
    worktree_has_flow: bool = False,
    mise_rc: int = 0,
    calls: list | None = None,
    main: Path | None = None,
    ignored: set[str] | None = None,
) -> fw.Runner:
    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append(args)
        if args[:3] == ["git", "worktree", "add"]:
            wt = Path(args[5])  # git worktree add -b <branch> <path> <base>
            wt.mkdir(parents=True, exist_ok=True)
            # real `git worktree add` checks out committed files (e.g. mise.toml)
            if main is not None:
                for committed in ("mise.toml", ".mise.toml"):
                    if (main / committed).exists():
                        (wt / committed).write_text(
                            (main / committed).read_text(), encoding="utf-8"
                        )
            if worktree_has_flow:
                (wt / ".flow").mkdir()
                (wt / ".flow" / "workspace.toml").write_text(
                    '[tracker]\nbackend = "jira"\n[pipeline]\nstages = ["ticket", "plan", "implement"]\n[memory]\nnamespace = "FT"\n',
                    encoding="utf-8",
                )
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["git", "check-ignore"]:
            req = [a for a in args[3:] if a != "--"]
            hit = [f for f in req if ignored and f in ignored]
            out = "".join(f + "\n" for f in hit)
            return subprocess.CompletedProcess(args, 0 if hit else 1, out, "")
        if args[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(args, 0, "wtsha0001\n", "")
        if args[:2] == ["mise", "trust"]:
            return subprocess.CompletedProcess(
                args, mise_rc, "", "" if mise_rc == 0 else "untrusted"
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    return run


def _plan_file(tmp: Path, text: str = "Goal: do the thing.\nFiles: a.py\n") -> Path:
    p = tmp / "plan.md"
    p.write_text(text, encoding="utf-8")
    return p


def _run(tmp: Path, main: Path, **kw):
    wt = kw.pop("worktree", tmp / "wt")
    return fw.bootstrap(
        ticket="FT-1",
        plan_from=_plan_file(tmp),
        base="main",
        branch="feature/FT-1-thing",
        main_root=main,
        worktree_override=str(wt),
        runner=kw.pop("runner", _fake_runner()),
        **kw,
    )


# ─── bootstrap ────────────────────────────────────────────────────────────────


def test_seeds_plan_completed_with_output_path(tmp_path: Path) -> None:
    main = _main_checkout(tmp_path)
    res = _run(tmp_path, main)
    td = Path(res["worktree"]) / ".flow" / "runs" / "FT-1"
    ts, code = state.read(td)
    assert code == 0 and ts is not None
    assert ts.stages["plan"].status == "completed"
    plan_out = td / "stages" / "plan.out"
    assert ts.stages["plan"].output_path == str(plan_out)
    assert "Goal: do the thing." in plan_out.read_text(encoding="utf-8")
    # ticket left pending so the tail self-fetches ticket.json + frontmatter
    assert ts.stages["ticket"].status == "pending"


def test_copies_gitignored_config(tmp_path: Path) -> None:
    main = _main_checkout(tmp_path)
    res = _run(tmp_path, main)
    wt = Path(res["worktree"])
    assert (wt / ".env").read_text(encoding="utf-8") == "SECRET=1\n"
    assert (wt / ".claude" / "settings.json").exists()
    assert ".env" in res["copied"] and ".claude" in res["copied"]


def test_redirects_memory_via_sibling_not_workspace_toml(tmp_path: Path) -> None:
    main = _main_checkout(tmp_path)
    res = _run(tmp_path, main)
    wt_flow = Path(res["worktree"]) / ".flow"
    sibling = (wt_flow / "memory-root").read_text(encoding="utf-8")
    assert sibling.strip() == str(main.resolve() / ".flow")
    # the tracked workspace.toml is NOT rewritten with an abs root
    assert "root =" not in (wt_flow / "workspace.toml").read_text(encoding="utf-8")


def test_bootstrap_leaves_workspace_toml_byte_identical_to_main(tmp_path: Path) -> None:
    # the direct ticket-level regression: the worktree's tracked workspace.toml
    # stays byte-for-byte equal to main's copy, so a per-machine abs path can never
    # ride into implement.diff / a commit to origin/main.
    main = _main_checkout(tmp_path)
    res = _run(tmp_path, main)
    wt_ws = Path(res["worktree"]) / ".flow" / "workspace.toml"
    main_ws = main / ".flow" / "workspace.toml"
    assert wt_ws.read_bytes() == main_ws.read_bytes()


def test_prepopulates_commit_frontmatter(tmp_path: Path) -> None:
    main = _main_checkout(tmp_path)
    res = _run(tmp_path, main, commit_type="feat", commit_summary="add the thing")
    fm = (Path(res["worktree"]) / ".flow" / "tickets" / "FT-1.md").read_text(encoding="utf-8")
    assert "commit_type" in fm and "feat" in fm
    assert "add the thing" in fm


def test_seeds_planned_files_as_list(tmp_path: Path) -> None:
    # the implement pre-hook reads frontmatter planned_files; without it the tail
    # would pause to ask. Confirm it lands as a TOML array (a list when parsed back).
    import ticket_frontmatter

    main = _main_checkout(tmp_path)
    res = _run(tmp_path, main, planned_files=["src/a.py", "src/b.py"])
    fm_path = Path(res["worktree"]) / ".flow" / "tickets" / "FT-1.md"
    parsed = ticket_frontmatter.read(fm_path)
    assert parsed["planned_files"] == ["src/a.py", "src/b.py"]


_PLUGIN_JSON = "plugins/flow/.claude-plugin/plugin.json"
_MARKETPLACE_JSON = ".claude-plugin/marketplace.json"


def _seed_planned(tmp_path: Path, main: Path, planned: list[str]) -> list[str]:
    import ticket_frontmatter

    res = _run(tmp_path, main, planned_files=planned)
    fm_path = Path(res["worktree"]) / ".flow" / "tickets" / "FT-1.md"
    return ticket_frontmatter.read(fm_path)["planned_files"]


def test_maintainer_plugin_change_auto_includes_version_files(tmp_path: Path) -> None:
    # maintainer self-target + a plugins/flow/ path -> both version files appended,
    # so the implement-stage version bump is already in planned_files (no reconcile).
    main = _main_checkout(tmp_path, maintainer=True)
    planned = _seed_planned(tmp_path, main, ["plugins/flow/skills/flow/scripts/x.py"])
    assert planned == [
        "plugins/flow/skills/flow/scripts/x.py",
        _PLUGIN_JSON,
        _MARKETPLACE_JSON,
    ]


def test_maintainer_non_plugin_change_leaves_planned_files(tmp_path: Path) -> None:
    # maintainer, but no plugins/flow/ path -> no version bump expected, unchanged.
    main = _main_checkout(tmp_path, maintainer=True)
    planned = _seed_planned(tmp_path, main, ["src/a.py"])
    assert planned == ["src/a.py"]


def test_maintainer_dedups_already_present_version_file(tmp_path: Path) -> None:
    # one version file already planned -> no duplicate, the other appended once.
    main = _main_checkout(tmp_path, maintainer=True)
    planned = _seed_planned(
        tmp_path, main, [_MARKETPLACE_JSON, "plugins/flow/skills/flow/SKILL.md"]
    )
    assert planned == [
        _MARKETPLACE_JSON,
        "plugins/flow/skills/flow/SKILL.md",
        _PLUGIN_JSON,
    ]
    assert planned.count(_MARKETPLACE_JSON) == 1


def test_user_project_plugin_path_not_augmented(tmp_path: Path, monkeypatch) -> None:
    # no maintainer marker (default main) -> user project, version files untouched
    # even when a plugins/flow/ path is planned. Pin the global-config path off so
    # a stray ~/.flow/config.toml pointer can't flip this on.
    import maintainer

    monkeypatch.setattr(maintainer, "_global_config_path", lambda: tmp_path / "no-global.toml")
    main = _main_checkout(tmp_path)
    planned = _seed_planned(tmp_path, main, ["plugins/flow/skills/flow/scripts/x.py"])
    assert planned == ["plugins/flow/skills/flow/scripts/x.py"]


def test_mise_trust_invoked_when_mise_present(tmp_path: Path) -> None:
    main = _main_checkout(tmp_path, with_mise=True)
    calls: list = []
    _run(tmp_path, main, runner=_fake_runner(calls=calls, main=main))
    assert any(c[:2] == ["mise", "trust"] for c in calls)


def test_mise_trust_failure_is_warning_not_fatal(tmp_path: Path) -> None:
    main = _main_checkout(tmp_path, with_mise=True)
    res = _run(tmp_path, main, runner=_fake_runner(mise_rc=1, main=main))
    assert any("mise trust failed" in w for w in res["warnings"])
    # still seeded successfully
    td = Path(res["worktree"]) / ".flow" / "runs" / "FT-1"
    ts, _ = state.read(td)
    assert ts is not None and ts.stages["plan"].status == "completed"


def test_works_when_worktree_already_has_committed_flow(tmp_path: Path) -> None:
    # committed-.flow case: the worktree already carries workspace.toml; bootstrap
    # writes the sibling redirect without clobbering the committed toml.
    main = _main_checkout(tmp_path)
    res = _run(tmp_path, main, runner=_fake_runner(worktree_has_flow=True))
    wt_flow = Path(res["worktree"]) / ".flow"
    assert (wt_flow / "memory-root").read_text(encoding="utf-8").strip() == str(
        main.resolve() / ".flow"
    )
    assert "root =" not in (wt_flow / "workspace.toml").read_text(encoding="utf-8")


def test_no_launch_cmd_emitted(tmp_path: Path) -> None:
    # in-session model: the spec session enters the worktree itself, so the
    # bootstrap no longer emits a `claude --bg` launch line.
    main = _main_checkout(tmp_path)
    res = _run(tmp_path, main)
    assert "launch_cmd" not in res
    assert res["worktree"]


def test_cli_missing_main_workspace_exits_2(tmp_path: Path, monkeypatch, capsys) -> None:
    # main has no .flow/workspace.toml -> _ConfigError -> exit 2
    main = tmp_path / "bare"
    main.mkdir()
    monkeypatch.setattr(fw, "_default_runner", lambda: _fake_runner())
    plan = _plan_file(tmp_path)
    rc = fw.cli_main(
        [
            "create",
            "--ticket",
            "FT-1",
            "--plan-from",
            str(plan),
            "--base",
            "main",
            "--branch",
            "feature/FT-1-x",
            "--main-root",
            str(main),
            "--worktree-path",
            str(tmp_path / "wt"),
        ]
    )
    assert rc == 2


# ─── e2e recipe gate ──────────────────────────────────────────────────────────


def _main_with_e2e_handler(tmp: Path, handler: str) -> Path:
    """Main checkout whose workspace.toml wires the e2e stage to `handler`."""
    main = _main_checkout(tmp, stages=["ticket", "plan", "implement", "e2e", "commit", "reflect"])
    ws = main / ".flow" / "workspace.toml"
    ws.write_text(
        ws.read_text(encoding="utf-8") + f'[pipeline.handlers]\ne2e = "{handler}"\n',
        encoding="utf-8",
    )
    return main


def test_e2e_enabled_without_recipe_refuses(tmp_path: Path) -> None:
    main = _main_with_e2e_handler(tmp_path, "subagent:general-purpose")
    try:
        _run(tmp_path, main)
    except fw._ConfigError as exc:
        assert "e2e-recipe" in str(exc)
    else:
        raise AssertionError("expected _ConfigError when e2e enabled and no recipe")
    # gate fires before any git side effect: no worktree dir
    assert not (tmp_path / "wt").exists()


def test_e2e_enabled_with_recipe_stamps_frontmatter(tmp_path: Path) -> None:
    import ticket_frontmatter

    main = _main_with_e2e_handler(tmp_path, "subagent:general-purpose")
    recipe = "runner=duckdb fixture=load 42 cmd='mise run ...' expected=green"
    _run(tmp_path, main, runner=_fake_runner(main=main), e2e_recipe=recipe)
    fm = ticket_frontmatter.read(tmp_path / "wt" / ".flow" / "tickets" / "FT-1.md")
    assert fm["e2e_recipe"] == recipe


def test_e2e_none_does_not_require_recipe(tmp_path: Path) -> None:
    main = _main_with_e2e_handler(tmp_path, "none")
    # no recipe passed, but e2e=none → no gate, bootstrap succeeds
    res = _run(tmp_path, main, runner=_fake_runner(main=main))
    assert res["ticket"] == "FT-1"


# ─── planned_files gitignore gate ─────────────────────────────────────────────


def test_bootstrap_rejects_gitignored_planned_file(tmp_path: Path) -> None:
    # A gitignored planned file (no .gitignore in the plan) would be silently
    # dropped from the commit: refuse at the gate. The ignore check runs INSIDE the
    # worktree (base may carry .gitignore negations main lacks), so the worktree is
    # created first, then removed on rejection — refusing leaves no orphan.
    main = _main_checkout(tmp_path)
    calls: list = []
    with pytest.raises(fw._ConfigError):
        _run(
            tmp_path,
            main,
            planned_files=["data/x.csv"],
            runner=_fake_runner(ignored={"data/x.csv"}, calls=calls, main=main),
        )
    assert any(c[:3] == ["git", "worktree", "add"] for c in calls)
    assert any(c[:4] == ["git", "worktree", "remove", "--force"] for c in calls)


def test_bootstrap_warns_when_gitignore_also_planned(tmp_path: Path) -> None:
    # The plan touches .gitignore, so a currently-ignored planned file may be
    # un-ignored by the planned negation: warn, do not refuse.
    main = _main_checkout(tmp_path)
    res = _run(
        tmp_path,
        main,
        planned_files=[".gitignore", "data/x.csv"],
        runner=_fake_runner(ignored={"data/x.csv"}, main=main),
    )
    assert res["ticket"] == "FT-1"
    assert any("data/x.csv" in w and "gitignored" in w for w in res["warnings"])


def test_bootstrap_accepts_non_ignored_planned_files(tmp_path: Path) -> None:
    main = _main_checkout(tmp_path)
    res = _run(
        tmp_path,
        main,
        planned_files=["a.py"],
        runner=_fake_runner(ignored=set(), main=main),
    )
    assert res["ticket"] == "FT-1"
    assert not any("gitignored" in w for w in res["warnings"])


def _base_runner(symref_outputs):
    """Runner answering `git symbolic-ref` from a queue; everything else ok."""
    calls: list[list[str]] = []
    seq = list(symref_outputs)

    def run(args, cwd):
        calls.append(args)
        if args[:2] == ["git", "symbolic-ref"]:
            rc, out = seq.pop(0) if seq else (1, "")
            return subprocess.CompletedProcess(args, rc, out, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    return run, calls


def test_resolve_base_passthrough(tmp_path):
    run, calls = _base_runner([])
    assert fw._resolve_base("feature/x", tmp_path, run) == "feature/x"
    assert calls == []  # a literal base never touches the network


def test_resolve_base_default_resolves_origin_head(tmp_path):
    run, calls = _base_runner([(0, "origin/main\n")])
    assert fw._resolve_base("@default", tmp_path, run) == "origin/main"
    assert ["git", "fetch", "--quiet", "origin"] in calls


def test_resolve_base_default_retries_via_set_head(tmp_path):
    run, calls = _base_runner([(1, ""), (0, "origin/dev\n")])
    assert fw._resolve_base("@default", tmp_path, run) == "origin/dev"
    assert any(a[:3] == ["git", "remote", "set-head"] for a in calls)


def test_resolve_base_default_fallback(tmp_path):
    run, _ = _base_runner([(1, ""), (1, "")])
    assert fw._resolve_base("@default", tmp_path, run) == "origin/main"


# ─── reap subcommand ──────────────────────────────────────────────────────────


def _porcelain(entries: list[tuple[str, str | None]]) -> str:
    """Render `git worktree list --porcelain` text. None branch -> detached."""
    blocks = []
    for path, branch in entries:
        lines = [f"worktree {path}", "HEAD abc123"]
        if branch is None:
            lines.append("detached")
        else:
            lines.append(f"branch refs/heads/{branch}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def test_parse_worktree_list_porcelain() -> None:
    blob = _porcelain(
        [
            ("/main", "main"),
            ("/main.worktrees/feature-FT-1-thing", "feature/FT-1-thing"),
            ("/main.worktrees/detached", None),
        ]
    )
    pairs = fw._parse_worktree_list(blob)
    assert pairs == [
        ("/main", "main"),
        ("/main.worktrees/feature-FT-1-thing", "feature/FT-1-thing"),
        ("/main.worktrees/detached", None),
    ]


def _reap_runner(*, worktrees: str, calls: list, remove_rc: int = 0, branch_rc: int = 0):
    """Runner answering `git worktree list --porcelain` from `worktrees`; records
    every call, and lets the worktree-remove / branch-delete return codes be set."""

    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:4] == ["git", "worktree", "list", "--porcelain"]:
            return subprocess.CompletedProcess(args, 0, worktrees, "")
        if args[:4] == ["git", "worktree", "remove", "--force"]:
            return subprocess.CompletedProcess(
                args, remove_rc, "", "" if remove_rc == 0 else "busy"
            )
        if args[:3] == ["git", "branch", "-D"]:
            return subprocess.CompletedProcess(
                args, branch_rc, "", "" if branch_rc == 0 else "gone"
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    return run


def _seed_live_lease(ticket_dir: Path) -> None:
    import lease

    ticket_dir.mkdir(parents=True, exist_ok=True)
    lease.acquire(
        ticket_dir,
        run_id="run-x",
        ttl_seconds=3600,
        now_iso="2999-01-01T00:00:00Z",
        current_boot="boot-x",
        hostname="host-x",
        cwd="/cwd-x",
    )


def test_reap_removes_worktree_and_branch_when_free(tmp_path: Path) -> None:
    wt = tmp_path / "main.worktrees" / "feature-FT-1-thing"
    wt.mkdir(parents=True)
    calls: list = []
    runner = _reap_runner(
        worktrees=_porcelain([(str(tmp_path / "main"), "main"), (str(wt), "feature/FT-1-thing")]),
        calls=calls,
    )
    receipt = fw.reap_worktree(ticket="FT-1", main_root=tmp_path / "main", runner=runner)
    assert receipt["worktree_removed"] is True
    assert receipt["branch_deleted"] is True
    assert receipt["branch"] == "feature/FT-1-thing"
    assert receipt["skipped"] is None
    assert any(c[:4] == ["git", "worktree", "remove", "--force"] for c in calls)
    assert any(c[:3] == ["git", "branch", "-D"] for c in calls)


def test_reap_skips_when_lease_live(tmp_path: Path) -> None:
    wt = tmp_path / "main.worktrees" / "feature-FT-1-thing"
    wt.mkdir(parents=True)
    _seed_live_lease(wt / ".flow" / "runs" / "FT-1")
    calls: list = []
    runner = _reap_runner(
        worktrees=_porcelain([(str(wt), "feature/FT-1-thing")]),
        calls=calls,
    )
    receipt = fw.reap_worktree(ticket="FT-1", main_root=tmp_path / "main", runner=runner)
    assert receipt["worktree_removed"] is False
    assert receipt["branch_deleted"] is False
    assert receipt["skipped"] and "live" in receipt["skipped"]
    # a live session: touch NOTHING
    assert not any(c[:4] == ["git", "worktree", "remove", "--force"] for c in calls)
    assert not any(c[:3] == ["git", "branch", "-D"] for c in calls)


def test_reap_skips_when_lease_corrupt(tmp_path: Path) -> None:
    import lease

    wt = tmp_path / "main.worktrees" / "feature-FT-1-thing"
    ticket_dir = wt / ".flow" / "runs" / "FT-1"
    ticket_dir.mkdir(parents=True)
    lease.run_lock_path(ticket_dir).write_text("{not json", encoding="utf-8")
    calls: list = []
    runner = _reap_runner(
        worktrees=_porcelain([(str(wt), "feature/FT-1-thing")]),
        calls=calls,
    )
    receipt = fw.reap_worktree(ticket="FT-1", main_root=tmp_path / "main", runner=runner)
    assert receipt["worktree_removed"] is False
    assert receipt["branch_deleted"] is False
    # distinct reason from the "live" skip, so the human can tell why it was held.
    assert receipt["skipped"] and "corrupt" in receipt["skipped"]
    assert receipt["skipped"] != "lease live (run still in progress)"
    # a possibly-live corrupt run: touch NOTHING
    assert not any(c[:4] == ["git", "worktree", "remove", "--force"] for c in calls)
    assert not any(c[:3] == ["git", "branch", "-D"] for c in calls)


def test_reap_idempotent_when_nothing_to_remove(tmp_path: Path) -> None:
    calls: list = []
    runner = _reap_runner(
        worktrees=_porcelain([(str(tmp_path / "main"), "main")]),
        calls=calls,
    )
    receipt = fw.reap_worktree(
        ticket="FT-1", main_root=tmp_path / "main", branch="feature/FT-1-thing", runner=runner
    )
    assert receipt["worktree_removed"] is False
    assert not any(c[:4] == ["git", "worktree", "remove", "--force"] for c in calls)
    # branch was supplied, so a (tolerant) delete is still attempted; here it returns 0
    assert receipt["branch"] == "feature/FT-1-thing"


def test_reap_noop_when_no_branch_and_no_worktree(tmp_path: Path) -> None:
    # neither a matching worktree nor an explicit --branch: a clean no-op,
    # zero git mutations (the true idempotent path).
    calls: list = []
    runner = _reap_runner(
        worktrees=_porcelain([(str(tmp_path / "main"), "main")]),
        calls=calls,
    )
    receipt = fw.reap_worktree(ticket="FT-1", main_root=tmp_path / "main", runner=runner)
    assert receipt["branch"] is None
    assert receipt["worktree_removed"] is False and receipt["branch_deleted"] is False
    assert not any(c[:3] == ["git", "branch", "-D"] for c in calls)
    assert not any(c[:4] == ["git", "worktree", "remove", "--force"] for c in calls)


def test_reap_tolerates_already_gone_branch(tmp_path: Path) -> None:
    # a squash-merge can leave the branch already absent; `git branch -D` returns
    # non-zero -> branch_deleted=False, no exception.
    calls: list = []
    runner = _reap_runner(
        worktrees=_porcelain([(str(tmp_path / "main"), "main")]),
        calls=calls,
        branch_rc=1,
    )
    receipt = fw.reap_worktree(
        ticket="FT-1", main_root=tmp_path / "main", branch="feature/FT-1-thing", runner=runner
    )
    assert receipt["branch_deleted"] is False
    assert receipt["skipped"] is None


def test_reap_deletes_leaked_branch_when_worktree_gone(tmp_path: Path) -> None:
    # the worktree is already gone but the local branch leaked: delete it.
    calls: list = []
    runner = _reap_runner(
        worktrees=_porcelain([(str(tmp_path / "main"), "main")]),
        calls=calls,
    )
    receipt = fw.reap_worktree(
        ticket="FT-1", main_root=tmp_path / "main", branch="feature/FT-1-thing", runner=runner
    )
    assert receipt["worktree_removed"] is False
    assert receipt["branch_deleted"] is True
    assert any(c == ["git", "branch", "-D", "feature/FT-1-thing"] for c in calls)


def test_reap_remove_failure_skips_branch_delete(tmp_path: Path) -> None:
    wt = tmp_path / "main.worktrees" / "feature-FT-1-thing"
    wt.mkdir(parents=True)
    calls: list = []
    runner = _reap_runner(
        worktrees=_porcelain([(str(wt), "feature/FT-1-thing")]),
        calls=calls,
        remove_rc=1,
    )
    receipt = fw.reap_worktree(ticket="FT-1", main_root=tmp_path / "main", runner=runner)
    assert receipt["worktree_removed"] is False
    assert receipt["branch_deleted"] is False
    assert receipt["skipped"]
    assert not any(c[:3] == ["git", "branch", "-D"] for c in calls)


def test_reap_cli_prints_receipt(tmp_path: Path, monkeypatch, capsys) -> None:
    calls: list = []
    runner = _reap_runner(
        worktrees=_porcelain([(str(tmp_path / "main"), "main")]),
        calls=calls,
    )
    monkeypatch.setattr(fw, "_default_runner", lambda: runner)
    (tmp_path / "main").mkdir()
    rc = fw.cli_main(
        [
            "reap",
            "--ticket",
            "FT-1",
            "--branch",
            "feature/FT-1-thing",
            "--main-root",
            str(tmp_path / "main"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert '"ticket": "FT-1"' in out and '"branch": "feature/FT-1-thing"' in out


# ─── hot hard-floor (code-enforced, flow-aen) ───────────────────────────────
# The is_hot_change floor lives here at the shared bootstrap so every autonomous
# self-approve path (incl. clean >=90%, which step-5 prose never gated) is caught.
# triage.decided's own logic is covered in test_triage.py; here we monkeypatch it
# to isolate the signal detection (--auto / @default) + the beads backend gate.


def _main_beads(tmp: Path, *, maintainer: bool = True) -> Path:
    main = tmp / "main"
    flow = main / ".flow"
    flow.mkdir(parents=True)
    (flow / ".initialized").touch()
    lines = [
        "[tracker]",
        'backend = "beads"',
        "[tracker.beads]",
        'prefix = "flow"',
        "shared_server = true",
        "[pipeline]",
        'stages = ["ticket", "plan", "implement", "commit", "reflect"]',
        "[memory]",
        'namespace = "flow"',
        "compounding = true",
    ]
    if maintainer:
        lines += ["[maintainer]", "self_target = true"]
    (flow / "workspace.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (main / ".env").write_text("S=1\n", encoding="utf-8")
    (main / ".claude").mkdir()
    (main / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    return main


def _boot(tmp: Path, main: Path, *, base: str, auto: bool, planned, runner=None):
    return fw.bootstrap(
        ticket="flow-x1",
        plan_from=_plan_file(tmp),
        base=base,
        branch="feature/flow-x1-thing",
        main_root=main,
        worktree_override=str(tmp / "wt"),
        planned_files=planned,
        auto=auto,
        runner=runner or _fake_runner(main=main),
    )


def test_auto_hot_no_decision_refuses(tmp_path, monkeypatch):
    main = _main_beads(tmp_path)
    monkeypatch.setattr(triage, "decided", lambda *a, **k: {"is_hot": True, "decided": False})
    calls: list = []
    with pytest.raises(fw._ConfigError):
        _boot(
            tmp_path,
            main,
            base="main",
            auto=True,
            planned=["lease.py"],
            runner=_fake_runner(main=main, calls=calls),
        )
    # refused BEFORE creating the worktree -> no orphan
    assert not any(c[:3] == ["git", "worktree", "add"] for c in calls)
    assert not (tmp_path / "wt").exists()


def test_default_base_hot_no_decision_refuses(tmp_path, monkeypatch):
    # the flow-6mx clean->90% path: --auto run passes @default, not args.auto
    main = _main_beads(tmp_path)
    monkeypatch.setattr(triage, "decided", lambda *a, **k: {"is_hot": True, "decided": False})
    with pytest.raises(fw._ConfigError):
        _boot(tmp_path, main, base="@default", auto=False, planned=["snapshot.py"])


def test_auto_hot_with_decision_proceeds(tmp_path, monkeypatch):
    main = _main_beads(tmp_path)
    monkeypatch.setattr(triage, "decided", lambda *a, **k: {"is_hot": True, "decided": True})
    res = _boot(tmp_path, main, base="main", auto=True, planned=["lease.py"])
    assert res["ticket"] == "flow-x1"


def test_auto_non_hot_proceeds(tmp_path, monkeypatch):
    main = _main_beads(tmp_path)
    monkeypatch.setattr(triage, "decided", lambda *a, **k: {"is_hot": False, "decided": False})
    res = _boot(tmp_path, main, base="main", auto=True, planned=["some_helper.py"])
    assert res["ticket"] == "flow-x1"


def test_interactive_hot_not_gated(tmp_path, monkeypatch):
    # no --auto, base is not @default -> the floor is the human at ExitPlanMode,
    # so decided() must never even be consulted
    main = _main_beads(tmp_path)

    def _boom(*a, **k):
        raise AssertionError("decided() must not run on the interactive path")

    monkeypatch.setattr(triage, "decided", _boom)
    res = _boot(tmp_path, main, base="main", auto=False, planned=["lease.py"])
    assert res["ticket"] == "flow-x1"


def test_non_beads_backend_skips_gate(tmp_path, monkeypatch):
    # Jira has no DECISION-record seam; gating it would permanently block a hot
    # --auto change. The gate must not consult decided() for a non-beads tracker.
    main = _main_checkout(tmp_path, maintainer=True)  # backend = jira

    def _boom(*a, **k):
        raise AssertionError("decided() must not run for a non-beads tracker")

    monkeypatch.setattr(triage, "decided", _boom)
    res = _boot(tmp_path, main, base="@default", auto=True, planned=["lease.py"])
    assert res["ticket"] == "flow-x1"
