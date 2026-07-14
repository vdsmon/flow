"""Resolve, freeze, attest, and migrate Flow agent routes.

The module owns route precedence and provenance. Callers work with complete
``harness/model/effort`` routes and never need to interpret workspace TOML,
activation capability, or digest rules themselves. The planner is the only profile
whose configured, built-in, or overridden route may cross the CLI boundary in this
increment.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

SCHEMA = "flow.agent-routes/v1"
RECEIPT_SCHEMA = "flow.agent-route-receipt/v1"
PUBLIC_HARNESSES = frozenset({"claude_code", "codex"})
OWNER_HARNESSES = frozenset({"claude_code", "codex", "generic"})
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})

_PROFILES = (
    "planner",
    "plan_assessor",
    "implementer",
    "e2e",
    "diff_reviewer",
    "guard_reviewer",
    "revision_fixer",
)

_COMMON_DEFAULTS = {
    "planner": {"harness": "codex", "model": "gpt-5.6-sol", "effort": "xhigh"},
    "plan_assessor": {"harness": "claude_code", "model": "opus", "effort": "high"},
}

_OWNER_DEFAULTS = {
    "implementer": {
        "claude_code": {"harness": "claude_code", "model": "sonnet", "effort": "high"},
        "codex": {"harness": "codex", "model": "gpt-5.6-luna", "effort": "high"},
    },
    "e2e": {
        "claude_code": {"harness": "claude_code", "model": "sonnet", "effort": "medium"},
        "codex": {"harness": "codex", "model": "gpt-5.6-luna", "effort": "medium"},
    },
    "diff_reviewer": {
        "claude_code": {"harness": "claude_code", "model": "opus", "effort": "high"},
        "codex": {"harness": "codex", "model": "gpt-5.6-sol", "effort": "high"},
    },
    "guard_reviewer": {
        "claude_code": {"harness": "claude_code", "model": "opus", "effort": "high"},
        "codex": {"harness": "codex", "model": "gpt-5.6-sol", "effort": "high"},
    },
    "revision_fixer": {
        "claude_code": {"harness": "claude_code", "model": "sonnet", "effort": "high"},
        "codex": {"harness": "codex", "model": "gpt-5.6-luna", "effort": "high"},
    },
}

_LEGACY_STAGE = {
    "implementer": "implement",
    "e2e": "e2e",
    "diff_reviewer": "code_review",
    "guard_reviewer": "code_review",
    "revision_fixer": "review_loop",
}

_STAGE_EXECUTION = {
    "ticket": {"kind": "tool", "model": "none"},
    "plan": {"kind": "agent", "profile": "planner"},
    "implement": {"kind": "agent", "profile": "implementer"},
    "code_review": {
        "kind": "composite",
        "owner": {"model": "unknown", "effort": "unknown"},
        "profile": "diff_reviewer",
    },
    "e2e": {"kind": "agent", "profile": "e2e"},
    "commit": {"kind": "tool", "model": "none"},
    "create_pr": {"kind": "tool", "model": "none"},
    "review_loop": {
        "kind": "composite",
        "owner": {"model": "unknown", "effort": "unknown"},
        "profile": "revision_fixer",
    },
    "reflect": {
        "kind": "owner",
        "model": "unknown",
        "effort": "unknown",
    },
    "merge": {"kind": "tool", "model": "none", "guard_profile": "guard_reviewer"},
}

_MIGRATABLE_CLAUDE_MODELS = frozenset({"sonnet", "opus", "haiku"})
_MIGRATION_EFFORT = {
    "implementer": "high",
    "e2e": "medium",
    "diff_reviewer": "high",
    "guard_reviewer": "high",
    "revision_fixer": "high",
}


class RouteError(ValueError):
    """An agent route, snapshot, attestation, or migration is invalid."""


def normalize_owner_harness(value: str) -> str:
    """Return Flow's canonical owner harness name."""
    normalized = value.strip().lower().replace("-", "_")
    if normalized not in OWNER_HARNESSES:
        raise RouteError(
            f"unsupported owner harness {value!r}; expected claude-code, codex, or generic"
        )
    return normalized


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _with_digest(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result["digest"] = _digest(value)
    return result


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _load_workspace(workspace_root: Path) -> tuple[dict[str, Any], bytes]:
    path = workspace_root.expanduser().resolve() / ".flow" / "workspace.toml"
    try:
        raw = path.read_bytes()
        return tomllib.loads(raw.decode()), raw
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RouteError(f"cannot read workspace routes from {path}: {exc}") from exc


def _parse_route(raw: object, field: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise RouteError(f"{field} must be a table")
    route: dict[str, str] = {}
    for key in ("harness", "model", "effort"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RouteError(f"{field}.{key} must be a non-empty string")
        route[key] = value.strip()
    extra = set(raw) - {"harness", "model", "effort"}
    if extra:
        raise RouteError(f"{field} has unknown fields: {', '.join(sorted(extra))}")
    if route["harness"] not in PUBLIC_HARNESSES:
        raise RouteError(
            f"{field}.harness must be one of {sorted(PUBLIC_HARNESSES)!r}, got {route['harness']!r}"
        )
    if route["effort"] not in EFFORTS:
        raise RouteError(f"{field}.effort must be one of {sorted(EFFORTS)!r}")
    return route


def _parse_override_values(values: list[str] | tuple[str, ...]) -> dict[str, dict[str, str]]:
    parsed: dict[str, dict[str, str]] = {}
    for raw in values:
        profile, separator, body = raw.partition("=")
        parts = [part.strip() for part in body.split(",")]
        if not separator or profile not in _PROFILES or len(parts) != 3 or not all(parts):
            raise RouteError("--route expects profile=harness,model,effort with a known profile")
        if profile in parsed:
            raise RouteError(f"duplicate --route for profile {profile!r}")
        parsed[profile] = _parse_route(
            {"harness": parts[0], "model": parts[1], "effort": parts[2]},
            f"override.{profile}",
        )
    return parsed


def _explicit_route(agents: object, profile: str, owner_harness: str) -> dict[str, str] | None:
    if agents is None:
        return None
    if not isinstance(agents, dict):
        raise RouteError("agents must be a table")
    raw = agents.get(profile)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RouteError(f"agents.{profile} must be a table")
    unknown = set(raw) - {"harness", "model", "effort", "by_owner"}
    if unknown:
        raise RouteError(f"agents.{profile} has unknown fields: {', '.join(sorted(unknown))}")
    has_common = any(key in raw for key in ("harness", "model", "effort"))
    has_by_owner = "by_owner" in raw
    if has_common and has_by_owner:
        raise RouteError(f"agents.{profile} must define a common route or by_owner, not both")
    if has_common:
        return _parse_route(raw, f"agents.{profile}")
    if not has_by_owner:
        raise RouteError(f"agents.{profile} must define harness/model/effort or a by_owner table")
    by_owner = raw.get("by_owner")
    if not isinstance(by_owner, dict) or not by_owner:
        raise RouteError(f"agents.{profile}.by_owner must be a non-empty table")
    unknown = set(by_owner) - PUBLIC_HARNESSES
    if unknown:
        raise RouteError(
            f"agents.{profile}.by_owner has unknown owners: {', '.join(sorted(unknown))}"
        )
    selected = by_owner.get(owner_harness)
    if selected is None:
        raise RouteError(
            f"agents.{profile}.by_owner has no route for owner {owner_harness!r}; "
            "explicit routes never inherit"
        )
    return _parse_route(selected, f"agents.{profile}.by_owner.{owner_harness}")


def configuration_errors(data: dict[str, Any]) -> list[str]:
    """Return schema errors for an optional ``[agents]`` configuration."""
    agents = data.get("agents")
    if agents is None:
        return []
    if not isinstance(agents, dict):
        return ["agents must be a table"]
    errors = [
        f"agents.{unknown} is not a known profile"
        for unknown in sorted(set(agents) - set(_PROFILES))
    ]
    for profile in _PROFILES:
        raw = agents.get(profile)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            errors.append(f"agents.{profile} must be a table")
            continue
        unknown = set(raw) - {"harness", "model", "effort", "by_owner"}
        if unknown:
            errors.append(f"agents.{profile} has unknown fields: {', '.join(sorted(unknown))}")
        has_common = any(key in raw for key in ("harness", "model", "effort"))
        has_by_owner = "by_owner" in raw
        if has_common and has_by_owner:
            errors.append(f"agents.{profile} must define a common route or by_owner, not both")
            continue
        if has_common:
            try:
                _parse_route(raw, f"agents.{profile}")
            except RouteError as exc:
                errors.append(str(exc))
            continue
        if not has_by_owner:
            errors.append(f"agents.{profile} must define harness/model/effort or a by_owner table")
            continue
        by_owner = raw.get("by_owner")
        if not isinstance(by_owner, dict) or not by_owner:
            errors.append(f"agents.{profile}.by_owner must be a non-empty table")
            continue
        for owner, route in by_owner.items():
            if owner not in PUBLIC_HARNESSES:
                errors.append(f"agents.{profile}.by_owner.{owner} is not a known owner")
                continue
            try:
                _parse_route(route, f"agents.{profile}.by_owner.{owner}")
            except RouteError as exc:
                errors.append(str(exc))
    return errors


def _legacy_route(data: dict[str, Any], profile: str) -> dict[str, str] | None:
    models = data.get("models")
    stage = _LEGACY_STAGE.get(profile)
    if not isinstance(models, dict):
        return None
    if profile == "planner":
        return {"field": "owner session model", "value": "host-native planning"}
    if stage is None:
        return None
    if isinstance(models.get(stage), str):
        return {"field": f"models.{stage}", "value": models[stage]}
    if isinstance(models.get("work_model"), str):
        return {"field": "models.work_model", "value": models["work_model"]}
    return {"field": "built-in legacy default", "value": "sonnet"}


def _builtin_route(profile: str, owner_harness: str) -> dict[str, str] | None:
    common = _COMMON_DEFAULTS.get(profile)
    if common is not None:
        return dict(common)
    owners = _OWNER_DEFAULTS.get(profile)
    if owners is None or owner_harness == "generic":
        return None
    selected = owners.get(owner_harness)
    return dict(selected) if selected is not None else None


def _activation(
    profile: str,
    desired: dict[str, str] | None,
    owner_harness: str,
    source: str,
) -> tuple[str, str]:
    if desired is None:
        return "unrouted", "no exact route exists for this owner harness"
    if profile == "planner":
        return "pending", "strict read-only planner CLI activation requires an exact receipt"
    if profile == "plan_assessor":
        return "shadow", "plan assessor routes remain non-activating in this increment"
    if owner_harness == "generic":
        return "shadow", "the generic adapter has no structured model and effort selector"
    if desired["harness"] != owner_harness:
        return "shadow", "cross-harness post-plan execution is not enabled in this increment"
    if owner_harness == "codex":
        return (
            "shadow",
            "the current Codex native spawn interface cannot select model and effort",
        )
    return "pending", "activation requires a structured native launch acceptance"


def _resolve_data(
    data: dict[str, Any],
    profile: str,
    owner_harness: str,
    parsed_overrides: dict[str, dict[str, str]],
) -> dict[str, Any]:
    if profile not in _PROFILES:
        raise RouteError(f"unknown agent profile {profile!r}")
    desired = parsed_overrides.get(profile)
    source = "override" if desired is not None else ""
    if desired is None:
        desired = _explicit_route(data.get("agents"), profile, owner_harness)
        if desired is not None:
            source = "workspace"
    if desired is None and data.get("agents") is None:
        legacy = _legacy_route(data, profile)
        if legacy is not None:
            return {
                "schema": SCHEMA,
                "profile": profile,
                "source": "legacy_models",
                "desired": None,
                "effective": None,
                "activation": "legacy",
                "legacy": legacy,
                "reason": "standalone [models] compatibility remains authoritative",
            }
    if desired is None:
        desired = _builtin_route(profile, owner_harness)
        source = "built_in" if desired is not None else "generic_legacy"
    activation, reason = _activation(profile, desired, owner_harness, source)
    return {
        "schema": SCHEMA,
        "profile": profile,
        "source": source,
        "desired": desired,
        "effective": None,
        "activation": activation,
        "legacy": None,
        "reason": reason,
    }


def resolve(
    workspace_root: Path,
    profile: str,
    owner_harness: str,
    *,
    overrides: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Resolve one profile without claiming that its desired route executed."""
    owner = normalize_owner_harness(owner_harness)
    data, _ = _load_workspace(workspace_root)
    return _resolve_data(data, profile, owner, _parse_override_values(overrides))


def snapshot(
    workspace_root: Path,
    owner_harness: str,
    *,
    overrides: list[str] | tuple[str, ...] = (),
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Build and optionally persist the canonical route snapshot."""
    root = workspace_root.expanduser().resolve()
    data, raw = _load_workspace(root)
    return snapshot_config(
        raw,
        owner_harness,
        overrides=overrides,
        output_path=output_path,
        parsed_data=data,
    )


def snapshot_config(
    raw: bytes,
    owner_harness: str,
    *,
    overrides: list[str] | tuple[str, ...] = (),
    output_path: Path | None = None,
    parsed_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a snapshot from exact fetched workspace configuration bytes."""
    try:
        data = parsed_data if parsed_data is not None else tomllib.loads(raw.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RouteError(f"cannot parse fetched workspace routes: {exc}") from exc
    owner = normalize_owner_harness(owner_harness)
    parsed = _parse_override_values(overrides)
    routes = {profile: _resolve_data(data, profile, owner, parsed) for profile in _PROFILES}
    stage_execution = json.loads(json.dumps(_STAGE_EXECUTION))
    for execution in stage_execution.values():
        if execution.get("kind") == "owner":
            execution["harness"] = owner
        if isinstance(execution.get("owner"), dict):
            execution["owner"]["harness"] = owner
    body = {
        "schema": SCHEMA,
        "owner_harness": owner,
        "workspace_config_digest": hashlib.sha256(raw).hexdigest(),
        "overrides": {profile: parsed[profile] for profile in sorted(parsed)},
        "routes": routes,
        "stage_execution": stage_execution,
    }
    result = _with_digest(body)
    if output_path is not None:
        _atomic_write(output_path.expanduser().resolve(), _canonical_bytes(result) + b"\n")
    return result


def _verified_snapshot(value: dict[str, Any]) -> dict[str, Any]:
    digest = value.get("digest")
    body = {key: item for key, item in value.items() if key != "digest"}
    if not isinstance(digest, str) or digest != _digest(body):
        raise RouteError("route snapshot digest does not match its canonical content")
    if value.get("schema") != SCHEMA:
        raise RouteError(f"unsupported route snapshot schema {value.get('schema')!r}")
    return value


def load_snapshot(path: Path) -> dict[str, Any]:
    """Read and digest-check a persisted snapshot."""
    try:
        value = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RouteError(f"cannot read route snapshot {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RouteError("route snapshot must be a JSON object")
    return _verified_snapshot(value)


def verify_receipt(value: dict[str, Any]) -> dict[str, Any]:
    """Digest-check one persisted launch receipt before another module trusts it."""
    digest = value.get("digest")
    body = {key: item for key, item in value.items() if key != "digest"}
    if value.get("schema") != RECEIPT_SCHEMA:
        raise RouteError(f"unsupported route receipt schema {value.get('schema')!r}")
    if not isinstance(digest, str) or digest != _digest(body):
        raise RouteError("route receipt digest does not match its canonical content")
    return value


def resolve_snapshot(path: Path, profile: str) -> dict[str, Any]:
    """Return one frozen profile from a persisted canonical snapshot."""
    snap = load_snapshot(path)
    routes = snap.get("routes")
    resolved = routes.get(profile) if isinstance(routes, dict) else None
    if not isinstance(resolved, dict):
        raise RouteError(f"snapshot has no route for profile {profile!r}")
    return resolved


def attest(route_snapshot: dict[str, Any], profile: str, acceptance: object) -> dict[str, Any]:
    """Return a receipt for a host-native structured launch response."""
    snap = _verified_snapshot(route_snapshot)
    if not isinstance(acceptance, dict):
        raise RouteError("launch acceptance must be a structured JSON object, not agent text")
    request = acceptance.get("request")
    response = acceptance.get("response")
    if not isinstance(request, dict) or not isinstance(response, dict):
        raise RouteError("structured launch acceptance requires request and response objects")
    routes = snap.get("routes")
    resolved = routes.get(profile) if isinstance(routes, dict) else None
    if not isinstance(resolved, dict):
        raise RouteError(f"snapshot has no route for profile {profile!r}")
    desired = resolved.get("desired")
    if not isinstance(desired, dict):
        raise RouteError(f"profile {profile!r} has no exact desired route to attest")
    can_activate = resolved.get("activation") == "pending"
    if can_activate and request != desired:
        raise RouteError("structured launch request does not match the frozen desired route")

    exact_response = all(response.get(key) == desired[key] for key in desired)
    transport = response.get("transport")
    supported_transport = transport == "native" or (profile == "planner" and transport == "cli")
    active = (
        response.get("accepted") is True and exact_response and supported_transport and can_activate
    )
    reason = (
        "structured launch accepted the exact desired route"
        if active
        else "launch response did not prove exact supported native execution"
    )
    body = {
        "schema": RECEIPT_SCHEMA,
        "snapshot_digest": snap["digest"],
        "profile": profile,
        "source": resolved.get("source"),
        "desired": desired,
        "effective": desired if active else None,
        "activation": "active" if active else "shadow",
        "reason": reason,
        "launch_request": {
            key: request.get(key) for key in ("harness", "model", "effort") if key in request
        },
        "transport": response.get("transport", "unknown"),
        "adapter_version": response.get("adapter_version", "unknown"),
        "canonical_model": response.get("canonical_model"),
        "worker_id": response.get("worker_id"),
        "prompt_hash": acceptance.get("prompt_hash"),
        "schema_hash": acceptance.get("schema_hash"),
    }
    return _with_digest(body)


def _legacy_model(models: dict[str, Any], stage: str) -> str:
    raw = models.get(stage, models.get("work_model", "sonnet"))
    if not isinstance(raw, str):
        raise RouteError(f"cannot migrate models.{stage}: expected a string")
    model = raw.strip().lower()
    if model not in _MIGRATABLE_CLAUDE_MODELS:
        raise RouteError(
            f"cannot migrate models.{stage}={raw!r}: OFF and untranslatable provider aliases "
            "require a hand-authored agent route"
        )
    return model


def _migration_appendix(models: dict[str, Any]) -> str:
    profile_stage = {
        "implementer": "implement",
        "e2e": "e2e",
        "diff_reviewer": "code_review",
        "guard_reviewer": "code_review",
        "revision_fixer": "review_loop",
    }
    lines = ["", "# Explicit agent routes migrated from the legacy [models] block."]
    for profile, stage in profile_stage.items():
        model = _legacy_model(models, stage)
        lines.extend(
            [
                f"[agents.{profile}]",
                'harness = "claude_code"',
                f'model = "{model}"',
                f'effort = "{_MIGRATION_EFFORT[profile]}"',
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def migrate(workspace_root: Path, *, apply: bool, confirm: bool = False) -> dict[str, Any]:
    """Propose or atomically append explicit routes for a legacy workspace."""
    root = workspace_root.expanduser().resolve()
    data, raw = _load_workspace(root)
    if data.get("agents") is not None:
        return {
            "schema": SCHEMA,
            "changed": False,
            "reason": "workspace already contains explicit agent routes",
        }
    models = data.get("models")
    if not isinstance(models, dict):
        return {
            "schema": SCHEMA,
            "changed": False,
            "reason": "workspace has no legacy [models] block",
        }
    appendix = _migration_appendix(models)
    updated = raw + (b"" if raw.endswith(b"\n") else b"\n") + appendix.encode()
    result = {
        "schema": SCHEMA,
        "changed": True,
        "before_digest": hashlib.sha256(raw).hexdigest(),
        "after_digest": hashlib.sha256(updated).hexdigest(),
        "appendix": appendix,
    }
    if apply:
        if not confirm:
            raise RouteError("migration apply requires explicit --confirm confirmation")
        _atomic_write(root / ".flow" / "workspace.toml", updated)
    return result


def _add_resolution_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace-root", default=".")
    parser.add_argument("--owner-harness", required=True)
    parser.add_argument("--route", action="append", default=[])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resolve and attest Flow agent routes.")
    sub = parser.add_subparsers(dest="operation", required=True)

    resolve_parser = sub.add_parser("resolve")
    resolve_parser.add_argument("--workspace-root")
    resolve_parser.add_argument("--owner-harness")
    resolve_parser.add_argument("--route", action="append", default=[])
    resolve_parser.add_argument("--snapshot")
    resolve_parser.add_argument("--profile", required=True, choices=_PROFILES)

    snapshot_parser = sub.add_parser("snapshot")
    _add_resolution_args(snapshot_parser)
    snapshot_parser.add_argument("--workspace-config")
    snapshot_parser.add_argument("--output")

    attest_parser = sub.add_parser("attest")
    attest_parser.add_argument("--snapshot", required=True)
    attest_parser.add_argument("--profile", required=True, choices=_PROFILES)
    attest_parser.add_argument("--acceptance-from", required=True)
    attest_parser.add_argument("--output")

    migrate_parser = sub.add_parser("migrate")
    migrate_parser.add_argument("--workspace-root", default=".")
    mode = migrate_parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    migrate_parser.add_argument("--confirm", action="store_true")
    return parser


def cli_main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.operation == "resolve":
            if args.snapshot:
                if args.workspace_root or args.owner_harness or args.route:
                    raise RouteError(
                        "--snapshot conflicts with --workspace-root, --owner-harness, and --route"
                    )
                result = resolve_snapshot(Path(args.snapshot), args.profile)
            else:
                if not args.workspace_root or not args.owner_harness:
                    raise RouteError(
                        "resolve requires --snapshot or both --workspace-root and --owner-harness"
                    )
                result = resolve(
                    Path(args.workspace_root),
                    args.profile,
                    args.owner_harness,
                    overrides=args.route,
                )
        elif args.operation == "snapshot":
            result = (
                snapshot_config(
                    Path(args.workspace_config).read_bytes(),
                    args.owner_harness,
                    overrides=args.route,
                    output_path=Path(args.output) if args.output else None,
                )
                if args.workspace_config
                else snapshot(
                    Path(args.workspace_root),
                    args.owner_harness,
                    overrides=args.route,
                    output_path=Path(args.output) if args.output else None,
                )
            )
        elif args.operation == "attest":
            snap = load_snapshot(Path(args.snapshot))
            try:
                acceptance = json.loads(Path(args.acceptance_from).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RouteError(f"cannot read structured launch acceptance: {exc}") from exc
            result = attest(snap, args.profile, acceptance)
            if args.output:
                _atomic_write(
                    Path(args.output).expanduser().resolve(), _canonical_bytes(result) + b"\n"
                )
        else:
            result = migrate(
                Path(args.workspace_root), apply=bool(args.apply), confirm=bool(args.confirm)
            )
    except (OSError, RouteError) as exc:
        sys.stderr.write(f"agent-route: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "EFFORTS",
    "PUBLIC_HARNESSES",
    "RouteError",
    "attest",
    "cli_main",
    "configuration_errors",
    "load_snapshot",
    "migrate",
    "normalize_owner_harness",
    "resolve",
    "resolve_snapshot",
    "snapshot",
    "snapshot_config",
    "verify_receipt",
]
