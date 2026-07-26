"""Byte-identical regression lock for the single-sourced handler grammar.

The grammar (`inline | none | subagent:<type>`) was once defined three times:
init._legal_handler_string (lax), resolve_handler.resolve's prefix dispatch (lax),
and validate_workspace._HANDLER_RE (charset-strict). They were consolidated onto
_registry.parse_handler + _registry.HANDLER_RE. The consumers had drifted on edge
cases (charset, trailing colon), so this locks each surviving consumer's acceptance
exactly rather than converging them.

The `_old_*` functions below are the frozen implementations, copied verbatim; the
tests assert the live symbols still match them across a battery that hits every
drift class.

Two changes have landed since the consolidation, and the battery pins both:

- a subagent type may carry a single plugin-namespace colon (`flow:codex-reviewer`),
  the form a host uses for an agent shipped by a plugin. `_WIDENED` names every
  battery value that widening newly accepts.
- the `skill:<name>[:<args>]` form is RETIRED: no handler is an installed plugin any
  more, so resolve_handler is gone and both surviving consumers must now REJECT every
  `skill:` string. `_RETIRED` names those values, and the assertions below require
  rejection rather than merely dropping the rows — that is what keeps the form from
  creeping back in through either consumer.
"""

from __future__ import annotations

import re

import pytest

import init
from _registry import HANDLER_RE

# ── frozen implementations (the spec being preserved) ─────────────────────────

_OLD_HANDLER_RE = re.compile(
    r"^(inline|none|subagent:[A-Za-z0-9_-]+|skill:[A-Za-z0-9_.-]+(?::.+)?)$"
)


def _old_init_legal(value: str) -> bool:
    if value in ("inline", "none"):
        return True
    if value.startswith("subagent:") and len(value) > len("subagent:"):
        return True
    return value.startswith("skill:") and len(value) > len("skill:")


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
    # plugin-namespaced subagent types
    "subagent:flow:codex-reviewer",
    "subagent:flow:",
    "subagent:a:b:c",
]

# Values the plugin-namespace widening newly accepts under HANDLER_RE. Everything
# else in BATTERY must still match the frozen pre-widening acceptance exactly.
# `subagent:flow:` and `subagent:a:b:c` are deliberately absent: one colon, and an
# identifier on both sides of it.
_WIDENED = frozenset({"subagent:foo:bar", "subagent:flow:codex-reviewer"})

# Every battery value the retired `skill:` form covered. Both consumers must reject
# all of them, including the ones the frozen spec accepted.
_RETIRED = frozenset(value for value in BATTERY if value.startswith("skill:"))


@pytest.mark.parametrize("value", BATTERY, ids=[repr(v) for v in BATTERY])
def test_init_legal_handler_string_unchanged(value: str) -> None:
    if value in _RETIRED:
        assert not init._legal_handler_string(value), "skill: handlers are retired"
        return
    assert init._legal_handler_string(value) == _old_init_legal(value)


@pytest.mark.parametrize("value", BATTERY, ids=[repr(v) for v in BATTERY])
def test_validate_handler_re_unchanged(value: str) -> None:
    if value in _RETIRED:
        assert not HANDLER_RE.match(value), "skill: handlers are retired"
        return
    if value in _WIDENED:
        assert HANDLER_RE.match(value), "plugin-namespaced type must be accepted"
        assert not _old_validate_accept(value), "value is not actually a widening"
        return
    assert bool(HANDLER_RE.match(value)) == _old_validate_accept(value)


def test_retired_skill_form_has_no_accepting_consumer() -> None:
    # The two frozen specs accepted `skill:foo`; the live grammar must not. This is
    # the lock on the retirement itself, independent of the battery's parametrize.
    assert _old_init_legal("skill:foo")
    assert _old_validate_accept("skill:foo")
    assert not init._legal_handler_string("skill:foo")
    assert not HANDLER_RE.match("skill:foo")
