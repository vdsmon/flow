"""Workspace-scaffold factory for the test suite.

The suite is full of `.flow/workspace.toml` seeders that were copied file to file
until dozens of them were byte-identical. This module holds the one builder they
all delegate to; each test file keeps its own helper name and signature so test
bodies stay untouched.

Defaults are deliberately minimal: a block emits only what the caller asked for.
Emitting a helpful extra (a tracker subtable nobody requested, a `[maintainer]`
marker) would satisfy a config check that a violation-branch test needs to fail,
turning that test green for the wrong reason.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

# One or more TOML sections. Keys are section paths, so a subtable is spelled
# out flat: {"tracker": {...}, "tracker.jira": {...}}. Section order is
# insertion order, which is what render_toml writes.
Block = Mapping[str, Mapping[str, object]]

MAINTAINER: Block = {"maintainer": {"self_target": True}}

_SUBTABLE_DEFAULTS: dict[str, dict[str, object]] = {
    "jira": {"cloud_id": "x", "project_key": "FT"},
    "beads": {"prefix": "bd"},
}


def tracker(backend: str | None = "jira", *, subtable: bool = True, **keys: object) -> Block:
    """A `[tracker]` section, by default with the matching backend subtable.

    `keys` override and extend the subtable's defaults, or land in `[tracker]`
    itself when there is no subtable. `backend=None` emits a bare header.
    """
    section: dict[str, object] = {} if backend is None else {"backend": backend}
    block: dict[str, dict[str, object]] = {"tracker": section}
    if backend is not None and subtable:
        block[f"tracker.{backend}"] = {**_SUBTABLE_DEFAULTS[backend], **keys}
    else:
        section.update(keys)
    return block


def memory(namespace: str = "demo", **keys: object) -> Block:
    """A `[memory]` section."""
    return {"memory": {"namespace": namespace, **keys}}


def render_toml(*blocks: Block) -> str:
    """Merge blocks left to right (later keys win) and render them as TOML."""
    merged: dict[str, dict[str, object]] = {}
    for block in blocks:
        for name, keys in block.items():
            merged.setdefault(name, {}).update(keys)
    return "\n".join(
        f"[{name}]\n" + "".join(f"{key} = {_fmt(value)}\n" for key, value in keys.items())
        for name, keys in merged.items()
    )


def make_workspace(
    root: Path,
    *blocks: Block,
    initialized: bool = False,
    namespace_dir: str | None = None,
    body: str | None = None,
) -> Path:
    """Write `<root>/.flow/workspace.toml` from `blocks`, and return `root`.

    `body` writes verbatim text instead, for the callers that assert on a
    hand-written config. `namespace_dir` is a path relative to `.flow/`.
    """
    if body is not None and blocks:
        raise ValueError("pass either blocks or body, not both")
    flow = root / ".flow"
    flow.mkdir(parents=True, exist_ok=True)
    if namespace_dir is not None:
        (flow / namespace_dir).mkdir(parents=True, exist_ok=True)
    if initialized:
        (flow / ".initialized").write_text("", encoding="utf-8")
    text = body if body is not None else render_toml(*blocks)
    (flow / "workspace.toml").write_text(text, encoding="utf-8")
    return root


def _fmt(value: object) -> str:
    if isinstance(value, bool):  # before int: bool is an int subclass
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_fmt(item) for item in value) + "]"
    raise TypeError(f"no TOML rendering for {type(value).__name__}: {value!r}")
