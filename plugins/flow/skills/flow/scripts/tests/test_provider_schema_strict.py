"""Every cognitive-worker output schema must be OpenAI strict-structured-output
compliant: each object node lists every one of its properties in `required`. Codex's
`--output-schema` enforces this and returns a 400 on any omission (flow-9ce2), while
claude's `--json-schema` tolerates it. Optionality is expressed with nullable-union
types, never by leaving a property out of `required`.
"""

import cognitive_workers as cw

_PROFILES = [
    "planner",
    "plan_assessor",
    "code_reviewer",
    "diff_reviewer",
    "guard_reviewer",
    "review_brief_author",
    "reflector",
    "machinery_fixer",
    "e2e",
    "implementer",
    "review_fixer",
    "revision_fixer",
]


def _non_strict_object_nodes(node: object, path: str) -> list[tuple[str, list[str]]]:
    found: list[tuple[str, list[str]]] = []
    if isinstance(node, dict):
        props = node.get("properties")
        if node.get("type") == "object" and isinstance(props, dict):
            required = node.get("required")
            declared = set(required) if isinstance(required, list) else set()
            missing = sorted(str(key) for key in props if key not in declared)
            if missing:
                found.append((path, missing))
        for key, value in node.items():
            found.extend(_non_strict_object_nodes(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_non_strict_object_nodes(value, f"{path}[{index}]"))
    return found


def test_every_provider_schema_is_openai_strict() -> None:
    violations = {
        profile: nodes
        for profile in _PROFILES
        if (nodes := _non_strict_object_nodes(cw.provider_schema(profile), profile))
    }
    assert not violations, f"object nodes whose required omits properties: {violations}"
