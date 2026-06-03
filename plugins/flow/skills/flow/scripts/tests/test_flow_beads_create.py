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


def _dispatch_runner(
    list_items: list[dict] | None = None, create_id: str = "flow-new"
) -> tuple[Callable[..., subprocess.CompletedProcess[str]], Recorder]:
    """Fake runner answering `bd list` (dedup check) and `bd create` distinctly."""
    import json

    calls: Recorder = []

    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append((args, cwd))
        if len(args) >= 2 and args[1] == "list":
            return subprocess.CompletedProcess(args, 0, json.dumps(list_items or []), "")
        return subprocess.CompletedProcess(args, 0, json.dumps({"id": create_id}), "")

    return run, calls


def test_fingerprint_is_format_invariant():
    a = fbc.fingerprint("scripts/mise.toml: TY skips hooks")
    b = fbc.fingerprint("scripts-mise-toml-ty-skips-hooks")
    c = fbc.fingerprint("Scripts/Mise.toml   ty skips HOOKS")
    assert a == b == c  # wording/format variance collapses to one key
    assert len(a) == 12
    assert fbc.fingerprint("a-different-finding") != a


def test_dedup_new_creates_with_evid_label(tmp_path):
    repo = _marked_ws(tmp_path)
    run, calls = _dispatch_runner(list_items=[])
    key = fbc.create_bead(repo, "t", "b", dedup_key="quotepath-bug", labels=["evolve"], runner=run)
    assert key == "flow-new"
    evid = f"evid:{fbc.fingerprint('quotepath-bug')}"
    assert calls[0][0][:2] == ["bd", "list"]  # dedup check first
    assert evid in calls[0][0]
    create_args = calls[1][0]
    assert create_args[:2] == ["bd", "create"]
    stamped = create_args[create_args.index("--labels") + 1]
    assert evid in stamped and "evolve" in stamped


def test_dedup_existing_skips_create(tmp_path):
    repo = _marked_ws(tmp_path)
    run, calls = _dispatch_runner(list_items=[{"id": "flow-old"}])
    with pytest.raises(fbc.DuplicateBead) as ei:
        fbc.create_bead(repo, "t", "b", dedup_key="quotepath-bug", runner=run)
    assert ei.value.existing_key == "flow-old"
    assert len(calls) == 1  # only the list check; create never ran
