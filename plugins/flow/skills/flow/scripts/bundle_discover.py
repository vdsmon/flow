"""Bundle discovery: walks installed plugins for `.flow-bundle.toml` manifests.

Library module (stdlib-only, no shebang, no PEP 723 inline deps). Imported by
`init.py` and `validate-workspace.py`. Also runnable as a CLI for ad-hoc
inspection / golden tests.

Schema (`schema_version = 1`):

    schema_version = 1

    [bundle]
    name        = "ship-it"
    description = "Push branch + open PR + wait on CI"

    [skills.create_pr]                          # key = flow stage name
    handler_string = "skill:ship-it:create"     # required, must start with "skill:"
    required_capabilities = []                  # optional, list[str]
    args_schema = {}                            # optional, dict
    required_outputs = ["pr_url"]               # optional, list[str]
    side_effects = ["git push", "gh pr create"] # optional, list[str]
    stage_compatibility = ["create_pr"]         # optional, list[str]

Invariants:

- Invalid UNRELATED manifest = warning (exit 0). One broken third-party plugin
  must not brick `bare` init.
- Invalid SELECTED manifest = error (exit 2). The caller passes `--select
  <bundle-name>` to opt into strict mode for a specific bundle.
- Duplicate-provider conflict (two valid manifests declare the same stage) is
  surfaced in `duplicates`. NOT an error here; `validate-workspace.py` decides
  whether the conflict matters given the workspace's chosen handlers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# Stage names that the flow stage-registry advertises. A manifest that declares
# a skill for a stage NOT in this set is rejected. This is the closed-vocabulary
# contract per build-sequence section "Adding a new stage".
_KNOWN_STAGES: frozenset[str] = frozenset(
    {
        "ticket",
        "plan",
        "implement",
        "code_review",
        "e2e",
        "commit",
        "create_pr",
        "review_loop",
        "reflect",
    }
)


# ─── Result types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ManifestSkill:
    stage: str
    handler_string: str
    required_capabilities: list[str] = field(default_factory=list)
    args_schema: dict[str, Any] = field(default_factory=dict)
    required_outputs: list[str] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)
    stage_compatibility: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Manifest:
    path: str
    bundle_name: str
    bundle_description: str
    skills: list[ManifestSkill] = field(default_factory=list)


@dataclass(frozen=True)
class ManifestError:
    path: str
    reason: str


@dataclass(frozen=True)
class DuplicateProvider:
    stage: str
    bundle_names: list[str]


@dataclass(frozen=True)
class DiscoveryResult:
    valid: list[Manifest] = field(default_factory=list)
    invalid: list[ManifestError] = field(default_factory=list)
    duplicates: list[DuplicateProvider] = field(default_factory=list)


# ─── Search roots ────────────────────────────────────────────────────────────


def default_search_roots(repo_root: Path | None = None) -> list[Path]:
    """Default plugin search locations.

    Order: env override (if set) > user-global plugins > repo-local plugins.

    `FLOW_BUNDLE_SEARCH_ROOTS` is a `:`-separated list of dirs. When set, it
    REPLACES the defaults entirely so tests + power-users can sandbox discovery.
    """
    env_override = os.environ.get("FLOW_BUNDLE_SEARCH_ROOTS")
    if env_override:
        return [Path(p).expanduser() for p in env_override.split(":") if p]

    roots = [Path.home() / ".claude" / "plugins"]
    if repo_root is not None:
        roots.append(repo_root / ".claude" / "plugins")
    return roots


# ─── Loader ──────────────────────────────────────────────────────────────────


def _validate_manifest(path: Path, data: dict[str, Any]) -> Manifest | ManifestError:
    schema_version = data.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        return ManifestError(
            path=str(path),
            reason=f"schema_version={schema_version!r}, expected {SCHEMA_VERSION}",
        )

    bundle = data.get("bundle")
    if not isinstance(bundle, dict):
        return ManifestError(path=str(path), reason="[bundle] table missing or not a table")

    name = bundle.get("name")
    if not isinstance(name, str) or not name:
        return ManifestError(path=str(path), reason="bundle.name missing or not a non-empty string")

    description = bundle.get("description")
    if not isinstance(description, str):
        return ManifestError(path=str(path), reason="bundle.description missing or not a string")

    skills_table = data.get("skills", {})
    if not isinstance(skills_table, dict):
        return ManifestError(path=str(path), reason="[skills.*] not a table")

    skills: list[ManifestSkill] = []
    for stage_name, entry in skills_table.items():
        if not isinstance(entry, dict):
            return ManifestError(path=str(path), reason=f"skills.{stage_name} is not a table")
        if stage_name not in _KNOWN_STAGES:
            return ManifestError(
                path=str(path),
                reason=f"skills.{stage_name} is not a registered flow stage",
            )

        handler_string = entry.get("handler_string")
        if not isinstance(handler_string, str) or not handler_string.startswith("skill:"):
            return ManifestError(
                path=str(path),
                reason=f"skills.{stage_name}.handler_string must be 'skill:<name>[:<args>]'",
            )

        skill_name = handler_string[len("skill:") :].split(":", 1)[0]
        if not skill_name:
            return ManifestError(
                path=str(path),
                reason=(
                    f"skills.{stage_name}.handler_string must have a non-empty "
                    f"skill name (got {handler_string!r})"
                ),
            )

        def _str_list(key: str, *, _entry: dict[str, Any] = entry) -> list[str] | None:
            v = _entry.get(key, [])
            if not isinstance(v, list):
                return None
            out: list[str] = []
            for x in v:
                if not isinstance(x, str):
                    return None
                out.append(x)
            return out

        required_capabilities = _str_list("required_capabilities")
        if required_capabilities is None:
            return ManifestError(
                path=str(path),
                reason=f"skills.{stage_name}.required_capabilities must be list[str]",
            )

        required_outputs = _str_list("required_outputs")
        if required_outputs is None:
            return ManifestError(
                path=str(path),
                reason=f"skills.{stage_name}.required_outputs must be list[str]",
            )

        side_effects = _str_list("side_effects")
        if side_effects is None:
            return ManifestError(
                path=str(path),
                reason=f"skills.{stage_name}.side_effects must be list[str]",
            )

        stage_compatibility = _str_list("stage_compatibility")
        if stage_compatibility is None:
            return ManifestError(
                path=str(path),
                reason=f"skills.{stage_name}.stage_compatibility must be list[str]",
            )

        args_schema = entry.get("args_schema", {})
        if not isinstance(args_schema, dict):
            return ManifestError(
                path=str(path),
                reason=f"skills.{stage_name}.args_schema must be a table",
            )

        skills.append(
            ManifestSkill(
                stage=stage_name,
                handler_string=handler_string,
                required_capabilities=required_capabilities,
                args_schema=args_schema,
                required_outputs=required_outputs,
                side_effects=side_effects,
                stage_compatibility=stage_compatibility,
            )
        )

    return Manifest(
        path=str(path),
        bundle_name=name,
        bundle_description=description,
        skills=skills,
    )


def _load_one(path: Path) -> Manifest | ManifestError:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return ManifestError(path=str(path), reason=f"read failed: {exc}")
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return ManifestError(path=str(path), reason=f"TOML parse failed: {exc}")
    return _validate_manifest(path, data)


def _find_manifests(roots: list[Path]) -> list[Path]:
    """Walk roots looking for `.flow-bundle.toml` files. Sorted by path for determinism."""
    found: set[Path] = set()
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for candidate in root.rglob(".flow-bundle.toml"):
            if candidate.is_file():
                found.add(candidate.resolve())
    return sorted(found)


# ─── Public entry points ─────────────────────────────────────────────────────


def discover(
    roots: list[Path] | None = None,
    repo_root: Path | None = None,
) -> DiscoveryResult:
    """Discover all `.flow-bundle.toml` manifests under `roots`.

    Returns a `DiscoveryResult` with valid manifests, invalid manifests
    (warnings), and duplicate stage-providers (warnings). Never raises for
    individual manifest failures — callers decide whether a failure blocks them
    based on the bundle name they care about.
    """
    if roots is None:
        roots = default_search_roots(repo_root=repo_root)

    valid: list[Manifest] = []
    invalid: list[ManifestError] = []
    for path in _find_manifests(roots):
        result = _load_one(path)
        if isinstance(result, Manifest):
            valid.append(result)
        else:
            invalid.append(result)

    # Duplicate-provider check across valid manifests.
    stage_to_bundles: dict[str, list[str]] = {}
    for manifest in valid:
        for skill in manifest.skills:
            stage_to_bundles.setdefault(skill.stage, []).append(manifest.bundle_name)
    duplicates = [
        DuplicateProvider(stage=stage, bundle_names=sorted(set(names)))
        for stage, names in stage_to_bundles.items()
        if len(set(names)) > 1
    ]
    duplicates.sort(key=lambda d: d.stage)

    return DiscoveryResult(valid=valid, invalid=invalid, duplicates=duplicates)


def to_json_dict(result: DiscoveryResult) -> dict[str, Any]:
    """Plain-dict shape for JSON output / cross-script piping."""
    return {
        "schema_version": SCHEMA_VERSION,
        "valid": [asdict(m) for m in result.valid],
        "invalid": [asdict(e) for e in result.invalid],
        "duplicates": [asdict(d) for d in result.duplicates],
    }


def select_bundle(result: DiscoveryResult, bundle_name: str) -> Manifest | None:
    """Find a single valid manifest by bundle name; None if not present."""
    for manifest in result.valid:
        if manifest.bundle_name == bundle_name:
            return manifest
    return None


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover .flow-bundle.toml manifests from installed plugins.",
    )
    parser.add_argument(
        "--roots",
        help="colon-separated dirs (overrides env + defaults)",
        default=None,
    )
    parser.add_argument(
        "--repo-root",
        help="repo root (for <repo>/.claude/plugins/* discovery); default cwd",
        default=None,
    )
    parser.add_argument(
        "--select",
        help="bundle name to opt into strict mode (exit 2 if its manifest is invalid)",
        default=None,
    )
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)

    if args.roots is not None:
        roots = [Path(p).expanduser() for p in args.roots.split(":") if p]
    else:
        repo_root = Path(args.repo_root).expanduser() if args.repo_root else Path.cwd()
        roots = default_search_roots(repo_root=repo_root)

    result = discover(roots=roots)
    payload = to_json_dict(result)
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
    sys.stdout.write("\n")

    # Strict mode for one bundle. Invalid SELECTED bundle = exit 2.
    if args.select and select_bundle(result, args.select) is None:
        for err in result.invalid:
            # Surface the most-likely culprit (path containing the selected name).
            if args.select in err.path:
                sys.stderr.write(
                    f"selected bundle {args.select!r} manifest invalid: {err.path}: {err.reason}\n"
                )
                return 2
        sys.stderr.write(f"selected bundle {args.select!r} not found among valid manifests\n")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "SCHEMA_VERSION",
    "DiscoveryResult",
    "DuplicateProvider",
    "Manifest",
    "ManifestError",
    "ManifestSkill",
    "cli_main",
    "default_search_roots",
    "discover",
    "select_bundle",
    "to_json_dict",
]
