"""Contract tests for _harness.flow_harness (the closed host-adapter vocabulary).

The selector decides nothing else on its own: it is read on every facade call, so the
only behavior to pin is the unset default, acceptance of the two real hosts, and loud
rejection of anything else.
"""

from __future__ import annotations

import pytest

import _harness


def test_unset_harness_defaults_to_claude_code(monkeypatch: pytest.MonkeyPatch) -> None:
    # Legacy behavior: before Flow exposed adapters, lookup assumed Claude Code.
    monkeypatch.delenv("FLOW_HARNESS", raising=False)
    assert _harness.flow_harness() == "claude-code"


def test_empty_harness_reads_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLOW_HARNESS", "")
    assert _harness.flow_harness() == "claude-code"


@pytest.mark.parametrize("value", ["codex", "claude-code"])
def test_both_real_hosts_are_accepted(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLOW_HARNESS", value)
    assert _harness.flow_harness() == value


def test_retired_generic_harness_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # `generic` was a third identity that supplied no host behavior. It now fails
    # like any other unknown selector rather than resolving to nothing.
    monkeypatch.setenv("FLOW_HARNESS", "generic")
    with pytest.raises(_harness.HarnessError, match=r"FLOW_HARNESS.*codex.*claude-code"):
        _harness.flow_harness()


def test_unknown_harness_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLOW_HARNESS", "mystery-host")
    with pytest.raises(ValueError, match=r"FLOW_HARNESS.*codex.*claude-code"):
        _harness.flow_harness()
