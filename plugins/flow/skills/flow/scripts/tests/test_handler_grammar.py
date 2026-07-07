"""Byte-identical regression lock for the single-sourced handler grammar.

The grammar (`inline | none | subagent:<type> | skill:<name>[:<args>]`) was
defined three times: init._legal_handler_string (lax), resolve_handler.resolve's
prefix dispatch (lax, rejects empty skill name), validate_workspace._HANDLER_RE
(charset-strict). They were consolidated onto _registry.parse_handler +
_registry.HANDLER_RE. The three consumers drifted on edge cases (charset, empty
skill name, trailing colon), so this locks each consumer's PRE-consolidation
acceptance exactly rather than converging them.

The `_old_*` functions below are the frozen pre-refactor implementations, copied
verbatim; the tests assert the live symbols still match them across a battery
that hits every drift class.
"""

from __future__ import annotations

import re

import pytest

import init
import resolve_handler
from _registry import HANDLER_RE

# ── frozen pre-consolidation implementations (the spec being preserved) ──────

_OLD_HANDLER_RE = re.compile(
    r"^(inline|none|subagent:[A-Za-z0-9_-]+|skill:[A-Za-z0-9_.-]+(?::.+)?)$"
)


def _old_init_legal(value: str) -> bool:
    if value in ("inline", "none"):
        return True
    if value.startswith("subagent:") and len(value) > len("subagent:"):
        return True
    return value.startswith("skill:") and len(value) > len("skill:")


def _old_resolve_accept(handler_string: str) -> tuple[bool, str | None]:
    """(accepted, error) mirroring resolve()'s grammar branch pre-consolidation."""
    if handler_string == "inline":
        return True, None
    if handler_string == "none":
        return True, None
    if handler_string.startswith("subagent:"):
        subagent_type = handler_string[len("subagent:") :]
        if not subagent_type:
            return False, f"empty subagent type in handler {handler_string!r}"
        return True, None
    if handler_string.startswith("skill:"):
        rest = handler_string[len("skill:") :]
        name = rest.split(":", 1)[0]
        if not name:
            return False, f"empty skill name in handler {handler_string!r}"
        return True, None
    return False, f"unrecognized handler string {handler_string!r}"


def _old_validate_accept(value: str) -> bool:
    return bool(_OLD_HANDLER_RE.match(value))


BATTERY = [
    # legal forms
    "inline",
    "none",
    "subagent:code-reviewer",
    "subagent:general-purpose",
    "skill:foo",
    "skill:foo:args",
    "skill:foo:a:b:c",
    "skill:my.skill.name",
    "skill:a:b:c",
    "skill:.",
    "subagent:a_b-c",
    "skill:a_b-c.d",
    "skill:foo:!@#$",
    # empty / bare
    "",
    "skill:",
    "subagent:",
    "inline ",
    " inline",
    "  ",
    ":inline",
    # unknown kinds / casing
    "INLINE",
    "Inline",
    "command:foo",
    "unknown",
    # whitespace inside (validate rejects, runtime path is lax)
    "subagent:foo bar",
    "skill:foo bar",
    "skill:foo bar:baz",
    "subagent: ",
    "skill: ",
    # weird colons / empty name / dotted subagent
    "skill::args",
    "skill::",
    "skill:foo:",
    "subagent:foo:bar",
    "subagent:.",
    "subagent:foo.bar",
]


@pytest.mark.parametrize("value", BATTERY, ids=[repr(v) for v in BATTERY])
def test_init_legal_handler_string_unchanged(value: str) -> None:
    assert init._legal_handler_string(value) == _old_init_legal(value)


@pytest.mark.parametrize("value", BATTERY, ids=[repr(v) for v in BATTERY])
def test_resolve_grammar_unchanged(value: str) -> None:
    old_accept, old_error = _old_resolve_accept(value)
    resolution = resolve_handler.resolve(value)
    new_accept = resolution.handler_type != "unknown"
    assert new_accept == old_accept
    if not old_accept:
        assert resolution.handler_type == "unknown"
        assert resolution.error == old_error


@pytest.mark.parametrize("value", BATTERY, ids=[repr(v) for v in BATTERY])
def test_validate_handler_re_unchanged(value: str) -> None:
    assert bool(HANDLER_RE.match(value)) == _old_validate_accept(value)
