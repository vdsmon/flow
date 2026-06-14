from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import fleet
import lease

T0 = "2020-01-01T00:00:00Z"


def _at(base: str, secs: int) -> str:
    dt = lease.parse_iso(base)
    assert dt is not None
    return (dt + timedelta(seconds=secs)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fleet(tmp_path: Path) -> Path:
    return tmp_path / "fleet"


# ─── register / upsert ────────────────────────────────────────────────────────


def test_register_creates_entry_both_stamps_equal(tmp_path):
    fd = _fleet(tmp_path)
    fleet.register(fd, "flow-x", "rid-1", now=T0, hostname="h", boot_id="b")
    e = fleet.read(fd, "flow-x")
    assert e == {
        "key": "flow-x",
        "run_id": "rid-1",
        "registered_at": T0,
        "heartbeat_at": T0,
        "hostname": "h",
        "boot_id": "b",
    }


def test_register_upsert_preserves_registered_at_bumps_heartbeat_and_runid(tmp_path):
    fd = _fleet(tmp_path)
    # launch register: no run_id yet
    fleet.register(fd, "flow-x", "", now=T0)
    later = _at(T0, 120)
    # first per-stage heartbeat: real run_id, later time
    fleet.register(fd, "flow-x", "rid-1", now=later, hostname="h", boot_id="b")
    e = fleet.read(fd, "flow-x")
    assert e is not None
    assert e["registered_at"] == T0  # preserved from launch
    assert e["heartbeat_at"] == later  # bumped
    assert e["run_id"] == "rid-1"  # claimed
    assert e["hostname"] == "h"


def test_register_no_temp_residue(tmp_path):
    fd = _fleet(tmp_path)
    fleet.register(fd, "flow-x", "rid-1", now=T0)
    names = sorted(p.name for p in fd.iterdir())
    assert names == ["flow-x.json", "flow-x.lock"]
    assert not any(n.endswith(".tmp") for n in names)


def test_register_quarantines_corrupt_prior_then_writes_fresh(tmp_path):
    fd = _fleet(tmp_path)
    fd.mkdir(parents=True)
    (fd / "flow-x.json").write_text("not-json{", encoding="utf-8")
    fleet.register(fd, "flow-x", "rid-1", now=T0)
    # fresh entry is valid
    fresh = fleet.read(fd, "flow-x")
    assert fresh is not None
    assert fresh["run_id"] == "rid-1"
    # the garbage was preserved for forensics, not deleted
    quarantined = list(fd.glob("flow-x.json.quarantine.*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == "not-json{"


# ─── live_keys: the staleness fallback ────────────────────────────────────────


def test_live_keys_excludes_stale_includes_fresh(tmp_path):
    fd = _fleet(tmp_path)
    fleet.register(fd, "flow-fresh", "r1", now=_at(T0, fleet.STALE_AFTER_S))
    fleet.register(fd, "flow-stale", "r2", now=T0)
    later = _at(T0, fleet.STALE_AFTER_S + 1)
    # flow-stale last beat at T0 -> aged past the window; flow-fresh beat recently
    assert fleet.live_keys(fd, now=later) == {"flow-fresh"}


def test_live_keys_boundary_is_strict_less_than(tmp_path):
    fd = _fleet(tmp_path)
    fleet.register(fd, "flow-x", "r1", now=T0)
    # exactly at the boundary -> NOT live (age < stale_after_s is strict)
    assert fleet.live_keys(fd, now=_at(T0, fleet.STALE_AFTER_S)) == set()
    assert fleet.live_keys(fd, now=_at(T0, fleet.STALE_AFTER_S - 1)) == {"flow-x"}


def test_live_keys_empty_when_no_dir(tmp_path):
    assert fleet.live_keys(_fleet(tmp_path), now=T0) == set()


def test_live_keys_skips_garbage_entry(tmp_path):
    fd = _fleet(tmp_path)
    fleet.register(fd, "flow-ok", "r1", now=T0)
    (fd / "flow-bad.json").write_text("xxx", encoding="utf-8")
    (fd / "flow-empty.json").write_text("", encoding="utf-8")
    assert fleet.live_keys(fd, now=T0) == {"flow-ok"}


# ─── deregister (run_id-gated) ────────────────────────────────────────────────


def test_deregister_unconditional_removes(tmp_path):
    fd = _fleet(tmp_path)
    fleet.register(fd, "flow-x", "r1", now=T0)
    fleet.deregister(fd, "flow-x")
    assert fleet.read(fd, "flow-x") is None


def test_deregister_runid_match_removes(tmp_path):
    fd = _fleet(tmp_path)
    fleet.register(fd, "flow-x", "r1", now=T0)
    fleet.deregister(fd, "flow-x", run_id="r1")
    assert fleet.read(fd, "flow-x") is None


def test_deregister_runid_mismatch_is_noop(tmp_path):
    fd = _fleet(tmp_path)
    fleet.register(fd, "flow-x", "r1", now=T0)
    fleet.deregister(fd, "flow-x", run_id="r2")  # a successor must not be dropped
    assert fleet.read(fd, "flow-x") is not None


def test_deregister_empty_placeholder_runid_is_claimable(tmp_path):
    fd = _fleet(tmp_path)
    fleet.register(fd, "flow-x", "", now=T0)  # launch placeholder
    fleet.deregister(fd, "flow-x", run_id="r1")  # the run owning it can drop it
    assert fleet.read(fd, "flow-x") is None


def test_deregister_missing_key_is_noop(tmp_path):
    fleet.deregister(_fleet(tmp_path), "flow-absent")  # no raise on missing dir/entry


# ─── prune ────────────────────────────────────────────────────────────────────


def test_prune_removes_stale_keeps_fresh(tmp_path):
    fd = _fleet(tmp_path)
    fleet.register(fd, "flow-old", "r1", now=T0)
    fleet.register(fd, "flow-new", "r2", now=_at(T0, fleet.STALE_AFTER_S))
    later = _at(T0, fleet.STALE_AFTER_S + 1)
    pruned = fleet.prune(fd, now=later)
    assert pruned == ["flow-old"]
    assert fleet.read(fd, "flow-old") is None
    assert fleet.read(fd, "flow-new") is not None


def test_prune_empty_when_all_fresh(tmp_path):
    fd = _fleet(tmp_path)
    fleet.register(fd, "flow-x", "r1", now=T0)
    assert fleet.prune(fd, now=_at(T0, 10)) == []


# ─── entries / read ───────────────────────────────────────────────────────────


def test_entries_returns_valid_skips_corrupt(tmp_path):
    fd = _fleet(tmp_path)
    fleet.register(fd, "flow-a", "r1", now=T0)
    (fd / "flow-bad.json").write_text("{", encoding="utf-8")
    keys = sorted(e["key"] for e in fleet.entries(fd))
    assert keys == ["flow-a"]


# ─── resolution + maintainer gate (the resolver correction) ───────────────────


def _marked(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    (repo / ".flow").mkdir(parents=True)
    (repo / ".flow" / "workspace.toml").write_text(
        "[maintainer]\nself_target = true\n", encoding="utf-8"
    )
    return repo


def test_resolve_fleet_dir_follows_memory_root_redirect(tmp_path):
    main = _marked(tmp_path, "flow")
    wt = _marked(tmp_path, "wt")
    # the worktree bootstrap redirects the store to the MAIN .flow
    (wt / ".flow" / "memory-root").write_text(str(main / ".flow"), encoding="utf-8")
    assert fleet.resolve_fleet_dir(wt) == main / ".flow" / "fleet"


def test_register_run_writes_to_main_from_a_worktree(tmp_path):
    main = _marked(tmp_path, "flow")
    wt = _marked(tmp_path, "wt")
    (wt / ".flow" / "memory-root").write_text(str(main / ".flow"), encoding="utf-8")
    wrote = fleet.register_run(wt, "flow-x", "rid-1", now=T0)
    assert wrote is True
    # landed in MAIN, not the doomed worktree inode
    assert (main / ".flow" / "fleet" / "flow-x.json").exists()
    assert not (wt / ".flow" / "fleet").exists()


def test_register_run_noop_when_not_maintainer(tmp_path, monkeypatch):
    plain = tmp_path / "proj"
    (plain / ".flow").mkdir(parents=True)
    (plain / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "beads"\n', encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    wrote = fleet.register_run(plain, "flow-x", "rid-1", now=T0)
    assert wrote is False
    assert not (plain / ".flow" / "fleet").exists()


def test_deregister_run_roundtrip(tmp_path):
    main = _marked(tmp_path, "flow")
    fleet.register_run(main, "flow-x", "rid-1", now=T0)
    assert (main / ".flow" / "fleet" / "flow-x.json").exists()
    assert fleet.deregister_run(main, "flow-x", run_id="rid-1") is True
    assert not (main / ".flow" / "fleet" / "flow-x.json").exists()


def test_deregister_run_runid_gated(tmp_path):
    main = _marked(tmp_path, "flow")
    fleet.register_run(main, "flow-x", "rid-1", now=T0)
    fleet.deregister_run(main, "flow-x", run_id="other")  # a successor must not be dropped
    assert (main / ".flow" / "fleet" / "flow-x.json").exists()


def test_deregister_run_noop_when_not_maintainer(tmp_path, monkeypatch):
    plain = tmp_path / "proj"
    (plain / ".flow").mkdir(parents=True)
    (plain / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "beads"\n', encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    assert fleet.deregister_run(plain, "flow-x") is False


# ─── CLI ──────────────────────────────────────────────────────────────────────


def _run_cli(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "fleet.py"), *argv],
        capture_output=True,
        text=True,
    )


def test_cli_register_then_live_keys_json(tmp_path):
    repo = _marked(tmp_path, "flow")
    reg = _run_cli(["register", "--key", "flow-a", "--workspace-root", str(repo)])
    assert reg.returncode == 0, reg.stderr
    listed = _run_cli(["live-keys", "--workspace-root", str(repo), "--json"])
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == ["flow-a"]


def test_cli_register_omitting_run_id_is_allowed(tmp_path):
    # the drain launch prose calls `register --key <key> --workspace-root .` (no run-id)
    repo = _marked(tmp_path, "flow")
    reg = _run_cli(["register", "--key", "flow-a", "--workspace-root", str(repo)])
    assert reg.returncode == 0, reg.stderr
    listed = _run_cli(["list", "--workspace-root", str(repo), "--json"])
    entry = json.loads(listed.stdout)[0]
    assert entry["key"] == "flow-a"
    assert entry["run_id"] == ""


def test_cli_not_maintainer_exit_4(tmp_path, monkeypatch):
    plain = tmp_path / "proj"
    (plain / ".flow").mkdir(parents=True)
    (plain / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "beads"\n', encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    out = _run_cli(["live-keys", "--workspace-root", str(plain)])
    assert out.returncode == 4, out.stdout + out.stderr
