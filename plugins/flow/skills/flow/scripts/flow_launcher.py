"""Install and repair the workspace-local ``.flow/runtime/flow`` facade."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import runtime_layout
from _atomicio import atomic_write_text
from bundle_discover import HarnessError, flow_harness

SKILL_ROOT = Path(__file__).resolve().parent.parent
_MIN_PYTHON = (3, 12)
_PYTHON_COMMANDS = ("python3", "python3.14", "python3.13", "python3.12")
_RUNTIME_PYTHON_TOKEN = "__FLOW_RUNTIME_PYTHON__"


class RuntimeCompatibilityError(OSError):
    """No supported Python runtime is available to the workspace facade."""


_SHIM = r'''#!/usr/bin/env python3
"""Generated Flow workspace launcher. Re-run Flow workspace setup to replace."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_RUNTIME_PYTHON = __FLOW_RUNTIME_PYTHON__


def _fail(message: str) -> int:
    sys.stderr.write(f"flow: {message}\n")
    return 1


def main() -> int:
    runtime_dir = Path(__file__).resolve().parent
    flow_dir = runtime_dir.parent
    workspace_root = flow_dir.parent.resolve()
    workspace_toml = flow_dir / "workspace.toml"
    if not workspace_toml.is_file():
        return _fail(
            f"workspace config is missing at {workspace_toml}; run Flow workspace setup"
        )

    version_file = runtime_dir / "layout-version"
    try:
        version = version_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        return _fail(f"cannot read {version_file}: {exc}; run Flow workspace setup")
    if version != "2":
        return _fail(f"unsupported runtime layout {version!r}; run Flow workspace setup")

    skill_file = runtime_dir / "skill-root"
    if not skill_file.is_file():
        return _fail(
            "this workspace has no .flow/runtime/skill-root; run Flow workspace setup"
        )
    try:
        # ``skill_dir`` is a one-line machine path. Remove only the record
        # terminator: spaces are legal POSIX path characters and must survive.
        raw_skill_dir = skill_file.read_text(encoding="utf-8").strip("\r\n")
    except (OSError, UnicodeError) as exc:
        return _fail(f"cannot read {skill_file}: {exc}; run Flow workspace setup")
    if not raw_skill_dir:
        return _fail(f"{skill_file} is empty; run Flow workspace setup")
    if "\x00" in raw_skill_dir:
        return _fail(
            f"{skill_file} contains an invalid path (NUL byte); run Flow workspace setup"
        )

    try:
        skill_dir = Path(raw_skill_dir).expanduser()
    except (OSError, RuntimeError, ValueError) as exc:
        return _fail(f"{skill_file} contains an invalid path: {exc}; run Flow workspace setup")
    if not skill_dir.is_absolute():
        return _fail(f"{skill_file} must contain an absolute path; run Flow workspace setup")
    if not skill_dir.is_dir():
        return _fail(
            f"Flow skill directory from {skill_file} does not exist: {skill_dir}; "
            "run Flow workspace setup"
        )

    flowctl = skill_dir / "scripts" / "flowctl.py"
    if not flowctl.is_file():
        return _fail(
            f"Flow installation is missing {flowctl}; update Flow and run Flow workspace setup"
        )

    runtime_python = Path(_RUNTIME_PYTHON)
    if not runtime_python.is_file():
        return _fail(
            f"Flow runtime Python is missing at {runtime_python}; rerun the Flow launcher"
        )

    os.environ["FLOW_SKILL_DIR"] = str(skill_dir)
    os.environ["CLAUDE_SKILL_DIR"] = str(skill_dir)
    os.execv(
        str(runtime_python),
        [str(runtime_python), str(flowctl), "--workspace-root", str(workspace_root), *sys.argv[1:]],
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _python_version(executable: str | Path) -> tuple[int, int] | None:
    try:
        result = subprocess.run(
            [
                str(executable),
                "-c",
                "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        major, minor = result.stdout.strip().split(".", 1)
        return int(major), int(minor)
    except ValueError:
        return None


def _runtime_compatibility_error() -> RuntimeCompatibilityError:
    harness = flow_harness()
    if harness == "codex":
        return RuntimeCompatibilityError(
            "Codex cannot initialize Flow: Python 3.12 or newer is required, and neither "
            "a compatible interpreter nor a usable uv runtime is available. Install uv or "
            "make python3.12+ available on Codex's PATH, then retry."
        )
    return RuntimeCompatibilityError(
        "Flow requires Python 3.12 or newer; install a compatible interpreter or uv, then retry."
    )


def resolve_runtime_python(runtime_dir: Path) -> Path:
    """Return a Python >=3.12 executable, provisioning one through uv when needed."""
    if sys.version_info[:2] >= _MIN_PYTHON:
        return Path(sys.executable).resolve()

    seen: set[str] = set()
    for command in _PYTHON_COMMANDS:
        candidate = shutil.which(command)
        if candidate is None:
            continue
        resolved = str(Path(candidate).resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        version = _python_version(resolved)
        if version is not None and version >= _MIN_PYTHON:
            return Path(resolved)

    uv = shutil.which("uv")
    if uv is None:
        raise _runtime_compatibility_error()
    cache_dir = runtime_dir / "uv-cache"
    try:
        result = subprocess.run(
            [
                uv,
                "--cache-dir",
                str(cache_dir),
                "run",
                "--no-project",
                "--python",
                ">=3.12",
                "--",
                "python",
                "-c",
                "import sys; print(sys.executable)",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _runtime_compatibility_error() from exc
    if result.returncode != 0:
        raise _runtime_compatibility_error()
    candidate = result.stdout.strip()
    version = _python_version(candidate)
    if not candidate or version is None or version < _MIN_PYTHON:
        raise _runtime_compatibility_error()
    return Path(candidate).resolve()


def _render_shim(runtime_python: Path) -> str:
    return _SHIM.replace(_RUNTIME_PYTHON_TOKEN, repr(str(runtime_python)))


def _plugin_source(manifest: Path, plugin: str) -> str | None:
    """Return one marketplace entry's local source path, if declared."""
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    entries = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("name") != plugin:
            continue
        source = entry.get("source")
        if isinstance(source, str) and source:
            return source
        if isinstance(source, dict):
            path = source.get("path")
            return path if isinstance(path, str) and path else None
    return None


def _codex_marketplace_roots(codex_home: Path, marketplace: str) -> list[Path]:
    """Known local roots for one Codex marketplace cache namespace."""
    roots: list[Path] = []
    try:
        config = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        config = {}
    marketplaces = config.get("marketplaces") if isinstance(config, dict) else None
    entry = marketplaces.get(marketplace) if isinstance(marketplaces, dict) else None
    source = entry.get("source") if isinstance(entry, dict) else None
    if isinstance(source, str) and source:
        configured = Path(source).expanduser()
        roots.append(configured if configured.is_absolute() else codex_home / configured)

    # Git marketplace snapshots use an internal checkout; local marketplaces
    # normally resolve through config.toml above. Keep both known CLI layouts.
    roots.extend(
        [
            codex_home / ".tmp" / "marketplaces" / marketplace,
            codex_home / "plugins" / "marketplaces" / marketplace,
        ]
    )

    # The implicit personal marketplace lives under ~/.agents, while its source
    # paths are relative to the user's home directory.
    personal_manifest = Path.home() / ".agents" / "plugins" / "marketplace.json"
    try:
        personal = json.loads(personal_manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        personal = None
    if isinstance(personal, dict) and personal.get("name") == marketplace:
        roots.append(Path.home())

    return roots


def stabilize_skill_dir(skill_dir: str) -> str:
    """Prefer this harness's stable marketplace source over a versioned cache."""
    harness = flow_harness()
    parts = Path(skill_dir).parts
    cache_index = next(
        (
            index
            for index in range(len(parts) - 1)
            if parts[index] == "plugins" and parts[index + 1] == "cache"
        ),
        None,
    )
    if cache_index is None or len(parts) < cache_index + 6:
        return skill_dir
    if harness == "generic":
        # Generic adapters have no native marketplace contract. Guessing from
        # another host's cache namespace can bind an uninstalled handler.
        return skill_dir

    base = Path(*parts[:cache_index])
    marketplace = parts[cache_index + 2]
    plugin = parts[cache_index + 3]
    suffix = parts[cache_index + 5 :]
    if harness == "claude-code":
        claude_root = base / "plugins" / "marketplaces" / marketplace
        claude_source = _plugin_source(claude_root / ".claude-plugin" / "marketplace.json", plugin)
        if claude_source is not None:
            candidate = claude_root / Path(claude_source) / Path(*suffix)
            if candidate.is_dir():
                return str(candidate)
        return skill_dir

    # `flow_harness()` closed-validates the selector, so the remaining adapter
    # is Codex. Never fall through from one native host's resolver to the other.
    for root in _codex_marketplace_roots(base, marketplace):
        source = _plugin_source(root / ".agents" / "plugins" / "marketplace.json", plugin)
        if source is None:
            continue
        candidate = root / Path(source) / Path(*suffix)
        if candidate.is_dir():
            return str(candidate)
    return skill_dir


def executing_skill_dir() -> Path:
    """Resolve the installation that is executing this installer."""
    return Path(stabilize_skill_dir(str(SKILL_ROOT))).expanduser().resolve()


def install(
    workspace_root: Path,
    *,
    skill_dir: Path | None = None,
    memory_base: Path | None = None,
) -> tuple[Path, Path]:
    """Migrate layout v2, then atomically install ``skill_dir`` and the shim."""
    root = workspace_root.expanduser().resolve()
    resolved_skill = Path(stabilize_skill_dir(str(skill_dir or executing_skill_dir()))).resolve()
    if not resolved_skill.is_dir():
        raise FileNotFoundError(f"Flow skill directory does not exist: {resolved_skill}")
    flowctl = resolved_skill / "scripts" / "flowctl.py"
    if not flowctl.is_file():
        raise FileNotFoundError(f"Flow installation is missing {flowctl}")
    runtime_python = resolve_runtime_python(root / ".flow" / "runtime")
    layout = runtime_layout.ensure_layout(root, memory_base=memory_base)
    skill_path = layout.skill_root_file
    shim_path = layout.launcher
    atomic_write_text(skill_path, str(resolved_skill) + "\n")
    atomic_write_text(shim_path, _render_shim(runtime_python), mode=0o755)
    return skill_path, shim_path


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install or repair the workspace-local Flow shim.")
    parser.add_argument("--workspace-root", required=True)
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    root = Path(args.workspace_root).expanduser().resolve()
    if not (root / ".flow" / "workspace.toml").is_file():
        sys.stderr.write(
            f"flow-launcher: no workspace.toml at {root / '.flow' / 'workspace.toml'}; "
            "run Flow workspace setup\n"
        )
        return 1
    try:
        install(root)
    except (OSError, HarnessError, runtime_layout.RuntimeLayoutError) as exc:
        sys.stderr.write(f"flow-launcher: install failed: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))
