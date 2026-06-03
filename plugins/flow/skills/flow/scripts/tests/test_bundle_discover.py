"""Contract tests for bundle_discover.py — manifest discovery + validation.

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
    # Valid manifest still discovered; broken one in invalid list.
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


def test_env_override_search_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_root = tmp_path / "custom_root"
    _write_manifest(plugin_root / "ship-it", _full_manifest_text())
    monkeypatch.setenv("FLOW_BUNDLE_SEARCH_ROOTS", str(plugin_root))
    roots = bd.default_search_roots()
    assert roots == [plugin_root]


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
