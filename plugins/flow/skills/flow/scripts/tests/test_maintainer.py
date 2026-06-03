from __future__ import annotations

from pathlib import Path

import maintainer

MARKER = "[maintainer]\nself_target = true\n"
NOMARKER = '[tracker]\nbackend = "beads"\n'


def _ws(parent: Path, name: str, toml: str) -> Path:
    d = parent / name
    (d / ".flow").mkdir(parents=True)
    (d / ".flow" / "workspace.toml").write_text(toml, encoding="utf-8")
    return d


def _no_global(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(maintainer, "_global_config_path", lambda: tmp_path / "absent.toml")


def test_self_target_marker_is_maintainer(tmp_path, monkeypatch):
    _no_global(monkeypatch, tmp_path)
    repo = _ws(tmp_path, "flow", MARKER)
    assert maintainer.resolve_maintainer_repo(repo) == repo.resolve()
    assert maintainer.is_maintainer(repo) is True


def test_no_marker_no_global_is_user(tmp_path, monkeypatch):
    _no_global(monkeypatch, tmp_path)
    repo = _ws(tmp_path, "proj", NOMARKER)
    assert maintainer.resolve_maintainer_repo(repo) is None
    assert maintainer.is_maintainer(repo) is False


def test_missing_workspace_toml_is_user(tmp_path, monkeypatch):
    _no_global(monkeypatch, tmp_path)
    assert maintainer.resolve_maintainer_repo(tmp_path / "nope") is None


def test_malformed_workspace_toml_does_not_crash(tmp_path, monkeypatch):
    _no_global(monkeypatch, tmp_path)
    d = tmp_path / "broken"
    (d / ".flow").mkdir(parents=True)
    (d / ".flow" / "workspace.toml").write_text("not = = toml", encoding="utf-8")
    assert maintainer.resolve_maintainer_repo(d) is None


def test_global_pointer_to_marked_repo(tmp_path, monkeypatch):
    flow_repo = _ws(tmp_path, "flow", MARKER)
    other = _ws(tmp_path, "other", NOMARKER)
    gconf = tmp_path / "config.toml"
    gconf.write_text(f'[maintainer]\nrepo_root = "{flow_repo}"\n', encoding="utf-8")
    monkeypatch.setattr(maintainer, "_global_config_path", lambda: gconf)
    assert maintainer.resolve_maintainer_repo(other) == flow_repo.resolve()


def test_global_pointer_to_unmarked_repo_rejected(tmp_path, monkeypatch):
    target = _ws(tmp_path, "target", NOMARKER)  # pointer target lacks the marker
    other = _ws(tmp_path, "other", NOMARKER)
    gconf = tmp_path / "config.toml"
    gconf.write_text(f'[maintainer]\nrepo_root = "{target}"\n', encoding="utf-8")
    monkeypatch.setattr(maintainer, "_global_config_path", lambda: gconf)
    assert maintainer.resolve_maintainer_repo(other) is None


def test_cli_maintainer_exit_0(tmp_path, monkeypatch, capsys):
    _no_global(monkeypatch, tmp_path)
    repo = _ws(tmp_path, "flow", MARKER)
    rc = maintainer.cli_main(["--workspace-root", str(repo)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == str(repo.resolve())


def test_cli_user_exit_1(tmp_path, monkeypatch, capsys):
    _no_global(monkeypatch, tmp_path)
    repo = _ws(tmp_path, "proj", NOMARKER)
    rc = maintainer.cli_main(["--workspace-root", str(repo)])
    assert rc == 1
    assert capsys.readouterr().out.strip() == ""
