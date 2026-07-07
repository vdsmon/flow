"""Tests for flow_worktree.py, the post-approval worktree bootstrap.

git/mise are injected via a fake runner; the worktree dir is materialized by the
fake `git worktree add` (simulating a checkout where .flow is gitignored, so the
bootstrap must copy config in).
"""

from __future__ import annotations

import fcntl
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path

import pytest

import flow_worktree as fw
import lease
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
    worktree_list: str | None = None,
    porcelain: str | None = None,
) -> fw.Runner:
    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append(args)
        if args[:4] == ["git", "worktree", "list", "--porcelain"]:
            return subprocess.CompletedProcess(args, 0, worktree_list or "", "")
        if args[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(args, 0, porcelain or "", "")
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
        branch="feat/FT-1-thing",
        main_root=main,
        worktree_override=str(wt),
        runner=kw.pop("runner", _fake_runner()),
        **kw,
    )


# ─── bootstrap ────────────────────────────────────────────────────────────────


def test_is_ticket_branch_accepts_both_prefixes() -> None:
    assert fw._is_ticket_branch("feat/FT-1", "FT-1")
    assert fw._is_ticket_branch("feat/FT-1-some-slug", "FT-1")
    assert fw._is_ticket_branch("feature/FT-1-some-slug", "FT-1")  # legacy
    assert not fw._is_ticket_branch("feat/FT-10-other", "FT-1")  # no prefix-bleed


def test_seeds_plan_completed_with_output_path(tmp_path: Path) -> None:
    main = _main_checkout(tmp_path)
    res = _run(tmp_path, main)
    td = Path(res["worktree"]) / ".flow" / "runs" / "FT-1"
    ts, code = state.read(td)
    assert code == 0
    assert ts is not None
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
    assert ".env" in res["copied"]
    assert ".claude" in res["copied"]


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


def test_memory_redirect_honors_main_memory_root(tmp_path: Path) -> None:
    # main initialized with [memory].root (a shared store): the sibling must
    # point at THAT store, not literally at main/.flow, or every run worktree
    # writes to a store no main-checkout read ever consults.
    main = _main_checkout(tmp_path)
    shared = tmp_path / "shared-flow"
    ws = main / ".flow" / "workspace.toml"
    ws.write_text(ws.read_text(encoding="utf-8") + f'root = "{shared}"\n', encoding="utf-8")
    res = _run(tmp_path, main)
    sibling = (Path(res["worktree"]) / ".flow" / "memory-root").read_text(encoding="utf-8")
    assert sibling.strip() == str(shared)


def test_prepopulates_commit_frontmatter(tmp_path: Path) -> None:
    main = _main_checkout(tmp_path)
    res = _run(tmp_path, main, commit_type="feat", commit_summary="add the thing")
    fm = (Path(res["worktree"]) / ".flow" / "tickets" / "FT-1.md").read_text(encoding="utf-8")
    assert "commit_type" in fm
    assert "feat" in fm
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


# ─── L2: detect-and-recover edits spilled onto main before bootstrap ───────────


def test_no_recovery_without_flag_even_with_dirty_planned_file(tmp_path: Path) -> None:
    # The CC-first guarantee: WITHOUT --recover-spill (the CC path never passes it),
    # a dirty planned file on main (the user's own pre-existing WIP) is NOT touched.
    main = _main_checkout(tmp_path)
    (main / "src").mkdir()
    (main / "src" / "a.py").write_text("user wip\n", encoding="utf-8")
    calls: list = []
    res = _run(
        tmp_path,
        main,
        planned_files=["src/a.py"],
        runner=_fake_runner(calls=calls, main=main, porcelain="?? src/a.py\n"),
    )
    assert (main / "src" / "a.py").read_text() == "user wip\n"  # untouched
    assert not any("carried uncommitted edits" in w for w in res["warnings"])
    assert not any(c[:3] == ["git", "status", "--porcelain"] for c in calls)  # not even probed


def test_clean_main_does_not_relocate(tmp_path: Path) -> None:
    # Recovery on, but plan-mode-clean main → porcelain empty → no carry, no checkout.
    main = _main_checkout(tmp_path)
    calls: list = []
    res = _run(
        tmp_path,
        main,
        planned_files=["src/a.py"],
        recover_spill=True,
        runner=_fake_runner(calls=calls, main=main, porcelain=""),
    )
    assert not any("carried uncommitted edits" in w for w in res["warnings"])
    assert not any(c[:3] == ["git", "checkout", "--"] for c in calls)


def test_unrelated_main_wip_is_not_relocated(tmp_path: Path) -> None:
    # Recovery on, but main's uncommitted work does NOT overlap planned_files → no-op.
    main = _main_checkout(tmp_path)
    (main / "unrelated.py").write_text("wip\n", encoding="utf-8")
    res = _run(
        tmp_path,
        main,
        planned_files=["src/a.py"],
        recover_spill=True,
        runner=_fake_runner(main=main, porcelain="?? unrelated.py\n"),
    )
    assert not any("carried uncommitted edits" in w for w in res["warnings"])
    assert (main / "unrelated.py").read_text() == "wip\n"


def test_spilled_untracked_planned_file_relocated(tmp_path: Path) -> None:
    # A soft-gate harness created a NEW planned file on main before bootstrap.
    main = _main_checkout(tmp_path)
    (main / "src").mkdir()
    (main / "src" / "a.py").write_text("agent work\n", encoding="utf-8")
    res = _run(
        tmp_path,
        main,
        planned_files=["src/a.py"],
        recover_spill=True,
        runner=_fake_runner(main=main, porcelain="?? src/a.py\n"),
    )
    wt = Path(res["worktree"])
    assert (wt / "src" / "a.py").read_text() == "agent work\n"  # carried in
    assert not (main / "src" / "a.py").exists()  # untracked file removed from main
    assert any("carried uncommitted edits" in w for w in res["warnings"])


def test_spilled_tracked_planned_file_relocated_and_main_checked_out(
    tmp_path: Path,
) -> None:
    # A modified (tracked) planned file: carried into the worktree, main restored
    # via `git checkout` rather than rm (the fake records the call).
    main = _main_checkout(tmp_path)
    (main / "src").mkdir()
    (main / "src" / "b.py").write_text("modified\n", encoding="utf-8")
    calls: list = []
    res = _run(
        tmp_path,
        main,
        planned_files=["src/b.py"],
        recover_spill=True,
        runner=_fake_runner(calls=calls, main=main, porcelain=" M src/b.py\n"),
    )
    wt = Path(res["worktree"])
    assert (wt / "src" / "b.py").read_text() == "modified\n"
    assert ["git", "checkout", "--", "src/b.py"] in calls


def test_relocate_spilled_real_git_leaves_work_in_worktree_main_reverted(
    tmp_path: Path,
) -> None:
    # The destructive path (real `git checkout` + real unlink) against a real repo:
    # the one load-bearing safety claim is "work ends in the worktree, never in
    # neither place"; fake-git can't prove it, so exercise real git + fs here.
    main = tmp_path / "main"
    main.mkdir()

    def git(*a: str) -> None:
        subprocess.run(["git", *a], cwd=main, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (main / "tracked.py").write_text("committed\n", encoding="utf-8")
    git("add", "tracked.py")
    git("commit", "-qm", "init")
    # the spill: tracked file modified + a new untracked file, both planned
    (main / "tracked.py").write_text("agent-modified\n", encoding="utf-8")
    (main / "new.py").write_text("agent-new\n", encoding="utf-8")

    worktree = tmp_path / "wt"
    worktree.mkdir()
    warnings: list[str] = []
    fw._relocate_spilled(
        [("tracked.py", False), ("new.py", True)],
        main,
        worktree,
        fw._default_runner(),
        warnings,
    )

    # work landed in the worktree
    assert (worktree / "tracked.py").read_text() == "agent-modified\n"
    assert (worktree / "new.py").read_text() == "agent-new\n"
    # main reverted: tracked back to HEAD, untracked removed
    assert (main / "tracked.py").read_text() == "committed\n"
    assert not (main / "new.py").exists()


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
    assert ts is not None
    assert ts.stages["plan"].status == "completed"


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
    monkeypatch.setattr(fw, "_default_runner", _fake_runner)
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
            "feat/FT-1-x",
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
    with pytest.raises(fw._ConfigError, match="e2e-recipe"):
        _run(tmp_path, main)
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


# ─── terminal-bead refusal gate (flow-d6gq) ───────────────────────────────────


class _FakeTracker:
    """Stand-in for a Tracker adapter; controls what `state()`/`get()` returns/raises."""

    def __init__(
        self, *, normalized=None, raises=False, empty=False, issue_type="task", get_raises=False
    ):
        self._normalized = normalized
        self._raises = raises
        self._empty = empty
        self._issue_type = issue_type
        self._get_raises = get_raises

    def state(self, key):
        if self._raises:
            raise RuntimeError("tracker read boom")
        if self._empty:
            return {}
        return {"normalized": self._normalized}

    def get(self, key):
        if self._get_raises:
            raise RuntimeError("tracker get boom")
        return {"type": self._issue_type}


def _patch_tracker(monkeypatch, fake) -> None:
    # _refuse_terminal_bead does `from tracker import make_tracker` at call time,
    # so patching the source module binds the fake.
    import tracker

    monkeypatch.setattr(tracker, "make_tracker", lambda config: fake)


def test_bootstrap_refuses_terminal_bead(tmp_path: Path, monkeypatch) -> None:
    main = _main_checkout(tmp_path)
    _patch_tracker(monkeypatch, _FakeTracker(normalized="done"))
    with pytest.raises(fw._TerminalBead):
        _run(tmp_path, main, runner=_fake_runner(main=main))
    # refusal fires before `git worktree add`: no worktree dir left behind
    assert not (tmp_path / "wt").exists()


def test_bootstrap_refuses_cancelled_bead(tmp_path: Path, monkeypatch) -> None:
    main = _main_checkout(tmp_path)
    _patch_tracker(monkeypatch, _FakeTracker(normalized="cancelled"))
    with pytest.raises(fw._TerminalBead):
        _run(tmp_path, main, runner=_fake_runner(main=main))


def test_bootstrap_proceeds_on_open_bead(tmp_path: Path, monkeypatch) -> None:
    main = _main_checkout(tmp_path)
    _patch_tracker(monkeypatch, _FakeTracker(normalized="open"))
    res = _run(tmp_path, main, runner=_fake_runner(main=main))
    assert res["ticket"] == "FT-1"


def test_bootstrap_fails_open_on_read_exception(tmp_path: Path, monkeypatch) -> None:
    # a genuine read failure must NOT strand a legitimate run (fail-open)
    main = _main_checkout(tmp_path)
    _patch_tracker(monkeypatch, _FakeTracker(raises=True))
    res = _run(tmp_path, main, runner=_fake_runner(main=main))
    assert res["ticket"] == "FT-1"


def test_bootstrap_refuses_on_empty_status_read(tmp_path: Path, monkeypatch) -> None:
    # a successful-but-incoherent read is NOT fail-open: refuse rather than proceed
    # on an unconfirmed status (fail-open stays exception-only)
    main = _main_checkout(tmp_path)
    _patch_tracker(monkeypatch, _FakeTracker(empty=True))
    with pytest.raises(fw._TerminalBead):
        _run(tmp_path, main, runner=_fake_runner(main=main))


def test_cli_terminal_bead_exits_6(tmp_path: Path, monkeypatch) -> None:
    main = _main_checkout(tmp_path)
    monkeypatch.setattr(fw, "_default_runner", lambda: _fake_runner(main=main))
    _patch_tracker(monkeypatch, _FakeTracker(normalized="done"))
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
            "feat/FT-1-x",
            "--main-root",
            str(main),
            "--worktree-path",
            str(tmp_path / "wt"),
        ]
    )
    assert rc == 6


# ─── epic refusal gate (flow-jvxj) ────────────────────────────────────────────


def test_bootstrap_refuses_epic_bead(tmp_path: Path, monkeypatch) -> None:
    main = _main_checkout(tmp_path)
    _patch_tracker(monkeypatch, _FakeTracker(normalized="open", issue_type="epic"))
    with pytest.raises(fw._EpicBead):
        _run(tmp_path, main, runner=_fake_runner(main=main))
    # refusal fires before `git worktree add`: no worktree dir left behind
    assert not (tmp_path / "wt").exists()


def test_bootstrap_refuses_epic_case_insensitive(tmp_path: Path, monkeypatch) -> None:
    # Jira's issue-type name is "Epic"; the refusal is type-name case-insensitive.
    main = _main_checkout(tmp_path)
    _patch_tracker(monkeypatch, _FakeTracker(normalized="open", issue_type="Epic"))
    with pytest.raises(fw._EpicBead):
        _run(tmp_path, main, runner=_fake_runner(main=main))


def test_bootstrap_proceeds_on_task_bead(tmp_path: Path, monkeypatch) -> None:
    main = _main_checkout(tmp_path)
    _patch_tracker(monkeypatch, _FakeTracker(normalized="open", issue_type="task"))
    res = _run(tmp_path, main, runner=_fake_runner(main=main))
    assert res["ticket"] == "FT-1"


def test_bootstrap_epic_check_fails_open_on_get_exception(tmp_path: Path, monkeypatch) -> None:
    # a genuine type-read failure must NOT strand a legitimate run (fail-open)
    main = _main_checkout(tmp_path)
    _patch_tracker(monkeypatch, _FakeTracker(normalized="open", get_raises=True))
    res = _run(tmp_path, main, runner=_fake_runner(main=main))
    assert res["ticket"] == "FT-1"


def test_cli_epic_bead_exits_7(tmp_path: Path, monkeypatch) -> None:
    main = _main_checkout(tmp_path)
    monkeypatch.setattr(fw, "_default_runner", lambda: _fake_runner(main=main))
    _patch_tracker(monkeypatch, _FakeTracker(normalized="open", issue_type="epic"))
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
            "feat/FT-1-x",
            "--main-root",
            str(main),
            "--worktree-path",
            str(tmp_path / "wt"),
        ]
    )
    assert rc == 7


# ─── covers (run-level ticket grouping) ───────────────────────────────────────


class _PerKeyTracker:
    """Tracker stand-in returning per-key issue types; every key reads open."""

    def __init__(self, types: dict[str, str]):
        self._types = types

    def state(self, key):
        return {"normalized": "open"}

    def get(self, key):
        return {"type": self._types.get(key, "task")}


def test_covers_stamped_as_list(tmp_path: Path, monkeypatch) -> None:
    import ticket_frontmatter

    main = _main_checkout(tmp_path)
    _patch_tracker(monkeypatch, _PerKeyTracker({"FT-1": "task", "FT-2": "task", "FT-3": "task"}))
    res = _run(tmp_path, main, covers=["FT-2", "FT-3"], runner=_fake_runner(main=main))
    fm = ticket_frontmatter.read(Path(res["worktree"]) / ".flow" / "tickets" / "FT-1.md")
    assert fm["covers"] == ["FT-2", "FT-3"]


def test_covers_self_reference_refused(tmp_path: Path, monkeypatch) -> None:
    main = _main_checkout(tmp_path)
    _patch_tracker(monkeypatch, _PerKeyTracker({"FT-1": "task"}))
    with pytest.raises(fw._ConfigError, match="lead ticket itself"):
        _run(tmp_path, main, covers=["FT-1"], runner=_fake_runner(main=main))


def test_covers_epic_refused(tmp_path: Path, monkeypatch) -> None:
    # lead is a task, a cover is an epic -> refuse (covers get the lead's floors)
    main = _main_checkout(tmp_path)
    _patch_tracker(monkeypatch, _PerKeyTracker({"FT-1": "task", "FT-9": "epic"}))
    with pytest.raises(fw._EpicBead):
        _run(tmp_path, main, covers=["FT-9"], runner=_fake_runner(main=main))


def test_no_covers_omits_frontmatter_key(tmp_path: Path, monkeypatch) -> None:
    import ticket_frontmatter

    main = _main_checkout(tmp_path)
    _patch_tracker(monkeypatch, _PerKeyTracker({"FT-1": "task"}))
    res = _run(tmp_path, main, runner=_fake_runner(main=main))
    fm = ticket_frontmatter.read(Path(res["worktree"]) / ".flow" / "tickets" / "FT-1.md")
    assert "covers" not in fm


# ─── verification lane stamping (--lane override + hot clamp, flow-cjgy) ───────


class _LabelTracker:
    """Tracker stand-in returning a fixed label set; every key reads open/task."""

    def __init__(self, labels: list[str]):
        self._labels = labels

    def state(self, key):
        return {"normalized": "open"}

    def get(self, key):
        return {"type": "task", "labels": list(self._labels)}


def _lane_fm(tmp_path: Path, main: Path, **kw):
    import ticket_frontmatter

    res = _run(tmp_path, main, runner=_fake_runner(main=main), **kw)
    return ticket_frontmatter.read(Path(res["worktree"]) / ".flow" / "tickets" / "FT-1.md")


def test_lane_explicit_express_stamps_express(tmp_path: Path, monkeypatch) -> None:
    main = _main_checkout(tmp_path)
    _patch_tracker(monkeypatch, _LabelTracker([]))
    fm = _lane_fm(tmp_path, main, lane="express", planned_files=["src/a.py"])
    assert fm["lane"] == "express"


def test_lane_explicit_full_omits_frontmatter(tmp_path: Path, monkeypatch) -> None:
    # full is the absent-field default; a normal run's frontmatter stays clean.
    main = _main_checkout(tmp_path)
    _patch_tracker(monkeypatch, _LabelTracker([]))
    fm = _lane_fm(tmp_path, main, lane="full", planned_files=["src/a.py"])
    assert "lane" not in fm


def test_lane_explicit_express_clamped_to_full_by_guard_file(tmp_path: Path, monkeypatch) -> None:
    # an explicit --lane express on a hot (guard-file) change clamps to full -> unstamped.
    main = _main_checkout(tmp_path)
    _patch_tracker(monkeypatch, _LabelTracker([]))
    fm = _lane_fm(tmp_path, main, lane="express", planned_files=["flow_worktree.py"])
    assert "lane" not in fm


def test_lane_explicit_wins_over_tier_label(tmp_path: Path, monkeypatch) -> None:
    # explicit --lane takes precedence over the bead's tier label.
    main = _main_checkout(tmp_path)
    _patch_tracker(monkeypatch, _LabelTracker(["tier:light"]))
    fm = _lane_fm(tmp_path, main, lane="express", planned_files=["src/a.py"])
    assert fm["lane"] == "express"


def test_lane_label_derived_when_no_explicit(tmp_path: Path, monkeypatch) -> None:
    main = _main_checkout(tmp_path)
    _patch_tracker(monkeypatch, _LabelTracker(["tier:light"]))
    fm = _lane_fm(tmp_path, main, planned_files=["src/a.py"])
    assert fm["lane"] == "light"


def test_lane_label_derived_clamped_by_guard_file(tmp_path: Path, monkeypatch) -> None:
    # a tier:trivial bead touching a guard file is NOT express -> clamped to full.
    main = _main_checkout(tmp_path)
    _patch_tracker(monkeypatch, _LabelTracker(["tier:trivial"]))
    fm = _lane_fm(tmp_path, main, planned_files=["flow_worktree.py"])
    assert "lane" not in fm


def test_lane_hot_label_clamps_explicit_express(tmp_path: Path, monkeypatch) -> None:
    # a hot-labelled bead clamps an explicit --lane express to full even with no guard file.
    main = _main_checkout(tmp_path)
    _patch_tracker(monkeypatch, _LabelTracker(["hot"]))
    fm = _lane_fm(tmp_path, main, lane="express", planned_files=["src/a.py"])
    assert "lane" not in fm


def test_lane_explicit_ignored_under_auto(tmp_path: Path, monkeypatch) -> None:
    # --lane is interactive-only: an --auto run derives from tier labels (none
    # here -> full -> unstamped), never from a stray --lane express.
    main = _main_checkout(tmp_path)
    _patch_tracker(monkeypatch, _LabelTracker([]))
    fm = _lane_fm(tmp_path, main, lane="express", planned_files=["src/a.py"], auto=True)
    assert "lane" not in fm


def test_lane_auto_derives_from_tier_label(tmp_path: Path, monkeypatch) -> None:
    # under --auto the bead's tier label wins over a passed --lane.
    main = _main_checkout(tmp_path)
    _patch_tracker(monkeypatch, _LabelTracker(["tier:trivial"]))
    fm = _lane_fm(tmp_path, main, lane="light", planned_files=["src/a.py"], auto=True)
    assert fm["lane"] == "express"


# ─── planned_files gitignore gate ─────────────────────────────────────────────


def test_bootstrap_rejects_gitignored_planned_file(tmp_path: Path) -> None:
    # A gitignored planned file (no .gitignore in the plan) would be silently
    # dropped from the commit: refuse at the gate. The ignore check runs INSIDE the
    # worktree (base may carry .gitignore negations main lacks), so the worktree is
    # created first, then removed on rejection. Refusing leaves no orphan.
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
    # the -b-created branch is also deleted, so a retry does not hit
    # "fatal: a branch named <branch> already exists"
    assert any(c == ["git", "branch", "-D", "feat/FT-1-thing"] for c in calls)


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
    calls: list = []
    res = _run(
        tmp_path,
        main,
        planned_files=["a.py"],
        runner=_fake_runner(ignored=set(), calls=calls, main=main),
    )
    assert res["ticket"] == "FT-1"
    assert not any("gitignored" in w for w in res["warnings"])
    # happy path never deletes the branch
    assert not any(c[:3] == ["git", "branch", "-D"] for c in calls)


def test_bootstrap_cleans_up_on_midbody_git_error(tmp_path: Path) -> None:
    # A non-deliberate exception AFTER `git worktree add` (here a _GitError from a
    # failing rev-parse) must still remove the worktree + delete the -b branch, so a
    # crash mid-bootstrap leaves no orphan (flow-fh05, broadening flow-n2a6).
    main = _main_checkout(tmp_path)
    calls: list = []
    base = _fake_runner(calls=calls, main=main)

    def run(args: list[str], cwd: Path):
        if args[:2] == ["git", "rev-parse"]:
            calls.append(args)
            return subprocess.CompletedProcess(args, 1, "", "fatal: bad object")
        return base(args, cwd)

    with pytest.raises(fw._GitError):
        _run(tmp_path, main, runner=run)
    assert any(c[:3] == ["git", "worktree", "add"] for c in calls)
    assert any(c[:4] == ["git", "worktree", "remove", "--force"] for c in calls)
    assert any(c == ["git", "branch", "-D", "feat/FT-1-thing"] for c in calls)


def test_bootstrap_cleans_up_on_midbody_raise(tmp_path: Path) -> None:
    # A raw exception from a body op (here `mise trust` raising, mirroring the
    # ticket's _seed_state / mise-raising examples) also triggers worktree+branch
    # cleanup before propagating, not only the deliberate gitignored refusal.
    main = _main_checkout(tmp_path, with_mise=True)
    calls: list = []
    base = _fake_runner(calls=calls, main=main)

    def run(args: list[str], cwd: Path):
        if args[:2] == ["mise", "trust"]:
            calls.append(args)
            raise RuntimeError("boom")
        return base(args, cwd)

    with pytest.raises(RuntimeError):
        _run(tmp_path, main, runner=run)
    assert any(c[:4] == ["git", "worktree", "remove", "--force"] for c in calls)
    assert any(c == ["git", "branch", "-D", "feat/FT-1-thing"] for c in calls)


def test_bootstrap_warns_on_planned_file_in_missing_dir(tmp_path: Path) -> None:
    # A planned path whose PARENT dir is also absent is a likely path typo
    # (the flow-kx17.1 case): warn, never refuse.
    main = _main_checkout(tmp_path)
    res = _run(
        tmp_path,
        main,
        planned_files=["nonexistent_dir/typo.py"],
        runner=_fake_runner(ignored=set(), main=main),
    )
    assert res["ticket"] == "FT-1"
    assert any("nonexistent_dir/typo.py" in w and "non-existent" in w for w in res["warnings"])


def test_bootstrap_no_typo_warn_for_new_file_in_existing_dir(tmp_path: Path) -> None:
    # A NEW (non-existent) file in an EXISTING dir is normal (TDD writes test
    # files that do not exist yet): no typo warning. The dir must really exist
    # under the resolved worktree, since _typo_planned uses Path.exists() and the
    # fake `git worktree add` does not populate the tree.
    main = _main_checkout(tmp_path)
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True, exist_ok=True)
    res = _run(
        tmp_path,
        main,
        worktree=wt,
        planned_files=["src/new_thing.py"],
        runner=_fake_runner(ignored=set(), main=main),
    )
    assert res["ticket"] == "FT-1"
    assert not any("non-existent" in w for w in res["warnings"])


def test_bootstrap_no_typo_warn_for_existing_file(tmp_path: Path) -> None:
    # An already-existing planned file never trips the typo guard. The file must
    # really exist under the resolved worktree (fake worktree add does not
    # populate the tree).
    main = _main_checkout(tmp_path)
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True, exist_ok=True)
    (wt / "src" / "existing.py").write_text("x = 1\n", encoding="utf-8")
    res = _run(
        tmp_path,
        main,
        worktree=wt,
        planned_files=["src/existing.py"],
        runner=_fake_runner(ignored=set(), main=main),
    )
    assert res["ticket"] == "FT-1"
    assert not any("non-existent" in w for w in res["warnings"])


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


def test_resolve_base_feature_branch_stacks_but_fetches(tmp_path):
    # a feature branch keeps stacking, but still fetches (always pull upstream).
    run, calls = _base_runner([(0, "origin/main\n")])
    assert fw._resolve_base("feat/x", tmp_path, run) == "feat/x"
    assert ["git", "fetch", "--quiet", "origin"] in calls


def test_resolve_base_local_default_redirects_to_origin_head(tmp_path):
    # launching from the local default branch redirects to the fresh remote tip.
    run, _ = _base_runner([(0, "origin/main\n")])
    assert fw._resolve_base("main", tmp_path, run) == "origin/main"


def test_resolve_base_detached_redirects_to_origin_head(tmp_path):
    run, _ = _base_runner([(0, "origin/main\n")])
    assert fw._resolve_base("HEAD", tmp_path, run) == "origin/main"


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


def _fetch_failing_runner():
    # fetch returns non-zero (unreachable/missing origin); symbolic-ref + set-head
    # also fail (no origin/HEAD), so the remote default never resolves.
    def run(args, cwd):
        return subprocess.CompletedProcess(args, 1, "", "fatal: no origin")

    return run


def test_resolve_base_default_hard_fails_on_fetch_error(tmp_path):
    # the autonomous @default contract is guaranteed-fresh: a fetch failure aborts.
    with pytest.raises(fw._GitError):
        fw._resolve_base("@default", tmp_path, _fetch_failing_runner())


def test_resolve_base_interactive_degrades_to_local_on_fetch_error(tmp_path):
    # an offline/origin-less interactive run still bootstraps off its local base.
    assert fw._resolve_base("feat/x", tmp_path, _fetch_failing_runner()) == "feat/x"


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
            ("/main/.flow/worktrees/feat-FT-1-thing", "feat/FT-1-thing"),
            ("/main/.flow/worktrees/detached", None),
        ]
    )
    pairs = fw._parse_worktree_list(blob)
    assert pairs == [
        ("/main", "main"),
        ("/main/.flow/worktrees/feat-FT-1-thing", "feat/FT-1-thing"),
        ("/main/.flow/worktrees/detached", None),
    ]


def test_worktree_path_derives_under_dot_flow_pool(tmp_path: Path) -> None:
    main = tmp_path / "repo"
    main.mkdir()
    assert fw._worktree_path(main, "feat/FT-1-x", None) == (
        main.resolve() / ".flow" / "worktrees" / "feat-FT-1-x"
    )


def test_worktree_path_override_wins(tmp_path: Path) -> None:
    main = tmp_path / "repo"
    override = tmp_path / "elsewhere" / "wt"
    assert fw._worktree_path(main, "feat/FT-1-x", str(override)) == override.resolve()


def test_copy_config_skips_nested_worktree_pool(tmp_path: Path) -> None:
    # the HARNESS pool lives at main/.claude/worktrees (claude --worktree); flow's
    # own pool is at .flow/worktrees. _copy_config copies .claude into each new
    # worktree and must NOT pull the harness peer worktrees back in (the 10G+
    # recursion). This pins the ignore_patterns("worktrees") invariant.
    main = tmp_path / "repo"
    claude = main / ".claude"
    (claude / "skills").mkdir(parents=True)
    (claude / "settings.json").write_text("{}", encoding="utf-8")
    (claude / "worktrees" / "feat-junk" / ".flow").mkdir(parents=True)
    (claude / "worktrees" / "feat-junk" / "big.bin").write_text("x", encoding="utf-8")
    worktree = main / ".flow" / "worktrees" / "feat-FT-1-x"
    worktree.mkdir(parents=True)

    copied = fw._copy_config(main, worktree, [])

    assert ".claude" in copied
    assert (worktree / ".claude" / "settings.json").exists()
    assert not (worktree / ".claude" / "worktrees").exists()


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
    wt = tmp_path / "main" / ".flow" / "worktrees" / "feat-FT-1-thing"
    wt.mkdir(parents=True)
    calls: list = []
    runner = _reap_runner(
        worktrees=_porcelain([(str(tmp_path / "main"), "main"), (str(wt), "feat/FT-1-thing")]),
        calls=calls,
    )
    receipt = fw.reap_worktree(ticket="FT-1", main_root=tmp_path / "main", runner=runner)
    assert receipt["worktree_removed"] is True
    assert receipt["branch_deleted"] is True
    assert receipt["branch"] == "feat/FT-1-thing"
    assert receipt["skipped"] is None
    assert any(c[:4] == ["git", "worktree", "remove", "--force"] for c in calls)
    assert any(c[:3] == ["git", "branch", "-D"] for c in calls)


def test_reap_locates_legacy_feature_prefix_worktree(tmp_path: Path) -> None:
    # a worktree created before the feat/ rename still resolves by ticket
    wt = tmp_path / "main" / ".flow" / "worktrees" / "feature-FT-1-thing"
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


def test_reap_skips_when_lease_live(tmp_path: Path) -> None:
    wt = tmp_path / "main" / ".flow" / "worktrees" / "feat-FT-1-thing"
    wt.mkdir(parents=True)
    _seed_live_lease(wt / ".flow" / "runs" / "FT-1")
    calls: list = []
    runner = _reap_runner(
        worktrees=_porcelain([(str(wt), "feat/FT-1-thing")]),
        calls=calls,
    )
    receipt = fw.reap_worktree(ticket="FT-1", main_root=tmp_path / "main", runner=runner)
    assert receipt["worktree_removed"] is False
    assert receipt["branch_deleted"] is False
    assert receipt["skipped"]
    assert "live" in receipt["skipped"]
    # a live session: touch NOTHING
    assert not any(c[:4] == ["git", "worktree", "remove", "--force"] for c in calls)
    assert not any(c[:3] == ["git", "branch", "-D"] for c in calls)


def test_reap_skips_when_lease_corrupt(tmp_path: Path) -> None:
    import lease

    wt = tmp_path / "main" / ".flow" / "worktrees" / "feat-FT-1-thing"
    ticket_dir = wt / ".flow" / "runs" / "FT-1"
    ticket_dir.mkdir(parents=True)
    lease.run_lock_path(ticket_dir).write_text("{not json", encoding="utf-8")
    calls: list = []
    runner = _reap_runner(
        worktrees=_porcelain([(str(wt), "feat/FT-1-thing")]),
        calls=calls,
    )
    receipt = fw.reap_worktree(ticket="FT-1", main_root=tmp_path / "main", runner=runner)
    assert receipt["worktree_removed"] is False
    assert receipt["branch_deleted"] is False
    # distinct reason from the "live" skip, so the human can tell why it was held.
    assert receipt["skipped"]
    assert "corrupt" in receipt["skipped"]
    assert receipt["skipped"] != "lease live (run still in progress)"
    # a possibly-live corrupt run: touch NOTHING
    assert not any(c[:4] == ["git", "worktree", "remove", "--force"] for c in calls)
    assert not any(c[:3] == ["git", "branch", "-D"] for c in calls)


def test_reap_removes_expired_same_host_previous_boot_lease(tmp_path: Path) -> None:
    import lease

    wt = tmp_path / "main" / ".flow" / "worktrees" / "feat-FT-1-thing"
    ticket_dir = wt / ".flow" / "runs" / "FT-1"
    ticket_dir.mkdir(parents=True)
    lease.acquire(
        ticket_dir,
        run_id="run-x",
        ttl_seconds=60,
        now_iso="2020-01-01T00:00:00Z",
        current_boot="previous-boot",
        hostname=lease.hostname(),
        cwd="/cwd-x",
    )
    calls: list = []
    runner = _reap_runner(
        worktrees=_porcelain([(str(wt), "feat/FT-1-thing")]),
        calls=calls,
    )
    receipt = fw.reap_worktree(ticket="FT-1", main_root=tmp_path / "main", runner=runner)
    assert receipt["worktree_removed"] is True
    assert receipt["skipped"] is None


def test_reap_idempotent_when_nothing_to_remove(tmp_path: Path) -> None:
    calls: list = []
    runner = _reap_runner(
        worktrees=_porcelain([(str(tmp_path / "main"), "main")]),
        calls=calls,
    )
    receipt = fw.reap_worktree(
        ticket="FT-1", main_root=tmp_path / "main", branch="feat/FT-1-thing", runner=runner
    )
    assert receipt["worktree_removed"] is False
    assert not any(c[:4] == ["git", "worktree", "remove", "--force"] for c in calls)
    # branch was supplied, so a (tolerant) delete is still attempted; here it returns 0
    assert receipt["branch"] == "feat/FT-1-thing"


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
    assert receipt["worktree_removed"] is False
    assert receipt["branch_deleted"] is False
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
        ticket="FT-1", main_root=tmp_path / "main", branch="feat/FT-1-thing", runner=runner
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
        ticket="FT-1", main_root=tmp_path / "main", branch="feat/FT-1-thing", runner=runner
    )
    assert receipt["worktree_removed"] is False
    assert receipt["branch_deleted"] is True
    assert any(c == ["git", "branch", "-D", "feat/FT-1-thing"] for c in calls)


def test_reap_remove_failure_skips_branch_delete(tmp_path: Path) -> None:
    wt = tmp_path / "main" / ".flow" / "worktrees" / "feat-FT-1-thing"
    wt.mkdir(parents=True)
    calls: list = []
    runner = _reap_runner(
        worktrees=_porcelain([(str(wt), "feat/FT-1-thing")]),
        calls=calls,
        remove_rc=1,
    )
    receipt = fw.reap_worktree(ticket="FT-1", main_root=tmp_path / "main", runner=runner)
    assert receipt["worktree_removed"] is False
    assert receipt["branch_deleted"] is False
    assert receipt["skipped"]
    assert not any(c[:3] == ["git", "branch", "-D"] for c in calls)


def test_reap_skips_remove_when_lease_goes_live_under_flock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the classify-then-mutate TOCTOU: the dir is free when reap looks, but a
    # concurrent acquire wins the flock first and writes a live lease. Because
    # classify_then classifies AND would run the worktree-remove teardown under
    # one flock span, the live lease is observed and the remove is never issued.
    # The "remove never issued" assertion is load-bearing.
    import contextlib
    from collections.abc import Iterator

    wt = tmp_path / "main" / ".flow" / "worktrees" / "feat-FT-1-thing"
    ticket_dir = wt / ".flow" / "runs" / "FT-1"
    ticket_dir.mkdir(parents=True)
    real_flock = lease.flock_blocking

    @contextlib.contextmanager
    def racing_flock(path: Path) -> Iterator[None]:
        with real_flock(path):
            lease.run_lock_path(ticket_dir).write_text(
                lease._serialize(
                    lease.Lease(
                        run_id="racer",
                        boot_id="boot-x",
                        hostname="host-x",
                        cwd="/cwd-x",
                        acquired_at="2999-01-01T00:00:00Z",
                        lease_expires_at="2999-01-01T01:00:00Z",
                    )
                ),
                encoding="utf-8",
            )
            yield

    monkeypatch.setattr(lease, "flock_blocking", racing_flock)
    calls: list = []
    runner = _reap_runner(
        worktrees=_porcelain([(str(wt), "feat/FT-1-thing")]),
        calls=calls,
    )
    receipt = fw.reap_worktree(ticket="FT-1", main_root=tmp_path / "main", runner=runner)
    assert receipt["worktree_removed"] is False
    assert receipt["branch_deleted"] is False
    assert receipt["skipped"] == "lease live (run still in progress)"
    assert not any(c[:4] == ["git", "worktree", "remove", "--force"] for c in calls)
    assert not any(c[:3] == ["git", "branch", "-D"] for c in calls)


def test_reap_refuses_mismatched_ticket_branch_pair(tmp_path: Path) -> None:
    # --branch of ticket B under --ticket A: the lease gate classifies A's
    # (absent) run dir inside B's worktree as free and would force-remove B's
    # LIVE worktree. The pair must refuse outright, touching nothing.
    wt = tmp_path / "main" / ".flow" / "worktrees" / "feat-FT-2-other"
    wt.mkdir(parents=True)
    _seed_live_lease(wt / ".flow" / "runs" / "FT-2")
    calls: list = []
    runner = _reap_runner(
        worktrees=_porcelain([(str(wt), "feat/FT-2-other")]),
        calls=calls,
    )
    receipt = fw.reap_worktree(
        ticket="FT-1", main_root=tmp_path / "main", branch="feat/FT-2-other", runner=runner
    )
    assert receipt["worktree_removed"] is False
    assert receipt["branch_deleted"] is False
    assert receipt["skipped"]
    assert "does not belong" in receipt["skipped"]
    assert not any(c[:4] == ["git", "worktree", "remove", "--force"] for c in calls)
    assert not any(c[:3] == ["git", "branch", "-D"] for c in calls)


def test_reap_mismatched_pair_never_deletes_loose_branch(tmp_path: Path) -> None:
    # even with no worktree checked out on it, a mismatched --branch is not
    # this ticket's to delete.
    calls: list = []
    runner = _reap_runner(
        worktrees=_porcelain([(str(tmp_path / "main"), "main")]),
        calls=calls,
    )
    receipt = fw.reap_worktree(
        ticket="FT-1", main_root=tmp_path / "main", branch="feat/FT-2-other", runner=runner
    )
    assert receipt["branch_deleted"] is False
    assert receipt["skipped"]
    assert "does not belong" in receipt["skipped"]
    assert not any(c[:3] == ["git", "branch", "-D"] for c in calls)


def test_reap_matching_explicit_branch_still_reaps(tmp_path: Path) -> None:
    # the pairing guard only refuses mismatches; the drain's normal
    # `reap --ticket <key> --branch feat/<key>-<slug>` call reaps as before.
    wt = tmp_path / "main" / ".flow" / "worktrees" / "feat-FT-1-thing"
    wt.mkdir(parents=True)
    calls: list = []
    runner = _reap_runner(
        worktrees=_porcelain([(str(wt), "feat/FT-1-thing")]),
        calls=calls,
    )
    receipt = fw.reap_worktree(
        ticket="FT-1", main_root=tmp_path / "main", branch="feat/FT-1-thing", runner=runner
    )
    assert receipt["worktree_removed"] is True
    assert receipt["branch_deleted"] is True
    assert receipt["skipped"] is None


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
            "feat/FT-1-thing",
            "--main-root",
            str(tmp_path / "main"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert '"ticket": "FT-1"' in out
    assert '"branch": "feat/FT-1-thing"' in out


# ─── checkpoint-then-reap (flow-vpg1) ───────────────────────────────────────
# reap now checkpoints uncommitted work as a WIP commit pushed to a
# `flow-rescue/<ticket>-<sha>` ref BEFORE the destructive teardown; a failed
# checkpoint leaves the worktree intact rather than destroy the work.


def _bare_origin_repo(
    tmp: Path, *, ticket: str = "FT-1", branch: str = "feat/FT-1-thing"
) -> tuple[Path, Path]:
    """A main checkout with a real `origin` remote (a bare repo) + a real
    registered worktree checked out on `branch`, for reap-checkpoint tests that
    need to prove an actual `git push` landed on a real remote (a fake runner
    can't prove that)."""
    origin = tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)

    main = tmp / "main"
    main.mkdir()
    git = ["git", "-C", str(main)]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    subprocess.run([*git, "config", "user.email", "t@t"], check=True)
    subprocess.run([*git, "config", "user.name", "t"], check=True)
    subprocess.run([*git, "remote", "add", "origin", str(origin)], check=True)
    (main / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run([*git, "add", "README.md"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    subprocess.run([*git, "push", "-q", "origin", "main"], check=True)

    wt = main / ".flow" / "worktrees" / branch.replace("/", "-")
    subprocess.run([*git, "worktree", "add", "-q", "-b", branch, str(wt), "main"], check=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.name", "t"], check=True)
    return main, wt


def test_reap_checkpoints_dirty_work_to_pushed_rescue_ref(tmp_path: Path) -> None:
    main, wt = _bare_origin_repo(tmp_path)
    (wt / "scratch.py").write_text("wip work\n", encoding="utf-8")

    receipt = fw.reap_worktree(ticket="FT-1", main_root=main, runner=fw._default_runner())

    assert receipt["worktree_removed"] is True
    assert receipt["branch_deleted"] is True
    checkpoint = receipt["checkpoint"]
    assert checkpoint["rescue_branch"].startswith("flow-rescue/FT-1-")

    origin = tmp_path / "origin.git"
    ls_remote = subprocess.run(
        ["git", "ls-remote", str(origin)], capture_output=True, text=True, check=True
    ).stdout
    assert f"refs/heads/{checkpoint['rescue_branch']}" in ls_remote

    show = subprocess.run(
        ["git", "show", f"refs/heads/{checkpoint['rescue_branch']}:scratch.py"],
        cwd=str(origin),
        capture_output=True,
        text=True,
        check=False,
    )
    assert show.returncode == 0
    assert show.stdout == "wip work\n"

    subject = subprocess.run(
        ["git", "log", "-1", "--format=%s", f"refs/heads/{checkpoint['rescue_branch']}"],
        cwd=str(origin),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert subject == "wip: flow checkpoint before reap (FT-1) [skip ci]"


def test_reap_checkpoint_excludes_secrets_and_flow_dir(tmp_path: Path) -> None:
    # the secret-leak guard: `.env` (a _DEFAULT_COPY path) and `.flow/` (only its
    # `runs/` subtree is actually gitignored in this repo; `tickets/<key>.md` is
    # not) must never ride into the PUBLIC flow-rescue ref.
    main, wt = _bare_origin_repo(tmp_path)
    (wt / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (wt / "real.py").write_text("wip\n", encoding="utf-8")
    (wt / ".flow" / "tickets").mkdir(parents=True)
    (wt / ".flow" / "tickets" / "FT-1.md").write_text(
        "+++\nticket = 'FT-1'\n+++\n", encoding="utf-8"
    )

    receipt = fw.reap_worktree(ticket="FT-1", main_root=main, runner=fw._default_runner())
    checkpoint = receipt["checkpoint"]
    assert checkpoint["rescue_branch"].startswith("flow-rescue/FT-1-")

    origin = tmp_path / "origin.git"
    tracked = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", f"refs/heads/{checkpoint['rescue_branch']}"],
        cwd=str(origin),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert "real.py" in tracked
    assert ".env" not in tracked
    assert not any(t.startswith(".flow/") for t in tracked)


def test_reap_recovers_orphaned_checkpoint_before_push(tmp_path: Path) -> None:
    # crash-window shape (flow-81xn): a prior reap's checkpoint commit landed
    # but its rescue push never did. The tree reads clean; recovery must
    # re-push the SAME commit rather than let the caller destroy it.
    main, wt = _bare_origin_repo(tmp_path)
    (wt / "scratch.py").write_text("wip work\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(wt), "commit", "-q", "--no-verify", "-m", fw._checkpoint_marker("FT-1")],
        check=True,
    )

    receipt = fw.reap_worktree(ticket="FT-1", main_root=main, runner=fw._default_runner())

    assert receipt["worktree_removed"] is True
    assert receipt["branch_deleted"] is True
    checkpoint = receipt["checkpoint"]
    assert checkpoint["rescue_branch"].startswith("flow-rescue/FT-1-")

    origin = tmp_path / "origin.git"
    ls_remote = subprocess.run(
        ["git", "ls-remote", str(origin)], capture_output=True, text=True, check=True
    ).stdout
    assert f"refs/heads/{checkpoint['rescue_branch']}" in ls_remote

    show = subprocess.run(
        ["git", "show", f"refs/heads/{checkpoint['rescue_branch']}:scratch.py"],
        cwd=str(origin),
        capture_output=True,
        text=True,
        check=False,
    )
    assert show.returncode == 0
    assert show.stdout == "wip work\n"


def test_reap_merged_orphan_with_unpushed_head_stays_clean(tmp_path: Path) -> None:
    # primary risk (flow-81xn): a squash-merge rewrites shas, so an already
    # landed feature commit's HEAD always looks unpushed too. Only the exact
    # marker subject may trigger recovery; a real feature subject must reap
    # clean, unchanged, with no flow-rescue ref pushed.
    main, wt = _bare_origin_repo(tmp_path)
    (wt / "real.py").write_text("feature work\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(wt), "commit", "-q", "--no-verify", "-m", "feat: real feature commit"],
        check=True,
    )

    receipt = fw.reap_worktree(ticket="FT-1", main_root=main, runner=fw._default_runner())

    assert receipt["worktree_removed"] is True
    assert receipt["branch_deleted"] is True
    assert "checkpoint" not in receipt

    origin = tmp_path / "origin.git"
    ls_remote = subprocess.run(
        ["git", "ls-remote", str(origin)], capture_output=True, text=True, check=True
    ).stdout
    assert "flow-rescue" not in ls_remote


def _checkpoint_runner(
    *,
    worktrees: str,
    calls: list,
    dirty: bool = False,
    push_rc: int = 0,
    commit_rc: int = 0,
    rev: str = "abc1234",
    head_subject: str = "",
    ls_remote_out: str = "",
):
    """A runner answering reap's full sequence, incl. the checkpoint's own
    status/add/commit/rev-parse/push, with configurable failure points.

    `head_subject` and `ls_remote_out` drive the clean-tree recovery probe
    (`git log -1 --format=%s HEAD` / `git ls-remote origin <ref>`); the default
    `head_subject=""` never matches a marker, so existing non-recovery tests
    are unaffected.
    """

    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:4] == ["git", "worktree", "list", "--porcelain"]:
            return subprocess.CompletedProcess(args, 0, worktrees, "")
        if args[:3] == ["git", "status", "--porcelain"]:
            out = "?? scratch.py\n" if dirty else ""
            return subprocess.CompletedProcess(args, 0, out, "")
        if args[:4] == ["git", "log", "-1", "--format=%s"]:
            return subprocess.CompletedProcess(args, 0, head_subject + "\n", "")
        if args[:2] == ["git", "ls-remote"]:
            return subprocess.CompletedProcess(args, 0, ls_remote_out, "")
        if args[:2] == ["git", "commit"]:
            return subprocess.CompletedProcess(
                args, commit_rc, "", "" if commit_rc == 0 else "commit failed"
            )
        if args[:3] == ["git", "rev-parse", "--short"]:
            return subprocess.CompletedProcess(args, 0, rev + "\n", "")
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(
                args, push_rc, "", "" if push_rc == 0 else "push failed"
            )
        if args[:4] == ["git", "worktree", "remove", "--force"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["git", "branch", "-D"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    return run


def test_reap_clean_worktree_skips_checkpoint_commit(tmp_path: Path) -> None:
    # only bootstrap-scratch uncommitted (nothing real to capture): the
    # merged-orphan no-op path must survive unchanged, no commit/push issued.
    wt = tmp_path / "main" / ".flow" / "worktrees" / "feat-FT-1-thing"
    wt.mkdir(parents=True)
    calls: list = []
    runner = _checkpoint_runner(
        worktrees=_porcelain([(str(wt), "feat/FT-1-thing")]), calls=calls, dirty=False
    )
    receipt = fw.reap_worktree(ticket="FT-1", main_root=tmp_path / "main", runner=runner)
    assert receipt["worktree_removed"] is True
    assert receipt["branch_deleted"] is True
    assert "checkpoint" not in receipt
    assert not any(c[:2] == ["git", "commit"] for c in calls)
    assert not any(c[:2] == ["git", "push"] for c in calls)


def test_reap_checkpoint_failure_leaves_worktree_intact(tmp_path: Path) -> None:
    wt = tmp_path / "main" / ".flow" / "worktrees" / "feat-FT-1-thing"
    wt.mkdir(parents=True)
    calls: list = []
    runner = _checkpoint_runner(
        worktrees=_porcelain([(str(wt), "feat/FT-1-thing")]),
        calls=calls,
        dirty=True,
        push_rc=1,
    )
    receipt = fw.reap_worktree(ticket="FT-1", main_root=tmp_path / "main", runner=runner)
    assert receipt["worktree_removed"] is False
    assert receipt["branch_deleted"] is False
    assert receipt["checkpoint_failed"] is True
    assert receipt["skipped"]
    assert "checkpoint failed" in receipt["skipped"]
    assert not any(c[:4] == ["git", "worktree", "remove", "--force"] for c in calls)
    assert not any(c[:3] == ["git", "branch", "-D"] for c in calls)


def test_reap_cli_exits_5_on_checkpoint_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    wt = tmp_path / "main" / ".flow" / "worktrees" / "feat-FT-1-thing"
    wt.mkdir(parents=True)
    calls: list = []
    runner = _checkpoint_runner(
        worktrees=_porcelain([(str(wt), "feat/FT-1-thing")]),
        calls=calls,
        dirty=True,
        push_rc=1,
    )
    monkeypatch.setattr(fw, "_default_runner", lambda: runner)
    rc = fw.cli_main(
        [
            "reap",
            "--ticket",
            "FT-1",
            "--branch",
            "feat/FT-1-thing",
            "--main-root",
            str(tmp_path / "main"),
        ]
    )
    assert rc == 5
    out = capsys.readouterr().out
    assert '"checkpoint_failed": true' in out


def test_reap_recovery_push_failure_leaves_worktree_intact(tmp_path: Path) -> None:
    wt = tmp_path / "main" / ".flow" / "worktrees" / "feat-FT-1-thing"
    wt.mkdir(parents=True)
    calls: list = []
    runner = _checkpoint_runner(
        worktrees=_porcelain([(str(wt), "feat/FT-1-thing")]),
        calls=calls,
        dirty=False,
        head_subject=fw._checkpoint_marker("FT-1"),
        ls_remote_out="",
        push_rc=1,
    )
    receipt = fw.reap_worktree(ticket="FT-1", main_root=tmp_path / "main", runner=runner)
    assert receipt["worktree_removed"] is False
    assert receipt["branch_deleted"] is False
    assert receipt["checkpoint_failed"] is True
    assert not any(c[:4] == ["git", "worktree", "remove", "--force"] for c in calls)
    assert not any(c[:3] == ["git", "branch", "-D"] for c in calls)


def test_reap_cli_exits_5_on_recovery_push_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    wt = tmp_path / "main" / ".flow" / "worktrees" / "feat-FT-1-thing"
    wt.mkdir(parents=True)
    calls: list = []
    runner = _checkpoint_runner(
        worktrees=_porcelain([(str(wt), "feat/FT-1-thing")]),
        calls=calls,
        dirty=False,
        head_subject=fw._checkpoint_marker("FT-1"),
        ls_remote_out="",
        push_rc=1,
    )
    monkeypatch.setattr(fw, "_default_runner", lambda: runner)
    rc = fw.cli_main(
        [
            "reap",
            "--ticket",
            "FT-1",
            "--branch",
            "feat/FT-1-thing",
            "--main-root",
            str(tmp_path / "main"),
        ]
    )
    assert rc == 5
    out = capsys.readouterr().out
    assert '"checkpoint_failed": true' in out


def test_reap_recovery_skips_push_when_rescue_ref_already_present(tmp_path: Path) -> None:
    # reap #1's push actually landed before it crashed; recovery must not
    # issue a duplicate push, just let the normal clean path remove.
    wt = tmp_path / "main" / ".flow" / "worktrees" / "feat-FT-1-thing"
    wt.mkdir(parents=True)
    calls: list = []
    runner = _checkpoint_runner(
        worktrees=_porcelain([(str(wt), "feat/FT-1-thing")]),
        calls=calls,
        dirty=False,
        head_subject=fw._checkpoint_marker("FT-1"),
        ls_remote_out="abc1234\trefs/heads/flow-rescue/FT-1-abc1234\n",
    )
    receipt = fw.reap_worktree(ticket="FT-1", main_root=tmp_path / "main", runner=runner)
    assert receipt["worktree_removed"] is True
    assert receipt["branch_deleted"] is True
    assert "checkpoint" not in receipt
    assert not any(c[:2] == ["git", "push"] for c in calls)


def test_rescue_branch_not_ticket_branch() -> None:
    assert not fw._is_ticket_branch("flow-rescue/flow-x1-abc1234", "flow-x1")


def test_rescue_branch_not_flow_key_re() -> None:
    import _evolve_common

    assert _evolve_common.key_from_ref("flow-rescue/flow-x1-abc1234") is None


def test_rescue_branch_not_inflight() -> None:
    import _evolve_common

    assert not _evolve_common.is_inflight("flow-x1", {"flow-rescue/flow-x1-abc1234"})


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
        branch="feat/flow-x1-thing",
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


def test_auto_hot_no_decision_floor_fires_when_adjudicate_hot_off(tmp_path, monkeypatch):
    # default off: _main_beads has no [evolve] section -> real adjudicate_hot
    # returns False, so the floor still refuses a hot+undecided change.
    main = _main_beads(tmp_path)
    monkeypatch.setattr(triage, "decided", lambda *a, **k: {"is_hot": True, "decided": False})
    assert triage.adjudicate_hot(main) is False
    with pytest.raises(fw._ConfigError):
        _boot(tmp_path, main, base="main", auto=True, planned=["lease.py"])


def test_auto_hot_no_decision_proceeds_when_adjudicate_hot_on(tmp_path, monkeypatch):
    # adjudicate_hot lifts the floor: a hot+undecided change bootstraps without
    # refusing (advisor proceed + merge-time guard review/CI are the gate).
    main = _main_beads(tmp_path)
    monkeypatch.setattr(triage, "decided", lambda *a, **k: {"is_hot": True, "decided": False})
    monkeypatch.setattr(triage, "adjudicate_hot", lambda *a, **k: True)
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


def _fake_beads_adapter(payload):
    """A BeadsAdapter stand-in whose `_run_json` returns canned `bd show` output,
    so the REAL triage.decided runs (label/comment parsing, is_hot, decided) with
    only the subprocess faked (the path the monkeypatch-decided tests skip)."""

    class _A:
        def __init__(self, config, runner=None):
            pass

        def _run_json(self, args):
            return payload

    return _A


def test_real_decided_hot_label_no_decision_refuses(tmp_path, monkeypatch):
    # real decided(): hot LABEL (file is non-hot) + no decision comment -> refuse
    main = _main_beads(tmp_path)
    monkeypatch.setattr(
        triage, "BeadsAdapter", _fake_beads_adapter([{"labels": ["evolve", "hot"], "comments": []}])
    )
    with pytest.raises(fw._ConfigError):
        _boot(tmp_path, main, base="@default", auto=False, planned=["some_helper.py"])


def test_real_decided_with_decision_clears_floor(tmp_path, monkeypatch):
    # the triage bypass MUST work: a recorded DECISION clears the floor. Regression
    # for the runner-protocol bug where a threaded positional runner threw inside
    # decided() -> block-by-default -> the bypass could never clear.
    main = _main_beads(tmp_path)
    monkeypatch.setattr(
        triage,
        "BeadsAdapter",
        _fake_beads_adapter(
            [
                {
                    "labels": ["evolve", "hot"],
                    "comments": [
                        {
                            "text": "DECISION: approved, ship it",
                            "created_at": "2026-06-08T00:00:00Z",
                        }
                    ],
                }
            ]
        ),
    )
    res = _boot(tmp_path, main, base="@default", auto=False, planned=["some_helper.py"])
    assert res["ticket"] == "flow-x1"


def test_auto_hot_label_empty_planned_refuses(tmp_path, monkeypatch):
    # the hot label is independent evidence of hotness: an empty --planned-files
    # must not disable the floor (real decided(): hot label + no decision -> refuse)
    main = _main_beads(tmp_path)
    monkeypatch.setattr(
        triage, "BeadsAdapter", _fake_beads_adapter([{"labels": ["hot"], "comments": []}])
    )
    calls: list = []
    with pytest.raises(fw._ConfigError):
        _boot(
            tmp_path,
            main,
            base="main",
            auto=True,
            planned=[],
            runner=_fake_runner(main=main, calls=calls),
        )
    # refused BEFORE creating the worktree -> no orphan
    assert not any(c[:3] == ["git", "worktree", "add"] for c in calls)
    assert not (tmp_path / "wt").exists()


def test_auto_non_hot_empty_planned_proceeds(tmp_path, monkeypatch):
    # the clean path is preserved: empty planned set + no hot label bootstraps.
    main = _main_beads(tmp_path)
    monkeypatch.setattr(
        triage, "BeadsAdapter", _fake_beads_adapter([{"labels": [], "comments": []}])
    )
    res = _boot(tmp_path, main, base="main", auto=True, planned=[])
    assert res["ticket"] == "flow-x1"


# ─── canonical per-ticket bootstrap claim (flow-594) ────────────────────────
# Two concurrent bootstraps of the same ticket must serialize on the claim
# (a flock under the MAIN checkout's .flow/tickets/), and the loser must
# refuse (exit 4) when a live sibling run exists. The worktree-local run.lock
# contract is untouched: bootstrap still holds no RUN lease.


def _sibling_ticket_dir(tmp: Path) -> tuple[Path, Path]:
    """A sibling worktree dir for FT-1 plus its .flow/runs/FT-1 ticket dir."""
    sib = tmp / "sib"
    td = sib / ".flow" / "runs" / "FT-1"
    td.mkdir(parents=True)
    return sib, td


def _siblings_porcelain(sib: Path, branch: str = "feat/FT-1-old") -> str:
    return _porcelain([(str(sib.parent / "main"), "main"), (str(sib), branch)])


def test_bootstrap_refuses_live_sibling_lease(tmp_path: Path) -> None:
    main = _main_checkout(tmp_path)
    sib, td = _sibling_ticket_dir(tmp_path)
    _seed_live_lease(td)
    calls: list = []
    runner = _fake_runner(calls=calls, worktree_list=_siblings_porcelain(sib))
    with pytest.raises(fw._DuplicateClaim) as exc:
        _run(tmp_path, main, runner=runner)
    msg = str(exc.value)
    assert str(sib) in msg
    assert "live" in msg
    assert "recover" in msg
    # refused BEFORE any git mutation: no worktree add, no orphan dir
    assert not any(c[:3] == ["git", "worktree", "add"] for c in calls)
    assert not (tmp_path / "wt").exists()


def test_live_sibling_unstick_hint_targets_sibling_worktree(tmp_path: Path) -> None:
    # recover reads <workspace_root>/.flow/runs/<ticket>; run from the main
    # checkout it sees an empty run dir (lease free, nothing broken) and the
    # operator loops back into exit 4. The hint must point INTO the sibling.
    main = _main_checkout(tmp_path)
    sib, td = _sibling_ticket_dir(tmp_path)
    _seed_live_lease(td)
    runner = _fake_runner(worktree_list=_siblings_porcelain(sib))
    with pytest.raises(fw._DuplicateClaim) as exc:
        _run(tmp_path, main, runner=runner)
    assert f"cd {sib} && /flow recover FT-1" in str(exc.value)


def test_cli_duplicate_claim_exits_4(tmp_path: Path, monkeypatch, capsys) -> None:
    main = _main_checkout(tmp_path)
    sib, td = _sibling_ticket_dir(tmp_path)
    _seed_live_lease(td)
    monkeypatch.setattr(
        fw, "_default_runner", lambda: _fake_runner(worktree_list=_siblings_porcelain(sib))
    )
    rc = fw.cli_main(
        [
            "create",
            "--ticket",
            "FT-1",
            "--plan-from",
            str(_plan_file(tmp_path)),
            "--base",
            "main",
            "--branch",
            "feat/FT-1-x",
            "--main-root",
            str(main),
            "--worktree-path",
            str(tmp_path / "wt"),
        ]
    )
    assert rc == 4
    assert "FT-1" in capsys.readouterr().err


def test_bootstrap_refuses_sibling_with_non_terminal_state(tmp_path: Path) -> None:
    # the bootstrap->cmd_init window: the sibling has a seeded state.json but no
    # run.lock yet. Fork 1 (settled): that IS a live sibling.
    main = _main_checkout(tmp_path)
    sib, td = _sibling_ticket_dir(tmp_path)
    state.init(td, "FT-1", "jira", ["ticket", "plan", "implement"], run_id="r1")
    with pytest.raises(fw._DuplicateClaim):
        _run(tmp_path, main, runner=_fake_runner(worktree_list=_siblings_porcelain(sib)))


def test_bootstrap_refuses_sibling_failed_mid_pipeline(tmp_path: Path) -> None:
    # a failed stage with later stages still pending is recover-resumable, so it
    # counts as non-terminal -> refuse (a resumable sibling double-shipping is
    # exactly flow-594).
    main = _main_checkout(tmp_path)
    sib, td = _sibling_ticket_dir(tmp_path)
    state.init(td, "FT-1", "jira", ["ticket", "plan", "implement"], run_id="r1")
    state.force_stage_status(td, "ticket", "completed")
    state.force_stage_status(td, "plan", "failed")
    with pytest.raises(fw._DuplicateClaim):
        _run(tmp_path, main, runner=_fake_runner(worktree_list=_siblings_porcelain(sib)))


def test_bootstrap_proceeds_past_expired_sibling_lease(tmp_path: Path) -> None:
    # a crashed run must not block re-running the ticket; reap owns its teardown.
    main = _main_checkout(tmp_path)
    sib, td = _sibling_ticket_dir(tmp_path)
    lease.acquire(
        td,
        run_id="r1",
        ttl_seconds=60,
        now_iso="2020-01-01T00:00:00Z",
        current_boot="other-boot",
        hostname="other-host",
        cwd="/x",
    )
    res = _run(tmp_path, main, runner=_fake_runner(worktree_list=_siblings_porcelain(sib)))
    assert res["ticket"] == "FT-1"


def test_bootstrap_proceeds_past_terminal_sibling_state(tmp_path: Path) -> None:
    main = _main_checkout(tmp_path)
    sib, td = _sibling_ticket_dir(tmp_path)
    state.init(td, "FT-1", "jira", ["ticket", "plan"], run_id="r1")
    state.force_stage_status(td, "ticket", "completed")
    state.force_stage_status(td, "plan", "completed")
    res = _run(tmp_path, main, runner=_fake_runner(worktree_list=_siblings_porcelain(sib)))
    assert res["ticket"] == "FT-1"


def test_bootstrap_refuses_corrupt_sibling_lock(tmp_path: Path) -> None:
    # unconfirmable ownership (possibly live): refuse, and point at /flow recover.
    main = _main_checkout(tmp_path)
    sib, td = _sibling_ticket_dir(tmp_path)
    lease.run_lock_path(td).write_text("{not json", encoding="utf-8")
    with pytest.raises(fw._DuplicateClaim) as exc:
        _run(tmp_path, main, runner=_fake_runner(worktree_list=_siblings_porcelain(sib)))
    msg = str(exc.value)
    assert "corrupt" in msg
    assert "recover" in msg


def test_bootstrap_no_siblings_creates_claim_file(tmp_path: Path) -> None:
    main = _main_checkout(tmp_path)
    res = _run(tmp_path, main)
    assert res["ticket"] == "FT-1"
    # the claim persists after release by design (flock targets are never deleted)
    assert (main.resolve() / ".flow" / "tickets" / "FT-1.claim").exists()


def _assert_claim_released(claim: Path) -> None:
    """LOCK_NB re-acquire on a fresh fd of the same path: conflicts iff another
    descriptor in this process still holds the flock."""
    fd = os.open(str(claim), os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pytest.fail("bootstrap leaked the claim flock")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_bootstrap_claim_released_after_success(tmp_path: Path) -> None:
    # same-process LOCK_NB re-acquire is the leak detector; the concurrent test
    # can't see an in-process fd leak (the OS frees flocks at process exit).
    main = _main_checkout(tmp_path)
    _run(tmp_path, main)
    _assert_claim_released(main.resolve() / ".flow" / "tickets" / "FT-1.claim")


def test_bootstrap_claim_released_after_refusal(tmp_path: Path) -> None:
    # a refused bootstrap (exit 4) must not leave the claim held, or the
    # relaunch the refusal message asks for would block.
    main = _main_checkout(tmp_path)
    sib, td = _sibling_ticket_dir(tmp_path)
    _seed_live_lease(td)
    runner = _fake_runner(worktree_list=_siblings_porcelain(sib))
    with pytest.raises(fw._DuplicateClaim):
        _run(tmp_path, main, runner=runner)
    _assert_claim_released(main.resolve() / ".flow" / "tickets" / "FT-1.claim")


# ─── auto-reap a dead colliding sibling at create (flow-vpg1) ──────────────
# A DEAD sibling checked out on the exact branch/path this bootstrap wants (a
# manual relaunch after a spend-limit death) would otherwise make
# `git worktree add -b` fail outright; create auto-reaps it first via the
# same checkpoint-then-remove reap_worktree uses.


def test_create_auto_reaps_dead_colliding_sibling_same_branch(tmp_path: Path) -> None:
    main = _main_checkout(tmp_path)
    sib, td = _sibling_ticket_dir(tmp_path)
    lease.acquire(
        td,
        run_id="r1",
        ttl_seconds=60,
        now_iso="2020-01-01T00:00:00Z",
        current_boot="other-boot",
        hostname="other-host",
        cwd="/x",
    )
    calls: list = []
    runner = _fake_runner(
        calls=calls, main=main, worktree_list=_siblings_porcelain(sib, branch="feat/FT-1-thing")
    )
    res = _run(tmp_path, main, runner=runner)
    assert res["ticket"] == "FT-1"
    assert any(c[:4] == ["git", "worktree", "remove", "--force"] for c in calls)
    assert any(c[:3] == ["git", "worktree", "add"] for c in calls)


def test_create_no_collision_auto_reap_is_noop(tmp_path: Path) -> None:
    # a dead sibling on a DIFFERENT branch/path never collides with THIS
    # bootstrap's worktree-add path; auto-reap must not touch it (regression:
    # test_bootstrap_proceeds_past_expired_sibling_lease already pins the
    # end-to-end proceed, this pins that no reap call fires alongside it).
    main = _main_checkout(tmp_path)
    sib, td = _sibling_ticket_dir(tmp_path)
    lease.acquire(
        td,
        run_id="r1",
        ttl_seconds=60,
        now_iso="2020-01-01T00:00:00Z",
        current_boot="other-boot",
        hostname="other-host",
        cwd="/x",
    )
    calls: list = []
    runner = _fake_runner(calls=calls, main=main, worktree_list=_siblings_porcelain(sib))
    res = _run(tmp_path, main, runner=runner)
    assert res["ticket"] == "FT-1"
    assert sib.exists()
    assert not any(c[:4] == ["git", "worktree", "remove", "--force"] for c in calls)


def test_create_refuses_when_auto_reap_checkpoint_fails(tmp_path: Path) -> None:
    main = _main_checkout(tmp_path)
    sib, td = _sibling_ticket_dir(tmp_path)
    lease.acquire(
        td,
        run_id="r1",
        ttl_seconds=60,
        now_iso="2020-01-01T00:00:00Z",
        current_boot="other-boot",
        hostname="other-host",
        cwd="/x",
    )
    calls: list = []

    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:4] == ["git", "worktree", "list", "--porcelain"]:
            return subprocess.CompletedProcess(
                args, 0, _siblings_porcelain(sib, branch="feat/FT-1-thing"), ""
            )
        if args[:3] == ["git", "status", "--porcelain"]:
            dirty = str(cwd) == str(sib)
            return subprocess.CompletedProcess(args, 0, "?? scratch.py\n" if dirty else "", "")
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(args, 1, "", "push failed")
        if args[:3] == ["git", "worktree", "add"]:
            wt = Path(args[5])  # git worktree add -b <branch> <path> <base>
            wt.mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["git", "check-ignore"]:
            return subprocess.CompletedProcess(args, 1, "", "")
        if args[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(args, 0, "wtsha0001\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    with pytest.raises(fw._ConfigError):
        _run(tmp_path, main, runner=run)
    assert sib.exists()
    assert not any(c[:4] == ["git", "worktree", "remove", "--force"] for c in calls)
    assert not any(c[:3] == ["git", "worktree", "add"] for c in calls)


def test_create_toctou_sibling_goes_live_during_auto_reap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # dead at _assert_no_live_sibling, but a concurrent acquire wins the
    # sibling's OWN run.lock.lock flock first during the auto-reap's
    # classify_then: the live lease must be observed and the reap refused,
    # mirroring test_reap_skips_remove_when_lease_goes_live_under_flock.
    import contextlib
    from collections.abc import Iterator

    main = _main_checkout(tmp_path)
    sib, td = _sibling_ticket_dir(tmp_path)
    lease.acquire(
        td,
        run_id="r1",
        ttl_seconds=60,
        now_iso="2020-01-01T00:00:00Z",
        current_boot="other-boot",
        hostname="other-host",
        cwd="/x",
    )
    real_flock = lease.flock_blocking

    @contextlib.contextmanager
    def racing_flock(path: Path) -> Iterator[None]:
        with real_flock(path):
            if path == lease._flock_path(td):
                lease.run_lock_path(td).write_text(
                    lease._serialize(
                        lease.Lease(
                            run_id="racer",
                            boot_id="boot-x",
                            hostname="host-x",
                            cwd="/cwd-x",
                            acquired_at="2999-01-01T00:00:00Z",
                            lease_expires_at="2999-01-01T01:00:00Z",
                        )
                    ),
                    encoding="utf-8",
                )
            yield

    monkeypatch.setattr(lease, "flock_blocking", racing_flock)
    runner = _fake_runner(
        main=main, worktree_list=_siblings_porcelain(sib, branch="feat/FT-1-thing")
    )
    with pytest.raises(fw._DuplicateClaim):
        _run(tmp_path, main, runner=runner)
    assert sib.exists()


def _real_repo(tmp: Path) -> Path:
    """A scratch git repo wrapping _main_checkout (so bootstrap runs real git)."""
    main = _main_checkout(tmp)
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    (main / "README.md").write_text("x\n", encoding="utf-8")
    git = ["git", "-C", str(main), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "add", "README.md"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    return main


def _bootstrap_proc(main_str: str, plan_str: str, branch: str) -> None:
    """Top-level so multiprocessing can pickle it on macOS spawn-start.

    Exit 0 on a successful bootstrap, 4 on the duplicate-claim refusal."""
    try:
        fw.bootstrap(
            ticket="FT-1",
            plan_from=Path(plan_str),
            base="main",
            branch=branch,
            main_root=Path(main_str),
            mise_trust=False,
        )
    except fw._DuplicateClaim:
        sys.exit(4)
    sys.exit(0)


def test_concurrent_bootstraps_exactly_one_wins(tmp_path: Path) -> None:
    # two processes, same ticket, different branch names: the claim serializes
    # them, the loser sees the winner's seeded non-terminal state and refuses.
    main = _real_repo(tmp_path)
    plan = _plan_file(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    p1 = ctx.Process(target=_bootstrap_proc, args=(str(main), str(plan), "feat/FT-1-a"))
    p2 = ctx.Process(target=_bootstrap_proc, args=(str(main), str(plan), "feat/FT-1-b"))
    p1.start()
    p2.start()
    p1.join(timeout=60)
    p2.join(timeout=60)
    assert sorted([p1.exitcode, p2.exitcode]) == [0, 4]
    pool = main.resolve() / ".flow" / "worktrees"
    made = [d for d in (pool / "feat-FT-1-a", pool / "feat-FT-1-b") if d.exists()]
    assert len(made) == 1


# ─── locate-or-reseed (flow-kx17.2) ─────────────────────────────────────────


def _locate_runner(
    *,
    worktree_list: str,
    calls: list,
    main: Path | None = None,
):
    """Runner for locate-or-reseed: answers `git worktree list --porcelain` from
    `worktree_list`; handles the reseed `git worktree add <path> <branch>` (NO -b,
    path at index 3, unlike bootstrap's `-b <branch> <path> <base>`)."""

    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:4] == ["git", "worktree", "list", "--porcelain"]:
            return subprocess.CompletedProcess(args, 0, worktree_list, "")
        if args[:3] == ["git", "worktree", "add"]:
            # reseed form: git worktree add <path> <branch>  (no -b flag)
            wt = Path(args[3])
            wt.mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    return run


def test_locate_existing_worktree(tmp_path: Path) -> None:
    main = _main_checkout(tmp_path)
    wt = main / ".flow" / "worktrees" / "feat-FT-1-thing"
    wt.mkdir(parents=True)
    calls: list = []
    runner = _locate_runner(
        worktree_list=_porcelain([(str(main), "main"), (str(wt), "feat/FT-1-thing")]),
        calls=calls,
    )
    result = fw.locate_or_reseed(
        ticket="FT-1", branch="feat/FT-1-thing", main_root=main, runner=runner
    )
    assert result == {"worktree": str(wt), "reseeded": False}
    # LOCATE never adds a worktree
    assert not any(c[:3] == ["git", "worktree", "add"] for c in calls)


def test_reseed_when_externally_removed(tmp_path: Path) -> None:
    # the worktree was reaped; locate-or-reseed checks out the EXISTING remote
    # branch (no -b) and re-copies config (reseeded:true).
    main = _main_checkout(tmp_path)
    calls: list = []
    # worktree list shows only main (the ticket's worktree is gone)
    runner = _locate_runner(
        worktree_list=_porcelain([(str(main), "main")]),
        calls=calls,
        main=main,
    )
    result = fw.locate_or_reseed(
        ticket="FT-1", branch="feat/FT-1-thing", main_root=main, runner=runner
    )
    assert result["reseeded"] is True
    wt = Path(result["worktree"])
    assert wt == main.resolve() / ".flow" / "worktrees" / "feat-FT-1-thing"
    assert wt.exists()
    # fetched the existing remote branch, then checked it out WITHOUT -b
    assert ["git", "fetch", "origin", "feat/FT-1-thing"] in calls
    add = next(c for c in calls if c[:3] == ["git", "worktree", "add"])
    assert "-b" not in add
    assert add == ["git", "worktree", "add", str(wt), "feat/FT-1-thing"]
    # config re-copied (the gitignored dev config from main)
    assert (wt / ".env").read_text(encoding="utf-8") == "SECRET=1\n"
    assert (wt / ".claude" / "settings.json").exists()
    # flow config ensured + memory redirect written
    assert (wt / ".flow" / "workspace.toml").exists()
    assert (wt / ".flow" / "memory-root").read_text(encoding="utf-8").strip() == str(
        main.resolve() / ".flow"
    )


def test_reseed_memory_redirect_honors_main_memory_root(tmp_path: Path) -> None:
    # same [memory].root contract as bootstrap: a reseeded revision worktree
    # must share main's configured store, not fragment into main/.flow.
    main = _main_checkout(tmp_path)
    shared = tmp_path / "shared-flow"
    ws = main / ".flow" / "workspace.toml"
    ws.write_text(ws.read_text(encoding="utf-8") + f'root = "{shared}"\n', encoding="utf-8")
    runner = _locate_runner(worktree_list=_porcelain([(str(main), "main")]), calls=[], main=main)
    result = fw.locate_or_reseed(
        ticket="FT-1", branch="feat/FT-1-thing", main_root=main, runner=runner
    )
    wt = Path(result["worktree"])
    assert result["reseeded"] is True
    assert (wt / ".flow" / "memory-root").read_text(encoding="utf-8").strip() == str(shared)


def test_locate_or_reseed_cli_locate(tmp_path: Path, monkeypatch, capsys) -> None:
    import json

    main = _main_checkout(tmp_path)
    wt = main / ".flow" / "worktrees" / "feat-FT-1-thing"
    wt.mkdir(parents=True)
    monkeypatch.setattr(
        fw,
        "_default_runner",
        lambda: _locate_runner(
            worktree_list=_porcelain([(str(main), "main"), (str(wt), "feat/FT-1-thing")]),
            calls=[],
        ),
    )
    rc = fw.cli_main(
        [
            "locate-or-reseed",
            "--ticket",
            "FT-1",
            "--branch",
            "feat/FT-1-thing",
            "--main-root",
            str(main),
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["reseeded"] is False
    assert out["worktree"] == str(wt)
