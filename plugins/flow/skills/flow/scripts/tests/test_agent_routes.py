"""Contracts for explicit agent routes and their execution provenance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import agent_routes
import cognitive_workers as cw

PROFILES = (
    "planner",
    "plan_assessor",
    "implementer",
    "e2e",
    "code_reviewer",
    "diff_reviewer",
    "guard_reviewer",
    "review_fixer",
    "revision_fixer",
    "review_brief_author",
    "reflector",
    "machinery_fixer",
)


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


def test_override_wins_and_machinery_fixer_enters_strict_cli_activation(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    # machinery_fixer is the last activation: a read_only capsule whose report reflect applies
    # through the machinery_edit guard. Nothing write-capable stays shadowed anymore.
    resolved = agent_routes.resolve(
        root,
        "machinery_fixer",
        "codex",
        overrides=["machinery_fixer=codex,gpt-5.6-sol,xhigh"],
    )
    assert resolved["source"] == "override"
    assert resolved["desired"] == {
        "harness": "codex",
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
    }
    assert resolved["activation"] == "pending"
    assert resolved["effective"] is None
    assert "machinery_edit guard" in resolved["reason"]


@pytest.mark.parametrize("owner", ["claude-code", "codex"])
def test_snapshot_contains_the_complete_cognitive_profile_catalog(
    tmp_path: Path, owner: str
) -> None:
    snapshot = agent_routes.snapshot(_workspace(tmp_path), owner)

    assert agent_routes.PROFILES == PROFILES
    assert tuple(snapshot["routes"]) == PROFILES
    assert snapshot["routes"]["planner"]["activation"] == "pending"
    pending = {
        profile for profile, route in snapshot["routes"].items() if route["activation"] == "pending"
    }
    assert pending == {
        "planner",
        "plan_assessor",
        "implementer",
        "code_reviewer",
        "diff_reviewer",
        "guard_reviewer",
        "review_fixer",
        "revision_fixer",
        "review_brief_author",
        "reflector",
        "e2e",
        "machinery_fixer",
    }
    assert all(route["effective"] is None for route in snapshot["routes"].values())


def test_no_exact_post_plan_route_is_shadowed_except_generic(tmp_path: Path) -> None:
    # machinery_fixer was the last non-generic shadow; the else->shadow branch is now dead for
    # every routed profile under a public owner. The generic adapter keeps its defensive shadow.
    root = _workspace(tmp_path)
    for owner in ("claude-code", "codex"):
        routes = agent_routes.snapshot(root, owner)["routes"]
        shadowed = sorted(p for p, route in routes.items() if route["activation"] == "shadow")
        assert shadowed == [], (owner, shadowed)
    generic = agent_routes.snapshot(root, "generic")["routes"]
    assert generic["planner"]["activation"] == "shadow"


@pytest.mark.parametrize(
    ("owner", "strong_harness", "strong_model", "fast_harness", "fast_model"),
    [
        ("claude-code", "claude_code", "opus", "claude_code", "sonnet"),
        ("codex", "codex", "gpt-5.6-sol", "codex", "gpt-5.6-luna"),
    ],
)
def test_builtin_profile_defaults_follow_the_approved_role_tiers(
    tmp_path: Path,
    owner: str,
    strong_harness: str,
    strong_model: str,
    fast_harness: str,
    fast_model: str,
) -> None:
    routes = agent_routes.snapshot(_workspace(tmp_path), owner)["routes"]

    assert routes["planner"]["desired"] == {
        "harness": "codex",
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
    }
    assert routes["plan_assessor"]["desired"] == {
        "harness": "claude_code",
        "model": "opus",
        "effort": "high",
    }
    for profile in ("code_reviewer", "diff_reviewer", "guard_reviewer", "reflector"):
        assert routes[profile]["desired"] == {
            "harness": strong_harness,
            "model": strong_model,
            "effort": "high",
        }
    for profile in (
        "implementer",
        "review_fixer",
        "revision_fixer",
        "review_brief_author",
        "machinery_fixer",
    ):
        assert routes[profile]["desired"] == {
            "harness": fast_harness,
            "model": fast_model,
            "effort": "high",
        }
    assert routes["e2e"]["desired"] == {
        "harness": fast_harness,
        "model": fast_model,
        "effort": "medium",
    }


def test_stage_execution_records_complete_composite_provenance(tmp_path: Path) -> None:
    execution = agent_routes.snapshot(_workspace(tmp_path), "codex")["stage_execution"]

    assert set(execution) == {
        "ticket",
        "plan",
        "implement",
        "code_review",
        "e2e",
        "commit",
        "create_pr",
        "review_loop",
        "review_brief",
        "reflect",
        "merge",
    }
    assert execution["plan"] == {
        "kind": "agent",
        "profile": "planner",
        "substeps": {
            "planning": {"profile": "planner"},
            "assessment": {"profile": "plan_assessor"},
        },
    }
    assert execution["code_review"] == {
        "kind": "composite",
        "owner": {"model": "unknown", "effort": "unknown", "harness": "codex"},
        "profile": "diff_reviewer",
        "substeps": {
            "primary_review": {"profile": "code_reviewer"},
            "plan_blind_review": {"profile": "diff_reviewer", "conditional": True},
            "review_fix": {"profile": "review_fixer", "conditional": True},
        },
    }
    assert execution["review_loop"] == {
        "kind": "composite",
        "owner": {"model": "unknown", "effort": "unknown", "harness": "codex"},
        "profile": "revision_fixer",
        "substeps": {
            "review_fix": {"profile": "review_fixer", "conditional": True},
            "revision_fix": {"profile": "revision_fixer", "conditional": True},
        },
    }
    assert execution["review_brief"] == {
        "kind": "agent",
        "profile": "review_brief_author",
    }
    assert execution["reflect"] == {
        "kind": "owner",
        "model": "unknown",
        "effort": "unknown",
        "harness": "codex",
        "substeps": {
            "reflection": {"profile": "reflector"},
            "machinery_fix": {"profile": "machinery_fixer", "conditional": True},
        },
    }
    assert execution["merge"] == {
        "kind": "tool",
        "model": "none",
        "guard_profile": "guard_reviewer",
        "substeps": {"guard_review": {"profile": "guard_reviewer", "conditional": True}},
    }
    assert {stage for stage, record in execution.items() if record.get("model") == "none"} == {
        "ticket",
        "commit",
        "create_pr",
        "merge",
    }


def test_code_review_reviewers_stay_read_only_and_only_the_fixer_writes(tmp_path: Path) -> None:
    # flow-7yjk invariant: a code_review reviewer NEVER gains write authority because it found an
    # issue. The two reader substeps map to read_only profiles; the write is a SEPARATE review_fixer
    # capsule_writer. (Making the readers conditional must not touch their authority.)
    substeps = agent_routes.snapshot(_workspace(tmp_path), "codex")["stage_execution"][
        "code_review"
    ]["substeps"]
    assert cw.ROLE_CATALOG[substeps["primary_review"]["profile"]].authority == "read_only"
    assert cw.ROLE_CATALOG[substeps["plan_blind_review"]["profile"]].authority == "read_only"
    assert cw.ROLE_CATALOG[substeps["review_fix"]["profile"]].authority == "capsule_writer"


def test_stage_execution_keeps_original_v1_fields_while_adding_substeps(
    tmp_path: Path,
) -> None:
    execution = agent_routes.snapshot(_workspace(tmp_path), "claude-code")["stage_execution"]

    assert execution["plan"]["kind"] == "agent"
    assert execution["plan"]["profile"] == "planner"
    assert execution["code_review"]["profile"] == "diff_reviewer"
    assert execution["code_review"]["owner"]["harness"] == "claude_code"
    assert execution["review_loop"]["profile"] == "revision_fixer"
    assert execution["review_loop"]["owner"]["harness"] == "claude_code"
    assert execution["reflect"] | {"substeps": None} == {
        "kind": "owner",
        "model": "unknown",
        "effort": "unknown",
        "harness": "claude_code",
        "substeps": None,
    }
    assert execution["merge"]["guard_profile"] == "guard_reviewer"


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
            "physical_attempt": {"terminal_acknowledged": True},
            "cleanup": {"capsule_absent": True, "quarantined": False},
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
            "physical_attempt": {"terminal_acknowledged": True},
            "cleanup": {"capsule_absent": True, "quarantined": False},
        },
    )
    assert receipt["activation"] == "active"
    assert receipt["effective"] == request


@pytest.mark.parametrize(
    "profile",
    [
        "planner",
        "plan_assessor",
        "code_reviewer",
        "diff_reviewer",
        "guard_reviewer",
        "review_brief_author",
        "reflector",
    ],
)
def test_read_only_receipt_activates_only_with_capsule_proof(tmp_path: Path, profile: str) -> None:
    snap = agent_routes.snapshot(_workspace(tmp_path), "codex")
    request = dict(snap["routes"][profile]["desired"])
    receipt = agent_routes.attest(
        snap,
        profile,
        {
            "request": request,
            "response": {
                "accepted": True,
                **request,
                "transport": "cli",
                "adapter_version": "adapter/1",
            },
            "prompt_hash": "a" * 64,
            "schema_hash": "b" * 64,
            "physical_attempt": {"pid": 17, "terminal_acknowledged": True},
            "cleanup": {"capsule_absent": True, "quarantined": False},
        },
    )
    assert receipt["activation"] == "active"
    assert receipt["physical_attempt"]["pid"] == 17
    assert receipt["cleanup"]["capsule_absent"] is True


def test_importing_writers_and_machinery_fixer_stamp_active_on_receipt(
    tmp_path: Path,
) -> None:
    snap = agent_routes.snapshot(_workspace(tmp_path), "codex")

    def cli_receipt(profile: str) -> dict:
        request = dict(snap["routes"][profile]["desired"])
        return agent_routes.attest(
            snap,
            profile,
            {
                "request": request,
                "response": {
                    "accepted": True,
                    **request,
                    "transport": "cli",
                    "adapter_version": "codex/1",
                },
                "prompt_hash": "a" * 64,
                "schema_hash": "b" * 64,
                "physical_attempt": {"pid": 9, "terminal_acknowledged": True},
                # capsule_absent holds: a capsule_writer disposes its capsule after a
                # successful import, so lifecycle_proven passes as it does for readers and E2E.
                "cleanup": {"capsule_absent": True, "quarantined": False},
            },
        )

    # The implementer, both review-loop fixers, and the read-only machinery_fixer all activate
    # on an exact CLI receipt; each is disposal-terminal so lifecycle_proven passes.
    for profile in ("implementer", "review_fixer", "revision_fixer", "machinery_fixer"):
        receipt = cli_receipt(profile)
        assert receipt["activation"] == "active", profile
        assert receipt["effective"] == snap["routes"][profile]["desired"], profile


def test_read_only_receipt_without_terminal_cleanup_proof_stays_shadow(tmp_path: Path) -> None:
    snap = agent_routes.snapshot(_workspace(tmp_path), "codex")
    request = dict(snap["routes"]["diff_reviewer"]["desired"])
    receipt = agent_routes.attest(
        snap,
        "diff_reviewer",
        {
            "request": request,
            "response": {"accepted": True, **request, "transport": "cli"},
            "prompt_hash": "a" * 64,
            "schema_hash": "b" * 64,
        },
    )
    assert receipt["activation"] == "shadow"


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


def test_partial_agents_use_per_profile_legacy_then_builtin_fallback(tmp_path: Path) -> None:
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
    implementer = agent_routes.resolve(root, "implementer", "claude-code")
    assessor = agent_routes.resolve(root, "plan_assessor", "claude-code")

    assert implementer["source"] == "legacy_models"
    assert implementer["desired"] is None
    assert implementer["legacy"] == {"field": "models.work_model", "value": "opus"}
    assert assessor["source"] == "built_in"
    assert assessor["desired"]["model"] == "opus"
    assert assessor["legacy"] is None


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
    assert first["stage_execution"]["reflect"]["substeps"]["reflection"]["profile"] == "reflector"
    assert (
        first["stage_execution"]["code_review"]["substeps"]["primary_review"]["profile"]
        == "code_reviewer"
    )
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
    assert receipt["activation"] == "shadow"
    assert receipt["effective"] is None
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


def test_attestation_cannot_promote_a_generic_owner_shadow_route(tmp_path: Path) -> None:
    # Every exact post-plan route is active now, so the only shadow left is the generic owner
    # adapter; not even an exact CLI acceptance can promote it.
    snap = agent_routes.snapshot(_workspace(tmp_path), "generic")
    assert snap["routes"]["planner"]["activation"] == "shadow"
    request = dict(snap["routes"]["planner"]["desired"])
    receipt = agent_routes.attest(
        snap,
        "planner",
        {
            "request": request,
            "response": {"accepted": True, **request, "transport": "cli"},
        },
    )
    assert receipt["activation"] == "shadow"
    assert receipt["effective"] is None


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
    assert json.loads(capsys.readouterr().out)["activation"] == "shadow"


def test_every_non_planner_exact_acceptance_remains_shadowed(tmp_path: Path) -> None:
    snapshot = agent_routes.snapshot(_workspace(tmp_path), "claude-code")

    for profile in PROFILES:
        if profile == "planner":
            continue
        desired = snapshot["routes"][profile]["desired"]
        receipt = agent_routes.attest(
            snapshot,
            profile,
            {
                "request": desired,
                "response": {"accepted": True, **desired, "transport": "native"},
            },
        )
        assert receipt["activation"] == "shadow", profile
        assert receipt["effective"] is None, profile


def test_new_profiles_are_valid_atomic_overrides_and_duplicates_are_rejected(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    override = "review_brief_author=codex,gpt-5.6-sol,max"

    resolved = agent_routes.resolve(
        root,
        "review_brief_author",
        "claude-code",
        overrides=[override],
    )
    assert resolved["source"] == "override"
    assert resolved["desired"] == {
        "harness": "codex",
        "model": "gpt-5.6-sol",
        "effort": "max",
    }
    with pytest.raises(agent_routes.RouteError, match="duplicate --route"):
        agent_routes.snapshot(root, "codex", overrides=[override, override])


def test_migration_emits_the_complete_catalog(tmp_path: Path) -> None:
    root = _workspace(tmp_path, '\n[models]\nwork_model = "sonnet"\ne2e = "opus"\n')

    appendix = agent_routes.migrate(root, apply=False)["appendix"]

    assert all(f"[agents.{profile}" in appendix for profile in PROFILES)
    assert "[agents.code_reviewer]" in appendix
    assert "[agents.review_fixer]" in appendix
    assert "[agents.review_brief_author.by_owner.codex]" in appendix
    assert "[agents.reflector.by_owner.claude_code]" in appendix
    assert "[agents.machinery_fixer.by_owner.codex]" in appendix


def test_old_v1_snapshot_retains_its_recorded_routes_without_synthesis(tmp_path: Path) -> None:
    old = agent_routes.snapshot(_workspace(tmp_path), "codex")
    for profile in (
        "code_reviewer",
        "review_fixer",
        "review_brief_author",
        "reflector",
        "machinery_fixer",
    ):
        del old["routes"][profile]
    del old["stage_execution"]["review_brief"]
    old["stage_execution"]["reflect"] = {
        "kind": "owner",
        "model": "unknown",
        "effort": "unknown",
        "harness": "codex",
    }
    body = {key: value for key, value in old.items() if key != "digest"}
    old["digest"] = agent_routes.canonical_digest(body)
    path = tmp_path / "old-route-snapshot.json"
    path.write_text(json.dumps(old), encoding="utf-8")

    loaded = agent_routes.load_snapshot(path)

    assert set(loaded["routes"]) == {
        "planner",
        "plan_assessor",
        "implementer",
        "e2e",
        "diff_reviewer",
        "guard_reviewer",
        "revision_fixer",
    }
    assert "review_brief" not in loaded["stage_execution"]
    with pytest.raises(agent_routes.RouteError, match="has no route"):
        agent_routes.resolve_snapshot(path, "reflector")
