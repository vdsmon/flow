"""Contract tests for validate_workspace.py.

Covers every schema-violation branch + the happy path. Uses tmp_path as the
workspace root; writes minimal `.flow/.initialized` + `.flow/workspace.toml`
fixtures and asserts the validator's verdict + violations.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import agent_routes
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
        handlers = dict.fromkeys(stages, "inline")
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


# ─── [forge] block (optional; validate-if-present) ───────────────────────────


def _append_forge(root: Path, body: str) -> None:
    p = root / ".flow" / "workspace.toml"
    p.write_text(p.read_text(encoding="utf-8") + "\n" + body, encoding="utf-8")


def test_forge_absent_is_valid(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path, backend="beads")
    result, snapshot = vw.validate(root)
    assert result.ok
    assert snapshot is not None


def test_forge_github_valid(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path, backend="beads")
    _append_forge(root, '[forge]\nbackend = "github"\n[forge.github]\n')
    result, _ = vw.validate(root)
    assert result.ok


def test_forge_bitbucket_valid(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path, backend="beads")
    _append_forge(
        root,
        '[forge]\nbackend = "bitbucket"\n[forge.bitbucket]\nworkspace = "ws"\nrepo_slug = "rs"\n',
    )
    result, _ = vw.validate(root)
    assert result.ok


def test_forge_bitbucket_missing_keys_fails(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path, backend="beads")
    _append_forge(root, '[forge]\nbackend = "bitbucket"\n[forge.bitbucket]\n')
    result, _ = vw.validate(root)
    assert not result.ok
    assert any("forge.bitbucket" in v for v in result.violations)


def test_forge_unknown_backend_fails(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path, backend="beads")
    _append_forge(root, '[forge]\nbackend = "gitlab"\n')
    result, _ = vw.validate(root)
    assert not result.ok
    assert any("forge.backend" in v for v in result.violations)


# ─── [models] work_model opt-in guard ────────────────────────────────────────


def test_inline_implement_with_work_model_warns(tmp_path: Path) -> None:
    # opt-in [models] work_model + an inline implement -> non-fatal warning (an inline
    # stage cannot be model-pinned), but validation still passes (ok stays True).
    root = _make_workspace(tmp_path, backend="beads")  # default handlers: implement inline
    _append_forge(root, '[models]\nwork_model = "sonnet"\n')
    result, _ = vw.validate(root)
    assert result.ok
    assert any("work_model" in w and "inline" in w for w in result.warnings)


def test_subagent_implement_with_work_model_no_warn(tmp_path: Path) -> None:
    stages = ["ticket", "plan", "implement", "commit", "reflect"]
    handlers = dict.fromkeys(stages, "inline")
    handlers["implement"] = "subagent:general-purpose"
    root = _make_workspace(tmp_path, backend="beads", stages=stages, handlers=handlers)
    _append_forge(root, '[models]\nwork_model = "sonnet"\n')
    result, _ = vw.validate(root)
    assert result.ok
    assert result.warnings == []


def test_work_model_absent_no_warn(tmp_path: Path) -> None:
    # no [models] block -> the downshift is on by default, but there is no explicit
    # config intent to defeat, so an inline implement warns nothing (avoids spam).
    root = _make_workspace(tmp_path, backend="beads")
    result, _ = vw.validate(root)
    assert result.warnings == []


def test_opt_out_work_model_inline_no_warn(tmp_path: Path) -> None:
    # work_model = "off" (opt-out) + inline implement -> no warning (nothing to apply).
    root = _make_workspace(tmp_path, backend="beads")  # default handlers: implement inline
    _append_forge(root, '[models]\nwork_model = "off"\n')
    result, _ = vw.validate(root)
    assert result.ok
    assert result.warnings == []


def test_inline_e2e_with_per_stage_pin_warns(tmp_path: Path) -> None:
    # a per-stage e2e pin + an inline e2e handler -> non-fatal warning (same logic as
    # implement: an inline stage cannot be model-pinned).
    stages = ["ticket", "plan", "implement", "e2e", "commit", "reflect"]
    handlers = dict.fromkeys(stages, "inline")
    handlers["implement"] = "subagent:general-purpose"  # avoid the implement warning
    root = _make_workspace(tmp_path, backend="beads", stages=stages, handlers=handlers)
    _append_forge(root, '[models]\ne2e = "sonnet"\n')
    result, _ = vw.validate(root)
    assert result.ok
    assert any("models.e2e" in w and "inline" in w for w in result.warnings)


def test_subagent_e2e_with_per_stage_pin_no_warn(tmp_path: Path) -> None:
    stages = ["ticket", "plan", "implement", "e2e", "commit", "reflect"]
    handlers = dict.fromkeys(stages, "inline")
    handlers["implement"] = "subagent:general-purpose"
    handlers["e2e"] = "subagent:general-purpose"
    root = _make_workspace(tmp_path, backend="beads", stages=stages, handlers=handlers)
    _append_forge(root, '[models]\ne2e = "sonnet"\n')
    result, _ = vw.validate(root)
    assert result.ok
    assert result.warnings == []


def test_complete_common_agent_route_is_valid(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path, backend="beads")
    _append_forge(
        root,
        '[agents.implementer]\nharness = "claude_code"\nmodel = "sonnet"\neffort = "high"\n',
    )
    result, _ = vw.validate(root)
    assert result.ok, result.violations


@pytest.mark.parametrize("profile", agent_routes.PROFILES)
def test_every_cognitive_profile_accepts_a_complete_common_route(
    tmp_path: Path, profile: str
) -> None:
    root = _make_workspace(tmp_path, backend="beads")
    _append_forge(
        root,
        f'[agents.{profile}]\nharness = "codex"\nmodel = "test-model"\neffort = "high"\n',
    )

    result, _ = vw.validate(root)

    assert result.ok, result.violations


def test_new_profile_accepts_owner_relative_routes(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path, backend="beads")
    _append_forge(
        root,
        """[agents.reflector.by_owner.claude_code]
harness = "claude_code"
model = "opus"
effort = "high"
[agents.reflector.by_owner.codex]
harness = "codex"
model = "gpt-5.6-sol"
effort = "high"
""",
    )

    result, _ = vw.validate(root)

    assert result.ok, result.violations


def test_partial_agent_route_is_invalid(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path, backend="beads")
    _append_forge(root, '[agents.implementer]\nharness = "claude_code"\nmodel = "sonnet"\n')
    result, _ = vw.validate(root)
    assert any("agents.implementer.effort" in violation for violation in result.violations)


def test_agent_route_common_xor_by_owner(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path, backend="beads")
    _append_forge(
        root,
        """[agents.implementer]
harness = "claude_code"
model = "sonnet"
effort = "high"
[agents.implementer.by_owner.codex]
harness = "codex"
model = "gpt-5.6-luna"
effort = "high"
""",
    )
    result, _ = vw.validate(root)
    assert any("common route or by_owner" in violation for violation in result.violations)


def test_agents_and_models_warn_which_profiles_use_legacy_fallback(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path, backend="beads")
    _append_forge(
        root,
        """[models]
work_model = "opus"
[agents.implementer]
harness = "claude_code"
model = "sonnet"
effort = "high"
""",
    )
    result, _ = vw.validate(root)
    assert result.ok
    assert any(
        "models" in warning
        and "fallback" in warning
        and "implementer" not in warning
        and "review_fixer" in warning
        for warning in result.warnings
    )


def test_complete_agents_and_models_warn_that_models_are_rollback_only(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path, backend="beads")
    routes = agent_routes.render_default_routes_toml()
    _append_forge(root, f'[models]\nwork_model = "opus"\n{routes}')

    result, _ = vw.validate(root)

    assert result.ok
    assert any("rollback only" in warning for warning in result.warnings)


def test_inline_code_review_with_per_stage_pin_no_warn(tmp_path: Path) -> None:
    # code_review is an inline parent that pins a subagent it spawns in its own prose, so
    # its per-stage model IS honored even while the parent is inline -> it must NOT warn.
    stages = ["ticket", "plan", "implement", "code_review", "commit", "reflect"]
    handlers = dict.fromkeys(stages, "inline")
    handlers["implement"] = "subagent:general-purpose"
    root = _make_workspace(tmp_path, backend="beads", stages=stages, handlers=handlers)
    _append_forge(root, '[models]\ncode_review = "opus"\n')
    result, _ = vw.validate(root)
    assert result.ok
    assert result.warnings == []


# ─── [memory] label_facets (optional; validate-if-present) ──────────────────


def test_label_facets_absent_is_valid(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path)
    result, _ = vw.validate(root)
    assert result.ok


def test_label_facets_list_str_valid(tmp_path: Path) -> None:
    root = _make_workspace(
        tmp_path,
        memory={
            "namespace": "x",
            "auto_recall": True,
            "compounding": True,
            "recall_by": ["branch"],
            "recall_top_n": 5,
            "label_facets": ["form"],
        },
    )
    result, _ = vw.validate(root)
    assert result.ok


def test_label_facets_non_list_fails(tmp_path: Path) -> None:
    root = _make_workspace(
        tmp_path,
        memory={
            "namespace": "x",
            "auto_recall": True,
            "compounding": True,
            "recall_by": ["branch"],
            "recall_top_n": 5,
        },
        workspace_toml_content=None,
    )
    p = root / ".flow" / "workspace.toml"
    p.write_text(p.read_text(encoding="utf-8") + '\nlabel_facets = "form"\n', encoding="utf-8")
    result, _ = vw.validate(root)
    assert not result.ok
    assert sum("memory.label_facets" in v for v in result.violations) == 1


def test_label_facets_non_str_element_fails(tmp_path: Path) -> None:
    root = _make_workspace(
        tmp_path,
        memory={
            "namespace": "x",
            "auto_recall": True,
            "compounding": True,
            "recall_by": ["branch"],
            "recall_top_n": 5,
        },
        workspace_toml_content=None,
    )
    p = root / ".flow" / "workspace.toml"
    p.write_text(p.read_text(encoding="utf-8") + "\nlabel_facets = [1]\n", encoding="utf-8")
    result, _ = vw.validate(root)
    assert not result.ok
    assert sum("memory.label_facets" in v for v in result.violations) == 1
