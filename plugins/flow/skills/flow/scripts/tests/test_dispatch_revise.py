"""Contract tests for the revision-run lifecycle seam (flow-kx17.2 keystone).

revise-open opens a SUB-RUN under a terminal ticket run: own lease/state/snapshot
at runs/<ticket>/revisions/<id>/, the original terminal run NEVER mutated. The
next/advance/finish/status/release `--revision` redirect drives the sub-run. git
rev-parse HEAD is stubbed via monkeypatch.setattr(subprocess, "run", ...).
"""

from __future__ import annotations

import multiprocessing
import subprocess
from pathlib import Path
from typing import Any

import pytest

import dispatch_stage as ds
import lease
import state


def _write_workspace(
    root: Path,
    *,
    stages: list[str] | None = None,
    backend: str = "jira",
) -> None:
    if stages is None:
        stages = ["ticket", "plan", "implement", "commit", "reflect"]
    flow = root / ".flow"
    flow.mkdir()
    (flow / ".initialized").touch()
    lines: list[str] = ["[tracker]", f'backend = "{backend}"']
    if backend == "jira":
        lines += ["[tracker.jira]", 'cloud_id = "x"', 'project_key = "FT"']
    else:
        lines += ["[tracker.beads]", 'prefix = "testpkg"']
    lines += [
        "[pipeline]",
        "stages = [" + ", ".join(f'"{s}"' for s in stages) + "]",
        "[pipeline.handlers]",
        *[f'{s} = "inline"' for s in stages],
        "[memory]",
        'namespace = "FT"',
        "auto_recall = true",
        "compounding = false",
        'recall_by = ["branch"]',
        "recall_top_n = 5",
    ]
    (flow / "workspace.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _stub_git_head(monkeypatch: pytest.MonkeyPatch, sha: str = "deadbeef") -> None:
    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=sha + "\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)


def _drive_to_terminal(root: Path, ticket: str, stages: list[str]) -> str:
    """Init + run every stage to completed, returns the session_nonce."""
    rc, payload = ds.cmd_init(root, ticket)
    assert rc == 0
    nonce = payload["session_nonce"]
    for _stage in stages:
        rc, nxt = ds.cmd_next(root, ticket, nonce)
        assert rc == 0
        if nxt.get("done"):
            break
        rc, _ = ds.cmd_finish(root, ticket, nxt["stage"], "completed", session_nonce=nonce)
        assert rc == 0
    rc, done = ds.cmd_next(root, ticket, nonce)
    assert done.get("done") is True
    return nonce


# ─── run_dir ─────────────────────────────────────────────────────────────────


def test_run_dir_backward_compat(tmp_path: Path) -> None:
    assert ds.run_dir(tmp_path, "FT-1", None) == ds._ticket_dir(tmp_path, "FT-1")
    assert ds.run_dir(tmp_path, "FT-1", "r1") == (
        tmp_path / ".flow" / "runs" / "FT-1" / "revisions" / "r1"
    )


# ─── revise-open ─────────────────────────────────────────────────────────────


def test_revise_open_leaves_original_state_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stages = ["ticket", "plan"]
    _write_workspace(tmp_path, stages=stages)
    _stub_git_head(monkeypatch)
    _drive_to_terminal(tmp_path, "FT-1", stages)

    import snapshot

    orig_state = tmp_path / ".flow" / "runs" / "FT-1" / "state.json"
    before = orig_state.read_bytes()
    orig_sha = snapshot.snapshot_sha_path(tmp_path, "FT-1")
    sha_before = orig_sha.read_bytes() if orig_sha.exists() else None

    rc, payload = ds.cmd_revise_open(tmp_path, "FT-1")
    assert rc == 0
    assert payload["rev_id"] == "r1"
    # byte-identical original state AND ticket-level snapshot (revise-open writes the
    # revision's snapshot nested, never rewrites the original's terminal baseline)
    assert orig_state.read_bytes() == before
    assert (orig_sha.read_bytes() if orig_sha.exists() else None) == sha_before


def test_revise_open_independent_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stages = ["ticket", "plan"]
    _write_workspace(tmp_path, stages=stages)
    _stub_git_head(monkeypatch)
    _drive_to_terminal(tmp_path, "FT-1", stages)

    # original lease was released at clean finish
    orig_lock = lease.run_lock_path(tmp_path / ".flow" / "runs" / "FT-1")
    assert not orig_lock.exists()

    rc, payload = ds.cmd_revise_open(tmp_path, "FT-1")
    assert rc == 0
    rev_dir = Path(payload["revision_dir"])
    rev_lock = lease.run_lock_path(rev_dir)
    assert rev_lock.exists()
    assert rev_lock != orig_lock
    # original lease still absent (untouched)
    assert not orig_lock.exists()


def test_revise_open_refuses_non_terminal_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stages = ["ticket", "plan"]
    _write_workspace(tmp_path, stages=stages)
    _stub_git_head(monkeypatch)
    rc, _ = ds.cmd_init(tmp_path, "FT-1")  # all-pending, not terminal
    assert rc == 0

    rc, out = ds.cmd_revise_open(tmp_path, "FT-1")
    assert rc == 3
    assert "not terminal" in out["error"]


def test_revise_open_refuses_no_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(tmp_path, stages=["ticket", "plan"])
    _stub_git_head(monkeypatch)
    rc, _ = ds.cmd_revise_open(tmp_path, "FT-1")
    assert rc == 2


def test_revise_open_refuses_concurrent_live_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stages = ["ticket", "plan"]
    _write_workspace(tmp_path, stages=stages)
    _stub_git_head(monkeypatch)
    _drive_to_terminal(tmp_path, "FT-1", stages)

    rc, _ = ds.cmd_revise_open(tmp_path, "FT-1")
    assert rc == 0
    # r1 holds a live lease (revise-open acquired it and never released)
    rc, second = ds.cmd_revise_open(tmp_path, "FT-1")
    assert rc == 4
    assert "already live" in second["error"]


def test_revise_open_proceeds_past_expired_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # an expired (dead) revision lease must not block a fresh revision.
    stages = ["ticket", "plan"]
    _write_workspace(tmp_path, stages=stages)
    _stub_git_head(monkeypatch)
    _drive_to_terminal(tmp_path, "FT-1", stages)

    rc, first = ds.cmd_revise_open(tmp_path, "FT-1")
    assert rc == 0
    # expire r1's lease by hand
    rev_dir = Path(first["revision_dir"])
    lease.release(rev_dir, first["run_id"], first["session_nonce"])

    rc, second = ds.cmd_revise_open(tmp_path, "FT-1")
    assert rc == 0
    assert second["rev_id"] == "r2"


def test_revise_open_seeds_default_subset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # ws.stages orders review_loop BEFORE reflect (the registry-legal order), which
    # differs from the default tuple order (reflect, review_loop); the seeded subset
    # is the default set filtered through ws.stages, preserving ws.stages order.
    stages = ["ticket", "plan", "implement", "code_review", "commit", "review_loop", "reflect"]
    _write_workspace(tmp_path, stages=stages)
    _stub_git_head(monkeypatch)
    _drive_to_terminal(tmp_path, "FT-1", stages)

    rc, payload = ds.cmd_revise_open(tmp_path, "FT-1")
    assert rc == 0
    # default ∩ ws.stages, in ws.stages order (review_loop before reflect, NOT the
    # default tuple's reflect-before-review_loop):
    expected = ["implement", "code_review", "commit", "review_loop", "reflect"]
    assert payload["stages"] == expected
    # state.json serializes stages sort_keys=True, so the seeded dict order isn't
    # load-bearing; the payload list above is the ordered contract. Assert the set.
    seeded, _ = state.read(Path(payload["revision_dir"]))
    assert seeded is not None
    assert set(seeded.stages.keys()) == set(expected)


def test_revise_open_stages_override_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stages = ["ticket", "plan", "implement", "commit", "reflect"]
    _write_workspace(tmp_path, stages=stages)
    _stub_git_head(monkeypatch)
    _drive_to_terminal(tmp_path, "FT-1", stages)

    rc, payload = ds.cmd_revise_open(tmp_path, "FT-1", stages=["implement", "commit"])
    assert rc == 0
    assert payload["stages"] == ["implement", "commit"]


def test_revise_open_rejects_off_pipeline_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # an explicit --stages naming a stage outside ws.stages would seed a subset
    # cmd_next can never visit (pick_next_pending iterates ws.stages), so the
    # revision would report done:true with everything still pending. Refuse,
    # naming the offenders, and seed nothing.
    stages = ["ticket", "plan", "implement", "commit", "reflect"]
    _write_workspace(tmp_path, stages=stages)
    _stub_git_head(monkeypatch)
    _drive_to_terminal(tmp_path, "FT-1", stages)

    rc, payload = ds.cmd_revise_open(tmp_path, "FT-1", stages=["impelment", "commit"])
    assert rc == 1
    assert "impelment" in payload["error"]
    assert not (tmp_path / ".flow" / "runs" / "FT-1" / "revisions").exists()

    # a valid subset still opens after the refusal (no claim side effects).
    rc, payload = ds.cmd_revise_open(tmp_path, "FT-1", stages=["implement", "commit"])
    assert rc == 0
    assert payload["stages"] == ["implement", "commit"]


# ─── concurrency: the revise.claim flock serializes two opens ────────────────


def _revise_open_subprocess(root: str, ticket: str, q: Any) -> None:
    import dispatch_stage as ds2

    rc, payload = ds2.cmd_revise_open(Path(root), ticket)
    q.put((rc, payload.get("rev_id"), payload.get("error")))


def test_revise_open_takes_claim_flock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Two concurrent revise-opens serialize on the per-ticket revise.claim flock:
    # exactly one wins (rc 0, rev_id r1, a live lease), the other refuses (rc 4).
    # Without the flock both would scan an empty revisions/, both pick r1, both
    # pass the live-lease check before either acquires.
    stages = ["ticket", "plan"]
    _write_workspace(tmp_path, stages=stages)
    _stub_git_head(monkeypatch)
    _drive_to_terminal(tmp_path, "FT-1", stages)

    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    procs = [
        ctx.Process(target=_revise_open_subprocess, args=(str(tmp_path), "FT-1", q))
        for _ in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    results = [q.get(timeout=5) for _ in range(2)]
    codes = sorted(r[0] for r in results)
    assert codes == [0, 4]
    # the claim file persists (flock targets are never deleted)
    assert ds._revise_claim_path(tmp_path / ".flow" / "runs" / "FT-1").exists()


# ─── next / advance --revision redirect ──────────────────────────────────────


def test_next_advance_redirect_with_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stages = ["ticket", "plan", "implement", "commit", "reflect"]
    _write_workspace(tmp_path, stages=stages)
    _stub_git_head(monkeypatch)
    nonce = _drive_to_terminal(tmp_path, "FT-1", stages)

    rc, payload = ds.cmd_revise_open(tmp_path, "FT-1")
    assert rc == 0
    rev = payload["rev_id"]
    rev_nonce = payload["session_nonce"]
    subset = payload["stages"]  # implement, commit, reflect

    # next --revision drives the SUB-run (first pending = implement)
    rc, nxt = ds.cmd_next(tmp_path, "FT-1", rev_nonce, revision=rev)
    assert rc == 0
    assert nxt["stage"] == subset[0]

    # advance --revision finishes that stage and returns the next descriptor
    rc, adv = ds.cmd_advance(
        tmp_path, "FT-1", subset[0], "completed", session_nonce=rev_nonce, revision=rev
    )
    assert rc == 0
    assert adv["finished"]["stage"] == subset[0]
    assert adv["stage"] == subset[1]

    # the ORIGINAL run is still done:true and untouched by the sub-run
    rc, orig_next = ds.cmd_next(tmp_path, "FT-1", nonce)
    assert orig_next.get("done") is True


def test_next_revision_reads_revision_snapshot_not_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Discriminates the dispatch-level drift threading (decision #2): cmd_next must
    # gate against the REVISION's snapshot.sha, not the original's. Poison the
    # original ticket-level sha while leaving the revision's intact; if revision were
    # dropped from the _gate_drift -> classify_drift call, cmd_next would read the
    # poisoned original and abort rc 1 with drift. (Both snapshots are byte-identical
    # otherwise, so only this poison distinguishes the two paths.)
    import snapshot

    stages = ["ticket", "plan", "implement", "commit", "reflect"]
    _write_workspace(tmp_path, stages=stages)
    _stub_git_head(monkeypatch)
    _drive_to_terminal(tmp_path, "FT-1", stages)

    rc, payload = ds.cmd_revise_open(tmp_path, "FT-1")
    assert rc == 0
    rev, rev_nonce, subset = payload["rev_id"], payload["session_nonce"], payload["stages"]

    snapshot.snapshot_sha_path(tmp_path, "FT-1").write_text("deadbeef\n", encoding="utf-8")
    rc, nxt = ds.cmd_next(tmp_path, "FT-1", rev_nonce, revision=rev)
    assert rc == 0  # read the revision sha (match) and proceeded, NOT the poisoned original
    assert nxt["stage"] == subset[0]


def test_status_release_redirect_with_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stages = ["ticket", "plan"]
    _write_workspace(tmp_path, stages=stages)
    _stub_git_head(monkeypatch)
    _drive_to_terminal(tmp_path, "FT-1", stages)

    rc, payload = ds.cmd_revise_open(tmp_path, "FT-1")
    rev, rev_nonce = payload["rev_id"], payload["session_nonce"]

    rc, st = ds.cmd_status(tmp_path, "FT-1", revision=rev)
    assert rc == 0
    assert st["ticket"] == "FT-1"
    assert set(st["stages"]) == set(payload["stages"])

    rc, rel = ds.cmd_release(tmp_path, "FT-1", rev_nonce, revision=rev)
    assert rc == 0 and rel["released"] is True
    assert not lease.run_lock_path(Path(payload["revision_dir"])).exists()
