"""Operational facade for Flow's public command registry.

``route`` lets skill prose hand a logical invocation to the deterministic
registry before it performs any orchestration. ``help`` emits the same logical
FLOW vocabulary for both harness adapters.

``route`` exits 7 when this script is executing from a skill tree that disagrees
with the workspace's ``.flow/runtime/skill-root`` pin; the error names both paths
and the remedy is re-binding to the pinned path, never retrying the stale copy.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from public_commands import (
    Registry,
    RegistryError,
    load_registry,
    render_help,
    route_tokens,
    tracker_key_patterns_from_workspace,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect Flow's public command grammar.")
    subparsers = parser.add_subparsers(dest="operation", required=True)

    route = subparsers.add_parser("route", help="Validate and classify public command tokens.")
    route.add_argument("--workspace-root")
    route.add_argument("tokens", nargs=argparse.REMAINDER)

    help_parser = subparsers.add_parser("help", help="Render logical FLOW help.")
    help_parser.add_argument("topic", nargs="?")
    return parser


def _stale_skill_root(workspace_root: Path) -> tuple[Path, Path] | None:
    """The pin decides which engine's registry answers a route.

    Witnessed 2026-08-07, twice: a session read the fresh workspace pin, then routed
    from its stale invocation cache in the same shell command; the cache's registry
    lacked a verb the pinned engine already carried, and the session fell back to
    ad-hoc work outside every flow covenant. An absent or dangling pin stays routable
    so setup and repair paths never deadlock.
    """
    pin = workspace_root / ".flow" / "runtime" / "skill-root"
    try:
        pinned = Path(pin.read_text(encoding="utf-8").strip())
    except OSError:
        return None
    if not pinned.is_dir():
        return None
    own = Path(__file__).resolve().parents[1]
    pinned = pinned.resolve()
    if own == pinned:
        return None
    return own, pinned


def _help_reference(registry: Registry, topic: str) -> str:
    for command in registry.commands:
        if command.path and command.path[0] == topic:
            return command.reference
    raise RegistryError(f"unknown help topic {topic!r}")


def _route_payload(registry: Registry, tokens: list[str], patterns: list[str]) -> dict[str, object]:
    route = route_tokens(tokens, registry, patterns)
    if route.command is not None:
        command_id = route.command.id
        effect = route.command.effect
        workspace = route.command.workspace
        reference = route.command.reference
    else:
        if route.topic is None:
            raise RegistryError("scoped help route is missing its topic")
        command_id = None
        effect = "read"
        workspace = "none"
        reference = _help_reference(registry, route.topic)
    option_values: dict[str, list[str]] = {}
    for name, value in route.option_values:
        option_values.setdefault(name, []).append(value)
    return {
        "command_id": command_id,
        "effect": effect,
        "kind": route.kind,
        "options": list(route.options),
        "option_values": option_values,
        "positionals": list(route.positionals),
        "reference": reference,
        "topic": route.topic,
        "workspace": workspace,
    }


def cli_main(argv: list[str]) -> int:
    if argv and argv[0] == "route" and "--" not in argv and not {"-h", "--help"} & set(argv):
        sys.stderr.write("commands: pass public command tokens after --\n")
        return 2

    args = _parser().parse_args(argv)
    registry = load_registry()
    try:
        if args.operation == "help":
            sys.stdout.write(render_help(registry, args.topic))
            return 0
        if args.operation == "route":
            tokens = list(args.tokens)
            if tokens and tokens[0] == "--":
                tokens.pop(0)
            if not tokens:
                # An explicit empty token sequence is the bare cockpit command.
                # The `--` separator itself is enough to express that intent.
                tokens = []
            patterns: tuple[str, ...] = ()
            if args.workspace_root:
                workspace_root = Path(args.workspace_root).expanduser()
                if not workspace_root.is_absolute():
                    raise RegistryError("--workspace-root must be an absolute path")
                stale = _stale_skill_root(workspace_root)
                if stale is not None:
                    own, pinned = stale
                    sys.stderr.write(
                        f"commands: stale skill_root: this router runs from {own} "
                        f"but the workspace pins {pinned}; re-bind skill_root to "
                        "the pinned path and re-route from there\n"
                    )
                    return 7
                patterns = tracker_key_patterns_from_workspace(workspace_root)
            payload = _route_payload(registry, tokens, list(patterns))
            sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
            return 0
    except RegistryError as exc:
        sys.stderr.write(f"commands: {exc}\n")
        return 2

    sys.stderr.write(f"commands: unknown operation {args.operation!r}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["cli_main"]
