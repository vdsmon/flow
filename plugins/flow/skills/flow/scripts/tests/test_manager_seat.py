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
    """A bare origin whose `main` and `dev` branches genuinely diverge, a seed clone to move origin
    from, and a workspace. `dev` branches off `main`'s first commit and gets its own commit, then
    `main` gets a further commit, so each remote branch holds a commit the other lacks. Both
    branches are pushed before the workspace clone, and `seed` ends on `main`: several existing
    tests assume the workspace clones at the current tip of `main` in sync (0/0) and depend on
    `seed` sitting on `main` for their own follow-up commits.
    """
    origin = tmp_path / "origin.git"
    _git(["init", "--bare", "-b", "main", str(origin)], tmp_path)
    seed = tmp_path / "seed"
    _git(["clone", str(origin), str(seed)], tmp_path)
    (seed / "README.md").write_text("seed\n")
    _git(["add", "."], seed)
    _git(["commit", "-m", "seed"], seed)
    _git(["push", "--quiet", "origin", "main"], seed)
    _git(["switch", "-c", "dev"], seed)
    (seed / "dev-only.md").write_text("dev-only\n")
    _git(["add", "."], seed)
    _git(["commit", "-m", "dev-only"], seed)
    _git(["push", "--quiet", "origin", "dev"], seed)
    _git(["switch", "main"], seed)
    (seed / "main-only.md").write_text("main-only\n")
    _git(["add", "."], seed)
    _git(["commit", "-m", "main-only"], seed)
    _git(["push", "--quiet", "origin", "main"], seed)
    workspace = tmp_path / "workspace"
    _git(["clone", str(origin), str(workspace)], tmp_path)
    return SimpleNamespace(origin=origin, seed=seed, workspace=workspace)


def _bench(repo: SimpleNamespace) -> Path:
    return repo.workspace / manager_seat.BENCH_RELPATH


def _write_run(
    worktree: Path,
    ticket: str,
    *,
    run_id: str = "run-1",
    status: str = "pending",
    revision: str | None = None,
) -> Path:
    run_dir = worktree / ".flow" / "runs" / ticket
    if revision is not None:
        run_dir = run_dir / "revisions" / revision
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "ticket": ticket,
        "run_id": run_id,
        "backend": "beads",
        "started_at": "2026-07-28T12:00:00Z",
        "stages": {"plan": {"status": status}},
    }
    (run_dir / "state.json").write_text(json.dumps(payload), encoding="utf-8")
    return run_dir


def test_creates_bench_detached_at_default(repo: SimpleNamespace) -> None:
    code, posture = manager_seat.seat(repo.workspace)
    assert code == 0
    assert posture["fetch"] == {"action": "fetched"}
    assert posture["default_branch"] == "origin/main"
    assert posture["integration_branch"] == "origin/main"
    assert posture["bench"]["action"] == "created"
    assert posture["bench"]["branch"] is None
    assert posture["bench"]["clean"] is True
    assert posture["bench"]["head"] == _git(["rev-parse", "origin/main"], repo.workspace)
    assert _bench(repo).is_dir()
    root = posture["workspace_root"]
    assert (root["branch"], root["clean"]) == ("main", True)
    assert (root["behind_integration"], root["ahead_integration"]) == (0, 0)


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
    assert posture["workspace_root"]["action"] == "fast_forwarded"
    assert posture["workspace_root"]["behind_integration"] == 0
    assert posture["workspace_root"]["ahead_integration"] == 0


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


def test_integration_branch_from_create_pr_base(repo: SimpleNamespace) -> None:
    (repo.workspace / ".flow").mkdir()
    (repo.workspace / ".flow" / "workspace.toml").write_text('[create_pr]\nbase = "dev"\n')
    _git(["switch", "-c", "dev", "origin/dev"], repo.workspace)
    code, posture = manager_seat.seat(repo.workspace)
    assert code == 0
    assert posture["default_branch"] == "origin/main"
    assert posture["integration_branch"] == "origin/dev"
    assert "integration_unresolved" not in posture
    root = posture["workspace_root"]
    # Not asserting `clean`: writing workspace.toml above adds an untracked file inside the tracked
    # tree, which correctly reads as dirty; branch and the integration deltas are what this test
    # pins.
    assert root["branch"] == "dev"
    assert (root["behind_integration"], root["ahead_integration"]) == (0, 0)
    # Same tree measured against the remote default reads non-zero on both sides: proof the two refs
    # actually differ, so a fix that left the old default-branch deltas in place would fail.
    counts = _git(
        ["rev-list", "--left-right", "--count", "origin/main...HEAD"], repo.workspace
    ).split()
    behind_main, ahead_main = (int(n) for n in counts)
    assert behind_main > 0
    assert ahead_main > 0
    assert posture["bench"]["action"] == "created"
    assert posture["bench"]["head"] == _git(["rev-parse", "origin/dev"], repo.workspace)
    assert posture["bench"]["head"] != _git(["rev-parse", "origin/main"], repo.workspace)

    # The other accepted spelling: `origin/origin/dev` misses, and the literal falls through to be
    # verified as `refs/remotes/origin/dev`.
    (repo.workspace / ".flow" / "workspace.toml").write_text('[create_pr]\nbase = "origin/dev"\n')
    code, posture = manager_seat.seat(repo.workspace)
    assert code == 0
    assert posture["integration_branch"] == "origin/dev"
    assert "integration_unresolved" not in posture


def test_bench_seating_reads_the_primary_checkouts_declared_base(repo: SimpleNamespace) -> None:
    # The delivery-workspace shape: `.flow/workspace.toml` is untracked, so a bench worktree carries
    # none of its own. Reading `[create_pr] base` from the invoking root rather than the primary
    # checkout would seat a bench-invoked manager on the remote default with nothing in the posture
    # saying so.
    manager_seat.seat(repo.workspace)
    (repo.workspace / ".flow").mkdir()
    (repo.workspace / ".flow" / "workspace.toml").write_text('[create_pr]\nbase = "dev"\n')
    bench = _bench(repo)
    assert bench.is_dir()
    assert not (bench / ".flow").exists()
    code, posture = manager_seat.seat(bench)
    assert code == 0
    assert Path(posture["target_root"]) == repo.workspace.resolve()
    assert posture["default_branch"] == "origin/main"
    assert posture["integration_branch"] == "origin/dev"
    assert "integration_unresolved" not in posture


def test_clean_detached_bench_is_reparked_when_no_local_run(repo: SimpleNamespace) -> None:
    code, posture = manager_seat.seat(repo.workspace)
    assert code == 0
    assert posture["bench"]["action"] == "created"
    bench_head_before = posture["bench"]["head"]

    (repo.workspace / ".flow").mkdir()
    (repo.workspace / ".flow" / "workspace.toml").write_text('[create_pr]\nbase = "dev"\n')
    code, posture = manager_seat.seat(repo.workspace)
    assert code == 0
    assert posture["integration_branch"] == "origin/dev"
    assert posture["bench"]["action"] == "present"
    assert posture["bench"]["head"] == bench_head_before
    assert posture["bench"]["ahead_integration"] > 0


def test_behind_only_bench_is_reparked_when_no_local_run(repo: SimpleNamespace) -> None:
    (repo.workspace / ".git" / "info" / "exclude").write_text(".claude/\n", encoding="utf-8")
    manager_seat.seat(repo.workspace)
    bench = _bench(repo)
    (repo.seed / "second.md").write_text("second\n")
    _git(["add", "."], repo.seed)
    _git(["commit", "-m", "second"], repo.seed)
    _git(["push", "--quiet", "origin", "main"], repo.seed)
    code, posture = manager_seat.seat(bench)
    assert code == 0
    assert posture["workspace_root"]["action"] == "fast_forwarded"
    assert posture["bench"]["action"] == "reparked"
    assert posture["bench"]["head"] == _git(["rev-parse", "origin/main"], repo.workspace)
    assert posture["bench"]["behind_integration"] == 0


def test_configured_integrations_are_named_without_adapter_construction(
    repo: SimpleNamespace,
) -> None:
    (repo.workspace / ".flow").mkdir()
    (repo.workspace / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "jira"\n[forge]\nbackend = "bitbucket"\n',
        encoding="utf-8",
    )
    code, posture = manager_seat.seat(repo.workspace)
    assert code == 0
    assert posture["integrations"] == {"tracker": "jira", "forge": "bitbucket"}


def test_local_runs_cover_base_revision_failure_stale_corruption_and_completed_omission(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_run(repo.workspace, "flow-open")
    _write_run(repo.workspace, "flow-failed", status="failed")
    stale = _write_run(repo.workspace, "flow-stale", revision="r1")
    corrupt = repo.workspace / ".flow" / "runs" / "flow-corrupt"
    corrupt.mkdir(parents=True)
    (corrupt / "state.json").write_text("{broken", encoding="utf-8")
    _write_run(repo.workspace, "flow-done", status="completed")

    real_classify = manager_seat.lease.classify

    def classify(path, *args, **kwargs):
        if path == stale:
            return {"state": "expired_foreign", "holder": {}}
        return real_classify(path, *args, **kwargs)

    monkeypatch.setattr(manager_seat.lease, "classify", classify)
    code, posture = manager_seat.seat(repo.workspace)
    assert code == 0
    by_ticket = {row["ticket"]: row for row in posture["local_runs"]}
    assert by_ticket["flow-open"]["status"] == "unfinished"
    assert by_ticket["flow-failed"]["status"] == "failed"
    assert by_ticket["flow-stale"]["status"] == "stale"
    assert by_ticket["flow-stale"]["revision"] == "r1"
    assert by_ticket["flow-corrupt"]["status"] == "corrupt"
    assert "flow-done" not in by_ticket


def test_contradictory_duplicate_run_evidence_is_marked(repo: SimpleNamespace) -> None:
    sibling = repo.workspace.parent / "sibling"
    _git(["worktree", "add", "--detach", str(sibling), "origin/main"], repo.workspace)
    _write_run(repo.workspace, "flow-dupe", run_id="run-a")
    _write_run(sibling, "flow-dupe", run_id="run-b", status="failed")
    code, posture = manager_seat.seat(repo.workspace)
    assert code == 0
    duplicates = [row for row in posture["local_runs"] if row["ticket"] == "flow-dupe"]
    assert len(duplicates) == 2
    assert all(row["contradictory"] is True for row in duplicates)


def test_local_run_prevents_primary_fast_forward(repo: SimpleNamespace) -> None:
    _write_run(repo.workspace, "flow-open")
    (repo.seed / "second.md").write_text("second\n")
    _git(["add", "."], repo.seed)
    _git(["commit", "-m", "second"], repo.seed)
    _git(["push", "--quiet", "origin", "main"], repo.seed)
    code, posture = manager_seat.seat(repo.workspace)
    assert code == 0
    assert "action" not in posture["workspace_root"]
    assert posture["workspace_root"]["behind_integration"] == 1


def test_non_idle_bench_prevents_primary_fast_forward(repo: SimpleNamespace) -> None:
    (repo.workspace / ".git" / "info" / "exclude").write_text(".claude/\n", encoding="utf-8")
    manager_seat.seat(repo.workspace)
    bench = _bench(repo)
    _git(["switch", "-c", "manager/inflight"], bench)
    (repo.seed / "second.md").write_text("second\n")
    _git(["add", "."], repo.seed)
    _git(["commit", "-m", "second"], repo.seed)
    _git(["push", "--quiet", "origin", "main"], repo.seed)
    code, posture = manager_seat.seat(repo.workspace)
    assert code == 0
    assert posture["bench"]["branch"] == "manager/inflight"
    assert "action" not in posture["workspace_root"]
    assert posture["workspace_root"]["behind_integration"] == 1


def test_unresolvable_configured_base_falls_back_and_says_so(repo: SimpleNamespace) -> None:
    # `origin/nodev` as a LOCAL branch and `main~1` as a revision expression both satisfy `git
    # rev-parse --verify origin/<base>` while naming no remote branch. `main~1` is the dangerous
    # one: accepted silently, it can never equal a branch name, so the manager's
    # branch-vs-integration comparison would report a divergence no rebase can clear.
    _git(["branch", "local-only"], repo.workspace)
    _git(["branch", "origin/nodev"], repo.workspace)
    (repo.workspace / ".flow").mkdir()
    workspace_toml = repo.workspace / ".flow" / "workspace.toml"
    for base in ("nope", "local-only", "nodev", "main~1"):
        workspace_toml.write_text(f'[create_pr]\nbase = "{base}"\n')
        code, posture = manager_seat.seat(repo.workspace)
        assert code == 0
        assert posture["integration_branch"] == "origin/main"
        assert base in posture["integration_unresolved"]
