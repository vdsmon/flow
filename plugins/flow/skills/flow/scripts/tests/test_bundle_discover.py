"""Contract tests for bundle_discover.py: manifest discovery + validation.

Covers: zero manifests, partial bundle, full bundle, invalid unrelated manifest
(warning-only), invalid SELECTED manifest (exit 2), duplicate-provider conflict,
env override search roots.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import bundle_discover as bd

# ─── Fixtures ────────────────────────────────────────────────────────────────


def _write_manifest(plugin_dir: Path, content: str) -> Path:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    path = plugin_dir / ".flow-bundle.toml"
    path.write_text(content, encoding="utf-8")
    return path


def _full_manifest_text(bundle_name: str = "ship-it") -> str:
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

[skills.review_loop]
handler_string = "skill:{bundle_name}:feedback"
required_capabilities = []
required_outputs = []
side_effects = []
stage_compatibility = ["review_loop"]
"""


def _partial_manifest_text() -> str:
    return """schema_version = 1

[bundle]
name = "code-review"
description = "Reviews own diff"

[skills.code_review]
handler_string = "skill:code-review"
"""


# ─── Tests ────────────────────────────────────────────────────────────────────


def test_zero_manifests(tmp_path: Path) -> None:
    result = bd.discover(roots=[tmp_path])
    assert result.valid == []
    assert result.invalid == []
    assert result.duplicates == []


def test_partial_manifest_valid(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "code-review", _partial_manifest_text())
    result = bd.discover(roots=[tmp_path])
    assert len(result.valid) == 1
    manifest = result.valid[0]
    assert manifest.bundle_name == "code-review"
    assert len(manifest.skills) == 1
    assert manifest.skills[0].stage == "code_review"
    assert manifest.skills[0].handler_string == "skill:code-review"


def test_full_manifest_valid(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "ship-it", _full_manifest_text())
    result = bd.discover(roots=[tmp_path])
    assert len(result.valid) == 1
    assert len(result.invalid) == 0
    stages = {s.stage for s in result.valid[0].skills}
    assert stages == {"create_pr", "review_loop"}


def test_invalid_unrelated_manifest_is_warning_not_error(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "ship-it", _full_manifest_text())
    _write_manifest(
        tmp_path / "broken-third-party",
        "schema_version = 1\n[bundle]\n# missing name\n",
    )
    result = bd.discover(roots=[tmp_path])
    assert len(result.valid) == 1
    assert len(result.invalid) == 1
    assert "broken-third-party" in result.invalid[0].path


def test_invalid_selected_manifest_returns_exit_2(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path / "broken-ship-it",
        "schema_version = 1\n[bundle]\n# missing name\n",
    )
    rc = bd.cli_main(["--roots", str(tmp_path), "--select", "broken-ship-it"])
    assert rc == 2


def test_valid_selected_manifest_returns_exit_0(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "ship-it", _full_manifest_text())
    rc = bd.cli_main(["--roots", str(tmp_path), "--select", "ship-it"])
    assert rc == 0


def test_select_nonexistent_bundle_returns_exit_2(tmp_path: Path) -> None:
    rc = bd.cli_main(["--roots", str(tmp_path), "--select", "ghost"])
    assert rc == 2


def test_duplicate_provider_surfaced(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "ship-it", _full_manifest_text("ship-it"))
    _write_manifest(tmp_path / "other-pr", _full_manifest_text("other-pr"))
    result = bd.discover(roots=[tmp_path])
    assert len(result.valid) == 2
    stages_with_dupes = {d.stage for d in result.duplicates}
    assert stages_with_dupes == {"create_pr", "review_loop"}
    # Bundle names sorted for determinism.
    for dup in result.duplicates:
        assert dup.bundle_names == sorted(dup.bundle_names)


def test_schema_version_wrong_rejected(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path / "stale",
        'schema_version = 2\n[bundle]\nname = "stale"\ndescription = ""\n',
    )
    result = bd.discover(roots=[tmp_path])
    assert result.valid == []
    assert len(result.invalid) == 1
    assert "schema_version" in result.invalid[0].reason


def test_unknown_stage_rejected(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path / "weird",
        """schema_version = 1

[bundle]
name = "weird"
description = ""

[skills.deploy]
handler_string = "skill:weird:run"
""",
    )
    result = bd.discover(roots=[tmp_path])
    assert result.valid == []
    assert "not a registered flow stage" in result.invalid[0].reason


def test_merge_stage_accepted(tmp_path: Path) -> None:
    # `merge` is registered in stage-registry.toml; a bundle providing it must
    # not be rejected as an unknown stage.
    _write_manifest(
        tmp_path / "auto-merge",
        """schema_version = 1

[bundle]
name = "auto-merge"
description = "Self-merge green PRs"

[skills.merge]
handler_string = "skill:auto-merge:merge"
""",
    )
    result = bd.discover(roots=[tmp_path])
    assert result.invalid == []
    assert result.valid[0].skills[0].stage == "merge"


def test_known_stages_match_stage_registry() -> None:
    # _KNOWN_STAGES hand-copies the registry's closed vocabulary; a stage
    # registered there but missing here gets its manifests falsely rejected.
    import tomllib

    registry = Path(bd.__file__).resolve().parent.parent / "stage-registry.toml"
    data = tomllib.loads(registry.read_text(encoding="utf-8"))
    assert {s["name"] for s in data["stage"]} == bd._KNOWN_STAGES


def test_handler_string_must_start_with_skill_prefix(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path / "broken-handler",
        """schema_version = 1

[bundle]
name = "broken-handler"
description = ""

[skills.create_pr]
handler_string = "inline"
""",
    )
    result = bd.discover(roots=[tmp_path])
    assert result.valid == []
    assert "handler_string" in result.invalid[0].reason


@pytest.mark.parametrize("handler", ["skill:", "skill::args"])
def test_handler_string_empty_skill_name_rejected(tmp_path: Path, handler: str) -> None:
    _write_manifest(
        tmp_path / "empty-name",
        f"""schema_version = 1

[bundle]
name = "empty-name"
description = ""

[skills.create_pr]
handler_string = "{handler}"
""",
    )
    result = bd.discover(roots=[tmp_path])
    assert result.valid == []
    assert len(result.invalid) == 1
    assert "non-empty skill name" in result.invalid[0].reason


@pytest.mark.parametrize(
    "field",
    ["required_capabilities", "required_outputs", "side_effects", "stage_compatibility"],
)
def test_list_field_not_a_list_rejected(tmp_path: Path, field: str) -> None:
    _write_manifest(
        tmp_path / "bad-list",
        f"""schema_version = 1

[bundle]
name = "bad-list"
description = ""

[skills.create_pr]
handler_string = "skill:bad-list:create"
{field} = "pr_url"
""",
    )
    result = bd.discover(roots=[tmp_path])
    assert result.valid == []
    assert len(result.invalid) == 1
    assert "must be list[str]" in result.invalid[0].reason


def test_list_field_with_non_str_element_rejected(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path / "bad-element",
        """schema_version = 1

[bundle]
name = "bad-element"
description = ""

[skills.create_pr]
handler_string = "skill:bad-element:create"
side_effects = [123]
""",
    )
    result = bd.discover(roots=[tmp_path])
    assert result.valid == []
    assert len(result.invalid) == 1
    assert "must be list[str]" in result.invalid[0].reason


def test_args_schema_not_a_table_rejected(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path / "bad-schema",
        """schema_version = 1

[bundle]
name = "bad-schema"
description = ""

[skills.create_pr]
handler_string = "skill:bad-schema:create"
args_schema = "nope"
""",
    )
    result = bd.discover(roots=[tmp_path])
    assert result.valid == []
    assert len(result.invalid) == 1
    assert "must be a table" in result.invalid[0].reason


def test_env_override_search_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_root = tmp_path / "custom_root"
    _write_manifest(plugin_root / "ship-it", _full_manifest_text())
    monkeypatch.setenv("FLOW_BUNDLE_SEARCH_ROOTS", str(plugin_root))
    roots = bd.default_search_roots()
    assert roots == [plugin_root]


def test_codex_harness_uses_codex_home_and_excludes_claude_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    codex_home = tmp_path / "custom codex"
    repo = tmp_path / "repo"
    _write_manifest(
        home / ".claude" / "plugins" / "claude-only",
        _full_manifest_text("claude-only"),
    )
    _write_manifest(
        codex_home / "plugins" / "cache" / "team" / "codex-only" / "1.0.0",
        _full_manifest_text("codex-only"),
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    monkeypatch.delenv("FLOW_BUNDLE_SEARCH_ROOTS", raising=False)

    result = bd.discover(repo_root=repo)

    assert {manifest.bundle_name for manifest in result.valid} == {"codex-only"}
    assert home / ".claude" / "plugins" not in bd.default_search_roots(repo_root=repo)


def test_codex_harness_does_not_select_repo_source_as_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / "codex"
    repo = tmp_path / "repo"
    _write_manifest(repo / "plugins" / "source-only", _full_manifest_text("source-only"))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    monkeypatch.delenv("FLOW_BUNDLE_SEARCH_ROOTS", raising=False)

    result = bd.discover(repo_root=repo)

    assert result.valid == []
    assert bd.default_search_roots(repo_root=repo) == [codex_home / "plugins"]


def test_claude_harness_excludes_codex_plugins_on_dual_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    codex_home = tmp_path / "custom codex"
    repo = tmp_path / "repo"
    _write_manifest(
        home / ".claude" / "plugins" / "claude-only",
        _full_manifest_text("claude-only"),
    )
    _write_manifest(
        codex_home / "plugins" / "cache" / "team" / "codex-only" / "1.0.0",
        _full_manifest_text("codex-only"),
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("FLOW_HARNESS", "claude-code")
    monkeypatch.delenv("FLOW_BUNDLE_SEARCH_ROOTS", raising=False)

    result = bd.discover(repo_root=repo)

    assert {manifest.bundle_name for manifest in result.valid} == {"claude-only"}


def test_unset_harness_preserves_claude_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FLOW_HARNESS", raising=False)
    monkeypatch.delenv("FLOW_BUNDLE_SEARCH_ROOTS", raising=False)

    assert bd.default_search_roots(repo_root=repo) == [
        home / ".claude" / "plugins",
        repo / ".claude" / "plugins",
    ]


def test_claude_harness_respects_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "custom claude"
    repo = tmp_path / "repo"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("FLOW_HARNESS", "claude-code")
    monkeypatch.delenv("FLOW_BUNDLE_SEARCH_ROOTS", raising=False)

    assert bd.default_search_roots(repo_root=repo) == [
        config_dir / "plugins",
        repo / ".claude" / "plugins",
    ]


def test_generic_harness_has_no_native_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    _write_manifest(home / ".claude" / "plugins" / "claude", _full_manifest_text("claude"))
    _write_manifest(home / ".codex" / "plugins" / "codex", _full_manifest_text("codex"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FLOW_HARNESS", "generic")
    monkeypatch.delenv("FLOW_BUNDLE_SEARCH_ROOTS", raising=False)

    assert bd.default_search_roots(repo_root=repo) == []
    assert bd.discover(repo_root=repo).valid == []


def test_unknown_flow_harness_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLOW_HARNESS", "mystery-host")
    monkeypatch.delenv("FLOW_BUNDLE_SEARCH_ROOTS", raising=False)
    with pytest.raises(ValueError, match=r"FLOW_HARNESS.*codex.*claude-code.*generic"):
        bd.default_search_roots()


def test_unknown_flow_harness_fails_with_explicit_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLOW_HARNESS", "mystery-host")
    with pytest.raises(ValueError, match=r"FLOW_HARNESS.*codex.*claude-code.*generic"):
        bd.discover(roots=[tmp_path])


def test_cli_emits_json_payload(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_manifest(tmp_path / "ship-it", _full_manifest_text())
    rc = bd.cli_main(["--roots", str(tmp_path)])
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 1
    assert payload["valid"][0]["bundle_name"] == "ship-it"
    assert payload["invalid"] == []
    assert payload["duplicates"] == []


def test_malformed_toml_is_invalid_not_crash(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "broken-toml", "this is not [ valid toml")
    result = bd.discover(roots=[tmp_path])
    assert result.valid == []
    assert len(result.invalid) == 1
    assert "TOML parse failed" in result.invalid[0].reason


def test_select_bundle_helper() -> None:
    manifest = bd.Manifest(path="/x", bundle_name="ship-it", bundle_description="", skills=[])
    result = bd.DiscoveryResult(valid=[manifest])
    assert bd.select_bundle(result, "ship-it") is manifest
    assert bd.select_bundle(result, "missing") is None
