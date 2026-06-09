from __future__ import annotations

import json
import subprocess

import pytest

import version

PLUGIN = version.PLUGIN_JSON


# ---- bump_patch (pure) ----


def test_bump_patch_increments_patch():
    assert version.bump_patch("0.27.56") == "0.27.57"
    assert version.bump_patch("1.0.9") == "1.0.10"


@pytest.mark.parametrize("bad", ["x.y", "1.2", "1.2.x"])
def test_bump_patch_malformed_raises(bad):
    with pytest.raises(ValueError):
        version.bump_patch(bad)


# ---- canned runner: dispatches on the git subcommand ----


def _plugin(version_str: str) -> str:
    return json.dumps({"name": "flow", "version": version_str}, indent=2)


def _runner(*, current: str, show_rc: int = 0):
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["git", "show"]:
            if show_rc != 0:
                return subprocess.CompletedProcess(args, show_rc, "", "no such ref")
            return subprocess.CompletedProcess(args, 0, _plugin(current), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    return run


def test_read_version_parses_show_blob(tmp_path):
    run = _runner(current="0.27.56")
    assert version.read_version(cwd=tmp_path, ref="origin/main", runner=run) == "0.27.56"


def test_read_version_git_failure_raises(tmp_path):
    run = _runner(current="0.27.56", show_rc=1)
    with pytest.raises(version.ToolError):
        version.read_version(cwd=tmp_path, ref="origin/main", runner=run)


def test_compute_shape(tmp_path):
    run = _runner(current="0.27.56")
    assert version.compute(cwd=tmp_path, ref="origin/main", runner=run) == {
        "ref": "origin/main",
        "current": "0.27.56",
        "next": "0.27.57",
    }


# ---- CLI ----


def test_cli_next_ok(monkeypatch, capsys):
    monkeypatch.setattr(
        version,
        "compute",
        lambda **_: {"ref": "origin/main", "current": "0.27.56", "next": "0.27.57"},
    )
    rc = version.cli_main(["next", "--ref", "origin/main", "--cwd", "."])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {
        "ref": "origin/main",
        "current": "0.27.56",
        "next": "0.27.57",
    }


def test_cli_next_tool_error_exit_2(monkeypatch, capsys):
    def _boom(**_):
        raise version.ToolError("git show failed")

    monkeypatch.setattr(version, "compute", _boom)
    rc = version.cli_main(["next", "--cwd", "."])
    assert rc == 2
    assert "git show failed" in capsys.readouterr().err
