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

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _write_manifest(plugin_dir: Path, content: str) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / ".flow-bundle.toml").write_text(content, encoding="utf-8")


def _ship_it_manifest() -> str:
    return """schema_version = 1
[bundle]
name = "ship-it"
description = ""
[skills.create_pr]
handler_string = "skill:ship-it:create"
[skills.review_loop]
handler_string = "skill:ship-it:feedback"
"""


def _code_review_manifest() -> str:
    return """schema_version = 1
[bundle]
name = "code-review"
description = ""
[skills.code_review]
handler_string = "skill:code-review"
"""


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
        bundle_search_roots=[tmp_path / "_empty"],
        checkpoint_manifest_path=tmp_path / "_ckpt.jsonl",
    )


def _beads_config(tmp_path: Path) -> initmod.InitConfig:
    return initmod.InitConfig(
        backend="beads",
        bundle="bare",
        workspace_root=tmp_path,
        beads=initmod.BeadsConfig(prefix="testpkg"),
        bundle_search_roots=[tmp_path / "_empty"],
        checkpoint_manifest_path=tmp_path / "_ckpt.jsonl",
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


def test_bare_jira_init_writes_workspace_toml(tmp_path: Path) -> None:
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
    assert handlers["plan"] == "subagent:Plan"
    assert handlers["implement"] == "subagent:general-purpose"
    assert handlers["create_pr"] == "none"
    assert handlers["review_loop"] == "none"
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


def test_native_setup_emits_explicit_owner_relative_agent_routes(tmp_path: Path) -> None:
    result = initmod.run_init(_jira_config(tmp_path))
    data = tomllib.loads(result.workspace_toml_path.read_text(encoding="utf-8"))
    assert data["agents"]["planner"] == {
        "harness": "codex",
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
    }
    assert data["agents"]["implementer"]["by_owner"]["claude_code"]["model"] == "sonnet"
    assert data["agents"]["implementer"]["by_owner"]["codex"]["model"] == "gpt-5.6-luna"


def test_generic_setup_emits_no_explicit_agent_routes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FLOW_HARNESS", "generic")
    result = initmod.run_init(_jira_config(tmp_path))
    data = tomllib.loads(result.workspace_toml_path.read_text(encoding="utf-8"))
    assert "agents" not in data


def test_reconfigure_preserves_legacy_models_without_migrating(tmp_path: Path) -> None:
    first = initmod.run_init(_jira_config(tmp_path))
    workspace = first.workspace_toml_path
    content = workspace.read_text(encoding="utf-8")
    agents_at = content.index("[agents.planner]")
    workspace.write_text(
        content[:agents_at] + '[models]\nwork_model = "opus"\ne2e = "off"\n',
        encoding="utf-8",
    )

    initmod.run_init(_jira_config(tmp_path), reconfigure=True)
    data = tomllib.loads(workspace.read_text(encoding="utf-8"))
    assert data["models"] == {"work_model": "opus", "e2e": "off"}
    assert "agents" not in data


def test_reconfigure_preserves_explicit_routes_and_legacy_rollback_block(
    tmp_path: Path,
) -> None:
    first = initmod.run_init(_jira_config(tmp_path))
    workspace = first.workspace_toml_path
    workspace.write_text(
        workspace.read_text(encoding="utf-8") + '\n[models]\nwork_model = "opus"\ne2e = "sonnet"\n',
        encoding="utf-8",
    )

    initmod.run_init(_jira_config(tmp_path), reconfigure=True)
    data = tomllib.loads(workspace.read_text(encoding="utf-8"))
    assert data["agents"]["planner"]["model"] == "gpt-5.6-sol"
    assert data["models"] == {"work_model": "opus", "e2e": "sonnet"}


# ─── L1: AGENTS.md cross-harness entry point (opt-in, CC-neutral by default) ──


def test_init_does_not_write_agents_md_by_default(tmp_path: Path) -> None:
    # Native Claude Code and Codex discovery need no tracked AGENTS.md by default.
    initmod.run_init(_jira_config(tmp_path))
    assert not (tmp_path / "AGENTS.md").exists()


def test_agents_md_flag_writes_entry_point(tmp_path: Path) -> None:
    cfg = dataclasses.replace(_jira_config(tmp_path), agents_md=True)
    initmod.run_init(cfg)
    agents = tmp_path / "AGENTS.md"
    body = agents.read_text(encoding="utf-8")
    assert initmod._AGENTS_MARKER in body
    assert ".flow/runtime/flow" in body
    assert "FLOW_SKILL_DIR" in body
    assert "Approval is not coding" in body


def test_agents_md_appends_to_existing_without_clobber(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("# House rules\nUse tabs.\n", encoding="utf-8")
    cfg = dataclasses.replace(_jira_config(tmp_path), agents_md=True)
    initmod.run_init(cfg)
    body = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "# House rules" in body  # original preserved
    assert body.count(initmod._AGENTS_MARKER) == 1  # stanza added once


def test_ensure_agents_md_is_idempotent(tmp_path: Path) -> None:
    assert initmod._ensure_agents_md(tmp_path, requested=True) is None  # first write
    skipped = initmod._ensure_agents_md(tmp_path, requested=True)  # second is a no-op
    assert skipped is not None
    assert skipped.get("skipped") is True
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8").count(initmod._AGENTS_MARKER) == 1


def test_existing_agents_block_is_upgraded_without_repeating_opt_in(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    prefix = "# House rules\n\n"
    suffix = "\n\n## Local notes\nKeep this byte-for-byte.\n"
    agents.write_text(
        prefix + "<!-- flow:begin -->\nold flow instructions\n<!-- flow:end -->" + suffix,
        encoding="utf-8",
    )

    assert initmod._ensure_agents_md(tmp_path, requested=False) is None

    body = agents.read_text(encoding="utf-8")
    assert body.startswith(prefix)
    assert body.endswith(suffix)
    assert "old flow instructions" not in body
    assert "$flow:flow" in body
    assert "explicit workdir" in body
    assert "--recover-spill" not in body


def test_reconfigure_upgrades_persisted_agents_opt_in(tmp_path: Path) -> None:
    initmod.run_init(dataclasses.replace(_jira_config(tmp_path), agents_md=True))
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        "before\n<!-- flow:begin -->\nold flow instructions\n<!-- flow:end -->\nafter\n",
        encoding="utf-8",
    )

    initmod.run_init(_jira_config(tmp_path), reconfigure=True)

    body = agents.read_text(encoding="utf-8")
    assert body.startswith("before\n")
    assert body.endswith("\nafter\n")
    assert "old flow instructions" not in body
    assert "$flow:flow" in body


@pytest.mark.parametrize(
    "body",
    [
        "before\n<!-- flow:begin -->\nunclosed\n",
        "before\n<!-- flow:end -->\n",
        "<!-- flow:begin -->\na\n<!-- flow:begin -->\nb\n<!-- flow:end -->\n",
        "<!-- flow:begin -->\na\n<!-- flow:end -->\n<!-- flow:end -->\n",
        "<!-- flow:end -->\nbackwards\n<!-- flow:begin -->\n",
    ],
)
def test_malformed_agents_markers_fail_without_rewriting(tmp_path: Path, body: str) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text(body, encoding="utf-8")

    with pytest.raises(initmod.InitError, match=r"flow:begin|flow:end"):
        initmod._ensure_agents_md(tmp_path, requested=False)

    assert agents.read_text(encoding="utf-8") == body


def test_ensure_agents_md_not_requested_is_noop(tmp_path: Path) -> None:
    skipped = initmod._ensure_agents_md(tmp_path, requested=False)
    assert skipped is not None
    assert skipped.get("skipped") is True
    assert not (tmp_path / "AGENTS.md").exists()


def test_init_skill_dir_falls_back_to_script_location(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_SKILL_DIR", raising=False)
    initmod.run_init(_jira_config(tmp_path))
    written = (tmp_path / ".flow" / "runtime" / "skill-root").read_text(encoding="utf-8").strip()
    expected = str(Path(initmod.__file__).resolve().parent.parent)
    assert written == expected
    assert Path(written).is_absolute()


def test_bare_beads_init_runs_bd_and_writes_workspace_toml(tmp_path: Path) -> None:
    runner = _bd_ok_runner()
    result = initmod.run_init(_beads_config(tmp_path), runner=runner)
    data = tomllib.loads(result.workspace_toml_path.read_text(encoding="utf-8"))
    assert data["tracker"]["backend"] == "beads"
    assert data["tracker"]["beads"]["prefix"] == "testpkg"
    assert data["tracker"]["beads"]["shared_server"] is True
    # Beads workspaces still get FT/code_review/etc handlers from defaults.
    assert data["pipeline"]["handlers"]["plan"] == "subagent:Plan"


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


def test_recommended_bundle_composes_from_discovered_manifests(tmp_path: Path) -> None:
    search_root = tmp_path / "plugins"
    _write_manifest(search_root / "ship-it", _ship_it_manifest())
    _write_manifest(search_root / "code-review", _code_review_manifest())
    config = initmod.InitConfig(
        backend="jira",
        bundle="recommended",
        workspace_root=tmp_path,
        jira=initmod.JiraConfig(cloud_id="x", project_key="FT", assignee_account_id=None),
        bundle_search_roots=[search_root],
        checkpoint_manifest_path=tmp_path / "_ckpt.jsonl",
    )
    result = initmod.run_init(config)
    assert result.handlers["create_pr"] == "skill:ship-it:create"
    assert result.handlers["review_loop"] == "skill:ship-it:feedback"
    assert result.handlers["code_review"] == "skill:code-review"


def test_recommended_bundle_conflict_raises(tmp_path: Path) -> None:
    search_root = tmp_path / "plugins"
    _write_manifest(search_root / "ship-it", _ship_it_manifest())
    _write_manifest(
        search_root / "rival-pr",
        """schema_version = 1
[bundle]
name = "rival-pr"
description = ""
[skills.create_pr]
handler_string = "skill:rival-pr:create"
""",
    )
    config = initmod.InitConfig(
        backend="jira",
        bundle="recommended",
        workspace_root=tmp_path,
        jira=initmod.JiraConfig(cloud_id="x", project_key="FT", assignee_account_id=None),
        bundle_search_roots=[search_root],
        checkpoint_manifest_path=tmp_path / "_ckpt.jsonl",
    )
    with pytest.raises(initmod.BundleConflictError, match="create_pr"):
        initmod.run_init(config)


def test_custom_bundle_uses_supplied_handlers(tmp_path: Path) -> None:
    config = initmod.InitConfig(
        backend="jira",
        bundle="custom",
        workspace_root=tmp_path,
        jira=initmod.JiraConfig(cloud_id="x", project_key="FT", assignee_account_id=None),
        handler_overrides={
            "create_pr": "skill:ship-it:create",
            "e2e": "subagent:general-purpose",
        },
        bundle_search_roots=[tmp_path / "_empty"],
        checkpoint_manifest_path=tmp_path / "_ckpt.jsonl",
    )
    result = initmod.run_init(config)
    assert result.handlers["create_pr"] == "skill:ship-it:create"
    assert result.handlers["e2e"] == "subagent:general-purpose"
    # Stages not overridden keep stage-registry defaults.
    assert result.handlers["plan"] == "subagent:Plan"


def test_custom_bundle_requires_at_least_one_override(tmp_path: Path) -> None:
    config = initmod.InitConfig(
        backend="jira",
        bundle="custom",
        workspace_root=tmp_path,
        jira=initmod.JiraConfig(cloud_id="x", project_key="FT", assignee_account_id=None),
        bundle_search_roots=[tmp_path / "_empty"],
        checkpoint_manifest_path=tmp_path / "_ckpt.jsonl",
    )
    with pytest.raises(initmod.InitError, match="custom requires"):
        initmod.run_init(config)


def test_custom_bundle_rejects_illegal_handler_string(tmp_path: Path) -> None:
    config = initmod.InitConfig(
        backend="jira",
        bundle="custom",
        workspace_root=tmp_path,
        jira=initmod.JiraConfig(cloud_id="x", project_key="FT", assignee_account_id=None),
        handler_overrides={"create_pr": "bogus-handler-string"},
        bundle_search_roots=[tmp_path / "_empty"],
        checkpoint_manifest_path=tmp_path / "_ckpt.jsonl",
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
        bundle_search_roots=[tmp_path / "_empty"],
        checkpoint_manifest_path=tmp_path / "_ckpt.jsonl",
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
    assert result.handlers["plan"] == "subagent:Plan"


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


def test_checkpoint_manifest_appended(tmp_path: Path) -> None:
    ckpt = tmp_path / "_ckpt.jsonl"
    initmod.run_init(_jira_config(tmp_path))
    lines = ckpt.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["backend"] == "jira"
    assert entry["namespace"] == "FT"
    assert entry["compounding"] is True
    assert "workspace_root" in entry


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
        bundle_search_roots=[tmp_path / "_empty"],
        checkpoint_manifest_path=tmp_path / "_ckpt.jsonl",
    )
    result = initmod.run_init(config)
    data = tomllib.loads(result.workspace_toml_path.read_text(encoding="utf-8"))
    assert "reflect" not in data["pipeline"]["stages"]
    assert data["memory"]["compounding"] is False


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_bare_jira(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ckpt = tmp_path / "_ckpt.jsonl"
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
            "--checkpoint-manifest",
            str(ckpt),
            "--bundle-search-roots",
            str(tmp_path / "_empty"),
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


def test_cli_guidance_only_updates_initialized_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    initmod.run_init(_jira_config(tmp_path))

    rc = initmod.cli_main(["--guidance-only", "--workspace-root", str(tmp_path)])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"changed": True, "guidance": str(tmp_path / "AGENTS.md")}
    assert "$flow:flow" in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")


def test_cli_guidance_only_refuses_uninitialized_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = initmod.cli_main(["--guidance-only", "--workspace-root", str(tmp_path)])

    assert rc == 1
    assert "initialized workspace" in capsys.readouterr().err
    assert not (tmp_path / "AGENTS.md").exists()


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
            "--checkpoint-manifest",
            str(tmp_path / "_ckpt.jsonl"),
            "--bundle-search-roots",
            str(tmp_path / "_empty"),
        ]
    )
    assert rc == 4


def test_cli_bundle_conflict_exit_code(tmp_path: Path) -> None:
    search_root = tmp_path / "plugins"
    _write_manifest(search_root / "ship-it", _ship_it_manifest())
    _write_manifest(
        search_root / "rival",
        """schema_version = 1
[bundle]
name = "rival"
description = ""
[skills.create_pr]
handler_string = "skill:rival:create"
""",
    )
    rc = initmod.cli_main(
        [
            "--backend",
            "jira",
            "--bundle",
            "recommended",
            "--workspace-root",
            str(tmp_path),
            "--jira-cloud-id",
            "x",
            "--jira-project-key",
            "FT",
            "--checkpoint-manifest",
            str(tmp_path / "_ckpt.jsonl"),
            "--bundle-search-roots",
            str(search_root),
        ]
    )
    assert rc == 3


def test_cli_config_file_provides_answers(tmp_path: Path) -> None:
    answers = tmp_path / "answers.json"
    answers.write_text(
        json.dumps(
            {
                "backend": "jira",
                "bundle": "bare",
                "workspace_root": str(tmp_path),
                "jira_cloud_id": "x",
                "jira_project_key": "FT",
                "checkpoint_manifest": str(tmp_path / "_ckpt.jsonl"),
                "bundle_search_roots": str(tmp_path / "_empty"),
            }
        ),
        encoding="utf-8",
    )
    rc = initmod.cli_main(["--config", str(answers)])
    assert rc == 0
    assert (tmp_path / ".flow" / ".initialized").exists()


# ─── Slug derivation ─────────────────────────────────────────────────────────


def test_derive_slug_normalizes() -> None:
    assert initmod._derive_slug("Safe Mic") == "safe-mic"
    assert initmod._derive_slug("Foo--Bar") == "foo-bar"
    assert initmod._derive_slug("UPPER") == "upper"
    assert initmod._derive_slug("with/slashes") == "with-slashes"


# ─── [U] --config JSON list normalization ─────────────────────────────────────


def test_config_bundle_search_roots_as_json_list(tmp_path: Path) -> None:
    # A --config file may hand bundle_search_roots as a JSON list. It must not
    # crash on .split(":") and the listed root must be honored for discovery.
    search_root = tmp_path / "plugins"
    _write_manifest(search_root / "code-review", _code_review_manifest())
    answers = tmp_path / "answers.json"
    answers.write_text(
        json.dumps(
            {
                "backend": "jira",
                "bundle": "recommended",
                "workspace_root": str(tmp_path),
                "jira_cloud_id": "x",
                "jira_project_key": "FT",
                "checkpoint_manifest": str(tmp_path / "_ckpt.jsonl"),
                "bundle_search_roots": [str(search_root)],
            }
        ),
        encoding="utf-8",
    )
    rc = initmod.cli_main(["--config", str(answers)])
    assert rc == 0
    data = tomllib.loads((tmp_path / ".flow" / "workspace.toml").read_text(encoding="utf-8"))
    assert data["pipeline"]["handlers"]["code_review"] == "skill:code-review"


def test_coerce_search_roots_handles_string_and_list(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    assert initmod._coerce_search_roots(None) is None
    assert initmod._coerce_search_roots(f"{a}:{b}") == [a, b]
    assert initmod._coerce_search_roots([str(a), str(b)]) == [a, b]


def test_coerce_checkpoint_path_handles_string_and_list(tmp_path: Path) -> None:
    p = tmp_path / "ckpt.jsonl"
    assert initmod._coerce_checkpoint_path(None) is None
    assert initmod._coerce_checkpoint_path(str(p)) == p.resolve()
    assert initmod._coerce_checkpoint_path([str(p)]) == p.resolve()


# ─── [V] validate before marker ──────────────────────────────────────────────


def test_invalid_input_leaves_no_initializing_marker(tmp_path: Path) -> None:
    # custom bundle with no handler overrides fails validation. The failure must
    # NOT leave a .initializing marker behind.
    bad = initmod.InitConfig(
        backend="jira",
        bundle="custom",
        workspace_root=tmp_path,
        jira=initmod.JiraConfig(cloud_id="x", project_key="FT", assignee_account_id=None),
        bundle_search_roots=[tmp_path / "_empty"],
        checkpoint_manifest_path=tmp_path / "_ckpt.jsonl",
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
        bundle_search_roots=[tmp_path / "_empty"],
        checkpoint_manifest_path=tmp_path / "_ckpt.jsonl",
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
        bundle_search_roots=[tmp_path / "_empty"],
        checkpoint_manifest_path=tmp_path / "_ckpt.jsonl",
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
    first = dataclasses.replace(_jira_config(tmp_path), agents_md=True)
    initmod.run_init(first)

    flow_path = tmp_path / ".flow" / "runtime" / "flow"
    skill_path = tmp_path / ".flow" / "runtime" / "skill-root"
    agents_path = tmp_path / "AGENTS.md"
    old_agents = (
        "# User-owned preface\n"
        "<!-- flow:begin -->\nold managed guidance\n<!-- flow:end -->\n"
        "User-owned suffix\n"
    )
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


def test_launcher_failure_does_not_append_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = tmp_path / "_ckpt.jsonl"

    def fail_install(workspace_root: Path, *, skill_dir: Path | None = None) -> tuple[Path, Path]:
        del workspace_root, skill_dir
        raise OSError("injected launcher failure")

    monkeypatch.setattr(initmod.flow_launcher, "install", fail_install)

    with pytest.raises(initmod.InitError, match="launcher failure"):
        initmod.run_init(_jira_config(tmp_path))

    assert not checkpoint.exists()


def test_failed_reconfigure_removes_files_absent_before_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initmod.run_init(_jira_config(tmp_path))
    generated = (
        tmp_path / ".flow" / "runtime" / "flow",
        tmp_path / ".flow" / "runtime" / "skill-root",
        tmp_path / "AGENTS.md",
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
        initmod.run_init(
            dataclasses.replace(_jira_config(tmp_path), agents_md=True), reconfigure=True
        )

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
        bundle_search_roots=[tmp_path / "_empty"],
        checkpoint_manifest_path=tmp_path / "_ckpt.jsonl",
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
        bundle_search_roots=[tmp_path / "_empty"],
        checkpoint_manifest_path=tmp_path / "_ckpt.jsonl",
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

    checkpoint = config.checkpoint_manifest_path
    assert checkpoint is not None
    entries = [json.loads(line) for line in checkpoint.read_text(encoding="utf-8").splitlines()]
    assert entries[-1]["init_run_id"] != stale_run_id
    assert not (flow_dir / ".initializing").exists()
    assert not (flow_dir / ".init-progress").exists()


# ─── [X] resume idempotency ───────────────────────────────────────────────────


def test_resume_does_not_duplicate_checkpoint_line(tmp_path: Path) -> None:
    # Simulate a crash after the checkpoint was appended but before its progress
    # phase was recorded. The run id lives in the .initializing marker.
    flow_dir = tmp_path / ".flow"
    flow_dir.mkdir()
    run_id = "fixedrunid"
    (flow_dir / ".initializing").write_text(run_id + "\n", encoding="utf-8")
    (flow_dir / "workspace.toml").write_text('[memory]\nnamespace = "FT"\n', encoding="utf-8")
    ckpt = tmp_path / "_ckpt.jsonl"
    ckpt.write_text(
        json.dumps(
            {
                "ts": "2026-05-28T00:00:00Z",
                "workspace_root": str(tmp_path.resolve()),
                "init_run_id": run_id,
                "backend": "jira",
                "namespace": "FT",
                "compounding": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # Progress recorded through verify_postconditions; append_checkpoint NOT yet.
    done = [
        "validate_inputs",
        "bundle_compose",
        "mkdirs",
        "bd_init",
        "write_workspace_toml",
        "verify_postconditions",
    ]
    (flow_dir / ".init-progress").write_text(
        "".join(json.dumps({"phase": p, "ts": "2026-05-28T00:00:00Z"}) + "\n" for p in done),
        encoding="utf-8",
    )

    initmod.run_init(_jira_config(tmp_path), resume=True)
    lines = ckpt.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


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


def test_recommended_with_no_coverage_refuses(tmp_path: Path) -> None:
    # An empty search root yields zero discovered manifests. recommended would
    # silently degrade to bare; refuse instead per no-silent-degrade.
    config = initmod.InitConfig(
        backend="jira",
        bundle="recommended",
        workspace_root=tmp_path,
        jira=initmod.JiraConfig(cloud_id="x", project_key="FT", assignee_account_id=None),
        bundle_search_roots=[tmp_path / "_empty"],
        checkpoint_manifest_path=tmp_path / "_ckpt.jsonl",
    )
    with pytest.raises(initmod.InitError, match="no discovered manifests"):
        initmod.run_init(config)
    assert not (tmp_path / ".flow" / ".initialized").exists()


def test_compose_rejects_empty_skill_handler(tmp_path: Path) -> None:
    # Defense in depth: even if a manifest with handler_string "skill:" (empty
    # name) reaches composition, init must reject it before it lands a nameless
    # handler in workspace.toml. Built directly to bypass bundle_discover's own
    # validation and exercise the init-level guard.
    from bundle_discover import DiscoveryResult, Manifest, ManifestSkill

    config = initmod.InitConfig(
        backend="jira",
        bundle="recommended",
        workspace_root=tmp_path,
        jira=initmod.JiraConfig(cloud_id="x", project_key="FT", assignee_account_id=None),
        bundle_search_roots=[tmp_path / "_empty"],
        checkpoint_manifest_path=tmp_path / "_ckpt.jsonl",
    )
    registry = initmod._load_stage_registry()
    stages = initmod._default_pipeline_stages(registry, config.memory_compounding)
    discovery = DiscoveryResult(
        valid=[
            Manifest(
                path="x",
                bundle_name="nameless",
                bundle_description="",
                skills=[ManifestSkill(stage="create_pr", handler_string="skill:")],
            )
        ]
    )
    with pytest.raises(initmod.InitError, match="illegal handler"):
        initmod._compose_handlers(config, registry, stages, discovery)


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


# ─── checkpoint mode (phase 8d) ──────────────────────────────────────────────


def test_resolve_checkpoint_mode_defaults_and_matrix() -> None:
    assert initmod._resolve_checkpoint_mode("jira", None) == "work"
    assert initmod._resolve_checkpoint_mode("beads", None) == "personal"
    assert initmod._resolve_checkpoint_mode("beads", "scratch") == "scratch"
    assert initmod._resolve_checkpoint_mode("jira", "scratch") == "scratch"


def test_jira_personal_checkpoint_mode_rejected() -> None:
    with pytest.raises(initmod.InitError, match="not allowed"):
        initmod._resolve_checkpoint_mode("jira", "personal")


def test_beads_work_checkpoint_mode_rejected() -> None:
    with pytest.raises(initmod.InitError, match="not allowed"):
        initmod._resolve_checkpoint_mode("beads", "work")


def test_checkpoint_entry_records_mode_and_initialized_at(tmp_path: Path) -> None:
    initmod.run_init(_jira_config(tmp_path))
    ckpt = tmp_path / "_ckpt.jsonl"
    entries = [
        json.loads(line) for line in ckpt.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert entries[-1]["checkpoint_mode"] == "work"
    assert entries[-1]["initialized_at"] == entries[-1]["ts"]


# ─── flow-nnft: reconfigure preserves customized handlers ──────────────────


def test_reconfigure_preserves_customized_handler(tmp_path: Path) -> None:
    # The incident: a customized handler must survive `--reconfigure`, not silently
    # reset to the registry default (code_review default is "inline").
    first = dataclasses.replace(
        _jira_config(tmp_path),
        bundle="custom",
        handler_overrides={"code_review": "skill:code-review"},
    )
    initmod.run_init(first)
    result = initmod.run_init(
        dataclasses.replace(_jira_config(tmp_path), bundle="bare"), reconfigure=True
    )
    assert result.handlers["code_review"] == "skill:code-review"


def test_reconfigure_handler_flag_overrides_preservation(tmp_path: Path) -> None:
    # Explicit --handler beats preservation, even when it resets a stage to default.
    first = dataclasses.replace(
        _jira_config(tmp_path),
        bundle="custom",
        handler_overrides={"code_review": "skill:code-review"},
    )
    initmod.run_init(first)
    second = dataclasses.replace(
        _jira_config(tmp_path),
        bundle="custom",
        handler_overrides={"code_review": "inline"},
    )
    result = initmod.run_init(second, reconfigure=True)
    assert result.handlers["code_review"] == "inline"


def test_reconfigure_preservation_beats_manifest(tmp_path: Path) -> None:
    # Prior customization outranks a discovered manifest. Three distinct values:
    # prior subagent:general-purpose != manifest skill:code-review != default inline.
    search_root = tmp_path / "plugins"
    _write_manifest(search_root / "code-review", _code_review_manifest())
    first = dataclasses.replace(
        _jira_config(tmp_path),
        bundle="custom",
        handler_overrides={"code_review": "subagent:general-purpose"},
        bundle_search_roots=[search_root],
    )
    initmod.run_init(first)
    second = dataclasses.replace(
        _jira_config(tmp_path),
        bundle="recommended",
        bundle_search_roots=[search_root],
    )
    result = initmod.run_init(second, reconfigure=True)
    assert result.handlers["code_review"] == "subagent:general-purpose"


def test_fresh_init_preserves_nothing(tmp_path: Path) -> None:
    # No reconfigure -> existing_handlers is {} -> handlers equal registry defaults.
    result = initmod.run_init(dataclasses.replace(_jira_config(tmp_path), bundle="bare"))
    assert result.handlers["code_review"] == "inline"
    assert result.handlers["e2e"] == "subagent:general-purpose"
    assert result.discovery_warnings == []


def test_reconfigure_freezes_value_differing_from_current_default(tmp_path: Path) -> None:
    # A prior value that differs from the current default is frozen on reconfigure
    # (e2e default is subagent:general-purpose; a prior "none" is preserved).
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


def test_reconfigure_preserved_warning_names_value_and_default(tmp_path: Path) -> None:
    # The reset-that-wasn't is legible: the warning carries value AND registry default.
    first = dataclasses.replace(
        _jira_config(tmp_path),
        bundle="custom",
        handler_overrides={"e2e": "none"},
    )
    initmod.run_init(first)
    result = initmod.run_init(
        dataclasses.replace(_jira_config(tmp_path), bundle="bare"), reconfigure=True
    )
    line = next(w for w in result.discovery_warnings if "e2e" in w)
    assert "none" in line
    assert "subagent:general-purpose" in line


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


def test_stabilize_skill_dir_non_cache_unchanged() -> None:
    assert flow_launcher.stabilize_skill_dir("/opt/flow/skills/flow") == "/opt/flow/skills/flow"


def test_stabilize_skill_dir_cache_but_marketplace_missing_unchanged(tmp_path: Path) -> None:
    # Cache-shaped input but no marketplace target on disk -> returned unchanged.
    cache = tmp_path / "plugins" / "cache" / "vdsmon-flow" / "flow" / "0.92.1" / "skills" / "flow"
    assert flow_launcher.stabilize_skill_dir(str(cache)) == str(cache)
