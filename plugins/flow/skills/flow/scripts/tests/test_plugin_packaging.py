"""Cross-harness plugin packaging and version-lockstep contracts."""

from __future__ import annotations

import json
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[4]
REPO_ROOT = Path(__file__).resolve().parents[6]
CLAUDE_MANIFEST = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
CODEX_MANIFEST = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
CLAUDE_MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
CODEX_MARKETPLACE = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
VERSION_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "version-stamp.yml"
ROOT_GITIGNORE = REPO_ROOT / ".gitignore"
CODEX_SKILL_FRONTMATTER_KEYS = {
    "allowed-tools",
    "description",
    "license",
    "metadata",
    "name",
}


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _frontmatter_keys(path: Path) -> set[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---"
    end = lines.index("---", 1)
    return {line.split(":", 1)[0] for line in lines[1:end] if ":" in line}


def test_claude_and_codex_manifest_versions_are_locked() -> None:
    claude = _json(CLAUDE_MANIFEST)
    codex = _json(CODEX_MANIFEST)
    claude_marketplace = _json(CLAUDE_MARKETPLACE)
    marketplace_flow = next(
        plugin for plugin in claude_marketplace["plugins"] if plugin["name"] == "flow"
    )

    assert claude["version"] == codex["version"] == marketplace_flow["version"]


def test_codex_manifest_discovers_shared_skills_without_claude_hooks() -> None:
    manifest = _json(CODEX_MANIFEST)

    assert manifest["name"] == PLUGIN_ROOT.name == "flow"
    assert manifest["skills"] == "./skills/"
    assert "hooks" not in manifest
    assert "apps" not in manifest
    assert "mcpServers" not in manifest
    assert (PLUGIN_ROOT / "skills" / "flow" / "SKILL.md").is_file()
    assert (PLUGIN_ROOT / "skills" / "bd" / "SKILL.md").is_file()
    assert manifest["interface"]["category"] == "Productivity"
    assert manifest["interface"]["capabilities"] == ["Interactive", "Read", "Write"]


def test_shared_skills_use_frontmatter_accepted_by_codex() -> None:
    for skill in ("flow", "bd"):
        keys = _frontmatter_keys(PLUGIN_ROOT / "skills" / skill / "SKILL.md")
        assert keys <= CODEX_SKILL_FRONTMATTER_KEYS


def test_repository_codex_marketplace_contract() -> None:
    marketplace = _json(CODEX_MARKETPLACE)

    assert marketplace["name"] == "vdsmon-flow"
    assert marketplace["interface"] == {"displayName": "Flow"}
    assert marketplace["plugins"] == [
        {
            "name": "flow",
            "source": {"source": "local", "path": "./plugins/flow"},
            "policy": {
                "installation": "AVAILABLE",
                "authentication": "ON_INSTALL",
            },
            "category": "Productivity",
        }
    ]


def test_version_workflow_stamps_and_commits_codex_manifest() -> None:
    workflow = VERSION_WORKFLOW.read_text(encoding="utf-8")
    codex_manifest = "plugins/flow/.codex-plugin/plugin.json"

    # The stamp clean-diff probe and commit pathspec include Codex. The guard
    # compares the canonical Claude manifest's version value, so adding/changing
    # manifest metadata does not suppress a post-merge version bump.
    assert workflow.count(codex_manifest) == 2
    assert "base_version=" in workflow
    assert "head_version=" in workflow
    assert '[ "$base_version" != "$head_version" ]' in workflow


def test_repository_ignores_generated_workspace_launcher_files() -> None:
    lines = ROOT_GITIGNORE.read_text(encoding="utf-8").splitlines()
    assert "**/.flow/runtime/" in lines
    assert "**/.flow/memory/" in lines
