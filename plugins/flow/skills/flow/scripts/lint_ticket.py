"""HARD GATE: validate required ticket frontmatter fields per stage.

Library + thin CLI. Stdlib-only.

Reads `stage-registry.toml` for the stage's `required_fields` list, reads
ticket frontmatter via `ticket_frontmatter.read()`, asserts each required
field is present + non-empty.

"Present + non-empty" definitions:
  - missing key → violation
  - empty string `""` → violation
  - empty list `[]` → violation
  - bool / int present → ok (any value)

Required-field defaults baked into stage-registry.toml:
  - all stages implicitly require `ticket` and `status` (treated as universal
    by this helper; not declared per-stage in the registry).
  - implement.required_fields  = ["planned_files"]
  - commit.required_fields     = ["commit_type", "commit_summary"]
  - create_pr.required_fields  = ["pr_title"]

Exit codes:
  0 = continue
  1 = block (one violation per stderr line as `<key>: <reason>`)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import ticket_frontmatter
from _registry import registry_by_name

UNIVERSAL_REQUIRED: tuple[str, ...] = ("ticket", "status")


def _registry_path(workspace_root: Path) -> Path:
    return workspace_root / "stage-registry.toml"


def _default_registry_path() -> Path:
    return Path(__file__).resolve().parent.parent / "stage-registry.toml"


def _load_required_fields(registry_path: Path, stage_name: str) -> list[str]:
    # exists() guard first so a missing registry keeps this file's FileNotFoundError
    # wording; registry_by_name's read_text would raise a different message.
    if not registry_path.exists():
        raise FileNotFoundError(f"stage-registry.toml not found at {registry_path}")
    entries = registry_by_name(registry_path)
    if stage_name not in entries:
        raise ValueError(f"stage {stage_name!r} not in stage-registry.toml")
    return entries[stage_name].required_fields


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    if isinstance(value, list):
        return len(value) == 0
    return False


def validate(
    stage: str,
    ticket_path: Path,
    registry_path: Path,
) -> list[str]:
    """Return list of violation strings. Empty list = ok."""
    required = list(UNIVERSAL_REQUIRED) + _load_required_fields(registry_path, stage)
    fm = ticket_frontmatter.read(ticket_path)
    violations: list[str] = []
    for key in required:
        if key not in fm:
            violations.append(f"{key}: missing")
        elif _is_empty(fm[key]):
            violations.append(f"{key}: present but empty")
    return violations


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="HARD GATE: validate required ticket frontmatter fields per stage."
    )
    parser.add_argument("--stage", required=True, help="stage name (e.g. implement).")
    parser.add_argument("--ticket-path", required=True, help="path to ticket .md file.")
    parser.add_argument(
        "--workspace-root",
        default=None,
        help="workspace dir containing stage-registry.toml (overrides plugin default).",
    )
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    ticket_path = Path(args.ticket_path).resolve()
    if args.workspace_root is not None:
        registry_path = _registry_path(Path(args.workspace_root).resolve())
    else:
        registry_path = _default_registry_path()
    try:
        violations = validate(args.stage, ticket_path, registry_path)
    except (FileNotFoundError, ValueError) as exc:
        sys.stderr.write(f"lint-ticket: {exc}\n")
        return 1
    if not violations:
        return 0
    for v in violations:
        sys.stderr.write(v + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["UNIVERSAL_REQUIRED", "cli_main", "validate"]
