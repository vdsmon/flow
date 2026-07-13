"""Maintainer-mode detection for the self-evolution loop.

Maintainer mode means this run may file machinery-friction beads into flow's OWN
beads DB and run `FLOW maintain evolution`. Outside it (a normal user of the flow plugin),
machinery friction stays dormant and `FLOW maintain evolution` is disabled.

Two signals, by context:

1. Committed marker: `[maintainer] self_target = true` in the workspace's own
   `.flow/workspace.toml`. It travels with the repo, so the cloud clone and local
   dogfooding both see it without any machine-local config; the flow repo IS the
   target, so repo_root is the workspace itself. This is the primary signal.
2. Local pointer (optional): `[maintainer] repo_root = "<path>"` in
   `~/.flow/config.toml`, so a run in some OTHER repo can sling flow friction back
   to a local flow checkout. The pointed-at repo must itself carry the committed
   marker, so a stray path can never be mistaken for the flow repo.

Neither present -> user mode (returns None).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from _workspace import WorkspaceConfigError, load_workspace_toml


def _global_config_path() -> Path:
    return Path.home() / ".flow" / "config.toml"


def _self_target(config: dict[str, Any]) -> bool:
    section = config.get("maintainer")
    return isinstance(section, dict) and section.get("self_target") is True


def resolve_maintainer_repo(workspace_root: Path) -> Path | None:
    """Return the flow repo root if this run is in maintainer mode, else None.

    Pure file reads; no side effects. Safe to call on any workspace.
    """
    try:
        config = load_workspace_toml(workspace_root)
    except WorkspaceConfigError:
        config = {}
    if _self_target(config):
        return workspace_root.resolve()

    gpath = _global_config_path()
    if not gpath.exists():
        return None
    try:
        gconfig = tomllib.loads(gpath.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return None
    section = gconfig.get("maintainer")
    root = section.get("repo_root") if isinstance(section, dict) else None
    if not isinstance(root, str) or not root:
        return None
    pointed = Path(root).expanduser()
    # only trust the pointer if the target actually carries the committed marker
    try:
        if _self_target(load_workspace_toml(pointed)):
            return pointed.resolve()
    except WorkspaceConfigError:
        return None
    return None


def is_maintainer(workspace_root: Path) -> bool:
    return resolve_maintainer_repo(workspace_root) is not None


def cli_main(argv: list[str]) -> int:
    """Print the maintainer repo root (exit 0) or nothing (exit 1, user mode).

    The gate `FLOW maintain evolution` checks before running a cold audit.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Resolve maintainer mode for a workspace.")
    parser.add_argument("--workspace-root", required=True)
    args = parser.parse_args(argv)
    repo = resolve_maintainer_repo(Path(args.workspace_root))
    if repo is None:
        return 1
    print(repo)
    return 0


__all__ = ["cli_main", "is_maintainer", "resolve_maintainer_repo"]


if __name__ == "__main__":
    import sys

    sys.exit(cli_main(sys.argv[1:]))
