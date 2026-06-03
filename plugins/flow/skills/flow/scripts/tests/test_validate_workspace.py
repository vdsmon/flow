"""Contract tests for validate_workspace.py.

Covers every schema-violation branch + the happy path. Uses tmp_path as the
workspace root; writes minimal `.flow/.initialized` + `.flow/workspace.toml`
fixtures and asserts the validator's verdict + violations.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import validate_workspace as vw

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_workspace(
    tmp_path: Path,
    *,
    backend: str = "jira",
    stages: list[str] | None = None,
    handlers: dict[str, str] | None = None,
    memory: dict[str, object] | None = None,
    initialized: bool = True,
    workspace_toml_content: str | None = None,
) -> Path:
    flow = tmp_path / ".flow"
    flow.mkdir()
    if initialized:
        (flow / ".initialized").touch()
    if workspace_toml_content is not None:
        (flow / "workspace.toml").write_text(workspace_toml_content, encoding="utf-8")
        return tmp_path

    if stages is None:
        stages = ["ticket", "plan", "implement", "commit", "reflect"]
    if handlers is None:
        handlers = {s: "inline" for s in stages}
    if memory is None:
        memory = {
            "namespace": "FT",
            "auto_recall": True,
            "compounding": True,
            "recall_by": ["branch", "current-ticket"],
            "recall_top_n": 5,
        }

    lines: list[str] = []
    lines.append("[tracker]")
    lines.append(f'backend = "{backend}"')
    lines.append("")
    if backend == "jira":
        lines.append("[tracker.jira]")
        lines.append('cloud_id = "cloud-x"')
        lines.append('project_key = "FT"')
    elif backend == "beads":
        lines.append("[tracker.beads]")
        lines.append('prefix = "testpkg"')
    lines.append("")
    lines.append("[pipeline]")
    lines.append("stages = [" + ", ".join(f'"{s}"' for s in stages) + "]")
    lines.append("")
    lines.append("[pipeline.handlers]")
    for stage, value in handlers.items():
        lines.append(f'{stage} = "{value}"')
    lines.append("")
    lines.append("[memory]")
    for k, v in memory.items():
        if isinstance(v, bool):
            lines.append(f"{k} = {str(v).lower()}")
        elif isinstance(v, int):
            lines.append(f"{k} = {v}")
        elif isinstance(v, list):
            lines.append(f"{k} = [" + ", ".join(f'"{x}"' for x in v) + "]")
        else:
            lines.append(f'{k} = "{v}"')
    (flow / "workspace.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


# ─── Happy paths ─────────────────────────────────────────────────────────────


def test_valid_jira_workspace_passes(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path)
    result, snapshot = vw.validate(root)
    assert result.ok
    assert snapshot is not None
    assert snapshot.backend == "jira"
    assert snapshot.stages == ["ticket", "plan", "implement", "commit", "reflect"]
    assert snapshot.namespace == "FT"
    assert snapshot.compounding is True


def test_valid_beads_workspace_passes(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path, backend="beads")
    result, snapshot = vw.validate(root)
    assert result.ok
    assert snapshot is not None
    assert snapshot.backend == "beads"


# ─── .flow/.initialized ──────────────────────────────────────────────────────


def test_missing_initialized_marker_fails(tmp_path: Path) -> None:
    _make_workspace(tmp_path, initialized=False)
    result, snapshot = vw.validate(tmp_path)
    assert not result.ok
    assert snapshot is None
    assert any(".initialized" in v for v in result.violations)


def test_missing_workspace_toml_fails(tmp_path: Path) -> None:
    flow = tmp_path / ".flow"
    flow.mkdir()
    (flow / ".initialized").touch()
    result, snapshot = vw.validate(tmp_path)
    assert not result.ok
    assert snapshot is None
    assert any("workspace.toml" in v for v in result.violations)


def test_malformed_toml_fails(tmp_path: Path) -> None:
    _make_workspace(tmp_path, workspace_toml_content="this is not = valid [ toml")
    result, _ = vw.validate(tmp_path)
    assert not result.ok
    assert any("failed to parse" in v for v in result.violations)


# ─── [tracker] block ─────────────────────────────────────────────────────────


def test_missing_tracker_block_fails(tmp_path: Path) -> None:
    _make_workspace(
        tmp_path,
        workspace_toml_content="""[pipeline]
stages = ["ticket"]
[pipeline.handlers]
ticket = "inline"
[memory]
namespace = "x"
auto_recall = true
compounding = true
recall_by = ["branch"]
recall_top_n = 5
""",
    )
    result, _ = vw.validate(tmp_path)
    assert any("tracker:" in v for v in result.violations)


def test_unknown_backend_fails(tmp_path: Path) -> None:
    _make_workspace(
        tmp_path,
        workspace_toml_content="""[tracker]
backend = "github"
[pipeline]
stages = ["ticket"]
[pipeline.handlers]
ticket = "inline"
[memory]
namespace = "x"
auto_recall = true
compounding = true
recall_by = ["branch"]
recall_top_n = 5
""",
    )
    result, _ = vw.validate(tmp_path)
    assert any("tracker.backend" in v for v in result.violations)


def test_jira_missing_cloud_id_fails(tmp_path: Path) -> None:
    _make_workspace(
        tmp_path,
        workspace_toml_content="""[tracker]
backend = "jira"
[tracker.jira]
project_key = "FT"
[pipeline]
stages = ["ticket"]
[pipeline.handlers]
ticket = "inline"
[memory]
namespace = "x"
auto_recall = true
compounding = true
recall_by = ["branch"]
recall_top_n = 5
""",
    )
    result, _ = vw.validate(tmp_path)
    assert any("tracker.jira.cloud_id" in v for v in result.violations)


def test_beads_missing_prefix_fails(tmp_path: Path) -> None:
    _make_workspace(
        tmp_path,
        workspace_toml_content="""[tracker]
backend = "beads"
[tracker.beads]
[pipeline]
stages = ["ticket"]
[pipeline.handlers]
ticket = "inline"
[memory]
namespace = "x"
auto_recall = true
compounding = true
recall_by = ["branch"]
recall_top_n = 5
""",
    )
    result, _ = vw.validate(tmp_path)
    assert any("tracker.beads.prefix" in v for v in result.violations)


# ─── [pipeline] block ────────────────────────────────────────────────────────


def test_empty_stages_fails(tmp_path: Path) -> None:
    _make_workspace(tmp_path, stages=[])
    result, _ = vw.validate(tmp_path)
    assert any("pipeline.stages" in v for v in result.violations)


def test_unknown_stage_fails(tmp_path: Path) -> None:
    _make_workspace(tmp_path, stages=["bogus_stage"], handlers={"bogus_stage": "inline"})
    result, _ = vw.validate(tmp_path)
    assert any("not registered" in v for v in result.violations)


def test_missing_handler_for_stage_fails(tmp_path: Path) -> None:
    _make_workspace(
        tmp_path,
        stages=["ticket", "plan"],
        handlers={"ticket": "inline"},  # plan handler missing
    )
    result, _ = vw.validate(tmp_path)
    assert any("pipeline.handlers.plan" in v for v in result.violations)


def test_invalid_handler_string_fails(tmp_path: Path) -> None:
    _make_workspace(
        tmp_path,
        stages=["ticket"],
        handlers={"ticket": "garbage:value"},
    )
    result, _ = vw.validate(tmp_path)
    assert any("does not match" in v for v in result.violations)


def test_predecessor_out_of_order_fails(tmp_path: Path) -> None:
    # plan requires ticket; here it precedes ticket.
    _make_workspace(
        tmp_path,
        stages=["plan", "ticket"],
        handlers={"plan": "inline", "ticket": "inline"},
    )
    result, _ = vw.validate(tmp_path)
    assert any("predecessor" in v for v in result.violations)


def test_missing_predecessor_in_pipeline_ok(tmp_path: Path) -> None:
    # Workspace omits ticket entirely (allowed; user choice).
    _make_workspace(
        tmp_path,
        stages=["plan", "implement", "commit"],
        handlers={"plan": "inline", "implement": "inline", "commit": "inline"},
    )
    result, _ = vw.validate(tmp_path)
    # Predecessor check is "ordered if present"; missing predecessor is allowed.
    # reflect is required_when_compounding=true so its absence fails (separate check).
    assert all("predecessor" not in v for v in result.violations)


def test_required_when_compounding_missing_fails(tmp_path: Path) -> None:
    _make_workspace(
        tmp_path,
        stages=["ticket", "plan"],
        handlers={"ticket": "inline", "plan": "inline"},
    )
    result, _ = vw.validate(tmp_path)
    assert any("reflect" in v and "compounding" in v for v in result.violations)


def test_required_when_compounding_skip_when_compounding_false(tmp_path: Path) -> None:
    _make_workspace(
        tmp_path,
        stages=["ticket", "plan"],
        handlers={"ticket": "inline", "plan": "inline"},
        memory={
            "namespace": "x",
            "auto_recall": True,
            "compounding": False,
            "recall_by": ["branch"],
            "recall_top_n": 5,
        },
    )
    result, _ = vw.validate(tmp_path)
    assert all("compounding" not in v for v in result.violations)


# ─── Handler-string variants ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "handler",
    [
        "inline",
        "none",
        "subagent:Plan",
        "subagent:general-purpose",
        "skill:ship-it",
        "skill:ship-it:create",
        "skill:ship-it:feedback",
    ],
)
def test_legal_handler_strings_accepted(tmp_path: Path, handler: str) -> None:
    _make_workspace(
        tmp_path,
        stages=["ticket"],
        handlers={"ticket": handler},
        memory={
            "namespace": "x",
            "auto_recall": True,
            "compounding": False,  # disable reflect-required check
            "recall_by": ["branch"],
            "recall_top_n": 5,
        },
    )
    result, snapshot = vw.validate(tmp_path)
    assert result.ok, result.violations
    assert snapshot is not None
    assert snapshot.handlers["ticket"] == handler


@pytest.mark.parametrize(
    "handler",
    [
        "subagent:",  # empty subagent type
        "inline-with-suffix",
        "agent:Plan",
        "skill:",  # empty skill name
        "  inline  ",  # whitespace
    ],
)
def test_illegal_handler_strings_rejected(tmp_path: Path, handler: str) -> None:
    _make_workspace(
        tmp_path,
        stages=["ticket"],
        handlers={"ticket": handler},
        memory={
            "namespace": "x",
            "auto_recall": True,
            "compounding": False,
            "recall_by": ["branch"],
            "recall_top_n": 5,
        },
    )
    result, _ = vw.validate(tmp_path)
    assert any("does not match" in v for v in result.violations)


# ─── [memory] block ──────────────────────────────────────────────────────────


def test_missing_memory_namespace_fails(tmp_path: Path) -> None:
    _make_workspace(
        tmp_path,
        memory={
            "auto_recall": True,
            "compounding": True,
            "recall_by": ["branch"],
            "recall_top_n": 5,
        },
    )
    result, _ = vw.validate(tmp_path)
    assert any("memory.namespace" in v for v in result.violations)


def test_memory_recall_top_n_must_be_int(tmp_path: Path) -> None:
    _make_workspace(
        tmp_path,
        workspace_toml_content="""[tracker]
backend = "jira"
[tracker.jira]
cloud_id = "x"
project_key = "FT"
[pipeline]
stages = ["ticket", "plan", "implement", "commit", "reflect"]
[pipeline.handlers]
ticket = "inline"
plan = "inline"
implement = "inline"
commit = "inline"
reflect = "inline"
[memory]
namespace = "x"
auto_recall = true
compounding = true
recall_by = ["branch"]
recall_top_n = "five"
""",
    )
    result, _ = vw.validate(tmp_path)
    assert any("memory.recall_top_n" in v for v in result.violations)


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_returns_0_on_valid_workspace(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path)
    rc = vw.cli_main(["--workspace-root", str(root)])
    assert rc == 0


def test_cli_returns_1_on_invalid_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_workspace(tmp_path, initialized=False)
    rc = vw.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 1
    assert "initialized" in capsys.readouterr().err


# ─── --emit-canonical-snapshot ────────────────────────────────────────────────


def _snapshot_paths(root: Path, ticket: str) -> tuple[Path, Path]:
    run_dir = root / ".flow" / "runs" / ticket
    return run_dir / "snapshot.json", run_dir / "snapshot.sha"


def test_emit_canonical_snapshot_writes_files_on_valid_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _make_workspace(tmp_path)
    json_path, sha_path = _snapshot_paths(root, "T")
    rc = vw.cli_main(["--workspace-root", str(root), "--ticket", "T", "--emit-canonical-snapshot"])
    assert rc == 0
    assert json_path.exists()
    assert sha_path.exists()
    assert str(json_path) in capsys.readouterr().out


def test_emit_canonical_snapshot_skipped_on_invalid_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_workspace(tmp_path, initialized=False)
    json_path, sha_path = _snapshot_paths(tmp_path, "T")
    rc = vw.cli_main(
        ["--workspace-root", str(tmp_path), "--ticket", "T", "--emit-canonical-snapshot"]
    )
    assert rc == 1
    assert not json_path.exists()
    assert not sha_path.exists()
    assert "initialized" in capsys.readouterr().err


def test_no_flag_does_not_write_snapshot(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path)
    json_path, sha_path = _snapshot_paths(root, "T")
    rc = vw.cli_main(["--workspace-root", str(root)])
    assert rc == 0
    assert not json_path.exists()
    assert not sha_path.exists()


def test_emit_flag_requires_ticket(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path)
    with pytest.raises(SystemExit):
        vw.cli_main(["--workspace-root", str(root), "--emit-canonical-snapshot"])


def test_emit_snapshot_matches_snapshot_emit_cli(tmp_path: Path) -> None:
    # both the validate --emit path and snapshot.py's own emit must produce the
    # identical master_hash for the same on-disk content.
    import snapshot as snap

    root = _make_workspace(tmp_path)
    skill_root = Path(vw.__file__).resolve().parent.parent
    expected = snap.compute_snapshot(root, skill_root=skill_root)["master_hash"]

    rc = vw.cli_main(["--workspace-root", str(root), "--ticket", "T", "--emit-canonical-snapshot"])
    assert rc == 0
    _, sha_path = _snapshot_paths(root, "T")
    assert sha_path.read_text(encoding="utf-8").strip() == expected
