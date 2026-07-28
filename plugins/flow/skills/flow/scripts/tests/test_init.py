"""Contract tests for init.py, transactional workspace bootstrap.

Coverage:
- Pre-flight refusals: already-initialized, already-initializing.
- Bare workspace happy path: jira + beads.
- `recommended` bundle composes overrides from discovered manifests.
- `custom` bundle accepts user-provided handler overrides + rejects illegal strings.
- Bundle conflict (two providers for one stage) → exit 3.
- `--resume` skips already-completed phases recorded in .init-progress.
- `--reconfigure` wipes prior markers and re-initializes.
- Beads `bd init` invoked (mocked subprocess) + postcondition `bd ready --json`.
- workspace.toml shape: parses back, [tracker] / [pipeline.handlers] / [memory] correct.
- Checkpoint manifest gets one appended line per init.
- Atomic .initializing → .initialized rename only after postconditions pass.
- Stale .initializing without --resume refused.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

import flow_launcher
import init as initmod


@pytest.fixture(autouse=True)
def _codex_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the Codex reviewer probe off for every init test.

    `_compose_handlers` defaults code_review to the bundled reviewer when `codex`
    resolves on PATH, so without this every handler assertion here would depend on
    whether the machine running the suite happens to have Codex installed. The probe's
    own tests re-patch `which` in their bodies, which wins over this fixture.
    """
    monkeypatch.setattr(initmod.shutil, "which", lambda name: None)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _bd_ok_runner() -> initmod.Runner:
    def runner(
        args: list[str],
        *,
        cwd: Path | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, check
        if args[:2] == ["bd", "init"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        if args[:2] == ["bd", "ready"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="unmocked")

    return runner


def _bd_failing_runner() -> initmod.Runner:
    def runner(
        args: list[str],
        *,
        cwd: Path | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, check
        return subprocess.CompletedProcess(
            args=args, returncode=1, stdout="", stderr="bd: prefix collision"
        )

    return runner


def _jira_config(tmp_path: Path) -> initmod.InitConfig:
    return initmod.InitConfig(
        backend="jira",
        bundle="bare",
        workspace_root=tmp_path,
        jira=initmod.JiraConfig(
            cloud_id="cloud-x",
            project_key="FT",
            assignee_account_id="acct-1",
        ),
    )


def _beads_config(tmp_path: Path) -> initmod.InitConfig:
    return initmod.InitConfig(
        backend="beads",
        bundle="bare",
        workspace_root=tmp_path,
        beads=initmod.BeadsConfig(prefix="testpkg"),
    )


# ─── Pre-flight ──────────────────────────────────────────────────────────────


def test_reserved_memory_namespace_fails_before_filesystem_mutation(tmp_path: Path) -> None:
    config = dataclasses.replace(_jira_config(tmp_path), memory_namespace="memory")

    with pytest.raises(initmod.InitError, match="reserved memory namespace"):
        initmod.run_init(config)

    assert not (tmp_path / ".flow").exists()


def test_refuses_when_already_initialized(tmp_path: Path) -> None:
    (tmp_path / ".flow").mkdir()
    (tmp_path / ".flow" / ".initialized").touch()
    with pytest.raises(initmod.InitPreflightError, match="initialized"):
        initmod.run_init(_jira_config(tmp_path))


def test_refuses_when_initializing_without_resume(tmp_path: Path) -> None:
    (tmp_path / ".flow").mkdir()
    (tmp_path / ".flow" / ".initializing").touch()
    with pytest.raises(initmod.InitPreflightError, match="initializing"):
        initmod.run_init(_jira_config(tmp_path))


def test_reconfigure_clears_prior_markers(tmp_path: Path) -> None:
    (tmp_path / ".flow").mkdir()
    (tmp_path / ".flow" / ".initialized").touch()
    (tmp_path / ".flow" / ".init-progress").write_text('{"phase":"finalize"}\n', encoding="utf-8")
    result = initmod.run_init(_jira_config(tmp_path), reconfigure=True)
    assert (tmp_path / ".flow" / ".initialized").exists()
    assert not (tmp_path / ".flow" / ".initializing").exists()
    assert not (tmp_path / ".flow" / ".init-progress").exists()
    assert (tmp_path / ".flow" / "runtime" / "flow").stat().st_mode & 0o111
    assert result.namespace == "FT"


def test_reconfigure_migrates_legacy_flow_namespace_before_writing(tmp_path: Path) -> None:
    config = dataclasses.replace(_jira_config(tmp_path), memory_namespace="flow")
    initmod.run_init(config)
    runtime = tmp_path / ".flow" / "runtime"
    legacy = tmp_path / ".flow" / "flow"
    shutil.rmtree(runtime)
    (tmp_path / ".flow" / "memory" / "flow").rename(legacy)
    (legacy / "knowledge.jsonl").write_text("preserve me\n", encoding="utf-8")

    initmod.run_init(config, reconfigure=True)

    migrated = tmp_path / ".flow" / "memory" / "flow" / "knowledge.jsonl"
    assert migrated.read_text(encoding="utf-8") == "preserve me\n"
    assert (runtime / "layout-version").read_text(encoding="utf-8") == "2\n"
    assert (runtime / "flow").stat().st_mode & 0o111


# ─── Bare happy paths ────────────────────────────────────────────────────────


def test_bare_jira_init_writes_workspace_toml(tmp_path: Path, monkeypatch) -> None:
    # Pinned to Codex so these read as the harness-neutral registry defaults; the
    # bundled `flow:implementer` / `flow:e2e-runner` types apply only under Claude
    # Code (see the composition tests below).
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    result = initmod.run_init(_jira_config(tmp_path))
    assert result.workspace_toml_path == tmp_path / ".flow" / "workspace.toml"
    assert (tmp_path / ".flow" / ".initialized").exists()
    assert not (tmp_path / ".flow" / ".initializing").exists()

    data = tomllib.loads(result.workspace_toml_path.read_text(encoding="utf-8"))
    assert data["tracker"]["backend"] == "jira"
    assert data["tracker"]["jira"]["cloud_id"] == "cloud-x"
    assert data["tracker"]["jira"]["project_key"] == "FT"
    assert data["tracker"]["jira"]["assignee_account_id"] == "acct-1"
    assert data["memory"]["namespace"] == "FT"
    assert data["memory"]["compounding"] is True
    assert data["memory"]["label_facets"] == []
    handlers = data["pipeline"]["handlers"]
    # Bare defaults from stage-registry.toml.
    assert handlers["plan"] == "inline"
    assert handlers["implement"] == "subagent:general-purpose"
    assert handlers["create_pr"] == "none"
    assert handlers["review_loop"] == "none"
    assert handlers["review_brief"] == "inline"
    assert handlers["code_review"] == "inline"
    assert handlers["e2e"] == "subagent:general-purpose"


def test_init_uses_executing_skill_dir_not_ambient_env(tmp_path: Path, monkeypatch) -> None:
    installed = tmp_path / "installed-flow"
    (installed / "scripts").mkdir(parents=True)
    (installed / "scripts" / "flowctl.py").touch()
    monkeypatch.setenv("FLOW_SKILL_DIR", str(installed))
    monkeypatch.setenv("CLAUDE_SKILL_DIR", str(installed))
    initmod.run_init(_jira_config(tmp_path))
    skill_dir = tmp_path / ".flow" / "runtime" / "skill-root"
    assert skill_dir.read_text(encoding="utf-8").strip() == str(
        Path(initmod.__file__).resolve().parent.parent
    )
    assert (tmp_path / ".flow" / "runtime" / "flow").stat().st_mode & 0o111


def test_setup_emits_no_provider_routes_or_default_model_hints(tmp_path: Path) -> None:
    result = initmod.run_init(_jira_config(tmp_path))
    data = tomllib.loads(result.workspace_toml_path.read_text(encoding="utf-8"))
    assert "agents" not in data
    assert "models" not in data


def test_codex_setup_has_the_same_simple_config(tmp_path: Path, monkeypatch) -> None:
    # Cross-harness parity: the non-default host writes the same workspace.toml.
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    result = initmod.run_init(_jira_config(tmp_path))
    data = tomllib.loads(result.workspace_toml_path.read_text(encoding="utf-8"))
    assert "agents" not in data


def test_reconfigure_preserves_optional_model_hints(tmp_path: Path) -> None:
    first = initmod.run_init(_jira_config(tmp_path))
    workspace = first.workspace_toml_path
    workspace.write_text(
        workspace.read_text(encoding="utf-8") + '[models]\nimplement = "opus"\ne2e = "off"\n',
        encoding="utf-8",
    )

    initmod.run_init(_jira_config(tmp_path), reconfigure=True)
    data = tomllib.loads(workspace.read_text(encoding="utf-8"))
    assert data["models"] == {"implement": "opus", "e2e": "off"}
    assert "agents" not in data


def test_reconfigure_preserves_role_keyed_model_hints(tmp_path: Path) -> None:
    # The nested half of the table (reviewer tuning) must survive reconfigure too;
    # a str-only round-trip would silently drop it.
    first = initmod.run_init(_jira_config(tmp_path))
    workspace = first.workspace_toml_path
    workspace.write_text(
        workspace.read_text(encoding="utf-8")
        + '[models]\nimplement = "opus"\n'
        + "[models.code_review]\n"
        + 'reviewer = { model = "gpt-5.6-sol", effort = "high" }\n'
        + 'fixer = "sonnet"\n',
        encoding="utf-8",
    )

    initmod.run_init(_jira_config(tmp_path), reconfigure=True)
    data = tomllib.loads(workspace.read_text(encoding="utf-8"))
    assert data["models"]["implement"] == "opus"
    assert data["models"]["code_review"] == {
        "reviewer": {"model": "gpt-5.6-sol", "effort": "high"},
        "fixer": "sonnet",
    }


# ─── AGENTS.md is never written ──────────────────────────────────────────────


def test_init_never_writes_agents_md(tmp_path: Path) -> None:
    # Claude Code and Codex both discover Flow natively; nothing generates a
    # managed guidance block, so init leaves the repo root alone.
    initmod.run_init(_jira_config(tmp_path))
    assert not (tmp_path / "AGENTS.md").exists()


def test_bare_beads_init_runs_bd_and_writes_workspace_toml(tmp_path: Path) -> None:
    runner = _bd_ok_runner()
    result = initmod.run_init(_beads_config(tmp_path), runner=runner)
    data = tomllib.loads(result.workspace_toml_path.read_text(encoding="utf-8"))
    assert data["tracker"]["backend"] == "beads"
    assert data["tracker"]["beads"]["prefix"] == "testpkg"
    # Beads workspaces still get FT/code_review/etc handlers from defaults.
    assert data["pipeline"]["handlers"]["plan"] == "inline"


def test_beads_bd_init_failure_blocks_finalization(tmp_path: Path) -> None:
    runner = _bd_failing_runner()
    with pytest.raises(initmod.InitError, match="bd init"):
        initmod.run_init(_beads_config(tmp_path), runner=runner)
    assert (tmp_path / ".flow" / ".initializing").exists()
    assert not (tmp_path / ".flow" / ".initialized").exists()


def test_beads_bd_ready_invalid_json_blocks_finalization(tmp_path: Path) -> None:
    def runner(
        args: list[str],
        *,
        cwd: Path | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, check
        if args[:2] == ["bd", "init"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        if args[:2] == ["bd", "ready"]:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="not json", stderr=""
            )
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")

    with pytest.raises(initmod.InitError, match="bd ready"):
        initmod.run_init(_beads_config(tmp_path), runner=runner)
    assert not (tmp_path / ".flow" / ".initialized").exists()


# ─── Recommended + custom bundles ────────────────────────────────────────────


def test_custom_bundle_uses_supplied_handlers(tmp_path: Path) -> None:
    config = initmod.InitConfig(
        backend="jira",
        bundle="custom",
        workspace_root=tmp_path,
        jira=initmod.JiraConfig(cloud_id="x", project_key="FT", assignee_account_id=None),
        handler_overrides={
            "create_pr": "inline",
            "e2e": "subagent:general-purpose",
        },
    )
    result = initmod.run_init(config)
    assert result.handlers["create_pr"] == "inline"
    assert result.handlers["e2e"] == "subagent:general-purpose"
    # Stages not overridden keep stage-registry defaults.
    assert result.handlers["plan"] == "inline"


def test_custom_bundle_rejects_illegal_handler_string(tmp_path: Path) -> None:
    config = initmod.InitConfig(
        backend="jira",
        bundle="custom",
        workspace_root=tmp_path,
        jira=initmod.JiraConfig(cloud_id="x", project_key="FT", assignee_account_id=None),
        handler_overrides={"create_pr": "bogus-handler-string"},
    )
    with pytest.raises(initmod.InitError, match="legal handler"):
        initmod.run_init(config)


def test_custom_bundle_rejects_unknown_stage(tmp_path: Path) -> None:
    config = initmod.InitConfig(
        backend="jira",
        bundle="custom",
        workspace_root=tmp_path,
        jira=initmod.JiraConfig(cloud_id="x", project_key="FT", assignee_account_id=None),
        handler_overrides={"deploy": "skill:foo:bar"},
    )
    with pytest.raises(initmod.InitError, match=r"pipeline\.stages"):
        initmod.run_init(config)


# ─── Resume ──────────────────────────────────────────────────────────────────


def test_resume_skips_completed_phases(tmp_path: Path) -> None:
    # Simulate prior interrupted init: .initializing present, some phases done.
    flow_dir = tmp_path / ".flow"
    flow_dir.mkdir()
    (flow_dir / ".initializing").touch()
    (flow_dir / ".init-progress").write_text(
        json.dumps({"phase": "validate_inputs", "ts": "2026-05-28T00:00:00Z"})
        + "\n"
        + json.dumps({"phase": "bundle_compose", "ts": "2026-05-28T00:00:01Z"})
        + "\n",
        encoding="utf-8",
    )

    result = initmod.run_init(_jira_config(tmp_path), resume=True)
    assert (tmp_path / ".flow" / ".initialized").exists()
    assert not (tmp_path / ".flow" / ".initializing").exists()
    assert (tmp_path / ".flow" / "runtime" / "skill-root").is_file()
    assert (tmp_path / ".flow" / "runtime" / "flow").stat().st_mode & 0o111
    assert result.handlers["plan"] == "inline"


def test_failure_leaves_initializing_marker(tmp_path: Path) -> None:
    runner = _bd_failing_runner()
    with pytest.raises(initmod.InitError):
        initmod.run_init(_beads_config(tmp_path), runner=runner)
    # Initializing marker stays; progress file records phases up to failure.
    assert (tmp_path / ".flow" / ".initializing").exists()
    progress = (tmp_path / ".flow" / ".init-progress").read_text(encoding="utf-8").splitlines()
    phases_done = [json.loads(line)["phase"] for line in progress]
    assert "validate_inputs" in phases_done
    assert "bundle_compose" in phases_done
    assert "mkdirs" in phases_done
    assert "bd_init" not in phases_done


# ─── Postconditions + side effects ───────────────────────────────────────────


def test_creates_flow_subdirs(tmp_path: Path) -> None:
    initmod.run_init(_jira_config(tmp_path))
    assert (tmp_path / ".flow" / "runs").is_dir()
    assert (tmp_path / ".flow" / "memory" / "FT").is_dir()
    assert (tmp_path / ".flow" / "memory" / "FT" / "ship-events").is_dir()
    assert not (tmp_path / ".flow" / "FT").exists()


def test_pipeline_handlers_covers_every_stage(tmp_path: Path) -> None:
    result = initmod.run_init(_jira_config(tmp_path))
    data = tomllib.loads(result.workspace_toml_path.read_text(encoding="utf-8"))
    stages = data["pipeline"]["stages"]
    handlers = data["pipeline"]["handlers"]
    for stage in stages:
        assert stage in handlers, f"missing handler for {stage}"


def test_compounding_false_drops_reflect_stage(tmp_path: Path) -> None:
    config = initmod.InitConfig(
        backend="jira",
        bundle="bare",
        workspace_root=tmp_path,
        jira=initmod.JiraConfig(cloud_id="x", project_key="FT", assignee_account_id=None),
        memory_compounding=False,
    )
    result = initmod.run_init(config)
    data = tomllib.loads(result.workspace_toml_path.read_text(encoding="utf-8"))
    assert "reflect" not in data["pipeline"]["stages"]
    assert data["memory"]["compounding"] is False


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_bare_jira(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = initmod.cli_main(
        [
            "--backend",
            "jira",
            "--bundle",
            "bare",
            "--workspace-root",
            str(tmp_path),
            "--jira-cloud-id",
            "x",
            "--jira-project-key",
            "FT",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["namespace"] == "FT"
    assert (tmp_path / ".flow" / ".initialized").exists()


def test_cli_missing_backend(capsys: pytest.CaptureFixture[str]) -> None:
    rc = initmod.cli_main(["--bundle", "bare"])
    assert rc == 2
    assert "backend" in capsys.readouterr().err


def test_cli_preflight_exit_code(tmp_path: Path) -> None:
    (tmp_path / ".flow").mkdir()
    (tmp_path / ".flow" / ".initialized").touch()
    rc = initmod.cli_main(
        [
            "--backend",
            "jira",
            "--bundle",
            "bare",
            "--workspace-root",
            str(tmp_path),
            "--jira-cloud-id",
            "x",
            "--jira-project-key",
            "FT",
        ]
    )
    assert rc == 4


def test_derive_slug_normalizes() -> None:
    assert initmod._derive_slug("Safe Mic") == "safe-mic"
    assert initmod._derive_slug("Foo--Bar") == "foo-bar"
    assert initmod._derive_slug("UPPER") == "upper"
    assert initmod._derive_slug("with/slashes") == "with-slashes"


# ─── [U] --config JSON list normalization ─────────────────────────────────────


def test_invalid_input_leaves_no_initializing_marker(tmp_path: Path) -> None:
    # custom bundle with no handler overrides fails validation. The failure must
    # NOT leave a .initializing marker behind.
    bad = initmod.InitConfig(
        backend="jira",
        bundle="custom",
        workspace_root=tmp_path,
        jira=initmod.JiraConfig(cloud_id="x", project_key="FT", assignee_account_id=None),
    )
    with pytest.raises(initmod.InitError, match="custom requires"):
        initmod.run_init(bad)
    assert not (tmp_path / ".flow" / ".initializing").exists()

    # A corrected plain re-run is accepted (not refused with a stale marker).
    result = initmod.run_init(_jira_config(tmp_path))
    assert (tmp_path / ".flow" / ".initialized").exists()
    assert result.namespace == "FT"


def test_invalid_harness_leaves_no_init_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLOW_HARNESS", "mystery-host")

    with pytest.raises(initmod.InitError, match="FLOW_HARNESS"):
        initmod.run_init(_jira_config(tmp_path))

    assert not (tmp_path / ".flow" / ".initializing").exists()
    assert not (tmp_path / ".flow" / ".init-progress").exists()


# ─── [W] reconfigure rollback ─────────────────────────────────────────────────


def _bd_init_ok_ready_bad_runner() -> initmod.Runner:
    # bd init succeeds, but `bd ready --json` returns non-JSON so the
    # verify_postconditions phase fails after workspace.toml is rewritten.
    def runner(
        args: list[str],
        *,
        cwd: Path | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, check
        if args[:2] == ["bd", "init"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        if args[:2] == ["bd", "ready"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="nope", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")

    return runner


def test_failed_reconfigure_restores_prior_workspace(tmp_path: Path) -> None:
    # First init: valid beads workspace with namespace "orig".
    first = initmod.InitConfig(
        backend="beads",
        bundle="bare",
        workspace_root=tmp_path,
        beads=initmod.BeadsConfig(prefix="testpkg"),
        memory_namespace="orig",
    )
    initmod.run_init(first, runner=_bd_ok_runner())
    toml_path = tmp_path / ".flow" / "workspace.toml"
    before = toml_path.read_text(encoding="utf-8")
    assert (tmp_path / ".flow" / ".initialized").exists()

    # Reconfigure that fails its postcondition (bd ready returns non-JSON) while
    # attempting to change the namespace to "changed".
    second = initmod.InitConfig(
        backend="beads",
        bundle="bare",
        workspace_root=tmp_path,
        beads=initmod.BeadsConfig(prefix="testpkg"),
        memory_namespace="changed",
    )
    with pytest.raises(initmod.InitError, match="bd ready"):
        initmod.run_init(second, runner=_bd_init_ok_ready_bad_runner(), reconfigure=True)

    # Prior valid state intact: .initialized present, workspace.toml unchanged.
    assert (tmp_path / ".flow" / ".initialized").exists()
    assert toml_path.read_text(encoding="utf-8") == before
    restored = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    assert restored["memory"]["namespace"] == "orig"


def test_failed_reconfigure_restores_launcher_metadata_and_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initmod.run_init(_jira_config(tmp_path))

    flow_path = tmp_path / ".flow" / "runtime" / "flow"
    skill_path = tmp_path / ".flow" / "runtime" / "skill-root"
    agents_path = tmp_path / "AGENTS.md"
    # Flow never writes AGENTS.md; this leg proves a hand-maintained one survives a
    # failed reconfigure, since it is the only non-`.flow/` file in the snapshot.
    old_agents = "# House rules\nUse tabs.\n"
    flow_path.write_bytes(b"prior launcher\n")
    skill_path.write_bytes(b"/prior/skill path\n")
    agents_path.write_text(old_agents, encoding="utf-8")
    flow_path.chmod(0o701)
    skill_path.chmod(0o604)
    agents_path.chmod(0o640)
    before = {
        path: (path.read_bytes(), path.stat().st_mode & 0o777)
        for path in (flow_path, skill_path, agents_path)
    }

    def partially_install_then_fail(
        workspace_root: Path, *, skill_dir: Path | None = None
    ) -> tuple[Path, Path]:
        del skill_dir
        flow = workspace_root / ".flow" / "runtime" / "flow"
        skill = workspace_root / ".flow" / "runtime" / "skill-root"
        flow.write_bytes(b"partial new launcher\n")
        skill.write_bytes(b"/partial/new/skill\n")
        flow.chmod(0o755)
        skill.chmod(0o644)
        raise OSError("injected launcher failure")

    monkeypatch.setattr(initmod.flow_launcher, "install", partially_install_then_fail)

    with pytest.raises(initmod.InitError, match="launcher failure"):
        initmod.run_init(_jira_config(tmp_path), reconfigure=True)

    for path, (content, mode) in before.items():
        assert path.read_bytes() == content
        assert path.stat().st_mode & 0o777 == mode


def test_launcher_failure_does_not_mark_initialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The launcher installs before the finalize rename: a broken facade is not a
    # completed initialization and must not be marked one.
    def fail_install(workspace_root: Path, *, skill_dir: Path | None = None) -> tuple[Path, Path]:
        del workspace_root, skill_dir
        raise OSError("injected launcher failure")

    monkeypatch.setattr(initmod.flow_launcher, "install", fail_install)

    with pytest.raises(initmod.InitError, match="launcher failure"):
        initmod.run_init(_jira_config(tmp_path))

    assert not (tmp_path / ".flow" / ".initialized").exists()


def test_failed_reconfigure_removes_files_absent_before_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initmod.run_init(_jira_config(tmp_path))
    generated = (
        tmp_path / ".flow" / "runtime" / "flow",
        tmp_path / ".flow" / "runtime" / "skill-root",
    )
    generated[0].unlink()
    generated[1].unlink()

    def partially_install_then_fail(
        workspace_root: Path, *, skill_dir: Path | None = None
    ) -> tuple[Path, Path]:
        del skill_dir
        flow = workspace_root / ".flow" / "runtime" / "flow"
        skill = workspace_root / ".flow" / "runtime" / "skill-root"
        flow.write_text("partial launcher\n", encoding="utf-8")
        skill.write_text("/partial/skill\n", encoding="utf-8")
        raise OSError("injected launcher failure")

    monkeypatch.setattr(initmod.flow_launcher, "install", partially_install_then_fail)

    with pytest.raises(initmod.InitError, match="launcher failure"):
        initmod.run_init(_jira_config(tmp_path), reconfigure=True)

    assert all(not path.exists() for path in generated)


def test_reconfigure_setup_failure_restores_before_phase_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initmod.run_init(_jira_config(tmp_path))
    flow_dir = tmp_path / ".flow"
    prior = {
        path: (path.read_bytes(), path.stat().st_mode & 0o777)
        for path in (
            flow_dir / "workspace.toml",
            flow_dir / "runtime" / "flow",
            flow_dir / "runtime" / "skill-root",
        )
    }

    def fail_registry_load(*_args, **_kwargs):
        raise initmod.InitError("injected registry failure")

    monkeypatch.setattr(initmod, "_load_stage_registry", fail_registry_load)

    with pytest.raises(initmod.InitError, match="registry failure"):
        initmod.run_init(_jira_config(tmp_path), reconfigure=True)

    assert (flow_dir / ".initialized").exists()
    assert not (flow_dir / ".initializing").exists()
    assert not (flow_dir / ".init-progress").exists()
    for path, (content, mode) in prior.items():
        assert path.read_bytes() == content
        assert path.stat().st_mode & 0o777 == mode


def test_failed_reconfigure_restores_preexisting_transient_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initmod.run_init(_jira_config(tmp_path))
    flow_dir = tmp_path / ".flow"
    initializing = flow_dir / ".initializing"
    progress = flow_dir / ".init-progress"
    initializing.write_bytes(b"prior-run-id\n")
    progress.write_bytes(b'{"phase":"write_workspace"}\n')
    initializing.chmod(0o640)
    progress.chmod(0o600)
    before = {
        path: (path.read_bytes(), path.stat().st_mode & 0o777) for path in (initializing, progress)
    }

    monkeypatch.setattr(
        initmod,
        "_load_stage_registry",
        lambda: (_ for _ in ()).throw(initmod.InitError("injected registry failure")),
    )

    with pytest.raises(initmod.InitError, match="registry failure"):
        initmod.run_init(_jira_config(tmp_path), reconfigure=True)

    for path, (content, mode) in before.items():
        assert path.read_bytes() == content
        assert path.stat().st_mode & 0o777 == mode


def test_successful_reconfigure_swaps_workspace(tmp_path: Path) -> None:
    # The other half of the atomic-swap contract: a reconfigure that passes all
    # postconditions overwrites the toml and leaves no .initializing marker.
    first = initmod.InitConfig(
        backend="beads",
        bundle="bare",
        workspace_root=tmp_path,
        beads=initmod.BeadsConfig(prefix="testpkg"),
        memory_namespace="orig",
    )
    initmod.run_init(first, runner=_bd_ok_runner())
    toml_path = tmp_path / ".flow" / "workspace.toml"
    assert tomllib.loads(toml_path.read_text(encoding="utf-8"))["memory"]["namespace"] == "orig"

    second = initmod.InitConfig(
        backend="beads",
        bundle="bare",
        workspace_root=tmp_path,
        beads=initmod.BeadsConfig(prefix="testpkg"),
        memory_namespace="changed",
    )
    initmod.run_init(second, runner=_bd_ok_runner(), reconfigure=True)
    assert tomllib.loads(toml_path.read_text(encoding="utf-8"))["memory"]["namespace"] == "changed"
    assert (tmp_path / ".flow" / ".initialized").exists()
    assert not (tmp_path / ".flow" / ".initializing").exists()


def test_successful_reconfigure_starts_with_a_fresh_run_id(tmp_path: Path) -> None:
    config = _jira_config(tmp_path)
    initmod.run_init(config)
    flow_dir = tmp_path / ".flow"
    stale_run_id = "interrupted-prior-reconfigure"
    (flow_dir / ".initializing").write_text(stale_run_id + "\n", encoding="utf-8")
    (flow_dir / ".init-progress").write_text('{"phase":"write_workspace_toml"}\n', encoding="utf-8")

    initmod.run_init(config, reconfigure=True)

    # finalize renames .initializing -> .initialized, so the marker holds the run id.
    assert (flow_dir / ".initialized").read_text(encoding="utf-8").strip() != stale_run_id
    assert not (flow_dir / ".initializing").exists()
    assert not (flow_dir / ".init-progress").exists()


# ─── [X] resume idempotency ───────────────────────────────────────────────────


class _StatefulBdRunner:
    # `bd ready` fails until `bd init` has run; counts bd init invocations.
    def __init__(self, *, already_initialized: bool = False) -> None:
        self.init_calls = 0
        self.initialized = already_initialized

    def __call__(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, check
        if args[:2] == ["bd", "init"]:
            self.init_calls += 1
            self.initialized = True
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        if args[:2] == ["bd", "ready"]:
            if self.initialized:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="[]", stderr="")
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="no store"
            )
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")


def test_fresh_beads_init_runs_bd_init_once(tmp_path: Path) -> None:
    runner = _StatefulBdRunner()
    initmod.run_init(_beads_config(tmp_path), runner=runner)
    assert runner.init_calls == 1


def test_bd_init_passes_skip_agents_and_non_interactive(tmp_path: Path) -> None:
    runner = _StatefulBdRunner()
    captured: list[list[str]] = []
    base_call = runner.__call__

    def recording(
        args: list[str],
        *,
        cwd: Path | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        captured.append(args)
        return base_call(args, cwd=cwd, check=check)

    initmod.run_init(_beads_config(tmp_path), runner=recording)
    init_argv = next(a for a in captured if a[:2] == ["bd", "init"])
    assert "--skip-agents" in init_argv
    assert "--non-interactive" in init_argv


def test_resume_skips_bd_init_when_store_ready(tmp_path: Path) -> None:
    # Store already initialized externally; bd_init phase not yet recorded.
    runner = _StatefulBdRunner(already_initialized=True)
    flow_dir = tmp_path / ".flow"
    flow_dir.mkdir()
    (flow_dir / ".initializing").write_text("rid\n", encoding="utf-8")
    done = ["validate_inputs", "bundle_compose", "mkdirs"]
    (flow_dir / ".init-progress").write_text(
        "".join(json.dumps({"phase": p, "ts": "2026-05-28T00:00:00Z"}) + "\n" for p in done),
        encoding="utf-8",
    )
    initmod.run_init(_beads_config(tmp_path), runner=runner, resume=True)
    # bd_init phase ran on resume but skipped the actual bd init call.
    assert runner.init_calls == 0
    assert (tmp_path / ".flow" / ".initialized").exists()


# ─── ensure_gitignore phase ──────────────────────────────────────────────────


def test_init_seeds_flow_gitignore_when_absent(tmp_path: Path) -> None:
    initmod.run_init(_jira_config(tmp_path))
    gi = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".flow/*" in gi.splitlines()
    assert "!.flow/workspace.toml" in gi.splitlines()
    assert "!.flow/.initialized" in gi.splitlines()
    assert ".claude/worktrees/" in gi.splitlines()  # flow-gh1u: the pool


def test_init_gitignore_idempotent_when_already_seeded(tmp_path: Path) -> None:
    gi_path = tmp_path / ".gitignore"
    gi_path.write_text("node_modules/\n.flow/*\n.claude/worktrees/\n", encoding="utf-8")
    initmod.run_init(_jira_config(tmp_path))
    content = gi_path.read_text(encoding="utf-8")
    assert content.count(".flow/*") == 1
    assert content.count(".claude/worktrees/") == 1
    assert "node_modules/" in content


def test_init_gitignore_adds_pool_line_to_pre_relocation_repo(tmp_path: Path) -> None:
    # A repo seeded before flow-gh1u has the .flow block but not the pool line;
    # re-init converges it instead of skipping on the .flow marker.
    gi_path = tmp_path / ".gitignore"
    gi_path.write_text(".flow/*\n!.flow/workspace.toml\n!.flow/.initialized\n", encoding="utf-8")
    initmod.run_init(_jira_config(tmp_path))
    content = gi_path.read_text(encoding="utf-8")
    assert ".claude/worktrees/" in content.splitlines()
    assert content.count(".flow/*") == 1


def test_init_gitignore_appends_preserving_existing(tmp_path: Path) -> None:
    gi_path = tmp_path / ".gitignore"
    gi_path.write_text("node_modules/\n", encoding="utf-8")  # no trailing-blank cleanup needed
    initmod.run_init(_jira_config(tmp_path))
    content = gi_path.read_text(encoding="utf-8")
    assert "node_modules/" in content  # original preserved
    assert ".flow/*" in content.splitlines()  # block appended


def test_generated_launcher_files_are_gitignored(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    initmod.run_init(_jira_config(tmp_path))
    for relative in (".flow/runtime/flow", ".flow/runtime/skill-root"):
        result = subprocess.run(["git", "check-ignore", "-q", relative], cwd=tmp_path, check=False)
        assert result.returncode == 0


# ─── [Y-init] recommended no-coverage + handler validation ────────────────────


def test_write_phase_rejects_illegal_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The write phase guards the toml: an illegal handler that slips past
    # composition (e.g. a future _compose_handlers regression) never lands and
    # the workspace is not finalized.
    def _bad_compose(
        config: initmod.InitConfig,
        registry: list[initmod.StageEntry],
        pipeline_stages: list[str],
        discovery: object,
        existing_handlers: dict[str, str] | None = None,
    ) -> tuple[dict[str, str], list[str]]:
        del config, registry, discovery, existing_handlers
        return dict.fromkeys(pipeline_stages, "bogus"), []

    monkeypatch.setattr(initmod, "_compose_handlers", _bad_compose)
    with pytest.raises(initmod.InitError, match="illegal handler"):
        initmod.run_init(_jira_config(tmp_path))
    assert not (tmp_path / ".flow" / ".initialized").exists()


def test_reconfigure_preserves_customized_handler(tmp_path: Path) -> None:
    # The incident: a customized handler must survive `--reconfigure`, not silently
    # reset to the registry default (code_review default is "inline").
    first = dataclasses.replace(
        _jira_config(tmp_path),
        bundle="custom",
        handler_overrides={"code_review": "subagent:code-reviewer"},
    )
    initmod.run_init(first)
    result = initmod.run_init(
        dataclasses.replace(_jira_config(tmp_path), bundle="bare"), reconfigure=True
    )
    assert result.handlers["code_review"] == "subagent:code-reviewer"


def test_reconfigure_handler_flag_overrides_preservation(tmp_path: Path) -> None:
    # Explicit --handler beats preservation, even when it resets a stage to default.
    first = dataclasses.replace(
        _jira_config(tmp_path),
        bundle="custom",
        handler_overrides={"code_review": "subagent:code-reviewer"},
    )
    initmod.run_init(first)
    second = dataclasses.replace(
        _jira_config(tmp_path),
        bundle="custom",
        handler_overrides={"code_review": "inline"},
    )
    result = initmod.run_init(second, reconfigure=True)
    assert result.handlers["code_review"] == "inline"


def test_fresh_init_preserves_nothing(tmp_path: Path, monkeypatch) -> None:
    # No reconfigure -> existing_handlers is {} -> handlers equal registry defaults.
    # Pinned to Codex; under Claude Code e2e's default is the bundled
    # `flow:e2e-runner` type instead (see the composition tests below).
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    result = initmod.run_init(dataclasses.replace(_jira_config(tmp_path), bundle="bare"))
    assert result.handlers["code_review"] == "inline"
    assert result.handlers["e2e"] == "subagent:general-purpose"
    assert result.warnings == []


def test_reconfigure_freezes_value_differing_from_current_default(tmp_path: Path) -> None:
    # A prior value that differs from the current default is frozen on reconfigure
    # (e2e's current default is the registry default under Codex, or the bundled
    # `flow:e2e-runner` type under Claude Code; a prior "none" differs from either
    # and is preserved).
    first = dataclasses.replace(
        _jira_config(tmp_path),
        bundle="custom",
        handler_overrides={"e2e": "none"},
    )
    initmod.run_init(first)
    result = initmod.run_init(
        dataclasses.replace(_jira_config(tmp_path), bundle="bare"), reconfigure=True
    )
    assert result.handlers["e2e"] == "none"


def test_reconfigure_preserved_warning_names_value_and_default(tmp_path: Path, monkeypatch) -> None:
    # The reset-that-wasn't is legible: the warning carries value AND current default.
    # Pinned to Codex, where the current default happens to be the registry default;
    # the Claude Code twin below covers the case where the two diverge.
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    first = dataclasses.replace(
        _jira_config(tmp_path),
        bundle="custom",
        handler_overrides={"e2e": "none"},
    )
    initmod.run_init(first)
    result = initmod.run_init(
        dataclasses.replace(_jira_config(tmp_path), bundle="bare"), reconfigure=True
    )
    line = next(w for w in result.warnings if "e2e" in w)
    assert "none" in line
    assert "subagent:general-purpose" in line


def test_claude_code_preserved_warning_names_current_not_registry_default(
    tmp_path: Path, monkeypatch
) -> None:
    # Under Claude Code the preserved value IS the registry default, so the old
    # "registry default: subagent:flow:implementer" label claimed a difference that
    # does not exist. The interpolated field is the default this workspace would now
    # receive (the bundled type), and the label has to say that.
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    initmod.run_init(_jira_config(tmp_path))

    monkeypatch.setenv("FLOW_HARNESS", "claude-code")
    result = initmod.run_init(
        dataclasses.replace(_jira_config(tmp_path), bundle="bare"), reconfigure=True
    )
    line = next(w for w in result.warnings if "implement=" in w)
    assert line == (
        "reconfigure preserved implement=subagent:general-purpose "
        "(current default: subagent:flow:implementer)"
    )
    assert "registry default" not in line


# ─── flow-js8p: stabilize installed skill-root path ─────────────────────


def test_stabilize_skill_dir_rewrites_cache_to_marketplace(tmp_path: Path) -> None:
    mp_dir = tmp_path / "plugins" / "marketplaces" / "vdsmon-flow"
    (mp_dir / ".claude-plugin").mkdir(parents=True)
    (mp_dir / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"plugins": [{"name": "flow", "source": "./plugins/flow"}]}),
        encoding="utf-8",
    )
    target = mp_dir / "plugins" / "flow" / "skills" / "flow"
    target.mkdir(parents=True)
    cache = tmp_path / "plugins" / "cache" / "vdsmon-flow" / "flow" / "0.92.1" / "skills" / "flow"
    assert flow_launcher.stabilize_skill_dir(str(cache)) == str(target)


# ─── Bundled Codex reviewer default ──────────────────────────────────────────


def _handlers_of(workspace_toml_path: Path) -> dict[str, str]:
    data = tomllib.loads(workspace_toml_path.read_text(encoding="utf-8"))
    return data["pipeline"]["handlers"]


def test_code_review_stays_inline_without_codex(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(initmod.shutil, "which", lambda name: None)
    monkeypatch.setenv("FLOW_HARNESS", "claude-code")
    result = initmod.run_init(_jira_config(tmp_path))
    assert _handlers_of(result.workspace_toml_path)["code_review"] == "inline"


def test_code_review_stays_inline_under_codex_harness(tmp_path: Path, monkeypatch) -> None:
    # `subagent:` names a Claude Code agent type; a Codex-hosted run cannot launch it,
    # and there the fresh native reviewer is already Codex.
    monkeypatch.setattr(initmod.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    result = initmod.run_init(_jira_config(tmp_path))
    assert _handlers_of(result.workspace_toml_path)["code_review"] == "inline"


def test_probe_owned_handler_is_not_preserved_when_codex_disappears(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FLOW_HARNESS", "claude-code")
    monkeypatch.setattr(initmod.shutil, "which", lambda name: "/usr/bin/codex")
    first = initmod.run_init(_jira_config(tmp_path))
    assert _handlers_of(first.workspace_toml_path)["code_review"] == (
        initmod._BUNDLED_CODEX_REVIEWER
    )

    monkeypatch.setattr(initmod.shutil, "which", lambda name: None)
    initmod.run_init(_jira_config(tmp_path), reconfigure=True)
    assert _handlers_of(first.workspace_toml_path)["code_review"] == "inline"


def test_explicit_handler_choice_survives_the_probe(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FLOW_HARNESS", "claude-code")
    monkeypatch.setattr(initmod.shutil, "which", lambda name: None)
    first = initmod.run_init(_jira_config(tmp_path))
    workspace = first.workspace_toml_path
    workspace.write_text(
        workspace.read_text(encoding="utf-8").replace(
            'code_review = "inline"', 'code_review = "subagent:general-purpose"'
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(initmod.shutil, "which", lambda name: "/usr/bin/codex")
    initmod.run_init(_jira_config(tmp_path), reconfigure=True)
    assert _handlers_of(workspace)["code_review"] == "subagent:general-purpose"


def test_reconfigure_hard_kill_leaves_no_stale_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The exception path restores the backup and finalize unlinks on success;
    # the reconfigure-time `.init-progress` unlink exists for the third path, a
    # hard kill mid-run. Simulate it with a BaseException (skips the
    # `except Exception` restore): the stale pre-reconfigure ledger must
    # already be gone, so a later `--resume` cannot read merged stale+new rows.
    initmod.run_init(_jira_config(tmp_path))
    progress = tmp_path / ".flow" / ".init-progress"
    progress.write_bytes(b'{"phase": "write_workspace", "stale": true}\n')

    monkeypatch.setattr(
        initmod,
        "_load_stage_registry",
        lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        initmod.run_init(_jira_config(tmp_path), reconfigure=True)

    assert not progress.exists()


# ─── Bundled stage agent defaults (implement / e2e) ──────────────────────────


def test_claude_code_composes_bundled_stage_agents(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FLOW_HARNESS", "claude-code")
    result = initmod.run_init(_jira_config(tmp_path))
    assert result.handlers["implement"] == "subagent:flow:implementer"
    assert result.handlers["e2e"] == "subagent:flow:e2e-runner"
    assert result.handlers["code_review"] == "inline"


def test_codex_composes_general_purpose_for_stage_agents(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    result = initmod.run_init(_jira_config(tmp_path))
    assert result.handlers["implement"] == "subagent:general-purpose"
    assert result.handlers["e2e"] == "subagent:general-purpose"


def test_explicit_handler_beats_bundled_stage_agent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FLOW_HARNESS", "claude-code")
    config = dataclasses.replace(
        _jira_config(tmp_path),
        bundle="custom",
        handler_overrides={"implement": "subagent:custom-implementer"},
    )
    result = initmod.run_init(config)
    assert result.handlers["implement"] == "subagent:custom-implementer"
    # The untouched stage still gets the bundled default.
    assert result.handlers["e2e"] == "subagent:flow:e2e-runner"


def test_codex_reconfigure_drops_bundled_stage_agents(tmp_path: Path, monkeypatch) -> None:
    # The _FLOW_OWNED_HANDLERS path: reconfiguring a Claude-Code-initialized
    # workspace under Codex must not preserve the bundled types as an operator
    # customization the way a genuine customization would survive.
    monkeypatch.setenv("FLOW_HARNESS", "claude-code")
    first = initmod.run_init(_jira_config(tmp_path))
    assert first.handlers["implement"] == "subagent:flow:implementer"
    assert first.handlers["e2e"] == "subagent:flow:e2e-runner"

    monkeypatch.setenv("FLOW_HARNESS", "codex")
    result = initmod.run_init(
        dataclasses.replace(_jira_config(tmp_path), bundle="bare"), reconfigure=True
    )
    assert result.handlers["implement"] == "subagent:general-purpose"
    assert result.handlers["e2e"] == "subagent:general-purpose"


def test_claude_code_reconfigure_preserves_stored_general_purpose(
    tmp_path: Path, monkeypatch
) -> None:
    # Pins WHERE the bundled-agent block runs: before `_preserved_handlers`, so a
    # stored value is compared against the bundled type. Move that block after the
    # call and a stored `subagent:general-purpose` reads as "equals the default", is
    # not preserved, and gets silently upgraded to the bundled type, breaking the
    # preservation promise in references/command-workspace.md.
    #
    # No other test sees that reordering: mutating the harness guard to `if True:`
    # removes the condition, never the order. Without this test the whole point of
    # the change is invisible to the suite.
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    first = initmod.run_init(_jira_config(tmp_path))
    assert first.handlers["implement"] == "subagent:general-purpose"
    assert first.handlers["e2e"] == "subagent:general-purpose"

    monkeypatch.setenv("FLOW_HARNESS", "claude-code")
    result = initmod.run_init(
        dataclasses.replace(_jira_config(tmp_path), bundle="bare"), reconfigure=True
    )
    assert result.handlers["implement"] == "subagent:general-purpose"
    assert result.handlers["e2e"] == "subagent:general-purpose"


def _frontmatter_name(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].strip() == "---", f"{path} missing frontmatter opening ---"
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    for line in lines[1:end]:
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"{path} frontmatter has no name: field")


def test_every_bundled_agent_type_resolves_to_a_matching_agent_file() -> None:
    # The main safety gain: HANDLER_RE and parse_handler validate the charset only,
    # and nothing under scripts/ references agents/, so a typo in either bundled map
    # would ship silently and fail only at spawn time, on every run.
    agents_dir = Path(initmod.__file__).resolve().parent.parent.parent.parent / "agents"
    bundled_types = {initmod._BUNDLED_CODEX_REVIEWER, *initmod._BUNDLED_STAGE_AGENTS.values()}
    assert bundled_types  # a suite with an empty set would vacuously pass below
    prefix = "subagent:flow:"
    for handler in bundled_types:
        assert handler.startswith(prefix), handler
        name = handler[len(prefix) :]
        agent_path = agents_dir / f"{name}.md"
        assert agent_path.is_file(), f"missing agent definition: {agent_path}"
        assert _frontmatter_name(agent_path) == name


def test_run_init_writes_bundled_stage_agents_to_disk_under_claude_code(
    tmp_path: Path, monkeypatch
) -> None:
    # Covers _render_workspace_toml -> atomic_write_text -> _verify_workspace_toml,
    # the path a composition-only test never reaches: read the WRITTEN toml, not the
    # returned dict.
    monkeypatch.setenv("FLOW_HARNESS", "claude-code")
    result = initmod.run_init(_jira_config(tmp_path))
    handlers = _handlers_of(result.workspace_toml_path)
    assert handlers["implement"] == "subagent:flow:implementer"
    assert handlers["e2e"] == "subagent:flow:e2e-runner"


def test_run_init_writes_general_purpose_to_disk_under_codex(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FLOW_HARNESS", "codex")
    result = initmod.run_init(_jira_config(tmp_path))
    handlers = _handlers_of(result.workspace_toml_path)
    assert handlers["implement"] == "subagent:general-purpose"
    assert handlers["e2e"] == "subagent:general-purpose"
