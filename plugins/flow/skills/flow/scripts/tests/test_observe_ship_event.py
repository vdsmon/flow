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


# ─── flow_attribution self-stamp ─────────────────────────────────────────────


def _seed_state(
    root: Path,
    ticket: str,
    *,
    run_id: str,
    plan_started_at_iso: str | None = "2026-05-28T00:00:00Z",
    create_pr_finished_at_iso: str | None = "2026-05-28T12:00:00Z",
) -> None:
    stages: dict = {}
    if plan_started_at_iso is not None:
        stages["plan"] = {"started_at_iso": plan_started_at_iso}
    if create_pr_finished_at_iso is not None:
        stages["create_pr"] = {"finished_at_iso": create_pr_finished_at_iso}
    state = {"ticket": ticket, "run_id": run_id, "stages": stages}
    state_dir = root / ".flow" / "runs" / ticket
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def test_stamp_present_when_state_coherent(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _seed_state(
        tmp_path,
        "FT-1",
        run_id="abcdef0123456789",
        plan_started_at_iso="2026-05-28T00:00:00Z",
        create_pr_finished_at_iso="2026-05-28T12:00:00Z",
    )
    path, _ = observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["flow_attribution"] == {
        "plan_started_at_iso": "2026-05-28T00:00:00Z",
        "create_pr_finished_at_iso": "2026-05-28T12:00:00Z",
    }


def test_no_stamp_when_no_state(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    path, _ = observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "flow_attribution" not in data
    assert data["observed_by_run_id"] == "abcdef0123456789"


def test_no_stamp_when_run_id_mismatch(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _seed_state(tmp_path, "FT-1", run_id="0000000000000000")
    path, _ = observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "flow_attribution" not in data


def test_no_stamp_when_timestamp_missing(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _seed_state(tmp_path, "FT-1", run_id="abcdef0123456789", create_pr_finished_at_iso=None)
    path, _ = observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "flow_attribution" not in data


def test_stamp_present_in_dupe_write(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _seed_state(tmp_path, "FT-1", run_id="abcdef0123456789")
    observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    p_dupe, is_dupe = observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    assert is_dupe is True
    data = json.loads(p_dupe.read_text(encoding="utf-8"))
    assert data["flow_attribution"] == {
        "plan_started_at_iso": "2026-05-28T00:00:00Z",
        "create_pr_finished_at_iso": "2026-05-28T12:00:00Z",
    }


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


# ─── arm (flow / control) ────────────────────────────────────────────────────


def test_arm_defaults_to_flow(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    path, _ = observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["arm"] == "flow"


def test_arm_control_stamps_record(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    path, _ = observe_ship_event.observe(
        tmp_path, "FT-1", _payload(), "abcdef0123456789", arm="control"
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["arm"] == "control"


def test_arm_invalid_raises(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    with pytest.raises(observe_ship_event._EvidenceInvalid, match="arm"):
        observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789", arm="bogus")


def test_arm_present_in_dupe_write(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789", arm="control")
    p_dupe, is_dupe = observe_ship_event.observe(
        tmp_path, "FT-1", _payload(), "abcdef0123456789", arm="control"
    )
    assert is_dupe is True
    data = json.loads(p_dupe.read_text(encoding="utf-8"))
    assert data["arm"] == "control"


def test_arm_in_input_evidence_rejected_as_extra_key(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    with pytest.raises(observe_ship_event._EvidenceInvalid, match="extra"):
        observe_ship_event.validate_evidence(_payload(extras={"arm": "control"}), "FT-1")


def test_cli_arm_control_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Deliverable (c): CLI threads --arm control into the written record."""
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
            "--arm",
            "control",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    data = json.loads(Path(out["path"]).read_text(encoding="utf-8"))
    assert data["arm"] == "control"


def test_cli_arm_defaults_to_flow(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
    out = json.loads(capsys.readouterr().out)
    data = json.loads(Path(out["path"]).read_text(encoding="utf-8"))
    assert data["arm"] == "flow"


# ─── tier (free-form, caller-supplied) ───────────────────────────────────────


def test_tier_defaults_to_empty(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    path, _ = observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tier"] == ""


def test_tier_stamps_record(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    path, _ = observe_ship_event.observe(
        tmp_path, "FT-1", _payload(), "abcdef0123456789", tier="tier:trivial"
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tier"] == "tier:trivial"


def test_tier_present_in_dupe_write(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    observe_ship_event.observe(
        tmp_path, "FT-1", _payload(), "abcdef0123456789", tier="tier:trivial"
    )
    p_dupe, is_dupe = observe_ship_event.observe(
        tmp_path, "FT-1", _payload(), "abcdef0123456789", tier="tier:trivial"
    )
    assert is_dupe is True
    data = json.loads(p_dupe.read_text(encoding="utf-8"))
    assert data["tier"] == "tier:trivial"


def test_tier_in_input_evidence_rejected_as_extra_key(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    with pytest.raises(observe_ship_event._EvidenceInvalid, match="extra"):
        observe_ship_event.validate_evidence(_payload(extras={"tier": "tier:trivial"}), "FT-1")


def test_cli_tier_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
            "--tier",
            "tier:small",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    data = json.loads(Path(out["path"]).read_text(encoding="utf-8"))
    assert data["tier"] == "tier:small"


def test_cli_tier_defaults_to_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
    out = json.loads(capsys.readouterr().out)
    data = json.loads(Path(out["path"]).read_text(encoding="utf-8"))
    assert data["tier"] == ""


# ─── acceptance_invariant (free-form, caller-supplied) ───────────────────────


def test_acceptance_invariant_defaults_to_empty(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    path, _ = observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["acceptance_invariant"] == ""


def test_acceptance_invariant_stamps_record(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    path, _ = observe_ship_event.observe(
        tmp_path,
        "FT-1",
        _payload(),
        "abcdef0123456789",
        acceptance_invariant="all amounts positive",
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["acceptance_invariant"] == "all amounts positive"


def test_acceptance_invariant_present_in_dupe_write(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    observe_ship_event.observe(
        tmp_path, "FT-1", _payload(), "abcdef0123456789", acceptance_invariant="sign stays +"
    )
    p_dupe, is_dupe = observe_ship_event.observe(
        tmp_path, "FT-1", _payload(), "abcdef0123456789", acceptance_invariant="sign stays +"
    )
    assert is_dupe is True
    data = json.loads(p_dupe.read_text(encoding="utf-8"))
    assert data["acceptance_invariant"] == "sign stays +"


# ─── lane (express|light|full the run took; the express-lane measurement join) ──


def test_lane_defaults_to_empty(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    path, _ = observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["lane"] == ""


def test_lane_stamps_record(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    path, _ = observe_ship_event.observe(
        tmp_path, "FT-1", _payload(), "abcdef0123456789", lane="express"
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["lane"] == "express"


def test_acceptance_invariant_in_input_evidence_rejected_as_extra_key(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    with pytest.raises(observe_ship_event._EvidenceInvalid, match="extra"):
        observe_ship_event.validate_evidence(_payload(extras={"acceptance_invariant": "x"}), "FT-1")


def test_cli_acceptance_invariant_round_trip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
            "--acceptance-invariant",
            "all amounts positive",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    data = json.loads(Path(out["path"]).read_text(encoding="utf-8"))
    assert data["acceptance_invariant"] == "all amounts positive"


def test_cli_acceptance_invariant_defaults_to_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
    out = json.loads(capsys.readouterr().out)
    data = json.loads(Path(out["path"]).read_text(encoding="utf-8"))
    assert data["acceptance_invariant"] == ""


# ─── plugin_version (self-read, fully guarded) ───────────────────────────────


def _live_plugin_version() -> str:
    path = Path(observe_ship_event.__file__).resolve().parents[3] / ".claude-plugin" / "plugin.json"
    return json.loads(path.read_text(encoding="utf-8"))["version"]


def test_plugin_version_stamps_live_version(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    path, _ = observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    data = json.loads(path.read_text(encoding="utf-8"))
    live = _live_plugin_version()
    assert isinstance(data["plugin_version"], str)
    assert data["plugin_version"]
    assert data["plugin_version"] == live


def test_plugin_version_present_in_dupe_write(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    p_dupe, is_dupe = observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    assert is_dupe is True
    data = json.loads(p_dupe.read_text(encoding="utf-8"))
    assert data["plugin_version"] == _live_plugin_version()


def test_plugin_version_in_input_evidence_rejected_as_extra_key(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    with pytest.raises(observe_ship_event._EvidenceInvalid, match="extra"):
        observe_ship_event.validate_evidence(_payload(extras={"plugin_version": "9.9.9"}), "FT-1")


def test_plugin_version_guarded_to_empty_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_workspace(tmp_path)
    monkeypatch.setattr(observe_ship_event, "_plugin_version", lambda: "")
    path, _ = observe_ship_event.observe(tmp_path, "FT-1", _payload(), "abcdef0123456789")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["plugin_version"] == ""


def test_plugin_version_helper_swallows_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: Path, *a: object, **k: object) -> str:
        raise OSError(13, "permission denied")

    monkeypatch.setattr(Path, "read_text", boom)
    assert observe_ship_event._plugin_version() == ""


# ─── observe_revert (durable immutable revert events) ────────────────────────


def _revert_record(reverting_sha: str = "a" * 40) -> dict:
    return {
        "kind": "revert",
        "ticket": "FT-1",
        "reverted_commit_sha": "b" * 40,
        "reverting_commit_sha": reverting_sha,
        "reverting_subject": 'Revert "feat: thing"',
        "source": "git",
    }


def test_observe_revert_writes_event(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    path, is_new = observe_ship_event.observe_revert(tmp_path, "demo", _revert_record())
    assert is_new is True
    assert path == _memory_paths.revert_event_path(tmp_path, "demo", "a" * 40)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["kind"] == "revert"
    assert data["ticket"] == "FT-1"
    assert data["reverting_commit_sha"] == "a" * 40
    assert data["source"] == "git"
    assert "observed_at" in data


def test_observe_revert_idempotent_no_overwrite(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    path, first = observe_ship_event.observe_revert(tmp_path, "demo", _revert_record())
    assert first is True
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns
    path2, second = observe_ship_event.observe_revert(
        tmp_path, "demo", _revert_record() | {"ticket": "FT-MUTATED"}
    )
    assert path2 == path
    assert second is False
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime


def test_observe_revert_missing_sha_raises(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    bad = _revert_record()
    del bad["reverting_commit_sha"]
    with pytest.raises(ValueError, match="reverting_commit_sha"):
        observe_ship_event.observe_revert(tmp_path, "demo", bad)


def test_observe_revert_does_not_mutate_caller(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    rec = _revert_record()
    observe_ship_event.observe_revert(tmp_path, "demo", rec)
    assert "observed_at" not in rec
