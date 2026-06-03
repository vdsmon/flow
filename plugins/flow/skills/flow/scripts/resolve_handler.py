"""Resolve a workspace handler string to a concrete, installed invocation.

Library + thin CLI. Stdlib-only. Used by the /flow do-loop before invoking a
skill handler, and re-usable by validate_workspace.

Handler-string grammar:

  inline                  -> run inline, no external skill.
  none                    -> stage is a no-op.
  subagent:<type>         -> dispatch to a Claude Code subagent of <type>.
  skill:<name>[:<args>]   -> invoke an installed flow-bundle skill.

For the `skill:` form we reuse `bundle_discover.discover` / `select_bundle`
rather than re-walking plugins. A bundle is "installed" when a `.flow-bundle.toml`
declaring that name is present on disk, regardless of whether it parses; it is
also "valid" only when the manifest passes bundle_discover's schema checks.

Exit codes (CLI):
  0 = resolved + installed + valid, or inline/subagent/none.
  1 = skill not installed.
  2 = skill installed but manifest invalid.
  3 = invalid handler string (handler_type=unknown).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import bundle_discover as bd

_SKILL_PREFIX = "skill:"
_SUBAGENT_PREFIX = "subagent:"


@dataclass(frozen=True)
class HandlerResolution:
    handler_type: str
    subagent_type: str | None = None
    skill_name: str | None = None
    skill_args: str | None = None
    invocation: str | None = None
    plugin_root: str | None = None
    installed: bool = False
    manifest_valid: bool = False
    error: str | None = None


def _bundle_dir_name(manifest_error_path: str) -> str:
    # ManifestError carries no bundle name (it may have failed before a name
    # could be trusted), so identify a present-but-invalid bundle by the plugin
    # directory holding its .flow-bundle.toml.
    return Path(manifest_error_path).parent.name


def _provides_skill(manifest: bd.Manifest, name: str) -> bool:
    for skill in manifest.skills:
        rest = skill.handler_string[len(_SKILL_PREFIX) :]
        if rest.split(":", 1)[0] == name:
            return True
    return False


def _resolve_skill(
    handler_string: str,
    name: str,
    args: str,
    search_roots: list[Path] | None,
) -> HandlerResolution:
    result = bd.discover(roots=search_roots)

    manifest = bd.select_bundle(result, name)
    if manifest is None:
        for candidate in result.valid:
            if _provides_skill(candidate, name):
                manifest = candidate
                break

    if manifest is not None:
        return HandlerResolution(
            handler_type="skill",
            skill_name=name,
            skill_args=args,
            invocation=handler_string,
            plugin_root=str(Path(manifest.path).parent),
            installed=True,
            manifest_valid=True,
        )

    for err in result.invalid:
        if _bundle_dir_name(err.path) == name:
            return HandlerResolution(
                handler_type="skill",
                skill_name=name,
                skill_args=args,
                plugin_root=str(Path(err.path).parent),
                installed=True,
                manifest_valid=False,
                error=err.reason,
            )

    return HandlerResolution(
        handler_type="skill",
        skill_name=name,
        skill_args=args,
        installed=False,
        manifest_valid=False,
        error=f"handler skill:{name} not installed",
    )


def resolve(handler_string: str, search_roots: list[Path] | None = None) -> HandlerResolution:
    """Resolve `handler_string` to a HandlerResolution.

    Never raises for a malformed handler string; the unparseable case returns
    `handler_type="unknown"` with `error` set so callers can branch on exit code.
    """
    if handler_string == "inline":
        return HandlerResolution(handler_type="inline", installed=True, manifest_valid=True)

    if handler_string == "none":
        return HandlerResolution(handler_type="none", installed=True, manifest_valid=True)

    if handler_string.startswith(_SUBAGENT_PREFIX):
        subagent_type = handler_string[len(_SUBAGENT_PREFIX) :]
        if not subagent_type:
            return HandlerResolution(
                handler_type="unknown",
                error=f"empty subagent type in handler {handler_string!r}",
            )
        return HandlerResolution(
            handler_type="subagent",
            subagent_type=subagent_type,
            invocation=handler_string,
            installed=True,
            manifest_valid=True,
        )

    if handler_string.startswith(_SKILL_PREFIX):
        rest = handler_string[len(_SKILL_PREFIX) :]
        parts = rest.split(":", 1)
        name = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        if not name:
            return HandlerResolution(
                handler_type="unknown",
                error=f"empty skill name in handler {handler_string!r}",
            )
        return _resolve_skill(handler_string, name, args, search_roots)

    return HandlerResolution(
        handler_type="unknown",
        error=f"unrecognized handler string {handler_string!r}",
    )


def _exit_code(resolution: HandlerResolution) -> int:
    if resolution.handler_type == "unknown":
        return 3
    if resolution.handler_type == "skill":
        if not resolution.installed:
            return 1
        if not resolution.manifest_valid:
            return 2
        return 0
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve a workspace handler string to a concrete invocation.",
    )
    parser.add_argument("--handler", required=True, help="handler string to resolve")
    parser.add_argument(
        "--search-roots",
        default=None,
        help="colon-separated plugin search dirs (overrides bundle_discover defaults)",
    )
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    search_roots: list[Path] | None = None
    if args.search_roots is not None:
        search_roots = [Path(p).expanduser() for p in args.search_roots.split(":") if p]

    resolution = resolve(args.handler, search_roots=search_roots)
    sys.stdout.write(json.dumps(asdict(resolution), indent=2, sort_keys=True))
    sys.stdout.write("\n")
    return _exit_code(resolution)


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["HandlerResolution", "cli_main", "resolve"]
