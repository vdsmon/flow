"""Tests for observe_ship_event.py — sole writer of ship-events."""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

import _memory_paths
import observe_ship_event


def _seed_workspace(root: Path, namespace: str = "demo") -> None:
    flow = root / ".flow"
    flow.mkdir(parents=True, exist_ok=True)
    (flow / "workspace.toml").write_text(
        f'[tracker]\nbackend = "jira"\n[tracker.jira]\ncloud_id = "x"\nproject_key = "FT"\n\n[memory]\nnamespace = "{namespace}"\n',
        encoding="utf-8",
    )


def _payload(ticket: str = "FT-1", extras: dict | None = None) -> dict:
    out = {
        "ticket": ticket,
        "shipped_at": "2026-05-28T14:32:00Z",
        "evidence": {"foo": "bar"},
    }
    if extras:
        out.update(extras)
    return out


# ─── validate_evidence ───────────────────────────────────────────────────────


def test_validate_happy() -> None:
    payload = _payload()
    out = observe_ship_event.validate_evidence(payload, "FT-1")
    assert out is payload


def test_validate_not_object_raises() -> None:
    with pytest.raises(observe_ship_event._EvidenceInvalid, match="not an object"):
        observe_ship_event.validate_evidence([], "FT-1")


def test_validate_ticket_mismatch_raises() -> None:
    with pytest.raises(observe_ship_event._EvidenceInvalid, match="mismatches"):
        observe_ship_event.validate_evidence(_payload(ticket="FT-99"), "FT-1")


def test_validate_missing_ticket_raises() -> None:
    bad = {"shipped_at": "2026-05-28T14:32:00Z", "evidence": {}}
    with pytest.raises(observe_ship_event._EvidenceInvalid, match="ticket"):
        observe_ship_event.validate_evidence(bad, "FT-1")


def test_validate_shipped_at_format_strict() -> None:
    bad = {"ticket": "FT-1", "shipped_at": "2026-05-28 14:32", "evidence": {}}
    with pytest.raises(observe_ship_event._EvidenceInvalid, match="shipped_at"):
        observe_ship_event.validate_evidence(bad, "FT-1")


def test_validate_missing_evidence_raises() -> None:
    bad = {"ticket": "FT-1", "shipped_at": "2026-05-28T14:32:00Z"}
    with pytest.raises(observe_ship_event._EvidenceInvalid, match="evidence"):
        observe_ship_event.validate_evidence(bad, "FT-1")


def test_validate_evidence_not_object_raises() -> None:
    bad = {"ticket": "FT-1", "shipped_at": "2026-05-28T14:32:00Z", "evidence": []}
    with pytest.raises(observe_ship_event._EvidenceInvalid, match="evidence"):
        observe_ship_event.validate_evidence(bad, "FT-1")


def test_validate_rejects_extra_top_keys() -> None:
    bad = _payload(extras={"observed_at": "x"})
    with pytest.raises(observe_ship_event._EvidenceInvalid, match="extra"):
        observe_ship_event.validate_evidence(bad, "FT-1")


# ─── observe() primary path ──────────────────────────────────────────────────


def test_observe_primary_path_succeeds(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    path, is_dupe = observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    assert is_dupe is False
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ticket"] == "FT-1"
    assert data["observed_by_run_id"] == "abcdef0123456789"
    assert "observed_at" in data


def test_observe_invalid_run_id_raises(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    with pytest.raises(observe_ship_event._EvidenceInvalid, match="run_id"):
        observe_ship_event.observe(tmp_path, "FT-1", _payload(), "not-hex")


def test_observe_creates_ship_events_dir(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    ship_dir = _memory_paths.ship_events_dir(tmp_path, "demo")
    assert ship_dir.is_dir()


def test_observe_primary_immutable_after_write(tmp_path: Path) -> None:
    """Two writes of identical payload: second goes to dupe.1.json."""
    _seed_workspace(tmp_path)
    p1, _ = observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    p2, is_dupe = observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    assert is_dupe is True
    assert p1 != p2
    assert p2.name.endswith(".dupe.1.json")
    # Primary content unchanged.
    assert json.loads(p1.read_text(encoding="utf-8"))["ticket"] == "FT-1"


def test_observe_monotonic_dupe_n(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    p2, _ = observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    p3, _ = observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    assert p2.name.endswith(".dupe.1.json")
    assert p3.name.endswith(".dupe.2.json")


def test_dupe_record_has_superseded_field(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    p_dupe, _ = observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    data = json.loads(p_dupe.read_text(encoding="utf-8"))
    assert data["superseded_by_dupe"] is False


# ─── observe() concurrency ───────────────────────────────────────────────────


def _observer_proc(root_str: str, queue) -> None:
    path, is_dupe = observe_ship_event.observe(
        Path(root_str), "FT-1", _payload(), "abcdef0123456789"
    )
    queue.put((str(path), is_dupe))


def test_concurrent_observers_race_o_excl(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    p1 = ctx.Process(target=_observer_proc, args=(str(tmp_path), q))
    p2 = ctx.Process(target=_observer_proc, args=(str(tmp_path), q))
    p1.start()
    p2.start()
    p1.join(timeout=10)
    p2.join(timeout=10)
    results = [q.get(timeout=5), q.get(timeout=5)]
    is_dupe_flags = sorted(r[1] for r in results)
    # Exactly one primary, one dupe.
    assert is_dupe_flags == [False, True]


# ─── Intent log on I/O error ─────────────────────────────────────────────────


def test_io_error_writes_intent_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_workspace(tmp_path)

    def fail_excl(path: Path, content: str) -> None:
        raise OSError(13, "permission denied")

    monkeypatch.setattr(observe_ship_event, "_write_o_excl", fail_excl)
    with pytest.raises(OSError, match="permission denied"):
        observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    intent_logs = list(
        _memory_paths.ship_events_dir(tmp_path, "demo").glob("FT-1.json.quarantine-intent.*.json")
    )
    assert len(intent_logs) == 1
    payload = json.loads(intent_logs[0].read_text(encoding="utf-8"))
    assert payload["error"]


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_happy_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    rc = observe_ship_event.cli_main(
        [
            "--ticket",
            "FT-1",
            "--evidence-json",
            json.dumps(_payload()),
            "--run-id",
            "abcdef0123456789",
            "--workspace-root",
            str(tmp_path),
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["is_dupe"] is False


def test_cli_dupe_returns_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    rc = observe_ship_event.cli_main(
        [
            "--ticket",
            "FT-1",
            "--evidence-json",
            json.dumps(_payload()),
            "--run-id",
            "abcdef0123456789",
            "--workspace-root",
            str(tmp_path),
        ]
    )
    assert rc == 2


def test_cli_malformed_json_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    rc = observe_ship_event.cli_main(
        [
            "--ticket",
            "FT-1",
            "--evidence-json",
            "{not json}",
            "--run-id",
            "abcdef0123456789",
            "--workspace-root",
            str(tmp_path),
        ]
    )
    assert rc == 1
    assert "not JSON" in capsys.readouterr().err


def test_cli_invalid_evidence_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    bad = json.dumps({"ticket": "FT-99", "shipped_at": "2026-05-28T14:32:00Z", "evidence": {}})
    rc = observe_ship_event.cli_main(
        [
            "--ticket",
            "FT-1",
            "--evidence-json",
            bad,
            "--run-id",
            "abcdef0123456789",
            "--workspace-root",
            str(tmp_path),
        ]
    )
    assert rc == 1


def test_cli_missing_workspace_returns_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = observe_ship_event.cli_main(
        [
            "--ticket",
            "FT-1",
            "--evidence-json",
            json.dumps(_payload()),
            "--run-id",
            "abcdef0123456789",
            "--workspace-root",
            str(tmp_path),
        ]
    )
    assert rc == 3
