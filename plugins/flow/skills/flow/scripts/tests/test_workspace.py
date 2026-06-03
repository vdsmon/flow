import pytest

from _workspace import WorkspaceConfigError, load_workspace_toml


def test_load_missing_raises(tmp_path):
    with pytest.raises(WorkspaceConfigError, match=r"no workspace\.toml"):
        load_workspace_toml(tmp_path)


def test_load_parse_error(tmp_path):
    flow = tmp_path / ".flow"
    flow.mkdir()
    (flow / "workspace.toml").write_text("not = = toml", encoding="utf-8")
    with pytest.raises(WorkspaceConfigError, match=r"does not parse"):
        load_workspace_toml(tmp_path)


def test_load_ok(tmp_path):
    flow = tmp_path / ".flow"
    flow.mkdir()
    (flow / "workspace.toml").write_text('[tracker]\nbackend = "beads"\n', encoding="utf-8")
    data = load_workspace_toml(tmp_path)
    assert data["tracker"]["backend"] == "beads"
