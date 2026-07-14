"""Contracts for explicit agent routes and their execution provenance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import agent_routes


def _workspace(tmp_path: Path, body: str = "") -> Path:
    flow = tmp_path / ".flow"
    flow.mkdir(parents=True)
    (flow / "workspace.toml").write_text(
        '[tracker]\nbackend = "beads"\n[tracker.beads]\nprefix = "x"\n' + body,
        encoding="utf-8",
    )
    return tmp_path


def test_owner_normalization_keeps_public_harness_names_small() -> None:
    assert agent_routes.normalize_owner_harness("claude-code") == "claude_code"
    assert agent_routes.normalize_owner_harness("claude_code") == "claude_code"
    assert agent_routes.normalize_owner_harness("codex") == "codex"
    assert agent_routes.normalize_owner_harness("generic") == "generic"
    with pytest.raises(agent_routes.RouteError, match="owner harness"):
        agent_routes.normalize_owner_harness("cursor")


def test_explicit_common_route_requires_a_complete_triple(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        '\n[agents.implementer]\nharness = "claude_code"\nmodel = "sonnet"\n',
    )
    with pytest.raises(agent_routes.RouteError, match="effort"):
        agent_routes.resolve(root, "implementer", "claude-code")


def test_common_and_by_owner_are_mutually_exclusive(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        """
[agents.implementer]
harness = "claude_code"
model = "sonnet"
effort = "high"
[agents.implementer.by_owner.claude_code]
harness = "claude_code"
model = "sonnet"
effort = "high"
""",
    )
    with pytest.raises(agent_routes.RouteError, match="common route or by_owner"):
        agent_routes.resolve(root, "implementer", "claude-code")


def test_override_wins_and_codex_post_plan_route_is_shadowed(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    resolved = agent_routes.resolve(
        root,
        "implementer",
        "codex",
        overrides=["implementer=codex,gpt-5.6-sol,xhigh"],
    )
    assert resolved["source"] == "override"
    assert resolved["desired"] == {
        "harness": "codex",
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
    }
    assert resolved["activation"] == "shadow"
    assert resolved["effective"] is None
    assert "cannot select model and effort" in resolved["reason"]


def test_configured_and_builtin_planner_routes_enter_strict_cli_activation(
    tmp_path: Path,
) -> None:
    root = _workspace(
        tmp_path,
        '\n[agents.planner]\nharness = "codex"\nmodel = "gpt-5.6-sol"\neffort = "xhigh"\n',
    )
    configured = agent_routes.resolve(root, "planner", "claude-code")
    explicit = agent_routes.resolve(
        root,
        "planner",
        "claude-code",
        overrides=["planner=codex,gpt-5.6-sol,xhigh"],
    )
    builtin = agent_routes.resolve(_workspace(tmp_path / "builtin"), "planner", "codex")
    assert configured["activation"] == "pending"
    assert configured["desired"] == {
        "harness": "codex",
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
    }
    assert builtin["activation"] == "pending"
    assert builtin["desired"] == configured["desired"]
    assert explicit["activation"] == "pending"
    assert "strict read-only planner CLI" in explicit["reason"]


@pytest.mark.parametrize("source", ["workspace", "built_in"])
def test_ordinary_planner_cli_attestation_proves_exact_execution(
    tmp_path: Path, source: str
) -> None:
    body = (
        '\n[agents.planner]\nharness = "codex"\nmodel = "gpt-5.6-sol"\neffort = "xhigh"\n'
        if source == "workspace"
        else ""
    )
    root = _workspace(tmp_path, body)
    snap = agent_routes.snapshot(root, "claude-code")
    request = dict(snap["routes"]["planner"]["desired"])
    receipt = agent_routes.attest(
        snap,
        "planner",
        {
            "request": request,
            "response": {
                "accepted": True,
                **request,
                "transport": "cli",
                "adapter_version": "codex-cli/test",
            },
            "prompt_hash": "a" * 64,
            "schema_hash": "b" * 64,
        },
    )
    assert receipt["source"] == source
    assert receipt["activation"] == "active"
    assert receipt["effective"] == request


def test_explicit_planner_cli_attestation_proves_exact_execution(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    snap = agent_routes.snapshot(
        root,
        "claude-code",
        overrides=["planner=codex,gpt-5.6-sol,xhigh"],
    )
    request = dict(snap["routes"]["planner"]["desired"])
    receipt = agent_routes.attest(
        snap,
        "planner",
        {
            "request": request,
            "response": {
                "accepted": True,
                **request,
                "transport": "cli",
                "adapter_version": "codex-cli/test",
                "canonical_model": "gpt-5.6-sol-2026-07-01",
            },
            "prompt_hash": "prompt",
            "schema_hash": "schema",
        },
    )
    assert receipt["activation"] == "active"
    assert receipt["effective"] == request


def test_legacy_models_are_classified_without_becoming_agent_routes(tmp_path: Path) -> None:
    root = _workspace(tmp_path, '\n[models]\nwork_model = "opus"\ne2e = "off"\n')
    planner = agent_routes.resolve(root, "planner", "claude-code")
    implement = agent_routes.resolve(root, "implementer", "claude-code")
    e2e = agent_routes.resolve(root, "e2e", "claude-code")
    assert planner["activation"] == "legacy"
    assert planner["desired"] is None
    assert planner["legacy"] == {
        "field": "owner session model",
        "value": "host-native planning",
    }
    assert implement["activation"] == "legacy"
    assert implement["desired"] is None
    assert implement["legacy"] == {"field": "models.work_model", "value": "opus"}
    assert e2e["legacy"] == {"field": "models.e2e", "value": "off"}


def test_agents_mode_never_partially_inherits_legacy_models(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        """
[models]
work_model = "opus"
[agents.e2e]
harness = "claude_code"
model = "sonnet"
effort = "medium"
""",
    )
    resolved = agent_routes.resolve(root, "implementer", "claude-code")
    assert resolved["source"] == "built_in"
    assert resolved["desired"]["model"] == "sonnet"
    assert resolved["legacy"] is None


def test_snapshot_is_canonical_stable_and_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    path = tmp_path / "route-snapshot.json"
    original_load = agent_routes._load_workspace
    load_count = 0

    def load_once(workspace_root: Path) -> tuple[dict[str, object], bytes]:
        nonlocal load_count
        load_count += 1
        return original_load(workspace_root)

    monkeypatch.setattr(agent_routes, "_load_workspace", load_once)
    first = agent_routes.snapshot(root, "claude-code", output_path=path)
    assert load_count == 1
    second = agent_routes.snapshot(root, "claude_code")
    assert load_count == 2
    assert first == second
    assert first["schema"] == "flow.agent-routes/v1"
    assert first["routes"]["implementer"]["desired"]["model"] == "sonnet"
    assert first["stage_execution"]["commit"]["model"] == "none"
    assert first["stage_execution"]["reflect"]["harness"] == "claude_code"
    assert first["stage_execution"]["code_review"]["owner"]["harness"] == "claude_code"
    assert agent_routes.load_snapshot(path) == first

    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["owner_harness"] = "codex"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(agent_routes.RouteError, match="digest"):
        agent_routes.load_snapshot(path)


def test_snapshot_can_resolve_exact_fetched_configuration_bytes(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    raw = (root / ".flow" / "workspace.toml").read_bytes()
    from_checkout = agent_routes.snapshot(root, "claude-code")
    from_base = agent_routes.snapshot_config(raw, "claude-code")
    assert from_base == from_checkout


def test_attestation_requires_structured_exact_native_acceptance(tmp_path: Path) -> None:
    snap = agent_routes.snapshot(_workspace(tmp_path), "claude-code")
    with pytest.raises(agent_routes.RouteError, match="structured"):
        agent_routes.attest(snap, "implementer", "agent says it used sonnet")

    request = dict(snap["routes"]["implementer"]["desired"])
    receipt = agent_routes.attest(
        snap,
        "implementer",
        {
            "request": request,
            "response": {
                "accepted": True,
                **request,
                "transport": "native",
                "adapter_version": "claude-code/test",
                "canonical_model": "claude-sonnet-test",
                "worker_id": "agent-42",
            },
            "prompt_hash": "prompt-1",
            "schema_hash": "schema-1",
        },
    )
    assert receipt["activation"] == "active"
    assert receipt["effective"] == request
    assert receipt["source"] == "built_in"
    assert receipt["canonical_model"] == "claude-sonnet-test"
    assert receipt["worker_id"] == "agent-42"
    assert agent_routes.verify_receipt(receipt) == receipt
    with pytest.raises(agent_routes.RouteError, match="digest"):
        agent_routes.verify_receipt({**receipt, "worker_id": "tampered"})

    mismatch = agent_routes.attest(
        snap,
        "implementer",
        {
            "request": request,
            "response": {"accepted": True, **request, "effort": "medium"},
        },
    )
    assert mismatch["activation"] == "shadow"
    assert mismatch["effective"] is None


def test_codex_attestation_cannot_promote_a_shadow_route(tmp_path: Path) -> None:
    snap = agent_routes.snapshot(_workspace(tmp_path), "codex")
    receipt = agent_routes.attest(
        snap,
        "implementer",
        {
            "request": {},
            "response": {"accepted": True, "transport": "native"},
        },
    )
    assert receipt["activation"] == "shadow"
    assert receipt["effective"] is None
    assert receipt["launch_request"] == {}


def test_unknown_agent_route_fields_are_rejected(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        """
[agents.implementer]
harness = "claude_code"
model = "sonnet"
effort = "high"
fallback = "codex"
""",
    )
    with pytest.raises(agent_routes.RouteError, match="unknown fields: fallback"):
        agent_routes.resolve(root, "implementer", "claude-code")


def test_migration_check_and_apply_preserve_existing_bytes(tmp_path: Path) -> None:
    original = (
        '# keep this spelling and spacing\n[tracker]\nbackend="beads"\n'
        '[models]\nwork_model = "sonnet"\ne2e = "opus"\n'
    )
    root = _workspace(tmp_path)
    path = root / ".flow" / "workspace.toml"
    path.write_text(original, encoding="utf-8")

    checked = agent_routes.migrate(root, apply=False)
    assert checked["changed"] is True
    assert path.read_text(encoding="utf-8") == original

    with pytest.raises(agent_routes.RouteError, match="confirmation"):
        agent_routes.migrate(root, apply=True)
    applied = agent_routes.migrate(root, apply=True, confirm=True)
    updated = path.read_text(encoding="utf-8")
    assert applied["changed"] is True
    assert updated.startswith(original)
    assert "[agents.implementer]" in updated
    assert 'model = "sonnet"' in updated
    assert "[agents.e2e]" in updated
    assert 'model = "opus"' in updated


@pytest.mark.parametrize("value", ["off", "gpt-5.6-sol", "provider/latest"])
def test_migration_refuses_off_and_untranslatable_models(tmp_path: Path, value: str) -> None:
    root = _workspace(tmp_path, f'\n[models]\nwork_model = "{value}"\n')
    before = (root / ".flow" / "workspace.toml").read_bytes()
    with pytest.raises(agent_routes.RouteError, match="cannot migrate"):
        agent_routes.migrate(root, apply=False)
    assert (root / ".flow" / "workspace.toml").read_bytes() == before


def test_cli_snapshot_resolve_attest_round_trip(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path)
    snap_path = tmp_path / "snapshot.json"
    assert (
        agent_routes.cli_main(
            [
                "snapshot",
                "--workspace-root",
                str(root),
                "--owner-harness",
                "claude-code",
                "--output",
                str(snap_path),
            ]
        )
        == 0
    )
    snap = json.loads(capsys.readouterr().out)
    assert snap["digest"]

    assert (
        agent_routes.cli_main(
            [
                "resolve",
                "--workspace-root",
                str(root),
                "--owner-harness",
                "claude-code",
                "--profile",
                "implementer",
            ]
        )
        == 0
    )
    desired = json.loads(capsys.readouterr().out)["desired"]
    acceptance = tmp_path / "acceptance.json"
    acceptance.write_text(
        json.dumps(
            {
                "request": desired,
                "response": {"accepted": True, **desired, "transport": "native"},
            }
        ),
        encoding="utf-8",
    )
    assert (
        agent_routes.cli_main(
            [
                "attest",
                "--snapshot",
                str(snap_path),
                "--profile",
                "implementer",
                "--acceptance-from",
                str(acceptance),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["activation"] == "active"
