from __future__ import annotations

import json
import subprocess

import pytest

import version_remerge as vr

PLUGIN = vr.PLUGIN_JSON
MARKET = vr.MARKETPLACE_JSON


# ---- pure helpers ----


def test_parse_version():
    assert vr.parse_version('{"version": "0.27.42"}') == (0, 27, 42)


def test_parse_version_no_match_raises():
    with pytest.raises(vr.ToolError):
        vr.parse_version("{}")


def test_next_version_patch_bump():
    assert vr.next_version((0, 27, 42)) == "0.27.43"
    assert vr.next_version((1, 0, 9)) == "1.0.10"


def test_is_version_only_conflict_exact_two():
    assert vr.is_version_only_conflict({PLUGIN, MARKET})


def test_is_version_only_conflict_extra_file_false():
    assert not vr.is_version_only_conflict({PLUGIN, MARKET, "scripts/x.py"})


def test_is_version_only_conflict_missing_one_false():
    assert not vr.is_version_only_conflict({PLUGIN})


# ---- fake runner: dispatches on git subcommand, records calls ----


def _plugin(version: str, *, extra: dict | None = None) -> str:
    body = {"name": "flow", "version": version}
    if extra:
        body.update(extra)
    return json.dumps(body, indent=2)


def _market(version: str) -> str:
    return json.dumps({"plugins": [{"name": "flow", "version": version}]}, indent=2)


def _runner(
    *,
    main_version: str,
    merge_rc: int,
    conflicts: list[str],
    calls: list[list[str]],
    ours: dict[str, str] | None = None,
    theirs: dict[str, str] | None = None,
):
    """Canned git runner. `git merge` returns merge_rc; `git diff --diff-filter=U`
    returns `conflicts` (one per line). `git show <default>:<rel>` answers main's
    blob (for the NEXT computation); `git show :2:<rel>` / `:3:<rel>` answer the
    per-file OURS (branch) / THEIRS (main) conflict blobs from `ours` / `theirs`.
    """
    ours = ours or {}
    theirs = theirs or {}

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["git", "symbolic-ref"]:
            return subprocess.CompletedProcess(args, 0, "origin/main\n", "")
        if args[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["git", "show"]:
            ref = args[2]
            for rel in (PLUGIN, MARKET):
                if ref == f":2:{rel}":
                    return subprocess.CompletedProcess(args, 0, ours.get(rel, ""), "")
                if ref == f":3:{rel}":
                    return subprocess.CompletedProcess(args, 0, theirs.get(rel, ""), "")
            # `git show <default>:plugin.json` — main's version for the NEXT bump
            return subprocess.CompletedProcess(args, 0, _plugin(main_version), "")
        if args[:2] == ["git", "merge"] and "--abort" not in args:
            return subprocess.CompletedProcess(args, merge_rc, "", "CONFLICT" if merge_rc else "")
        if args[:3] == ["git", "diff", "--name-only"]:
            # the post-resolution safety re-check sees an empty set once both version
            # files have been `git add`ed (mirrors git clearing the resolved paths).
            added = [a[2] for a in calls if a[:2] == ["git", "add"]]
            remaining = [c for c in conflicts if c not in added]
            return subprocess.CompletedProcess(args, 0, "\n".join(remaining), "")
        if args[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(args, 0, "deadbeef\n", "")
        # add / commit / push / merge --abort
        return subprocess.CompletedProcess(args, 0, "", "")

    return run


def test_remerged_clean_when_no_conflict(tmp_path):
    cwd = tmp_path
    calls: list[list[str]] = []
    run = _runner(main_version="0.27.42", merge_rc=0, conflicts=[], calls=calls)
    out = vr.recover("feature/flow-x-foo", cwd=cwd, runner=run)
    assert out["status"] == "remerged_clean"
    assert out["sha"] == "deadbeef"
    assert out["version"] is None
    assert ["git", "push"] in calls
    assert not any(a[:3] == ["git", "merge", "--abort"] for a in calls)


def test_version_only_conflict_resolves_and_bumps(tmp_path):
    cwd = tmp_path
    (cwd / PLUGIN).parent.mkdir(parents=True, exist_ok=True)
    (cwd / MARKET).parent.mkdir(parents=True, exist_ok=True)
    calls: list[list[str]] = []
    # ours (branch) and theirs (main) differ ONLY in the version line.
    run = _runner(
        main_version="0.27.42",
        merge_rc=1,
        conflicts=[PLUGIN, MARKET],
        calls=calls,
        ours={PLUGIN: _plugin("0.27.39"), MARKET: _market("0.27.39")},
        theirs={PLUGIN: _plugin("0.27.42"), MARKET: _market("0.27.42")},
    )
    out = vr.recover("feature/flow-x-foo", cwd=cwd, runner=run)
    assert out["status"] == "remerged"
    assert out["version"] == "0.27.43"
    assert (cwd / PLUGIN).read_text().count("0.27.43") == 1
    assert (cwd / MARKET).read_text().count("0.27.43") == 1
    assert ["git", "commit", "--no-edit"] in calls
    assert ["git", "push"] in calls
    assert not any(a[:3] == ["git", "merge", "--abort"] for a in calls)


def test_pr151_regression_branch_39_main_42(tmp_path):
    # the live PR #151 case: branch carried 0.27.39, main walked to 0.27.42; the two
    # version files conflict on the version line ONLY. Recovery KEEPS the PR's content
    # (ours) and bumps to 0.27.43 (NEXT is computed from MAIN, never the branch 0.27.39).
    cwd = tmp_path
    (cwd / PLUGIN).parent.mkdir(parents=True, exist_ok=True)
    (cwd / MARKET).parent.mkdir(parents=True, exist_ok=True)
    calls: list[list[str]] = []
    run = _runner(
        main_version="0.27.42",
        merge_rc=1,
        conflicts=[PLUGIN, MARKET],
        calls=calls,
        ours={PLUGIN: _plugin("0.27.39"), MARKET: _market("0.27.39")},
        theirs={PLUGIN: _plugin("0.27.42"), MARKET: _market("0.27.42")},
    )
    out = vr.recover("feature/flow-hso-version-remerge", cwd=cwd, runner=run)
    assert out["version"] == "0.27.43"
    assert (cwd / PLUGIN).read_text().count("0.27.39") == 0
    assert (cwd / PLUGIN).read_text().count("0.27.43") == 1


def test_nonversion_content_diff_in_version_file_aborts(tmp_path):
    # the discard-bug regression: the conflict set is EXACTLY the two version files
    # (so the file-level detector passes), BUT the PR's plugin.json carries a "hooks"
    # block main lacks, on TOP of the version-line conflict. A `--theirs` take would
    # silently drop that legitimate PR change; the content check must abort instead.
    cwd = tmp_path
    (cwd / PLUGIN).parent.mkdir(parents=True, exist_ok=True)
    (cwd / MARKET).parent.mkdir(parents=True, exist_ok=True)
    calls: list[list[str]] = []
    run = _runner(
        main_version="0.27.42",
        merge_rc=1,
        conflicts=[PLUGIN, MARKET],
        calls=calls,
        ours={
            PLUGIN: _plugin("0.27.39", extra={"hooks": {"SessionStart": []}}),
            MARKET: _market("0.27.39"),
        },
        theirs={PLUGIN: _plugin("0.27.42"), MARKET: _market("0.27.42")},
    )
    with pytest.raises(vr.NonVersionConflict) as exc:
        vr.recover("feature/flow-x-foo", cwd=cwd, runner=run)
    assert PLUGIN in exc.value.files
    assert ["git", "merge", "--abort"] in calls
    assert ["git", "push"] not in calls


def test_non_version_conflict_aborts(tmp_path):
    cwd = tmp_path
    calls: list[list[str]] = []
    run = _runner(
        main_version="0.27.42",
        merge_rc=1,
        conflicts=[PLUGIN, MARKET, "scripts/foo.py"],
        calls=calls,
    )
    with pytest.raises(vr.NonVersionConflict) as exc:
        vr.recover("feature/flow-x-foo", cwd=cwd, runner=run)
    assert "scripts/foo.py" in exc.value.files
    assert ["git", "merge", "--abort"] in calls
    assert ["git", "push"] not in calls


def test_cli_non_version_conflict_exit_3(tmp_path, monkeypatch, capsys):
    def fake_recover(branch, *, cwd, runner=None):
        raise vr.NonVersionConflict([PLUGIN, MARKET, "scripts/foo.py"])

    monkeypatch.setattr(vr, "recover", fake_recover)
    rc = vr.cli_main(["recover", "--branch", "feature/flow-x", "--cwd", str(tmp_path)])
    assert rc == 3
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "non_version_conflict"
    assert "scripts/foo.py" in out["files"]


def test_cli_remerged_exit_0(tmp_path, monkeypatch, capsys):
    def fake_recover(branch, *, cwd, runner=None):
        return {"status": "remerged", "sha": "abc", "version": "0.27.43"}

    monkeypatch.setattr(vr, "recover", fake_recover)
    rc = vr.cli_main(["recover", "--branch", "feature/flow-x", "--cwd", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "remerged"
    assert out["version"] == "0.27.43"


def test_strip_version_normalizes_only_first():
    # count=1: only the FIRST "version" is normalized, so a difference in a second
    # "version" field is preserved and trips the equality check (safe abort).
    a = '{"version": "1.2.3", "dep": {"version": "9.9.9"}}'
    b = '{"version": "4.5.6", "dep": {"version": "9.9.9"}}'
    assert vr._strip_version(a) == vr._strip_version(b)
    c = '{"version": "1.2.3", "dep": {"version": "8.8.8"}}'
    assert vr._strip_version(a) != vr._strip_version(c)


def test_set_version_bumps_only_first(tmp_path):
    f = tmp_path / "x.json"
    f.write_text('{"version": "0.27.40", "dep": {"version": "9.9.9"}}')
    vr._set_version_in_file(f, "0.27.43")
    txt = f.read_text()
    assert '"version": "0.27.43"' in txt
    assert '"version": "9.9.9"' in txt
