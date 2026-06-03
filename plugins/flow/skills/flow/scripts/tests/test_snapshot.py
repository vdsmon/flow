"""Contract tests for snapshot.py — TOCTOU run snapshot emit + verify.

Covers: emit then verify match; workspace.toml edit -> drift names workspace_toml;
stage-registry edit -> drift names stage_registry; no snapshot -> (True, absent);
skill-handler plugin file change -> drift via plugin tree hash.
"""

from __future__ import annotations

from pathlib import Path

import snapshot

# ─── Fixtures ────────────────────────────────────────────────────────────────


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _stage_registry_text() -> str:
    return """[[stage]]
name = "create_pr"
default_handler = "none"
"""


def _bare_workspace_text() -> str:
    return """[pipeline]
stages = ["create_pr"]

[pipeline.handlers]
create_pr = "inline"
"""


def _skill_workspace_text() -> str:
    return """[pipeline]
stages = ["create_pr"]

[pipeline.handlers]
create_pr = "skill:ship-it:create"
"""


def _manifest_text(bundle_name: str = "ship-it") -> str:
    return f"""schema_version = 1

[bundle]
name = "{bundle_name}"
description = "Push branch + open PR + wait on CI"

[skills.create_pr]
handler_string = "skill:{bundle_name}:create"
required_capabilities = []
required_outputs = ["pr_url"]
side_effects = ["git push"]
stage_compatibility = ["create_pr"]
"""


def _make_skill_root(tmp_path: Path) -> Path:
    skill_root = tmp_path / "skill_root"
    _write(snapshot.stage_registry_path(skill_root), _stage_registry_text())
    return skill_root


def _make_workspace(tmp_path: Path, workspace_text: str) -> Path:
    workspace_root = tmp_path / "workspace"
    _write(workspace_root / ".flow" / "workspace.toml", workspace_text)
    return workspace_root


def _make_plugin(tmp_path: Path, bundle_name: str = "ship-it") -> tuple[Path, Path]:
    """Build a fake plugin dir holding a manifest + one tracked .py file.

    Returns (plugin_parent, plugin_dir). plugin_parent is the search_root that
    bundle_discover walks; plugin_dir is the manifest's parent (the plugin_root
    the tree hash covers).
    """
    plugin_parent = tmp_path / "plugins"
    plugin_dir = plugin_parent / bundle_name
    _write(plugin_dir / ".flow-bundle.toml", _manifest_text(bundle_name))
    _write(plugin_dir / "handler.py", "def run():\n    return 0\n")
    return plugin_parent, plugin_dir


# ─── Tests ────────────────────────────────────────────────────────────────────


def test_emit_then_verify_match_bare(tmp_path: Path) -> None:
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())

    json_path = snapshot.write_snapshot(workspace_root, "FT-1", skill_root=skill_root)
    assert json_path == snapshot.snapshot_json_path(workspace_root, "FT-1")
    assert json_path.exists()
    assert snapshot.snapshot_sha_path(workspace_root, "FT-1").exists()

    ok, detail = snapshot.verify_snapshot(workspace_root, "FT-1", skill_root=skill_root)
    assert ok is True
    assert detail == "match"


def test_bare_workspace_has_empty_handlers(tmp_path: Path) -> None:
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    snap = snapshot.compute_snapshot(workspace_root, skill_root=skill_root)
    assert snap["handlers"] == {}
    assert "workspace_toml" in snap
    assert "stage_registry" in snap
    assert "master_hash" in snap


def test_workspace_edit_drift_names_workspace_toml(tmp_path: Path) -> None:
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    snapshot.write_snapshot(workspace_root, "FT-1", skill_root=skill_root)

    _write(
        workspace_root / ".flow" / "workspace.toml",
        _bare_workspace_text() + "\n# user edit\n",
    )

    ok, detail = snapshot.verify_snapshot(workspace_root, "FT-1", skill_root=skill_root)
    assert ok is False
    assert "workspace_toml" in detail


def test_stage_registry_edit_drift_names_stage_registry(tmp_path: Path) -> None:
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    snapshot.write_snapshot(workspace_root, "FT-1", skill_root=skill_root)

    _write(
        snapshot.stage_registry_path(skill_root),
        _stage_registry_text() + '\n[[stage]]\nname = "plan"\n',
    )

    ok, detail = snapshot.verify_snapshot(workspace_root, "FT-1", skill_root=skill_root)
    assert ok is False
    assert "stage_registry" in detail


def test_verify_with_no_snapshot_is_absent(tmp_path: Path) -> None:
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    ok, detail = snapshot.verify_snapshot(workspace_root, "FT-1", skill_root=skill_root)
    assert ok is True
    assert "no snapshot" in detail


def test_skill_handler_match(tmp_path: Path) -> None:
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _skill_workspace_text())
    plugin_parent, _ = _make_plugin(tmp_path)

    snap = snapshot.compute_snapshot(
        workspace_root, skill_root=skill_root, search_roots=[plugin_parent]
    )
    assert "create_pr" in snap["handlers"]
    assert snap["handlers"]["create_pr"]["manifest"]
    assert snap["handlers"]["create_pr"]["tree_hash"]

    snapshot.write_snapshot(
        workspace_root, "FT-1", skill_root=skill_root, search_roots=[plugin_parent]
    )
    ok, detail = snapshot.verify_snapshot(
        workspace_root, "FT-1", skill_root=skill_root, search_roots=[plugin_parent]
    )
    assert ok is True
    assert detail == "match"


def test_skill_handler_plugin_file_change_drift(tmp_path: Path) -> None:
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _skill_workspace_text())
    plugin_parent, plugin_dir = _make_plugin(tmp_path)

    snapshot.write_snapshot(
        workspace_root, "FT-1", skill_root=skill_root, search_roots=[plugin_parent]
    )

    _write(plugin_dir / "handler.py", "def run():\n    return 1\n")

    ok, detail = snapshot.verify_snapshot(
        workspace_root, "FT-1", skill_root=skill_root, search_roots=[plugin_parent]
    )
    assert ok is False
    assert "handler create_pr" in detail


def test_cli_emit_then_verify(tmp_path: Path) -> None:
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())

    rc = snapshot.cli_main(
        [
            "emit",
            "--ticket",
            "FT-1",
            "--workspace-root",
            str(workspace_root),
            "--skill-root",
            str(skill_root),
        ]
    )
    assert rc == 0

    rc = snapshot.cli_main(
        [
            "verify",
            "--ticket",
            "FT-1",
            "--workspace-root",
            str(workspace_root),
            "--skill-root",
            str(skill_root),
        ]
    )
    assert rc == 0


def test_cli_verify_drift_exit_1(tmp_path: Path) -> None:
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    snapshot.write_snapshot(workspace_root, "FT-1", skill_root=skill_root)

    _write(
        workspace_root / ".flow" / "workspace.toml",
        _bare_workspace_text() + "\n# edit\n",
    )

    rc = snapshot.cli_main(
        [
            "verify",
            "--ticket",
            "FT-1",
            "--workspace-root",
            str(workspace_root),
            "--skill-root",
            str(skill_root),
        ]
    )
    assert rc == 1
