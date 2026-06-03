"""Contract tests for init.py — transactional workspace bootstrap.

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

import json
import subprocess
import tomllib
from pathlib import Path

import pytest

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
    assert result.namespace == "FT"


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
    handlers = data["pipeline"]["handlers"]
    # Bare defaults from stage-registry.toml.
    assert handlers["plan"] == "subagent:Plan"
    assert handlers["implement"] == "subagent:general-purpose"
    assert handlers["create_pr"] == "none"
    assert handlers["review_loop"] == "none"
    assert handlers["code_review"] == "inline"


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
    assert (tmp_path / ".flow" / "FT").is_dir()
    assert (tmp_path / ".flow" / "FT" / "ship-events").is_dir()


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


# ─── [X] resume idempotency ───────────────────────────────────────────────────


def test_resume_does_not_duplicate_checkpoint_line(tmp_path: Path) -> None:
    # Simulate a crash after the checkpoint was appended but before its progress
    # phase was recorded. The run id lives in the .initializing marker.
    flow_dir = tmp_path / ".flow"
    flow_dir.mkdir()
    run_id = "fixedrunid"
    (flow_dir / ".initializing").write_text(run_id + "\n", encoding="utf-8")
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
    assert len(lines) == 1  # not duplicated


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
    ) -> tuple[dict[str, str], list[str]]:
        del config, registry, discovery
        return {stage: "bogus" for stage in pipeline_stages}, []

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
