from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

import flow_beads_create as fbc

Recorder = list[tuple[list[str], Path]]


def _marked_ws(tmp_path: Path) -> Path:
    d = tmp_path / "flow"
    (d / ".flow").mkdir(parents=True)
    (d / ".flow" / "workspace.toml").write_text(
        "[maintainer]\nself_target = true\n", encoding="utf-8"
    )
    return d


def _plain_ws(tmp_path: Path) -> Path:
    d = tmp_path / "proj"
    (d / ".flow").mkdir(parents=True)
    (d / ".flow" / "workspace.toml").write_text('[tracker]\nbackend = "beads"\n', encoding="utf-8")
    return d


def _runner(
    returncode: int = 0, stdout: str = '{"id": "flow-x1"}', stderr: str = ""
) -> tuple[Callable[..., subprocess.CompletedProcess[str]], Recorder]:
    calls: Recorder = []

    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append((args, cwd))
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    return run, calls


def test_create_bead_targets_flow_beads(tmp_path):
    repo = _marked_ws(tmp_path)
    run, calls = _runner()
    key = fbc.create_bead(
        repo,
        "title",
        "body",
        type="bug",
        labels=["evolve", "machinery"],
        parent="flow-aut",
        runner=run,
    )
    assert key == "flow-x1"
    args, cwd = calls[0]
    assert cwd == repo.resolve()  # bd runs in the flow repo, not the run's cwd
    assert args[:3] == ["bd", "create", "title"]
    assert "--json" in args
    assert args[args.index("--type") + 1] == "bug"
    assert args[args.index("--labels") + 1] == "evolve,machinery"
    assert args[args.index("--parent") + 1] == "flow-aut"


def test_create_bead_not_maintainer_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("maintainer._global_config_path", lambda: tmp_path / "absent.toml")
    plain = _plain_ws(tmp_path)
    run, calls = _runner()
    with pytest.raises(fbc.NotMaintainer):
        fbc.create_bead(plain, "t", "b", runner=run)
    assert calls == []  # bd never invoked


def test_create_bead_bd_error(tmp_path):
    repo = _marked_ws(tmp_path)
    run, _ = _runner(returncode=1, stderr="boom")
    with pytest.raises(fbc.BeadCreateError):
        fbc.create_bead(repo, "t", "b", runner=run)


def test_create_bead_no_id_does_not_retry(tmp_path):
    repo = _marked_ws(tmp_path)
    run, calls = _runner(stdout="{}")
    with pytest.raises(fbc.BeadCreateError):
        fbc.create_bead(repo, "t", "b", runner=run)
    assert len(calls) == 1  # no duplicate create on a parse miss


def test_cli_not_maintainer_exit_4(tmp_path, monkeypatch):
    monkeypatch.setattr("maintainer._global_config_path", lambda: tmp_path / "absent.toml")
    plain = _plain_ws(tmp_path)
    rc = fbc.cli_main(["--workspace-root", str(plain), "--summary", "t", "--description", "b"])
    assert rc == 4
