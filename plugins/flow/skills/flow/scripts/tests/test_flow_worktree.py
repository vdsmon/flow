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


def _main_checkout(tmp: Path, *, with_mise: bool = False, stages: list[str] | None = None) -> Path:
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


# ─── _set_memory_root (pure) ──────────────────────────────────────────────────


def test_set_memory_root_inserts_under_memory() -> None:
    toml = '[tracker]\nbackend = "jira"\n[memory]\nnamespace = "FT"\ncompounding = true\n'
    out = fw._set_memory_root(toml, "/abs/main/.flow")
    assert 'root = "/abs/main/.flow"' in out
    assert out.index("[memory]") < out.index("root =")
    # tracker section untouched
    assert "[tracker]" in out and 'backend = "jira"' in out


def test_set_memory_root_replaces_existing() -> None:
    toml = '[memory]\nnamespace = "FT"\nroot = "/old/.flow"\n'
    out = fw._set_memory_root(toml, "/new/.flow")
    assert 'root = "/new/.flow"' in out
    assert "/old/.flow" not in out


def test_set_memory_root_memory_is_last_table() -> None:
    toml = '[tracker]\nbackend = "jira"\n[memory]\nnamespace = "FT"\n'
    out = fw._set_memory_root(toml, "/x/.flow")
    assert 'root = "/x/.flow"' in out


def test_set_memory_root_header_with_inline_comment() -> None:
    # a [memory] header carrying an inline comment must still be recognized,
    # else a duplicate [memory] table gets appended and the file won't parse.
    import tomllib

    toml = '[tracker]\nbackend = "jira"\n[memory]  # the compounding store\nnamespace = "FT"\n'
    out = fw._set_memory_root(toml, "/x/.flow")
    parsed = tomllib.loads(out)
    assert parsed["memory"]["root"] == "/x/.flow"
    assert out.count("[memory]") == 1  # no duplicate table


def test_set_memory_root_does_not_match_rootlike_key() -> None:
    # `root_dir` under [memory] must not be mistaken for the `root` key.
    import tomllib

    toml = '[memory]\nnamespace = "FT"\nroot_dir = "/keep/me"\n'
    out = fw._set_memory_root(toml, "/x/.flow")
    parsed = tomllib.loads(out)
    assert parsed["memory"]["root"] == "/x/.flow"
    assert parsed["memory"]["root_dir"] == "/keep/me"


def test_set_memory_root_output_always_parses() -> None:
    import tomllib

    for toml in (
        '[memory]\nnamespace = "FT"\n',
        '[tracker]\nx = 1\n[memory]\nnamespace = "FT"\n[pipeline]\nstages = ["ticket"]\n',
        '[memory]\nnamespace = "FT"\nroot = "/old"\n',
    ):
        parsed = tomllib.loads(fw._set_memory_root(toml, "/new/.flow"))
        assert parsed["memory"]["root"] == "/new/.flow"


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


def test_sets_memory_root_to_main_flow(tmp_path: Path) -> None:
    main = _main_checkout(tmp_path)
    res = _run(tmp_path, main)
    wt_ws = (Path(res["worktree"]) / ".flow" / "workspace.toml").read_text(encoding="utf-8")
    assert f'root = "{main.resolve() / ".flow"}"' in wt_ws


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
    # still sets memory_root and seeds state without clobbering it.
    main = _main_checkout(tmp_path)
    res = _run(tmp_path, main, runner=_fake_runner(worktree_has_flow=True))
    wt_ws = (Path(res["worktree"]) / ".flow" / "workspace.toml").read_text(encoding="utf-8")
    assert "root =" in wt_ws


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
