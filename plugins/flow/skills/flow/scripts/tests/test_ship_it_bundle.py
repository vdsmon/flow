"""Validates the ship-it .flow-bundle.toml against flow's bundle discovery.

Locks the cross-plugin wiring: ship-it's manifest must parse and provide create_pr
+ review_loop with skill handlers, so `/flow init --bundle recommended` auto-routes
the autonomous tail's PR delivery.
"""

from __future__ import annotations

from pathlib import Path

import bundle_discover

# tests/ -> scripts -> flow -> skills -> flow -> plugins -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[6]
_SHIP_IT = _REPO_ROOT / "plugins" / "ship-it"


def test_ship_it_manifest_exists() -> None:
    assert (_SHIP_IT / ".flow-bundle.toml").exists()


def test_ship_it_manifest_is_valid() -> None:
    result = bundle_discover.discover(roots=[_SHIP_IT])
    assert not result.invalid, [f"{e.path}: {e.reason}" for e in result.invalid]
    assert len(result.valid) == 1
    manifest = result.valid[0]
    assert manifest.bundle_name == "ship-it"
    by_stage = {s.stage: s.handler_string for s in manifest.skills}
    assert by_stage["create_pr"] == "skill:ship-it:create"
    assert by_stage["review_loop"] == "skill:ship-it:feedback"
