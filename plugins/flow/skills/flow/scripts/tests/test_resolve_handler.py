"""Contract tests for resolve_handler.py.

Covers handler classification (inline/none/subagent/unknown) and the skill path:
present+valid -> installed, plugin_root set; absent -> exit 1; present but
manifest invalid -> manifest_valid False, exit 2.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import resolve_handler as rh


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
"""


def _invalid_manifest_text() -> str:
    return "schema_version = 1\n[bundle]\n# missing name\n"


# ─── Classification (no discovery) ─────────────────────────────────────────────


def test_inline_classifies_installed() -> None:
    res = rh.resolve("inline")
    assert res.handler_type == "inline"
    assert res.installed is True
    assert rh._exit_code(res) == 0


def test_none_classifies_installed() -> None:
    res = rh.resolve("none")
    assert res.handler_type == "none"
    assert res.installed is True
    assert rh._exit_code(res) == 0


def test_subagent_classifies_installed() -> None:
    res = rh.resolve("subagent:code-explorer")
    assert res.handler_type == "subagent"
    assert res.subagent_type == "code-explorer"
    assert res.installed is True
    assert res.invocation == "subagent:code-explorer"
    assert rh._exit_code(res) == 0


def test_unknown_handler_is_error_exit_3() -> None:
    res = rh.resolve("garbage")
    assert res.handler_type == "unknown"
    assert res.error is not None
    assert rh._exit_code(res) == 3


def test_empty_subagent_type_is_unknown() -> None:
    res = rh.resolve("subagent:")
    assert res.handler_type == "unknown"
    assert rh._exit_code(res) == 3


def test_empty_skill_name_is_unknown() -> None:
    res = rh.resolve("skill:")
    assert res.handler_type == "unknown"
    assert rh._exit_code(res) == 3


# ─── Skill discovery ───────────────────────────────────────────────────────────


def test_skill_present_and_valid(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "ship-it", _full_manifest_text())
    res = rh.resolve("skill:ship-it:create", search_roots=[tmp_path])
    assert res.handler_type == "skill"
    assert res.skill_name == "ship-it"
    assert res.skill_args == "create"
    assert res.installed is True
    assert res.manifest_valid is True
    assert res.invocation == "skill:ship-it:create"
    assert res.plugin_root == str(tmp_path / "ship-it")
    assert rh._exit_code(res) == 0


def test_skill_no_args_yields_empty_string(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "ship-it", _full_manifest_text())
    res = rh.resolve("skill:ship-it", search_roots=[tmp_path])
    assert res.installed is True
    assert res.skill_args == ""


def test_skill_multi_segment_args_preserved(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "ship-it", _full_manifest_text())
    res = rh.resolve("skill:ship-it:create:extra", search_roots=[tmp_path])
    assert res.skill_args == "create:extra"


def test_skill_absent_not_installed_exit_1(tmp_path: Path) -> None:
    res = rh.resolve("skill:ghost", search_roots=[tmp_path])
    assert res.handler_type == "skill"
    assert res.installed is False
    assert res.manifest_valid is False
    assert res.error == "handler skill:ghost not installed"
    assert rh._exit_code(res) == 1


def test_skill_present_but_manifest_invalid_exit_2(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "ship-it", _invalid_manifest_text())
    res = rh.resolve("skill:ship-it", search_roots=[tmp_path])
    assert res.handler_type == "skill"
    assert res.installed is True
    assert res.manifest_valid is False
    assert res.error is not None
    assert res.plugin_root == str(tmp_path / "ship-it")
    assert rh._exit_code(res) == 2


def test_skill_name_prefix_does_not_false_match(tmp_path: Path) -> None:
    # bundle "ship-it" must not satisfy a request for skill "ship".
    _write_manifest(tmp_path / "ship-it", _full_manifest_text())
    res = rh.resolve("skill:ship", search_roots=[tmp_path])
    assert res.installed is False


def test_broken_unrelated_bundle_does_not_false_match(tmp_path: Path) -> None:
    # broken "code-review" bundle must not be reported as the missing "review".
    _write_manifest(tmp_path / "code-review", _invalid_manifest_text())
    res = rh.resolve("skill:review", search_roots=[tmp_path])
    assert res.installed is False
    assert rh._exit_code(res) == 1


# ─── CLI ───────────────────────────────────────────────────────────────────────


def test_cli_emits_json_and_exit_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_manifest(tmp_path / "ship-it", _full_manifest_text())
    rc = rh.cli_main(["--handler", "skill:ship-it:create", "--search-roots", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["handler_type"] == "skill"
    assert payload["installed"] is True
    assert payload["plugin_root"] == str(tmp_path / "ship-it")


def test_cli_not_installed_exit_1(tmp_path: Path) -> None:
    rc = rh.cli_main(["--handler", "skill:ghost", "--search-roots", str(tmp_path)])
    assert rc == 1


def test_cli_manifest_invalid_exit_2(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "ship-it", _invalid_manifest_text())
    rc = rh.cli_main(["--handler", "skill:ship-it", "--search-roots", str(tmp_path)])
    assert rc == 2


def test_cli_unknown_handler_exit_3() -> None:
    rc = rh.cli_main(["--handler", "garbage"])
    assert rc == 3
