from __future__ import annotations

import pytest

from forge import ForgeConfigError, make_forge, read_forge_config


def _write_ws(tmp_path, body: str):
    (tmp_path / ".flow").mkdir()
    (tmp_path / ".flow" / "workspace.toml").write_text(body, encoding="utf-8")


def test_make_forge_missing_backend():
    with pytest.raises(ForgeConfigError):
        make_forge({})


def test_make_forge_unknown_backend():
    with pytest.raises(ForgeConfigError):
        make_forge({"backend": "gitlab"})


def test_make_forge_github():
    from forge_github import GitHubAdapter

    f = make_forge({"backend": "github", "workspace_root": "."})
    assert isinstance(f, GitHubAdapter)
    assert f.backend == "github"


def test_make_forge_bitbucket():
    from forge_bitbucket import BitbucketAdapter

    f = make_forge(
        {"backend": "bitbucket", "workspace": "ws", "repo_slug": "rs", "workspace_root": "."}
    )
    assert isinstance(f, BitbucketAdapter)


def test_read_forge_config_absent_returns_none(tmp_path):
    _write_ws(tmp_path, '[tracker]\nbackend = "beads"\n[tracker.beads]\nprefix = "flow"\n')
    assert read_forge_config(tmp_path) is None


def test_read_forge_config_github_flattens(tmp_path):
    _write_ws(tmp_path, '[forge]\nbackend = "github"\n[forge.github]\n')
    cfg = read_forge_config(tmp_path)
    assert cfg is not None
    assert cfg["backend"] == "github"
    assert cfg["workspace_root"] == str(tmp_path)


def test_read_forge_config_bitbucket_flattens_subblock(tmp_path):
    _write_ws(
        tmp_path,
        '[forge]\nbackend = "bitbucket"\n[forge.bitbucket]\nworkspace = "ws"\nrepo_slug = "rs"\n',
    )
    cfg = read_forge_config(tmp_path)
    assert cfg is not None
    assert cfg["backend"] == "bitbucket"
    assert cfg["workspace"] == "ws"
    assert cfg["repo_slug"] == "rs"


def test_read_forge_config_unknown_backend_raises(tmp_path):
    _write_ws(tmp_path, '[forge]\nbackend = "gitlab"\n')
    with pytest.raises(ForgeConfigError):
        read_forge_config(tmp_path)
