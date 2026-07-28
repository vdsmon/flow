"""Real-git tests for manager_seat: the module is git plumbing, so fakes would prove nothing."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import manager_seat


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> SimpleNamespace:
    """A bare origin with one commit on main, a seed clone to move origin from, and a workspace."""
    origin = tmp_path / "origin.git"
    _git(["init", "--bare", "-b", "main", str(origin)], tmp_path)
    seed = tmp_path / "seed"
    _git(["clone", str(origin), str(seed)], tmp_path)
    (seed / "README.md").write_text("seed\n")
    _git(["add", "."], seed)
    _git(["commit", "-m", "seed"], seed)
    _git(["push", "--quiet", "origin", "main"], seed)
    workspace = tmp_path / "workspace"
    _git(["clone", str(origin), str(workspace)], tmp_path)
    return SimpleNamespace(origin=origin, seed=seed, workspace=workspace)


def _bench(repo: SimpleNamespace) -> Path:
    return repo.workspace / manager_seat.BENCH_RELPATH


def test_creates_bench_detached_at_default(repo: SimpleNamespace) -> None:
    code, posture = manager_seat.seat(repo.workspace)
    assert code == 0
    assert posture["fetch"] == {"action": "fetched"}
    assert posture["default_branch"] == "origin/main"
    assert posture["bench"]["action"] == "created"
    assert posture["bench"]["branch"] is None
    assert posture["bench"]["clean"] is True
    assert posture["bench"]["head"] == _git(["rev-parse", "origin/main"], repo.workspace)
    assert _bench(repo).is_dir()
    root = posture["workspace_root"]
    assert (root["branch"], root["clean"]) == ("main", True)
    assert (root["behind_default"], root["ahead_default"]) == (0, 0)


def test_present_bench_is_never_mutated(repo: SimpleNamespace) -> None:
    manager_seat.seat(repo.workspace)
    bench = _bench(repo)
    _git(["switch", "-c", "manager/inflight"], bench)
    (bench / "wip.txt").write_text("in flight\n")
    code, posture = manager_seat.seat(repo.workspace)
    assert code == 0
    assert posture["bench"]["action"] == "present"
    assert posture["bench"]["branch"] == "manager/inflight"
    assert posture["bench"]["clean"] is False
    assert (bench / "wip.txt").read_text() == "in flight\n"
    assert _git(["symbolic-ref", "--short", "HEAD"], bench) == "manager/inflight"


def test_dry_run_writes_nothing(repo: SimpleNamespace) -> None:
    # Move origin and unset origin/HEAD first, so a fetch or set-head that leaked through the
    # dry-run gate would be visible in the refs instead of vacuously matching an idle remote.
    before = _git(["rev-parse", "origin/main"], repo.workspace)
    (repo.seed / "second.md").write_text("second\n")
    _git(["add", "."], repo.seed)
    _git(["commit", "-m", "second"], repo.seed)
    _git(["push", "--quiet", "origin", "main"], repo.seed)
    _git(["symbolic-ref", "--delete", "refs/remotes/origin/HEAD"], repo.workspace)
    code, posture = manager_seat.seat(repo.workspace, dry_run=True)
    assert code == 0
    assert posture["dry_run"] is True
    assert posture["fetch"] == {"action": "would_fetch"}
    assert posture["default_branch"] is None
    assert posture["bench"]["action"] == "would_create"
    assert not _bench(repo).exists()
    assert not (repo.workspace / ".claude").exists()
    assert _git(["rev-parse", "origin/main"], repo.workspace) == before
    probe = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
        cwd=str(repo.workspace),
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode != 0


def test_live_seat_tracks_a_remote_default_rename(repo: SimpleNamespace) -> None:
    # A plain fetch never rewrites an existing origin/HEAD, so only the set-head sync keeps the
    # posture truthful after the remote renames its default branch.
    _git(["branch", "-m", "main", "master"], repo.origin)
    code, posture = manager_seat.seat(repo.workspace)
    assert code == 0
    assert posture["default_branch"] == "origin/master"
    assert posture["bench"]["action"] == "created"
    assert posture["bench"]["head"] == _git(["rev-parse", "origin/master"], repo.workspace)


def test_deleted_bench_with_stale_registration_fails_predictably(repo: SimpleNamespace) -> None:
    manager_seat.seat(repo.workspace)
    shutil.rmtree(_bench(repo))
    for dry_run in (True, False):
        code, posture = manager_seat.seat(repo.workspace, dry_run=dry_run)
        assert code == manager_seat.EXIT_ERROR
        assert posture["bench"]["action"] == "failed"
        assert "prune" in posture["bench"]["reason"]


def test_file_at_bench_path_is_unrecognized_in_both_modes(repo: SimpleNamespace) -> None:
    bench = _bench(repo)
    bench.parent.mkdir(parents=True)
    bench.write_text("not a worktree\n")
    for dry_run in (True, False):
        code, posture = manager_seat.seat(repo.workspace, dry_run=dry_run)
        assert code == manager_seat.EXIT_ERROR
        assert posture["bench"]["action"] == "unrecognized"


def test_fetch_surfaces_remote_movement(repo: SimpleNamespace) -> None:
    (repo.seed / "second.md").write_text("second\n")
    _git(["add", "."], repo.seed)
    _git(["commit", "-m", "second"], repo.seed)
    _git(["push", "--quiet", "origin", "main"], repo.seed)
    code, posture = manager_seat.seat(repo.workspace)
    assert code == 0
    assert posture["workspace_root"]["behind_default"] == 1
    assert posture["workspace_root"]["ahead_default"] == 0


def test_fetch_failure_still_emits_posture(repo: SimpleNamespace) -> None:
    _git(["remote", "set-url", "origin", str(repo.workspace / "missing.git")], repo.workspace)
    code, posture = manager_seat.seat(repo.workspace)
    assert code == manager_seat.EXIT_ERROR
    assert posture["fetch"]["action"] == "failed"
    # origin/HEAD and origin/main survive from the clone, so the bench still materializes.
    assert posture["bench"]["action"] == "created"


def test_invoked_from_bench_targets_primary_checkout(repo: SimpleNamespace) -> None:
    manager_seat.seat(repo.workspace)
    code, posture = manager_seat.seat(_bench(repo))
    assert code == 0
    assert Path(posture["target_root"]) == repo.workspace.resolve()
    assert posture["bench"]["action"] == "present"


def test_plain_directory_at_bench_path_is_unrecognized(repo: SimpleNamespace) -> None:
    _bench(repo).mkdir(parents=True)
    code, posture = manager_seat.seat(repo.workspace)
    assert code == manager_seat.EXIT_ERROR
    assert posture["bench"]["action"] == "unrecognized"


def test_cli_prints_posture_json(repo: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    code = manager_seat.cli_main(["--workspace-root", str(repo.workspace)])
    assert code == 0
    posture = json.loads(capsys.readouterr().out)
    assert posture["bench"]["action"] == "created"


def test_non_repo_workspace_is_a_probe_error(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    code = manager_seat.cli_main(["--workspace-root", str(plain)])
    assert code == manager_seat.EXIT_ERROR
