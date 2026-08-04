"""Credential preflight: the `[preflight]` block of workspace.toml.

Library + thin CLI. Stdlib-only.

Two knobs, both command strings, both optional (an absent block or key means
"unconfigured" and exits 0 so workspaces without expiring credentials pay nothing):

  credential_check  attended refresh-or-verify, run at delivery-plan section 1 while
                    the human is present; it MAY go interactive (a check-then-login
                    wrapper like brinta's `mise sso` verifies in ~1s when valid and
                    opens the SSO browser flow only on expiry), so `check` inherits
                    stdio and carries a generous timeout.
  credential_probe  silent liveness read (e.g. `aws sts get-caller-identity`), run by
                    the e2e stage before its recipe; it must NEVER go interactive, so
                    `probe` captures output and closes stdin. A stage agent that sees
                    exit 2 stops and reports the expired credential; the refusal to
                    launch a login from a stage is the point (FT-1560's e2e agent
                    blocked 122s inside `aws sso login`).

Scope is deliberately credentials-only (human ruling 2026-08-04): no binary or
daemon checks, no auto-install, no generic probe framework. Missing binaries are
one-time-per-machine papercuts; expiring credentials recur every ~12 hours.

Exit codes:
  0 = ok, unconfigured, or --dry-run
  2 = the configured command failed or timed out (credentials not usable)
  3 = config error (unparseable workspace.toml, non-string or empty command)
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

import _workspace

# check may carry a browser round-trip on expiry; probe is a bounded network read.
_CHECK_TIMEOUT_S = 600
_PROBE_TIMEOUT_S = 60

_KEY_BY_MODE = {"check": "credential_check", "probe": "credential_probe"}


class PreflightConfigError(ValueError):
    """The `[preflight]` block exists but its value cannot be used."""


def configured_command(workspace_root: Path, mode: str) -> list[str] | None:
    """Return the argv for `mode` (`check` | `probe`), or None when unconfigured.

    Raises PreflightConfigError on an unparseable workspace.toml, a non-table
    `preflight` value, a non-string command, or a command that parses to nothing.
    """
    key = _KEY_BY_MODE[mode]
    try:
        block = _workspace.load_workspace_toml(workspace_root).get("preflight", {})
    except _workspace.WorkspaceConfigError as exc:
        raise PreflightConfigError(str(exc)) from exc
    if not isinstance(block, dict):
        raise PreflightConfigError(f"[preflight] must be a table, got {type(block).__name__}")
    value = block.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise PreflightConfigError(
            f"[preflight] {key} must be a string, got {type(value).__name__}"
        )
    argv = shlex.split(value)
    if not argv:
        raise PreflightConfigError(f"[preflight] {key} is empty")
    return argv


def run_preflight(workspace_root: Path, mode: str, dry_run: bool = False) -> dict[str, object]:
    """Execute the configured command for `mode` and return its result record."""
    argv = configured_command(workspace_root, mode)
    if argv is None:
        return {"status": "unconfigured", "mode": mode}
    if dry_run:
        return {"status": "would_run", "mode": mode, "command": argv}
    started = time.monotonic()
    timeout = _CHECK_TIMEOUT_S if mode == "check" else _PROBE_TIMEOUT_S
    try:
        if mode == "check":
            # Attended: inherit stdio so an SSO URL/code prompt stays visible live.
            proc = subprocess.run(argv, cwd=str(workspace_root), timeout=timeout, check=False)
        else:
            proc = subprocess.run(
                argv,
                cwd=str(workspace_root),
                timeout=timeout,
                check=False,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )
        exit_code = proc.returncode
        status = "ok" if exit_code == 0 else "failed"
    except subprocess.TimeoutExpired:
        exit_code = None
        status = "timeout"
    except OSError as exc:
        return {
            "status": "failed",
            "mode": mode,
            "command": argv,
            "error": str(exc),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    return {
        "status": status,
        "mode": mode,
        "command": argv,
        "exit_code": exit_code,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the [preflight] credential commands of workspace.toml."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser(
        "check", help="attended credential check; may go interactive (plan time only)"
    )
    probe = sub.add_parser("probe", help="silent credential probe; never interactive (stage-side)")
    for p in (check, probe):
        p.add_argument("--workspace-root", default=".")
        p.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        record = run_preflight(Path(args.workspace_root).resolve(), args.cmd, dry_run=args.dry_run)
    except PreflightConfigError as exc:
        sys.stderr.write(f"preflight: {exc}\n")
        return 3
    sys.stdout.write(json.dumps(record) + "\n")
    return 0 if record["status"] in ("ok", "unconfigured", "would_run") else 2


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["PreflightConfigError", "cli_main", "configured_command", "run_preflight"]
