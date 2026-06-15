"""Tests for revise_config.py — the [revise] block reader + plain-comment floor."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import revise_config


def _workspace(tmp_path: Path, body: str) -> Path:
    (tmp_path / ".flow").mkdir(parents=True)
    (tmp_path / ".flow" / "workspace.toml").write_text(body, encoding="utf-8")
    return tmp_path


def _threads() -> list[dict]:
    return [
        {"id": "t1", "severity": "minor", "resolved": False, "title": "plain comment"},
        {"id": "t2", "severity": "minor", "resolved": True, "title": "resolved minor"},
        {"id": "t3", "severity": "major", "resolved": False, "title": "change requested"},
        {"id": "t4", "severity": "critical", "resolved": False, "title": "crit"},
        {"id": "t5", "severity": "nit", "resolved": False, "title": "nit"},
    ]


# ─── plain_comment_severity() ────────────────────────────────────────────────


def test_default_when_no_revise_block(tmp_path):
    root = _workspace(tmp_path, "[forge]\nbackend = 'github'\n")
    assert revise_config.plain_comment_severity(root) == "minor"


def test_default_when_no_workspace_toml(tmp_path):
    assert revise_config.plain_comment_severity(tmp_path) == "minor"


def test_override_reads_configured_value(tmp_path):
    root = _workspace(tmp_path, "[revise]\nplain_comment_severity = 'major'\n")
    assert revise_config.plain_comment_severity(root) == "major"


def test_invalid_value_falls_back_to_default(tmp_path, capsys):
    root = _workspace(tmp_path, "[revise]\nplain_comment_severity = 'bogus'\n")
    assert revise_config.plain_comment_severity(root) == "minor"
    assert "bogus" in capsys.readouterr().err


# ─── apply_floor() ───────────────────────────────────────────────────────────


def test_apply_floor_minor_is_noop():
    threads = _threads()
    out = revise_config.apply_floor(threads, "minor")
    assert [t["severity"] for t in out] == [t["severity"] for t in threads]


def test_apply_floor_does_not_mutate_input():
    threads = _threads()
    revise_config.apply_floor(threads, "major")
    assert threads[0]["severity"] == "minor"


def test_apply_floor_major_bumps_only_unresolved_minor():
    out = {t["id"]: t["severity"] for t in revise_config.apply_floor(_threads(), "major")}
    assert out["t1"] == "major"  # unresolved minor → bumped
    assert out["t2"] == "minor"  # resolved minor → untouched
    assert out["t3"] == "major"  # already major
    assert out["t4"] == "critical"  # untouched
    assert out["t5"] == "nit"  # nit is not minor → untouched


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _run_cli(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "revise_config.py"), *argv],
        capture_output=True,
        text=True,
    )


def test_cli_severity_prints_default(tmp_path):
    root = _workspace(tmp_path, "[forge]\nbackend = 'github'\n")
    res = _run_cli(["severity", "--workspace-root", str(root)])
    assert res.returncode == 0
    assert json.loads(res.stdout) == {"plain_comment_severity": "minor"}


def test_cli_severity_prints_override(tmp_path):
    root = _workspace(tmp_path, "[revise]\nplain_comment_severity = 'major'\n")
    res = _run_cli(["severity", "--workspace-root", str(root)])
    assert res.returncode == 0
    assert json.loads(res.stdout) == {"plain_comment_severity": "major"}
