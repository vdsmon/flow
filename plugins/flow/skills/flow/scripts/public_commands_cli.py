"""Operational facade for Flow's public command registry.

``route`` lets skill prose hand a logical invocation to the deterministic
registry before it performs any orchestration. ``help`` emits the same logical
FLOW vocabulary for both harness adapters.
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
    return {
        "command_id": command_id,
        "effect": effect,
        "kind": route.kind,
        "options": list(route.options),
        "positionals": list(route.positionals),
        "reference": reference,
        "topic": route.topic,
        "workspace": workspace,
    }


def cli_main(argv: list[str]) -> int:
    if argv and argv[0] == "route" and "--" not in argv:
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
