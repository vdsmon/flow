"""Tests for the generated workspace-local Flow launcher."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

import _atomicio
import flow_launcher


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace with spaces"
    (root / ".flow").mkdir(parents=True)
    (root / ".flow" / "workspace.toml").write_text(
        '[tracker]\n[memory]\nnamespace = "demo"\n', encoding="utf-8"
    )
    return root


def _fixture_skill(tmp_path: Path, script_body: str) -> Path:
    skill = tmp_path / "skill install with spaces"
    scripts = skill / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "flowctl.py").write_text(
        Path(flow_launcher.__file__).with_name("flowctl.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (scripts / "bundle_discover.py").write_text(
        Path(flow_launcher.__file__).with_name("bundle_discover.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (scripts / "status.py").write_text(script_body, encoding="utf-8")
    return skill


def test_install_is_idempotent_and_executable(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    skill = _fixture_skill(tmp_path, "")
    _, shim = flow_launcher.install(root, skill_dir=skill)
    first = shim.read_bytes()
    flow_launcher.install(root, skill_dir=skill)
    assert shim.read_bytes() == first
    assert stat.S_IMODE(shim.stat().st_mode) == 0o755
    assert (root / ".flow" / "runtime" / "skill-root").read_text(encoding="utf-8").strip() == str(
        skill.resolve()
    )


def test_install_uses_atomic_replacement_for_both_files(tmp_path: Path, monkeypatch) -> None:
    root = _workspace(tmp_path)
    skill = _fixture_skill(tmp_path, "")
    replaced: list[Path] = []
    real_replace = _atomicio.os.replace

    def recording_replace(source: Path, destination: Path) -> None:
        replaced.append(Path(destination))
        real_replace(source, destination)

    monkeypatch.setattr(_atomicio.os, "replace", recording_replace)
    flow_launcher.install(root, skill_dir=skill)
    assert replaced == [
        root / ".flow" / "runtime" / "memory-root",
        root / ".flow" / "runtime" / "layout-version",
        root / ".flow" / "runtime" / "skill-root",
        root / ".flow" / "runtime" / "flow",
    ]


@pytest.mark.parametrize("harness", [None, "claude-code"])
def test_stabilize_skill_dir_rewrites_claude_cache_to_marketplace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str | None
) -> None:
    if harness is None:
        monkeypatch.delenv("FLOW_HARNESS", raising=False)
    else:
        monkeypatch.setenv("FLOW_HARNESS", harness)
    marketplace = tmp_path / "plugins" / "marketplaces" / "vdsmon-flow"
    (marketplace / ".claude-plugin").mkdir(parents=True)
    (marketplace / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"plugins": [{"name": "flow", "source": "./plugins/flow"}]}),
        encoding="utf-8",
    )
    target = marketplace / "plugins" / "flow" / "skills" / "flow"
    target.mkdir(parents=True)
    cache = tmp_path / "plugins" / "cache" / "vdsmon-flow" / "flow" / "1.2.3" / "skills" / "flow"
    assert flow_launcher.stabilize_skill_dir(str(cache)) == str(target)


def test_stabilize_skill_dir_fails_safe(tmp_path: Path) -> None:
    plain = "/opt/flow/skills/flow"
    assert flow_launcher.stabilize_skill_dir(plain) == plain
    cache = tmp_path / "plugins" / "cache" / "vdsmon-flow" / "flow" / "1.2.3" / "skills" / "flow"
    assert flow_launcher.stabilize_skill_dir(str(cache)) == str(cache)


def _codex_marketplace_skill(tmp_path: Path) -> tuple[Path, Path, Path]:
    codex_home = tmp_path / "codex home"
    marketplace = tmp_path / "marketplace source"
    target = marketplace / "plugins" / "flow" / "skills" / "flow"
    source = _fixture_skill(tmp_path / "source fixture", "")
    target.parent.mkdir(parents=True)
    source.rename(target)
    (marketplace / ".agents" / "plugins").mkdir(parents=True)
    (marketplace / ".agents" / "plugins" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "vdsmon-flow",
                "plugins": [
                    {
                        "name": "flow",
                        "source": {"source": "local", "path": "./plugins/flow"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text(
        f'[marketplaces.vdsmon-flow]\nsource_type = "local"\nsource = "{marketplace}"\n',
        encoding="utf-8",
    )
    cache = codex_home / "plugins" / "cache" / "vdsmon-flow" / "flow" / "1.2.3" / "skills" / "flow"
    cache.mkdir(parents=True)
    return codex_home, cache, target


def test_stabilize_skill_dir_rewrites_codex_cache_to_local_marketplace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    _, cache, target = _codex_marketplace_skill(tmp_path)
    assert flow_launcher.stabilize_skill_dir(str(cache)) == str(target)


def _add_claude_collision(codex_home: Path) -> Path:
    marketplace = codex_home / "plugins" / "marketplaces" / "vdsmon-flow"
    (marketplace / ".claude-plugin").mkdir(parents=True)
    (marketplace / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"plugins": [{"name": "flow", "source": "./claude-flow"}]}),
        encoding="utf-8",
    )
    target = marketplace / "claude-flow" / "skills" / "flow"
    target.mkdir(parents=True)
    return target


def test_claude_harness_does_not_fall_through_to_codex_source_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLOW_HARNESS", "claude-code")
    _, cache, codex_target = _codex_marketplace_skill(tmp_path)

    assert flow_launcher.stabilize_skill_dir(str(cache)) == str(cache)
    assert str(cache) != str(codex_target)


def test_codex_harness_ignores_claude_marketplace_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    codex_home, cache, codex_target = _codex_marketplace_skill(tmp_path)
    claude_target = _add_claude_collision(codex_home)

    assert flow_launcher.stabilize_skill_dir(str(cache)) == str(codex_target)
    assert codex_target != claude_target


def test_generic_harness_does_not_guess_native_marketplace_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLOW_HARNESS", "generic")
    codex_home, cache, _ = _codex_marketplace_skill(tmp_path)
    _add_claude_collision(codex_home)

    assert flow_launcher.stabilize_skill_dir(str(cache)) == str(cache)


def test_codex_cache_upgrade_does_not_break_installed_workspace_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    root = _workspace(tmp_path)
    _, cache, target = _codex_marketplace_skill(tmp_path)
    _, shim = flow_launcher.install(root, skill_dir=cache)
    assert (root / ".flow" / "runtime" / "skill-root").read_text(encoding="utf-8") == str(
        target
    ) + "\n"

    cache.parents[2].rename(cache.parents[2].with_name("removed old version"))
    result = subprocess.run([str(shim), "status"], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_install_rejects_missing_skill_path_clearly(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    missing = tmp_path / "missing skill"
    with pytest.raises(FileNotFoundError, match="Flow skill directory does not exist"):
        flow_launcher.install(root, skill_dir=missing)


def test_executing_skill_dir_ignores_stale_ambient_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executing = _fixture_skill(tmp_path / "executing", "")
    ambient = _fixture_skill(tmp_path / "ambient", "")
    monkeypatch.setattr(flow_launcher, "SKILL_ROOT", executing)
    monkeypatch.setenv("FLOW_SKILL_DIR", str(ambient))
    monkeypatch.setenv("CLAUDE_SKILL_DIR", str(ambient))

    assert flow_launcher.executing_skill_dir() == executing.resolve()


def test_shim_invoked_outside_repo_observes_workspace_and_arguments(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    skill = _fixture_skill(
        tmp_path,
        """import json, os, sys
payload = {
    "cwd": os.getcwd(),
    "flow_skill": os.environ["FLOW_SKILL_DIR"],
    "claude_skill": os.environ["CLAUDE_SKILL_DIR"],
    "args": sys.argv[1:],
}
print(json.dumps(payload))
""",
    )
    _, shim = flow_launcher.install(root, skill_dir=skill)
    outside = tmp_path / "outside"
    outside.mkdir()
    result = subprocess.run(
        [str(shim), "status", "hello world", "--json"],
        cwd=outside,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    observed = json.loads(result.stdout)
    assert observed == {
        "cwd": str(root.resolve()),
        "flow_skill": str(skill.resolve()),
        "claude_skill": str(skill.resolve()),
        "args": ["hello world", "--json"],
    }


def test_codex_style_reset_cwd_keeps_dispatch_sequence_in_workspace(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    skill = _fixture_skill(tmp_path, "")
    (skill / "scripts" / "dispatch_stage.py").write_text(
        """import json, sys
from pathlib import Path

state_path = Path('.flow/codex-sequence.json')
events = json.loads(state_path.read_text()) if state_path.exists() else []
events.append(sys.argv[1])
state_path.write_text(json.dumps(events))
print(json.dumps({'events': events}))
""",
        encoding="utf-8",
    )
    _, shim = flow_launcher.install(root, skill_dir=skill)
    original_checkout = tmp_path / "original checkout"
    (original_checkout / ".flow").mkdir(parents=True)

    for subcommand in ("init", "next", "advance", "release"):
        result = subprocess.run(
            [str(shim), "dispatch", subcommand],
            cwd=original_checkout,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    assert json.loads((root / ".flow" / "codex-sequence.json").read_text()) == [
        "init",
        "next",
        "advance",
        "release",
    ]
    assert not (original_checkout / ".flow" / "codex-sequence.json").exists()


def test_shim_preserves_trailing_space_in_skill_path(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    skill = _fixture_skill(tmp_path / "parent", "").with_name("skill install ")
    skill.parent.mkdir(parents=True, exist_ok=True)
    source = _fixture_skill(tmp_path / "source", "")
    source.rename(skill)
    _, shim = flow_launcher.install(root, skill_dir=skill)

    result = subprocess.run(
        [str(shim), "status"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (root / ".flow" / "runtime" / "skill-root").read_text(encoding="utf-8") == str(
        skill.resolve()
    ) + "\n"


def test_shim_propagates_exact_child_exit_code(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    skill = _fixture_skill(tmp_path, "raise SystemExit(37)\n")
    _, shim = flow_launcher.install(root, skill_dir=skill)
    result = subprocess.run([str(shim), "status"], check=False)
    assert result.returncode == 37


def test_shim_reports_workspace_without_skill_root(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    skill = _fixture_skill(tmp_path, "")
    _, shim = flow_launcher.install(root, skill_dir=skill)
    (root / ".flow" / "runtime" / "skill-root").unlink()
    result = subprocess.run([str(shim), "status"], capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "no .flow/runtime/skill-root" in result.stderr
    assert "workspace setup" in result.stderr


def test_shim_reports_stale_skill_path_without_searching(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    skill = _fixture_skill(tmp_path, "")
    _, shim = flow_launcher.install(root, skill_dir=skill)
    (root / ".flow" / "runtime" / "skill-root").write_text(
        str(tmp_path / "missing skill") + "\n", encoding="utf-8"
    )
    result = subprocess.run([str(shim), "status"], capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "does not exist" in result.stderr
    assert "missing skill" in result.stderr
    assert "workspace setup" in result.stderr


def test_shim_reports_non_utf8_skill_metadata_without_traceback(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    skill = _fixture_skill(tmp_path, "")
    _, shim = flow_launcher.install(root, skill_dir=skill)
    (root / ".flow" / "runtime" / "skill-root").write_bytes(b"/invalid/\xff\n")

    result = subprocess.run([str(shim), "status"], capture_output=True, text=True, check=False)

    assert result.returncode == 1
    assert "cannot read" in result.stderr
    assert "workspace setup" in result.stderr
    assert "Traceback" not in result.stderr


def test_shim_reports_invalid_skill_path_without_traceback(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    skill = _fixture_skill(tmp_path, "")
    _, shim = flow_launcher.install(root, skill_dir=skill)
    (root / ".flow" / "runtime" / "skill-root").write_bytes(b"/invalid/\x00/path\n")

    result = subprocess.run([str(shim), "status"], capture_output=True, text=True, check=False)

    assert result.returncode == 1
    assert "invalid path" in result.stderr
    assert "workspace setup" in result.stderr
    assert "Traceback" not in result.stderr


def test_launcher_cli_requires_initialized_workspace(tmp_path: Path, capsys) -> None:
    assert flow_launcher.cli_main(["--workspace-root", str(tmp_path)]) == 1
    assert "workspace setup" in capsys.readouterr().err


def test_launcher_cli_repairs_legacy_workspace_from_executing_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    executing = _fixture_skill(tmp_path / "executing", "")
    ambient = _fixture_skill(tmp_path / "ambient", "")
    monkeypatch.setattr(flow_launcher, "SKILL_ROOT", executing)
    monkeypatch.setenv("CLAUDE_SKILL_DIR", str(ambient))

    assert flow_launcher.cli_main(["--workspace-root", str(root)]) == 0
    assert (root / ".flow" / "runtime" / "skill-root").read_text(encoding="utf-8") == (
        str(executing.resolve()) + "\n"
    )
    assert os.access(root / ".flow" / "runtime" / "flow", os.X_OK)
