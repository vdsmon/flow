from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import launch_ledger as ll


def _marked_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "flow"
    (repo / ".flow").mkdir(parents=True)
    (repo / ".flow" / "workspace.toml").write_text(
        "[maintainer]\nself_target = true\n", encoding="utf-8"
    )
    return repo


def _at(base: str, secs: int) -> str:
    from datetime import timedelta

    import lease

    dt = lease.parse_iso(base)
    assert dt is not None
    return (dt + timedelta(seconds=secs)).strftime("%Y-%m-%dT%H:%M:%SZ")


T0 = "2020-01-01T00:00:00Z"


def test_add_writes_marker_and_live(tmp_path):
    repo = _marked_repo(tmp_path)
    ll.add(repo, "flow-x", now=T0)
    marker = repo / ".flow" / "launch-ledger" / "flow-x"
    assert marker.read_text().strip() == T0
    assert ll.live_keys(repo, now=T0) == {"flow-x"}
    # atomic write leaves only the marker behind (no temp-file residue)
    assert [p.name for p in marker.parent.iterdir()] == ["flow-x"]


def test_live_keys_drops_expired_past_ttl(tmp_path):
    repo = _marked_repo(tmp_path)
    ll.add(repo, "flow-x", now=T0)
    later = _at(T0, ll.LAUNCH_TTL_SECONDS + 1)
    assert ll.live_keys(repo, now=later) == set()
    # still fresh, one second before the boundary
    assert ll.live_keys(repo, now=_at(T0, ll.LAUNCH_TTL_SECONDS - 1)) == {"flow-x"}


def test_prune_removes_expired_keeps_fresh(tmp_path):
    repo = _marked_repo(tmp_path)
    ll.add(repo, "flow-old", now=T0)
    ll.add(repo, "flow-new", now=_at(T0, ll.LAUNCH_TTL_SECONDS))
    later = _at(T0, ll.LAUNCH_TTL_SECONDS + 1)
    pruned = ll.prune(repo, now=later)
    assert pruned == ["flow-old"]
    d = repo / ".flow" / "launch-ledger"
    assert not (d / "flow-old").exists()
    assert (d / "flow-new").exists()


def test_remove_deletes_marker_and_drops_from_live(tmp_path):
    repo = _marked_repo(tmp_path)
    ll.add(repo, "flow-x", now=T0)
    marker = repo / ".flow" / "launch-ledger" / "flow-x"
    assert marker.exists()
    ll.remove(repo, "flow-x")
    assert not marker.exists()
    assert ll.live_keys(repo, now=T0) == set()


def test_remove_missing_key_is_noop(tmp_path):
    repo = _marked_repo(tmp_path)
    ll.remove(repo, "flow-absent")  # no raise on a missing marker / dir


def test_live_keys_robust_to_garbage_marker(tmp_path):
    repo = _marked_repo(tmp_path)
    d = repo / ".flow" / "launch-ledger"
    d.mkdir(parents=True)
    (d / "flow-bad").write_text("not-a-timestamp", encoding="utf-8")
    (d / "flow-empty").write_text("", encoding="utf-8")
    ll.add(repo, "flow-ok", now=T0)
    assert ll.live_keys(repo, now=T0) == {"flow-ok"}


def test_live_keys_empty_when_no_dir(tmp_path):
    repo = _marked_repo(tmp_path)
    assert ll.live_keys(repo, now=T0) == set()


def _run_cli(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "launch_ledger.py"), *argv],
        capture_output=True,
        text=True,
    )


def test_cli_add_then_list_json(tmp_path):
    repo = _marked_repo(tmp_path)
    add = _run_cli(["add", "--key", "flow-a", "--workspace-root", str(repo)])
    assert add.returncode == 0, add.stderr
    listed = _run_cli(["list", "--workspace-root", str(repo), "--json"])
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == ["flow-a"]


def test_cli_not_maintainer_exit_4(tmp_path, monkeypatch):
    plain = tmp_path / "proj"
    (plain / ".flow").mkdir(parents=True)
    (plain / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "beads"\n', encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    out = _run_cli(["list", "--workspace-root", str(plain)])
    assert out.returncode == 4, out.stdout + out.stderr


def test_prune_cli_exits_zero(tmp_path):
    repo = _marked_repo(tmp_path)
    out = _run_cli(["prune", "--workspace-root", str(repo)])
    assert out.returncode == 0, out.stderr
    assert "AttributeError" not in out.stderr


def test_prune_cli_output_is_plain_lines(tmp_path):
    # verify prune stdout is newline-delimited (not JSON) when keys are pruned;
    # drive via cli_main with a monkeypatched prune to control which keys appear.
    repo = _marked_repo(tmp_path)
    import io
    from contextlib import redirect_stdout
    from unittest.mock import patch

    fake_pruned = ["flow-b", "flow-a"]
    buf = io.StringIO()
    with patch.object(ll, "prune", return_value=fake_pruned), redirect_stdout(buf):
        rc = ll.cli_main(["prune", "--workspace-root", str(repo)])
    assert rc == 0
    assert buf.getvalue().strip() == "flow-a\nflow-b"
