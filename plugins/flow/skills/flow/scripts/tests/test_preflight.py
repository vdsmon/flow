"""Tests for preflight.py: the [preflight] credential check/probe of workspace.toml."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import preflight

SCRIPT = Path(preflight.__file__).resolve()


def _workspace(tmp_path: Path, body: str) -> Path:
    (tmp_path / ".flow").mkdir(parents=True)
    (tmp_path / ".flow" / "workspace.toml").write_text(body, encoding="utf-8")
    return tmp_path


def _cli(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--workspace-root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


# ─── configured_command() ────────────────────────────────────────────────────


def test_unconfigured_when_no_preflight_block(tmp_path):
    root = _workspace(tmp_path, "[forge]\nbackend = 'github'\n")
    assert preflight.configured_command(root, "check") is None
    assert preflight.configured_command(root, "probe") is None


def test_command_string_is_shell_split(tmp_path):
    root = _workspace(tmp_path, "[preflight]\ncredential_probe = 'aws sts get-caller-identity'\n")
    assert preflight.configured_command(root, "probe") == ["aws", "sts", "get-caller-identity"]


def test_non_string_command_is_config_error(tmp_path):
    root = _workspace(tmp_path, "[preflight]\ncredential_probe = 123\n")
    with pytest.raises(preflight.PreflightConfigError):
        preflight.configured_command(root, "probe")


def test_empty_command_is_config_error(tmp_path):
    root = _workspace(tmp_path, "[preflight]\ncredential_check = '  '\n")
    with pytest.raises(preflight.PreflightConfigError):
        preflight.configured_command(root, "check")


def test_unparseable_toml_is_config_error(tmp_path):
    root = _workspace(tmp_path, "[preflight\n")
    with pytest.raises(preflight.PreflightConfigError):
        preflight.configured_command(root, "probe")


# ─── CLI: the commands actually run ──────────────────────────────────────────


def test_probe_ok_runs_the_command(tmp_path):
    marker = tmp_path / "ran"
    root = _workspace(tmp_path, f"[preflight]\ncredential_probe = 'touch {marker}'\n")
    proc = _cli(root, "probe")
    assert proc.returncode == 0
    assert marker.exists()
    record = json.loads(proc.stdout)
    assert record["status"] == "ok"
    assert record["exit_code"] == 0


def test_probe_failure_exits_2(tmp_path):
    root = _workspace(tmp_path, "[preflight]\ncredential_probe = 'false'\n")
    proc = _cli(root, "probe")
    assert proc.returncode == 2
    record = json.loads(proc.stdout)
    assert record["status"] == "failed"
    assert record["exit_code"] == 1


def test_probe_captures_command_output_keeping_json_clean(tmp_path):
    root = _workspace(tmp_path, "[preflight]\ncredential_probe = 'echo garbage'\n")
    proc = _cli(root, "probe")
    assert proc.returncode == 0
    # The probe's own stdout must be exactly one JSON line; the command's
    # output is captured, never interleaved (a stage agent parses this).
    assert proc.stdout.count("\n") == 1
    record = json.loads(proc.stdout)
    assert record["status"] == "ok"


def test_check_passes_command_output_through(tmp_path):
    root = _workspace(tmp_path, "[preflight]\ncredential_check = 'echo sso-still-valid'\n")
    proc = _cli(root, "check")
    assert proc.returncode == 0
    # Attended mode inherits stdio: the human sees the wrapper's own report
    # (or an SSO URL) live, then the JSON record follows.
    assert "sso-still-valid" in proc.stdout
    assert json.loads(proc.stdout.splitlines()[-1])["status"] == "ok"


def test_check_failure_exits_2(tmp_path):
    root = _workspace(tmp_path, "[preflight]\ncredential_check = 'false'\n")
    proc = _cli(root, "check")
    assert proc.returncode == 2


def test_unconfigured_cli_is_silent_success(tmp_path):
    root = _workspace(tmp_path, "[forge]\nbackend = 'github'\n")
    for mode in ("check", "probe"):
        proc = _cli(root, mode)
        assert proc.returncode == 0
        assert json.loads(proc.stdout)["status"] == "unconfigured"


def test_dry_run_reports_without_executing(tmp_path):
    marker = tmp_path / "ran"
    root = _workspace(tmp_path, f"[preflight]\ncredential_probe = 'touch {marker}'\n")
    proc = _cli(root, "probe", "--dry-run")
    assert proc.returncode == 0
    record = json.loads(proc.stdout)
    assert record["status"] == "would_run"
    assert record["command"] == ["touch", str(marker)]
    assert not marker.exists()


def test_config_error_exits_3(tmp_path):
    root = _workspace(tmp_path, "[preflight]\ncredential_probe = 123\n")
    proc = _cli(root, "probe")
    assert proc.returncode == 3
    assert "must be a string" in proc.stderr


def test_probe_timeout_exits_2(tmp_path, monkeypatch):
    root = _workspace(tmp_path, "[preflight]\ncredential_probe = 'sleep 5'\n")
    monkeypatch.setattr(preflight, "_PROBE_TIMEOUT_S", 1)
    record = preflight.run_preflight(root, "probe")
    assert record["status"] == "timeout"


def test_missing_binary_is_failed_not_crash(tmp_path):
    root = _workspace(tmp_path, "[preflight]\ncredential_probe = 'no-such-binary-xyzzy'\n")
    proc = _cli(root, "probe")
    assert proc.returncode == 2
    record = json.loads(proc.stdout)
    assert record["status"] == "failed"
    assert "error" in record
