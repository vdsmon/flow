"""Harness identity: which host adapter this invocation is running under.

Library module (stdlib-only, no shebang, no PEP 723 inline deps). Deliberately
import-light — `flowctl.py` and `flow_launcher.py` are the workspace shims and pull
this in on every facade call, so it must never grow a dependency on the registry, a
config read, or the filesystem.

The vocabulary is closed to the two hosts Flow runs on. An unrecognized selector
fails loudly rather than guessing a host's install layout; the adapter supplies the
value call-locally (never an export), per SKILL.md's entry contract.
"""

from __future__ import annotations

import os

_FLOW_HARNESSES = ("codex", "claude-code")


class HarnessError(ValueError):
    """The adapter supplied an unsupported Flow harness name."""


def flow_harness() -> str:
    """Return the selected adapter. An unset, empty, or unknown selector fails loudly:
    the adapter supplies the value call-locally on every invocation (SKILL.md's entry
    contract), so a missing one is a caller defect, never a default.
    """
    harness = os.environ.get("FLOW_HARNESS") or ""
    if harness not in _FLOW_HARNESSES:
        allowed = ", ".join(_FLOW_HARNESSES)
        raise HarnessError(f"FLOW_HARNESS must be one of: {allowed}; got {harness!r}")
    return harness


__all__ = [
    "HarnessError",
    "flow_harness",
]
