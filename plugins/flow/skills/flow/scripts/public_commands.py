"""Declarative public command registry, routing, and generated prose helpers.

This module is stdlib-only and side-effect free.  It intentionally stops at the
public intent boundary: it selects and validates a command, but does not dispatch
runtime scripts or touch workspace state.
"""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlparse

SCHEMA_VERSION = 1
DEFAULT_REGISTRY = Path(__file__).resolve().parent.parent / "public-commands.toml"
STATIC_NAMESPACES = ("ticket", "memory", "measure", "workspace", "maintain", "help")

Effect = Literal["read", "confirm", "write"]
WorkspaceRequirement = Literal["none", "optional", "required"]
Cardinality = Literal["one", "zero_or_one", "zero_or_more", "one_or_more"]

_EFFECTS = frozenset({"read", "confirm", "write"})
_WORKSPACE_REQUIREMENTS = frozenset({"none", "optional", "required"})
_CARDINALITIES = frozenset({"one", "zero_or_one", "zero_or_more", "one_or_more"})
_HARNESSES = frozenset({"claude-code", "codex"})
_PATH_TOKEN_RE = re.compile(r"[a-z][a-z0-9-]*")
_OPTION_RE = re.compile(r"--[a-z][a-z0-9-]*")
_PR_PATH_RE = re.compile(r"/(?:pull|pull-requests)/(\d+)(?:/|$)")


class RegistryError(ValueError):
    """Registry schema or user command tokens are invalid."""


class GeneratedContentDrift(RegistryError):
    """A managed documentation block does not match its registry rendering."""


class TargetKind(StrEnum):
    NAMESPACE = "namespace"
    TICKET = "ticket"
    PR = "pr"
    PR_URL = "pr_url"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClassifiedTarget:
    kind: TargetKind
    value: str
    raw: str


@dataclass(frozen=True)
class ArgumentSpec:
    name: str
    value_type: str
    cardinality: Cardinality
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class OptionSpec:
    name: str
    value_type: str
    cardinality: Cardinality
    choices: tuple[str, ...] = ()
    conflicts: frozenset[str] = frozenset()
    requires: frozenset[str] = frozenset()
    allowed_argument_values: frozenset[str] = frozenset()
    forbidden_argument_values: frozenset[str] = frozenset()

    @property
    def consumes_value(self) -> bool:
        return self.value_type != "boolean"


@dataclass(frozen=True)
class CommandSpec:
    id: str
    path: tuple[str, ...]
    usage: str
    summary: str
    effect: Effect
    workspace: WorkspaceRequirement
    reference: str
    harnesses: frozenset[str]
    additional_usages: tuple[str, ...] = ()
    arguments: tuple[ArgumentSpec, ...] = ()
    options: tuple[OptionSpec, ...] = ()
    constraints: tuple[str, ...] = ()


@dataclass(frozen=True)
class Registry:
    schema_version: int
    logical_trigger: str
    static_namespaces: tuple[str, ...]
    forbidden_root_tokens: frozenset[str]
    commands: tuple[CommandSpec, ...]

    @property
    def by_id(self) -> dict[str, CommandSpec]:
        return {command.id: command for command in self.commands}

    @property
    def root_tokens(self) -> frozenset[str]:
        return frozenset(
            command.path[0]
            for command in self.commands
            if command.path and not command.path[0].startswith("<")
        )


@dataclass(frozen=True)
class Route:
    kind: Literal["command", "help"]
    command: CommandSpec | None
    positionals: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    topic: str | None = None


def _strings(value: object, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RegistryError(f"{field} must be an array of strings")
    return tuple(str(item) for item in value)


def _entries(value: object, *, field: str) -> list[object]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise RegistryError(f"{field} must be an array of tables")
    return cast(list[object], value)


def _parse_argument(raw: object, *, command_id: str) -> ArgumentSpec:
    if not isinstance(raw, dict):
        raise RegistryError(f"{command_id}: argument must be a table")
    name = raw.get("name")
    value_type = raw.get("value_type")
    cardinality = raw.get("cardinality")
    if not isinstance(name, str) or not name:
        raise RegistryError(f"{command_id}: argument missing name")
    if not isinstance(value_type, str) or not value_type:
        raise RegistryError(f"{command_id}: argument {name!r} missing value_type")
    if cardinality not in _CARDINALITIES:
        raise RegistryError(f"{command_id}: argument {name!r} has invalid cardinality")
    return ArgumentSpec(
        name=name,
        value_type=value_type,
        cardinality=cast(Cardinality, cardinality),
        choices=_strings(raw.get("choices"), field=f"{command_id}.{name}.choices"),
    )


def _parse_option(raw: object, *, command_id: str) -> OptionSpec:
    if not isinstance(raw, dict):
        raise RegistryError(f"{command_id}: option must be a table")
    name = raw.get("name")
    value_type = raw.get("value_type")
    cardinality = raw.get("cardinality")
    if not isinstance(name, str) or not _OPTION_RE.fullmatch(name):
        raise RegistryError(f"{command_id}: invalid option name {name!r}")
    if not isinstance(value_type, str) or not value_type:
        raise RegistryError(f"{command_id}: option {name!r} missing value_type")
    if cardinality not in _CARDINALITIES:
        raise RegistryError(f"{command_id}: option {name!r} has invalid cardinality")
    return OptionSpec(
        name=name,
        value_type=value_type,
        cardinality=cast(Cardinality, cardinality),
        choices=_strings(raw.get("choices"), field=f"{command_id}.{name}.choices"),
        conflicts=frozenset(_strings(raw.get("conflicts"), field=f"{command_id}.{name}.conflicts")),
        requires=frozenset(_strings(raw.get("requires"), field=f"{command_id}.{name}.requires")),
        allowed_argument_values=frozenset(
            _strings(
                raw.get("allowed_argument_values"),
                field=f"{command_id}.{name}.allowed_argument_values",
            )
        ),
        forbidden_argument_values=frozenset(
            _strings(
                raw.get("forbidden_argument_values"),
                field=f"{command_id}.{name}.forbidden_argument_values",
            )
        ),
    )


def _parse_command(raw: object, *, logical_trigger: str) -> CommandSpec:
    if not isinstance(raw, dict):
        raise RegistryError("command entry must be a table")
    command_id = raw.get("id")
    if not isinstance(command_id, str) or not command_id:
        raise RegistryError("command entry missing id")
    path = _strings(raw.get("path"), field=f"{command_id}.path")
    for token in path:
        if token != "<target>" and not _PATH_TOKEN_RE.fullmatch(token):
            raise RegistryError(f"{command_id}: invalid path token {token!r}")
    effect = raw.get("effect")
    workspace = raw.get("workspace")
    if effect not in _EFFECTS:
        raise RegistryError(f"{command_id}: invalid effect {effect!r}")
    if workspace not in _WORKSPACE_REQUIREMENTS:
        raise RegistryError(f"{command_id}: invalid workspace requirement {workspace!r}")
    summary = raw.get("summary")
    reference = raw.get("reference")
    if not isinstance(summary, str) or not summary:
        raise RegistryError(f"{command_id}: missing summary")
    if not isinstance(reference, str) or not reference:
        raise RegistryError(f"{command_id}: missing reference")
    usage = raw.get("usage")
    if usage is None:
        usage = " ".join((logical_trigger, *path))
    if not isinstance(usage, str) or not usage.startswith(logical_trigger):
        raise RegistryError(f"{command_id}: usage must start with {logical_trigger}")
    arguments = tuple(
        _parse_argument(item, command_id=command_id)
        for item in _entries(raw.get("arguments"), field=f"{command_id}.arguments")
    )
    options = tuple(
        _parse_option(item, command_id=command_id)
        for item in _entries(raw.get("options"), field=f"{command_id}.options")
    )
    return CommandSpec(
        id=command_id,
        path=path,
        usage=usage,
        summary=summary,
        effect=cast(Effect, effect),
        workspace=cast(WorkspaceRequirement, workspace),
        reference=reference,
        harnesses=frozenset(_strings(raw.get("harnesses"), field=f"{command_id}.harnesses")),
        additional_usages=_strings(
            raw.get("additional_usages"), field=f"{command_id}.additional_usages"
        ),
        arguments=arguments,
        options=options,
        constraints=_strings(raw.get("constraints"), field=f"{command_id}.constraints"),
    )


def _validate_command_options(command: CommandSpec) -> None:
    option_by_name = {option.name: option for option in command.options}
    if len(option_by_name) != len(command.options):
        raise RegistryError(f"{command.id}: duplicate option")
    for option in command.options:
        unknown = (option.conflicts | option.requires) - option_by_name.keys()
        if unknown:
            raise RegistryError(f"{command.id}: {option.name} references unknown option")
        for conflict in option.conflicts:
            if option.name not in option_by_name[conflict].conflicts:
                raise RegistryError(
                    f"{command.id}: conflict {option.name}/{conflict} is not symmetric"
                )
        if option.value_type == "choice" and not option.choices:
            raise RegistryError(f"{command.id}: {option.name} choice has no values")
    for argument in command.arguments:
        if argument.value_type == "choice" and not argument.choices:
            raise RegistryError(f"{command.id}: {argument.name} choice has no values")


def _validate_required_routes(registry: Registry, ids: set[str]) -> None:
    missing = {"cockpit", "target"} - ids
    if missing:
        raise RegistryError(f"required commands missing: {', '.join(sorted(missing))}")
    by_id = registry.by_id
    if by_id["cockpit"].path:
        raise RegistryError("cockpit command path must be empty")
    if by_id["target"].path != ("<target>",):
        raise RegistryError("target command path must be exactly <target>")
    for command in registry.commands:
        if "<target>" in command.path and command.id != "target":
            raise RegistryError(f"{command.id}: only target may use the <target> path token")


def _validate_commands(registry: Registry) -> None:
    ids: set[str] = set()
    paths: set[tuple[str, ...]] = set()
    for command in registry.commands:
        if command.id in ids:
            raise RegistryError(f"duplicate command id {command.id!r}")
        if command.path in paths:
            rendered = " ".join(command.path) or "<root>"
            raise RegistryError(f"duplicate command path {rendered!r}")
        ids.add(command.id)
        paths.add(command.path)

        if command.harnesses != _HARNESSES:
            raise RegistryError(f"{command.id}: harnesses must be exactly {sorted(_HARNESSES)}")
        if (
            command.path
            and command.path[0] != "<target>"
            and command.path[0] not in registry.static_namespaces
        ):
            raise RegistryError(f"{command.id}: root {command.path[0]!r} is not a static namespace")
        _validate_command_options(command)

    _validate_required_routes(registry, ids)


def load_registry(path: Path = DEFAULT_REGISTRY) -> Registry:
    """Load and fully validate ``public-commands.toml``."""

    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RegistryError(f"cannot load command registry {path}: {exc}") from exc
    schema_version = raw.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise RegistryError(
            f"unsupported command registry schema {schema_version!r}; expected {SCHEMA_VERSION}"
        )
    logical_trigger = raw.get("logical_trigger", "FLOW")
    if not isinstance(logical_trigger, str) or not logical_trigger:
        raise RegistryError("logical_trigger must be a non-empty string")
    static_namespaces = _strings(raw.get("static_namespaces"), field="static_namespaces")
    if len(static_namespaces) != len(set(static_namespaces)):
        raise RegistryError("static_namespaces contains duplicates")
    forbidden_root_tokens = frozenset(
        _strings(raw.get("forbidden_root_tokens"), field="forbidden_root_tokens")
    )
    overlap = forbidden_root_tokens & set(static_namespaces)
    if overlap:
        raise RegistryError(f"forbidden root token is also a namespace: {sorted(overlap)[0]}")
    commands_raw = raw.get("command")
    if not isinstance(commands_raw, list):
        raise RegistryError("command must be an array of tables")
    commands = tuple(_parse_command(item, logical_trigger=logical_trigger) for item in commands_raw)
    registry = Registry(
        schema_version=schema_version,
        logical_trigger=logical_trigger,
        static_namespaces=static_namespaces,
        forbidden_root_tokens=forbidden_root_tokens,
        commands=commands,
    )
    _validate_commands(registry)
    return registry


def tracker_key_patterns_from_workspace(workspace_root: Path) -> tuple[str, ...]:
    """Derive the tracker-key grammar from one initialized workspace.

    The derivation is shared by every harness. Callers must not invent a looser
    regular expression from prose or tracker examples.
    """

    root = workspace_root.expanduser().resolve()
    config_path = root / ".flow" / "workspace.toml"
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RegistryError(f"cannot read tracker configuration from {config_path}: {exc}") from exc
    tracker = data.get("tracker")
    if not isinstance(tracker, dict):
        raise RegistryError(f"{config_path} is missing [tracker]")
    backend = tracker.get("backend")
    backend_config = tracker.get(backend) if isinstance(backend, str) else None
    if not isinstance(backend_config, dict):
        raise RegistryError(f"{config_path} is missing [tracker.{backend}]")
    if backend == "jira":
        project_key = backend_config.get("project_key")
        if not isinstance(project_key, str) or not project_key:
            raise RegistryError(f"{config_path} is missing tracker.jira.project_key")
        return (rf"{re.escape(project_key)}-\d+",)
    if backend == "beads":
        prefix = backend_config.get("prefix")
        if not isinstance(prefix, str) or not prefix:
            raise RegistryError(f"{config_path} is missing tracker.beads.prefix")
        return (rf"{re.escape(prefix)}-[a-z0-9]+(?:\.[a-z0-9]+)*",)
    raise RegistryError(f"{config_path} has unsupported tracker backend {backend!r}")


def validate_tracker_key_patterns(patterns: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Compile every explicit pattern before routing, including static routes."""

    validated: list[str] = []
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise RegistryError(f"invalid tracker key pattern {pattern!r}: {exc}") from exc
        validated.append(pattern)
    return tuple(validated)


def classify_root_token(
    token: str,
    tracker_key_patterns: tuple[str, ...] | list[str] = (),
    *,
    static_namespaces: tuple[str, ...] | list[str] = STATIC_NAMESPACES,
    forbidden_root_tokens: frozenset[str] | set[str] = frozenset(),
) -> ClassifiedTarget:
    """Classify a root token, giving static namespaces absolute precedence."""

    if token in static_namespaces:
        return ClassifiedTarget(TargetKind.NAMESPACE, token, token)

    if token in forbidden_root_tokens:
        return ClassifiedTarget(TargetKind.UNKNOWN, token, token)

    if token.startswith("ticket:"):
        key = token.removeprefix("ticket:")
        if key and not any(char.isspace() for char in key):
            return ClassifiedTarget(TargetKind.TICKET, key, token)

    if token.startswith("pr:"):
        number = token.removeprefix("pr:")
        if number.isdigit() and int(number) > 0:
            return ClassifiedTarget(TargetKind.PR, number, token)

    parsed = urlparse(token)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        match = _PR_PATH_RE.search(parsed.path)
        if match and int(match.group(1)) > 0:
            return ClassifiedTarget(TargetKind.PR_URL, match.group(1), token)

    for pattern in tracker_key_patterns:
        try:
            if re.fullmatch(pattern, token):
                return ClassifiedTarget(TargetKind.TICKET, token, token)
        except re.error as exc:
            raise RegistryError(f"invalid tracker key pattern {pattern!r}: {exc}") from exc

    return ClassifiedTarget(TargetKind.UNKNOWN, token, token)


def _validate_typed_value(
    value: str,
    *,
    value_type: str,
    label: str,
    tracker_key_patterns: tuple[str, ...] | list[str],
    forbidden_root_tokens: frozenset[str] | set[str],
) -> None:
    if not value or value.isspace() or "\x00" in value:
        raise RegistryError(f"{label} requires a non-empty value")
    if value_type == "integer":
        try:
            int(value, 10)
        except ValueError as exc:
            raise RegistryError(f"{label} requires an integer, got {value!r}") from exc
    elif value_type == "float":
        try:
            parsed = float(value)
        except ValueError as exc:
            raise RegistryError(f"{label} requires a float, got {value!r}") from exc
        if not math.isfinite(parsed):
            raise RegistryError(f"{label} requires a finite float, got {value!r}")
    elif value_type == "date":
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise RegistryError(
                f"{label} requires an ISO date (YYYY-MM-DD), got {value!r}"
            ) from exc
    elif value_type == "ticket":
        kind = classify_root_token(
            value,
            tracker_key_patterns,
            forbidden_root_tokens=forbidden_root_tokens,
        ).kind
        if kind is not TargetKind.TICKET:
            raise RegistryError(f"{label} requires a configured ticket key, got {value!r}")


def _validate_argument_values(
    command: CommandSpec,
    positionals: list[str],
    tracker_key_patterns: tuple[str, ...] | list[str],
    forbidden_root_tokens: frozenset[str] | set[str] = frozenset(),
) -> None:
    if not command.arguments:
        if positionals:
            raise RegistryError(f"{command.id}: unexpected argument {positionals[0]!r}")
        return

    # The v2 grammar has at most one positional argument group per command.
    # Refuse a more complicated registry until the allocation rule is explicit.
    if len(command.arguments) != 1:
        raise RegistryError(f"{command.id}: multiple argument groups are not supported")
    argument = command.arguments[0]
    count = len(positionals)
    if argument.cardinality == "one" and count != 1:
        raise RegistryError(f"{command.id}: expected exactly one {argument.name}")
    if argument.cardinality == "zero_or_one" and count > 1:
        raise RegistryError(f"{command.id}: expected at most one {argument.name}")
    if argument.cardinality == "one_or_more" and count < 1:
        raise RegistryError(f"{command.id}: expected one or more {argument.name}")
    if argument.choices:
        invalid = [value for value in positionals if value not in argument.choices]
        if invalid:
            raise RegistryError(
                f"{command.id}: invalid {argument.name} {invalid[0]!r}; "
                f"choose from {', '.join(argument.choices)}"
            )
    for value in positionals:
        _validate_typed_value(
            value,
            value_type=argument.value_type,
            label=f"{command.id}: {argument.name}",
            tracker_key_patterns=tracker_key_patterns,
            forbidden_root_tokens=forbidden_root_tokens,
        )
    if argument.value_type in {"target", "ticket"}:
        allowed = (
            {TargetKind.TICKET, TargetKind.PR, TargetKind.PR_URL}
            if argument.value_type == "target"
            else {TargetKind.TICKET}
        )
        for value in positionals:
            if (
                classify_root_token(
                    value,
                    tracker_key_patterns,
                    forbidden_root_tokens=forbidden_root_tokens,
                ).kind
                not in allowed
            ):
                raise RegistryError(f"{command.id}: invalid {argument.value_type} {value!r}")


def _consume_tail(
    command: CommandSpec,
    tail: list[str],
    tracker_key_patterns: tuple[str, ...] | list[str],
    forbidden_root_tokens: frozenset[str] | set[str],
) -> tuple[list[str], list[str]]:
    option_by_name = {option.name: option for option in command.options}
    positionals: list[str] = []
    seen: list[str] = []
    index = 0
    while index < len(tail):
        token = tail[index]
        if not token.startswith("--"):
            positionals.append(token)
            index += 1
            continue

        name, separator, attached = token.partition("=")
        option = option_by_name.get(name)
        if option is None:
            raise RegistryError(f"{command.id}: unknown option {name!r}")
        if option.cardinality in {"one", "zero_or_one"} and name in seen:
            raise RegistryError(f"{command.id}: option {name} may not be repeated")
        seen.append(name)
        if option.consumes_value:
            if separator:
                value = attached
            else:
                index += 1
                if index >= len(tail) or tail[index].startswith("--"):
                    raise RegistryError(f"{command.id}: option {name} requires a value")
                value = tail[index]
            if option.choices and value not in option.choices:
                raise RegistryError(
                    f"{command.id}: invalid value {value!r} for {name}; "
                    f"choose from {', '.join(option.choices)}"
                )
            _validate_typed_value(
                value,
                value_type=option.value_type,
                label=f"{command.id}: option {name}",
                tracker_key_patterns=tracker_key_patterns,
                forbidden_root_tokens=forbidden_root_tokens,
            )
        elif separator:
            raise RegistryError(f"{command.id}: boolean option {name} takes no value")
        index += 1

    return positionals, seen


def _validate_seen_options(command: CommandSpec, positionals: list[str], seen: list[str]) -> None:
    option_by_name = {option.name: option for option in command.options}
    first_argument = positionals[0] if positionals else None
    seen_set = set(seen)
    for name in seen_set:
        option = option_by_name[name]
        conflict = option.conflicts & seen_set
        if conflict:
            other = sorted(conflict)[0]
            raise RegistryError(f"{command.id}: option {name} conflicts with {other}")
        missing = option.requires - seen_set
        if missing:
            raise RegistryError(f"{command.id}: option {name} requires {sorted(missing)[0]}")
        if option.allowed_argument_values and first_argument not in option.allowed_argument_values:
            raise RegistryError(f"{command.id}: option {name} is not valid for {first_argument!r}")
        if first_argument in option.forbidden_argument_values:
            raise RegistryError(f"{command.id}: option {name} is not valid for {first_argument!r}")


def _parse_command_tail(
    command: CommandSpec,
    tail: list[str],
    tracker_key_patterns: tuple[str, ...] | list[str],
    forbidden_root_tokens: frozenset[str] | set[str] = frozenset(),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    positionals, seen = _consume_tail(command, tail, tracker_key_patterns, forbidden_root_tokens)

    _validate_argument_values(command, positionals, tracker_key_patterns, forbidden_root_tokens)
    _validate_seen_options(command, positionals, seen)
    seen_set = set(seen)
    if command.id == "ticket.group":
        explicit = bool(positionals)
        mine = "--mine" in seen_set
        if explicit == mine:
            raise RegistryError("ticket.group: pass explicit tickets or --mine, not both")

    return tuple(positionals), tuple(seen)


def route_tokens(
    argv: list[str] | tuple[str, ...],
    registry: Registry,
    tracker_key_patterns: tuple[str, ...] | list[str] = (),
) -> Route:
    """Select and structurally validate one public command invocation."""

    tracker_key_patterns = validate_tracker_key_patterns(tracker_key_patterns)
    tokens = list(argv)
    if not tokens:
        return Route(kind="command", command=registry.by_id["cockpit"])

    classified = classify_root_token(
        tokens[0],
        tracker_key_patterns,
        static_namespaces=registry.static_namespaces,
        forbidden_root_tokens=registry.forbidden_root_tokens,
    )
    if classified.kind is TargetKind.NAMESPACE:
        root = classified.value
        candidates = [
            command
            for command in registry.commands
            if command.path
            and command.path[0] == root
            and tuple(tokens[: len(command.path)]) == command.path
        ]
        if not candidates:
            raise RegistryError(f"unknown {root} command: {' '.join(tokens[1:]) or '<none>'}")
        command = max(candidates, key=lambda candidate: len(candidate.path))
        tail = tokens[len(command.path) :]
        positionals, options = _parse_command_tail(
            command, tail, tracker_key_patterns, registry.forbidden_root_tokens
        )
        topic = positionals[0] if command.id == "help" and positionals else None
        return Route(
            kind="help" if command.id == "help" else "command",
            command=command,
            positionals=positionals,
            options=options,
            topic=topic,
        )

    if classified.kind in {TargetKind.TICKET, TargetKind.PR, TargetKind.PR_URL}:
        command = registry.by_id["target"]
        positionals, options = _parse_command_tail(
            command, tokens, tracker_key_patterns, registry.forbidden_root_tokens
        )
        return Route(kind="command", command=command, positionals=positionals, options=options)

    raise RegistryError(f"unknown command or target {tokens[0]!r}")


def render_help(registry: Registry, topic: str | None = None) -> str:
    """Render stable logical help; harness adapters substitute ``FLOW`` later."""

    if topic is not None and topic not in registry.static_namespaces:
        raise RegistryError(f"unknown help topic {topic!r}")
    commands = [
        command
        for command in registry.commands
        if command.id != "cockpit"
        and (topic is None or (command.path and command.path[0] == topic))
    ]
    title = "Flow commands" if topic is None else f"Flow {topic} commands"
    lines = [title, ""]
    if topic is None:
        lines.append(f"  {registry.logical_trigger}")
    for command in commands:
        lines.extend(f"  {usage}" for usage in (command.usage, *command.additional_usages))
        lines.append(f"      {command.summary}")
    return "\n".join(lines).rstrip() + "\n"


def render_grammar_block(registry: Registry) -> str:
    """Render the complete managed grammar block embedded in ``SKILL.md``."""

    usages: list[str] = []
    for command in registry.commands:
        usages.extend((command.usage, *command.additional_usages))
    return (
        "<!-- flow:public-grammar:begin -->\n"
        "```text\n" + "\n".join(usages) + "\n```\n"
        "<!-- flow:public-grammar:end -->\n"
    )


def render_router_block(registry: Registry) -> str:
    """Render the compact router contract embedded in skill instructions."""

    roots = " | ".join(registry.static_namespaces)
    return (
        "<!-- flow:public-router:begin -->\n"
        "Interpret the invocation through `public-commands.toml`.\n"
        "Static namespaces win over target parsing.\n"
        f"Static roots: `{roots}`.\n"
        "Bare `FLOW` is the read-only cockpit; a recognized target enters the lifecycle reducer.\n"
        "Unknown tokens stop. Never reinterpret removed commands as ticket keys.\n"
        "<!-- flow:public-router:end -->\n"
    )


def render_trigger_description(registry: Registry) -> str:
    """Render shared trigger metadata without host-specific invocation syntax."""

    roots = ", ".join(registry.static_namespaces[:-1])
    return (
        "State-aware ticket-to-PR delivery and workspace operations. Use FLOW for a cockpit, "
        f"a ticket or PR target, or the {roots} namespaces."
    )


def generated_sections(registry: Registry) -> dict[str, str]:
    """Return every deterministic public-prose artifact for check-only generators."""

    return {
        "grammar": render_grammar_block(registry),
        "help": render_help(registry),
        "router": render_router_block(registry),
        "trigger_description": render_trigger_description(registry),
    }


def replace_generated_block(
    document: str,
    *,
    begin_marker: str,
    end_marker: str,
    rendered: str,
) -> str:
    """Return a document with one managed block replaced; never writes files."""

    begin = document.find(begin_marker)
    end = document.find(end_marker, begin + len(begin_marker)) if begin >= 0 else -1
    if begin < 0 or end < 0:
        raise RegistryError(f"managed markers not found: {begin_marker!r}, {end_marker!r}")
    end += len(end_marker)
    normalized = rendered.removesuffix("\n")
    return document[:begin] + normalized + document[end:]


def check_generated_block(
    document: str,
    *,
    begin_marker: str,
    end_marker: str,
    rendered: str,
) -> None:
    """Raise when a managed block differs from deterministic registry output."""

    expected = replace_generated_block(
        document,
        begin_marker=begin_marker,
        end_marker=end_marker,
        rendered=rendered,
    )
    if expected != document:
        raise GeneratedContentDrift(f"generated block beginning {begin_marker!r} is stale")


__all__ = [
    "DEFAULT_REGISTRY",
    "STATIC_NAMESPACES",
    "ArgumentSpec",
    "ClassifiedTarget",
    "CommandSpec",
    "GeneratedContentDrift",
    "OptionSpec",
    "Registry",
    "RegistryError",
    "Route",
    "TargetKind",
    "check_generated_block",
    "classify_root_token",
    "generated_sections",
    "load_registry",
    "render_grammar_block",
    "render_help",
    "render_router_block",
    "render_trigger_description",
    "replace_generated_block",
    "route_tokens",
    "tracker_key_patterns_from_workspace",
    "validate_tracker_key_patterns",
]
