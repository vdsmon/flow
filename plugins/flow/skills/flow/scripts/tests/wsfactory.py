"""Workspace-scaffold factory for the test suite.

The suite is full of `.flow/workspace.toml` seeders that were copied file to file
until dozens of them were byte-identical. This module holds the one builder they
all delegate to; each test file keeps its own helper name and signature so test
bodies stay untouched. Run-state seeders that were copied the same way
(`write_lease`) live here too.

Defaults are deliberately minimal: a block emits only what the caller asked for.
Emitting a helpful extra (a tracker subtable nobody requested, a `[maintainer]`
marker) would satisfy a config check that a violation-branch test needs to fail,
turning that test green for the wrong reason.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import lease
from _timeutil import utcnow_iso

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
    # Stamp layout v2 with the memory base aimed at `.flow` itself (a legitimate
    # absolute base), so fixture paths like `.flow/<namespace>/...` resolve
    # through the one real v2 pointer.
    runtime = flow / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    version_file = runtime / "layout-version"
    if not version_file.exists():
        version_file.write_text("2\n", encoding="utf-8")
        (runtime / "memory-root").write_text(str(flow.resolve()) + "\n", encoding="utf-8")
    if namespace_dir is not None:
        (flow / namespace_dir).mkdir(parents=True, exist_ok=True)
    if initialized:
        (flow / ".initialized").write_text("", encoding="utf-8")
    text = body if body is not None else render_toml(*blocks)
    (flow / "workspace.toml").write_text(text, encoding="utf-8")
    return root


def stamp_layout_v2(flow: Path, memory_base: Path | None = None) -> None:
    """Stamp a hand-rolled `.flow` dir as layout v2 pointing at `memory_base` (default: `.flow`)."""
    runtime = flow / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "layout-version").write_text("2\n", encoding="utf-8")
    (runtime / "memory-root").write_text(
        str((memory_base or flow).resolve()) + "\n", encoding="utf-8"
    )


def write_lease(run_dir: Path, *, expired: bool = False) -> None:
    """Acquire a real lease in run_dir (live by default, expired on request)."""
    now = "2020-01-01T00:00:00Z" if expired else utcnow_iso()
    ttl = 1 if expired else 3600
    lease.acquire(
        run_dir,
        "run-test",
        ttl,
        now,
        stage="implement",
        current_boot="boot-A",
        hostname="host-1",
        cwd=str(run_dir),
    )


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
