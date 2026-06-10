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


# ---- bump_minor / bump_for_type (pure) ----


def test_bump_minor_increments_minor_resets_patch():
    assert version.bump_minor("0.27.56") == "0.28.0"
    assert version.bump_minor("1.0.9") == "1.1.0"


@pytest.mark.parametrize("bad", ["x.y", "1.2", "1.2.x"])
def test_bump_minor_malformed_raises(bad):
    with pytest.raises(ValueError):
        version.bump_minor(bad)


def test_bump_for_type_feat_is_minor():
    assert version.bump_for_type("0.27.56", "feat") == "0.28.0"


@pytest.mark.parametrize("commit_type", ["fix", "chore", None, "wat"])
def test_bump_for_type_non_feat_is_patch(commit_type):
    assert version.bump_for_type("0.27.56", commit_type) == "0.27.57"


# ---- parse_commit_type (pure) ----


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("feat: add queue verb", "feat"),
        ("feat(queue): add verb", "feat"),
        ("feat!: breaking add", "feat"),
        ("fix: stop the bleeding", "fix"),
        ("merge branch 'main' into x", None),
        ("Update README", None),
    ],
)
def test_parse_commit_type(subject, expected):
    assert version.parse_commit_type(subject) == expected


# ---- canned runner: dispatches on the git subcommand ----


def _plugin(version_str: str) -> str:
    return json.dumps({"name": "flow", "version": version_str}, indent=2)


def _runner(*, current: str, show_rc: int = 0, log_subject: str = ""):
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["git", "show"]:
            if show_rc != 0:
                return subprocess.CompletedProcess(args, show_rc, "", "no such ref")
            return subprocess.CompletedProcess(args, 0, _plugin(current), "")
        if args[:2] == ["git", "log"]:
            return subprocess.CompletedProcess(args, 0, f"{log_subject}\n", "")
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
        "bump": "patch",
        "commit_type": None,
    }


def test_compute_explicit_feat_bumps_minor(tmp_path):
    run = _runner(current="0.27.56")
    assert version.compute(cwd=tmp_path, ref="origin/main", runner=run, commit_type="feat") == {
        "ref": "origin/main",
        "current": "0.27.56",
        "next": "0.28.0",
        "bump": "minor",
        "commit_type": "feat",
    }


def test_compute_explicit_fix_bumps_patch(tmp_path):
    run = _runner(current="0.27.56")
    result = version.compute(cwd=tmp_path, ref="origin/main", runner=run, commit_type="fix")
    assert result["next"] == "0.27.57"
    assert result["bump"] == "patch"
    assert result["commit_type"] == "fix"


def test_compute_head_subject_fallback_feat(tmp_path):
    # no explicit flag: the HEAD commit subject's conventional prefix decides.
    run = _runner(current="0.27.56", log_subject="feat(queue): add verb")
    result = version.compute(cwd=tmp_path, ref="origin/main", runner=run)
    assert result["next"] == "0.28.0"
    assert result["bump"] == "minor"
    assert result["commit_type"] == "feat"


def test_compute_head_subject_fallback_non_conventional_is_patch(tmp_path):
    run = _runner(current="0.27.56", log_subject="Update README")
    result = version.compute(cwd=tmp_path, ref="origin/main", runner=run)
    assert result["next"] == "0.27.57"
    assert result["bump"] == "patch"
    assert result["commit_type"] is None


def test_compute_empty_string_commit_type_is_unset(tmp_path):
    # prose passes --commit-type "$COMMIT_TYPE" unconditionally; empty → fallback.
    run = _runner(current="0.27.56", log_subject="feat: add queue verb")
    result = version.compute(cwd=tmp_path, ref="origin/main", runner=run, commit_type="")
    assert result["next"] == "0.28.0"
    assert result["commit_type"] == "feat"


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


# ---- write_version (surgical file write) ----

_PLUGIN_FIXTURE = """\
{
  "name": "flow",
  "version": "0.27.57",
  "description": "ticket pipeline",
  "skills": "./skills"
}
"""

_MARKETPLACE_FIXTURE = """\
{
  "name": "vdsmon-flow",
  "owner": {"name": "Victor"},
  "plugins": [
    {
      "name": "flow",
      "source": "./plugins/flow",
      "version": "0.27.57"
    }
  ]
}
"""


def _seed_version_files(tmp_path):
    plugin = tmp_path / version.PLUGIN_JSON
    market = tmp_path / version.MARKETPLACE_JSON
    plugin.parent.mkdir(parents=True, exist_ok=True)
    market.parent.mkdir(parents=True, exist_ok=True)
    plugin.write_text(_PLUGIN_FIXTURE, encoding="utf-8")
    market.write_text(_MARKETPLACE_FIXTURE, encoding="utf-8")
    return plugin, market


def test_write_version_bumps_both_files(tmp_path):
    plugin, market = _seed_version_files(tmp_path)
    version.write_version(cwd=tmp_path, version="0.27.58")
    assert '"version": "0.27.58"' in plugin.read_text(encoding="utf-8")
    assert '"version": "0.27.58"' in market.read_text(encoding="utf-8")


def test_write_version_preserves_surrounding_bytes(tmp_path):
    plugin, market = _seed_version_files(tmp_path)
    version.write_version(cwd=tmp_path, version="0.27.58")
    assert plugin.read_text(encoding="utf-8") == _PLUGIN_FIXTURE.replace("0.27.57", "0.27.58")
    assert market.read_text(encoding="utf-8") == _MARKETPLACE_FIXTURE.replace("0.27.57", "0.27.58")


def test_set_version_already_at_target_is_noop_success(tmp_path):
    # flow-wkn: a file already stamped with the target version is a benign no-op,
    # not "no version line to replace".
    f = tmp_path / "x.json"
    before = '{"name": "flow", "version": "0.27.61"}'
    f.write_text(before, encoding="utf-8")
    version._set_version_in_file(f, "0.27.61")
    assert f.read_text(encoding="utf-8") == before


def test_set_version_no_version_line_raises(tmp_path):
    f = tmp_path / "x.json"
    f.write_text('{"name": "flow"}', encoding="utf-8")
    with pytest.raises(version.ToolError):
        version._set_version_in_file(f, "0.27.61")


def test_write_version_idempotent_same_version(tmp_path):
    plugin, market = _seed_version_files(tmp_path)
    version.write_version(cwd=tmp_path, version="0.27.58")
    version.write_version(cwd=tmp_path, version="0.27.58")
    assert plugin.read_text(encoding="utf-8") == _PLUGIN_FIXTURE.replace("0.27.57", "0.27.58")
    assert market.read_text(encoding="utf-8") == _MARKETPLACE_FIXTURE.replace("0.27.57", "0.27.58")


def test_set_version_replaces_only_first(tmp_path):
    # count=1: only the FIRST "version" is rewritten, so a nested second "version"
    # field (e.g. a dep pin) is preserved untouched.
    f = tmp_path / "x.json"
    f.write_text('{"version": "0.27.40", "dep": {"version": "9.9.9"}}', encoding="utf-8")
    version._set_version_in_file(f, "0.27.43")
    txt = f.read_text(encoding="utf-8")
    assert '"version": "0.27.43"' in txt
    assert '"version": "9.9.9"' in txt


# ---- stamp (compute + write) ----


def test_stamp_writes_and_returns_compute(tmp_path):
    _seed_version_files(tmp_path)
    run = _runner(current="0.27.57")
    result = version.stamp(cwd=tmp_path, ref="origin/main", runner=run)
    assert result == {
        "ref": "origin/main",
        "current": "0.27.57",
        "next": "0.27.58",
        "bump": "patch",
        "commit_type": None,
    }
    assert '"version": "0.27.58"' in (tmp_path / version.PLUGIN_JSON).read_text(encoding="utf-8")
    assert '"version": "0.27.58"' in (tmp_path / version.MARKETPLACE_JSON).read_text(
        encoding="utf-8"
    )


def test_stamp_feat_writes_minor(tmp_path):
    _seed_version_files(tmp_path)
    run = _runner(current="0.27.57")
    result = version.stamp(cwd=tmp_path, ref="origin/main", runner=run, commit_type="feat")
    assert result["next"] == "0.28.0"
    assert result["bump"] == "minor"
    assert '"version": "0.28.0"' in (tmp_path / version.PLUGIN_JSON).read_text(encoding="utf-8")
    assert '"version": "0.28.0"' in (tmp_path / version.MARKETPLACE_JSON).read_text(
        encoding="utf-8"
    )


def test_stamp_head_subject_fallback(tmp_path):
    _seed_version_files(tmp_path)
    run = _runner(current="0.27.57", log_subject="feat(queue): add verb")
    result = version.stamp(cwd=tmp_path, ref="origin/main", runner=run)
    assert result["next"] == "0.28.0"
    assert result["commit_type"] == "feat"


# ---- CLI stamp ----


def test_cli_stamp_ok(monkeypatch, capsys):
    monkeypatch.setattr(
        version,
        "stamp",
        lambda **_: {"ref": "origin/main", "current": "0.27.57", "next": "0.27.58"},
    )
    rc = version.cli_main(["stamp", "--ref", "origin/main", "--cwd", "."])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {
        "ref": "origin/main",
        "current": "0.27.57",
        "next": "0.27.58",
    }


def test_cli_stamp_tool_error_exit_2(monkeypatch, capsys):
    def _boom(**_):
        raise version.ToolError("git show failed")

    monkeypatch.setattr(version, "stamp", _boom)
    rc = version.cli_main(["stamp", "--cwd", "."])
    assert rc == 2
    assert "git show failed" in capsys.readouterr().err


def test_cli_stamp_plumbs_commit_type(monkeypatch, capsys):
    seen: dict = {}

    def fake_stamp(**kwargs):
        seen.update(kwargs)
        return {
            "ref": "origin/main",
            "current": "0.27.57",
            "next": "0.28.0",
            "bump": "minor",
            "commit_type": "feat",
        }

    monkeypatch.setattr(version, "stamp", fake_stamp)
    rc = version.cli_main(["stamp", "--cwd", ".", "--commit-type", "feat"])
    assert rc == 0
    assert seen["commit_type"] == "feat"
    assert json.loads(capsys.readouterr().out)["bump"] == "minor"


def test_cli_next_plumbs_commit_type(monkeypatch, capsys):
    seen: dict = {}

    def fake_compute(**kwargs):
        seen.update(kwargs)
        return {
            "ref": "origin/main",
            "current": "0.27.57",
            "next": "0.28.0",
            "bump": "minor",
            "commit_type": "feat",
        }

    monkeypatch.setattr(version, "compute", fake_compute)
    rc = version.cli_main(["next", "--cwd", ".", "--commit-type", "feat"])
    assert rc == 0
    assert seen["commit_type"] == "feat"


def test_cli_commit_type_defaults_empty(monkeypatch, capsys):
    seen: dict = {}

    def fake_stamp(**kwargs):
        seen.update(kwargs)
        return {
            "ref": "origin/main",
            "current": "0.27.57",
            "next": "0.27.58",
            "bump": "patch",
            "commit_type": None,
        }

    monkeypatch.setattr(version, "stamp", fake_stamp)
    rc = version.cli_main(["stamp", "--cwd", "."])
    assert rc == 0
    assert seen["commit_type"] == ""
