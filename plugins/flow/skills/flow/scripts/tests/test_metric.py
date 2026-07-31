"""Tests for metric.py, tickets-per-week over a UTC window.

Builds real ship-events + state.json on temp dirs (matching the on-disk shapes
observe_ship_event.py and state.py write), with an explicit `now` so window math
is deterministic. Checkpoint mode aggregates across two manifest participants
written to a temp manifest path.
"""

from __future__ import annotations

import json
from pathlib import Path

import metric
from tests.wsfactory import make_workspace, memory, tracker


def _seed_workspace(root: Path, namespace: str = "demo") -> None:
    make_workspace(
        root,
        tracker("jira", subtable=False),
        memory(namespace),
        initialized=True,
        namespace_dir=f"{namespace}/ship-events",
    )


def _write_ship_event(
    root: Path,
    ticket: str,
    *,
    shipped_at: str,
    observed_by_run_id: str = "abcdef0123456789",
    namespace: str = "demo",
    filename: str | None = None,
) -> Path:
    record = {
        "ticket": ticket,
        "shipped_at": shipped_at,
        "evidence": {"merged": True},
        "observed_at": "2026-05-20T10:00:00Z",
        "observed_by_run_id": observed_by_run_id,
    }
    ship_dir = root / ".flow" / namespace / "ship-events"
    ship_dir.mkdir(parents=True, exist_ok=True)
    path = ship_dir / (filename or f"{ticket}.json")
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _write_state(
    root: Path,
    ticket: str,
    *,
    run_id: str,
    reflect_status: str | None = "completed",
    plan_started_at_iso: str | None = None,
    create_pr_finished_at_iso: str | None = None,
) -> Path:
    stages: dict = {
        "implement": {"status": "completed"},
    }
    if reflect_status is not None:
        stages["reflect"] = {"status": reflect_status}
    if plan_started_at_iso is not None:
        stages["plan"] = {"started_at_iso": plan_started_at_iso}
    if create_pr_finished_at_iso is not None:
        stages["create_pr"] = {"finished_at_iso": create_pr_finished_at_iso}
    state = {
        "schema_version": 1,
        "ticket": ticket,
        "run_id": run_id,
        "backend": "jira",
        "started_at": "2026-05-19T09:00:00Z",
        "stages": stages,
    }
    state_dir = root / ".flow" / "runs" / ticket
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "state.json"
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


_NOW = "2026-05-28T12:00:00Z"
_SINCE = "2026-05-14T00:00:00Z"
_UNTIL = "2026-05-28T12:00:00Z"


def _compute(root: Path, namespace: str = "demo") -> dict:
    return metric.compute(root, namespace, since_iso=_SINCE, until_iso=_UNTIL, now_iso=_NOW)


# ─── default_window ──────────────────────────────────────────────────────────


def test_default_window_floors_since_to_midnight() -> None:
    since, until = metric.default_window(_NOW)
    assert since == "2026-05-14T00:00:00Z"
    assert until == _NOW


def test_default_window_bad_now_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="now"):
        metric.default_window("not-a-date")


# ─── load_ship_events ────────────────────────────────────────────────────────


def test_load_ship_events_skips_dupe_corrupt_intent(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z")
    # dupe / corrupt / intent siblings must be ignored by name.
    _write_ship_event(
        tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z", filename="FT-1.json.dupe.1.json"
    )
    _write_ship_event(
        tmp_path, "FT-2", shipped_at="2026-05-20T10:00:00Z", filename="FT-2.json.corrupt.x.json"
    )
    _write_ship_event(
        tmp_path,
        "FT-3",
        shipped_at="2026-05-20T10:00:00Z",
        filename="FT-3.json.quarantine-intent.20260520T100000Z.json",
    )
    events = metric.load_ship_events(tmp_path, "demo")
    assert [e["ticket"] for e in events] == ["FT-1"]


def test_load_ship_events_quarantines_malformed(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z")
    ship_dir = tmp_path / ".flow" / "demo" / "ship-events"
    (ship_dir / "FT-bad.json").write_text("{not json", encoding="utf-8")
    (ship_dir / "FT-noship.json").write_text(json.dumps({"ticket": "FT-noship"}), encoding="utf-8")
    events = metric.load_ship_events(tmp_path, "demo")
    assert [e["ticket"] for e in events] == ["FT-1"]
    quarantine = tmp_path / ".flow" / "demo" / "ship-events.quarantine"
    assert quarantine.exists()
    assert len(quarantine.read_text(encoding="utf-8").splitlines()) == 2


def test_load_ship_events_no_dir(tmp_path: Path) -> None:
    assert metric.load_ship_events(tmp_path, "demo") == []


# ─── classify_attribution ────────────────────────────────────────────────────


def test_classify_via_flow_when_state_matches(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(
        tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z", observed_by_run_id="run-aaa"
    )
    _write_state(tmp_path, "FT-1", run_id="run-aaa", reflect_status="completed")
    event = metric.load_ship_events(tmp_path, "demo")[0]
    assert metric.classify_attribution(tmp_path, event) == metric.ATTR_VIA_FLOW


def test_classify_not_attributed_run_id_mismatch(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(
        tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z", observed_by_run_id="run-aaa"
    )
    _write_state(tmp_path, "FT-1", run_id="run-zzz", reflect_status="completed")
    event = metric.load_ship_events(tmp_path, "demo")[0]
    assert metric.classify_attribution(tmp_path, event) == metric.ATTR_NOT_ATTRIBUTED


def test_classify_not_attributed_reflect_not_completed(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(
        tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z", observed_by_run_id="run-aaa"
    )
    _write_state(tmp_path, "FT-1", run_id="run-aaa", reflect_status="in_progress")
    event = metric.load_ship_events(tmp_path, "demo")[0]
    assert metric.classify_attribution(tmp_path, event) == metric.ATTR_NOT_ATTRIBUTED


def test_classify_not_attributed_no_reflect_stage(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(
        tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z", observed_by_run_id="run-aaa"
    )
    _write_state(tmp_path, "FT-1", run_id="run-aaa", reflect_status=None)
    event = metric.load_ship_events(tmp_path, "demo")[0]
    assert metric.classify_attribution(tmp_path, event) == metric.ATTR_NOT_ATTRIBUTED


def test_classify_not_attributed_corrupt_state(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z")
    state_dir = tmp_path / ".flow" / "runs" / "FT-1"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text("{broken", encoding="utf-8")
    event = metric.load_ship_events(tmp_path, "demo")[0]
    assert metric.classify_attribution(tmp_path, event) == metric.ATTR_NOT_ATTRIBUTED


# ─── compute: window + attribution mix ───────────────────────────────────────


def test_compute_counts_two_attributions(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    # FT-1: reflect completed + matching run id -> shipped_via_flow
    _write_ship_event(
        tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z", observed_by_run_id="run-1"
    )
    _write_state(tmp_path, "FT-1", run_id="run-1", reflect_status="completed")
    # FT-2: no state -> shipped_backend_not_attributed
    _write_ship_event(tmp_path, "FT-2", shipped_at="2026-05-21T11:00:00Z")
    result = _compute(tmp_path)
    assert result["shipped"] == 2
    assert result[metric.ATTR_VIA_FLOW] == 1
    assert result[metric.ATTR_NOT_ATTRIBUTED] == 1
    by_ticket = {t["ticket"]: t["attribution"] for t in result["tickets"]}
    assert by_ticket["FT-1"] == metric.ATTR_VIA_FLOW
    assert by_ticket["FT-2"] == metric.ATTR_NOT_ATTRIBUTED


def test_compute_excludes_out_of_window(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    # in window
    _write_ship_event(tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z")
    # before since (exclusive lower bound is inclusive at 00:00; this is earlier)
    _write_ship_event(tmp_path, "FT-OLD", shipped_at="2026-05-13T23:59:59Z")
    # at/after until -> excluded (half-open upper bound)
    _write_ship_event(tmp_path, "FT-FUTURE", shipped_at="2026-05-28T12:00:00Z")
    result = _compute(tmp_path)
    assert result["shipped"] == 1
    assert [t["ticket"] for t in result["tickets"]] == ["FT-1"]


def test_compute_window_boundaries_half_open(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    # exactly at since -> included
    _write_ship_event(tmp_path, "FT-SINCE", shipped_at=_SINCE)
    # exactly at until -> excluded
    _write_ship_event(tmp_path, "FT-UNTIL", shipped_at=_UNTIL)
    result = _compute(tmp_path)
    assert [t["ticket"] for t in result["tickets"]] == ["FT-SINCE"]


def test_compute_bad_since_raises(tmp_path: Path) -> None:
    import pytest

    _seed_workspace(tmp_path)
    with pytest.raises(ValueError, match="since"):
        metric.compute(tmp_path, "demo", since_iso="nope", until_iso=_UNTIL, now_iso=_NOW)


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_namespace_required(tmp_path: Path, capsys) -> None:
    rc = metric.cli_main(["tickets-per-week", "--workspace-root", str(tmp_path)])
    assert rc == 1
    assert "namespace is required" in capsys.readouterr().err


def test_cli_happy_prints_json(tmp_path: Path, capsys) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z")
    rc = metric.cli_main(
        [
            "tickets-per-week",
            "--namespace",
            "demo",
            "--workspace-root",
            str(tmp_path),
            "--since",
            "2026-05-14",
            "--until",
            "2026-05-28",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["shipped"] == 1
    assert payload["since"] == "2026-05-14T00:00:00Z"
    assert payload["until"] == "2026-05-28T00:00:00Z"


def test_cli_bad_date_returns_1(tmp_path: Path, capsys) -> None:
    rc = metric.cli_main(["tickets-per-week", "--namespace", "demo", "--since", "not-a-date"])
    assert rc == 1


def test_cli_no_flow_dir_tickets_per_week(tmp_path: Path, capsys) -> None:
    rc = metric.cli_main(
        ["tickets-per-week", "--namespace", "demo", "--workspace-root", str(tmp_path)]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "no .flow" in err
    assert str(tmp_path.resolve()) in err


def test_cli_no_flow_dir_time_to_pr(tmp_path: Path, capsys) -> None:
    rc = metric.cli_main(["time-to-pr", "--namespace", "demo", "--workspace-root", str(tmp_path)])
    assert rc == 1
    assert "no .flow" in capsys.readouterr().err


def test_cli_tpw_output_includes_resolved_workspace_root(tmp_path: Path, capsys) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z")
    rc = metric.cli_main(
        [
            "tickets-per-week",
            "--namespace",
            "demo",
            "--workspace-root",
            str(tmp_path),
            "--since",
            "2026-05-14",
            "--until",
            "2026-05-28",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "resolved_workspace_root" in payload
    assert payload["resolved_workspace_root"] == str(tmp_path.resolve())


# ─── time-to-pr ──────────────────────────────────────────────────────────────


def _compute_ttp(root: Path, namespace: str = "demo") -> dict:
    return metric.compute_time_to_pr(
        root, namespace, since_iso=_SINCE, until_iso=_UNTIL, now_iso=_NOW
    )


def test_ttp_happy_single(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(
        tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z", observed_by_run_id="run-1"
    )
    _write_state(
        tmp_path,
        "FT-1",
        run_id="run-1",
        reflect_status="completed",
        plan_started_at_iso="2026-05-20T00:00:00Z",
        create_pr_finished_at_iso="2026-05-20T12:00:00Z",
    )
    result = _compute_ttp(tmp_path)
    assert result["n_measured"] == 1
    assert result["n_skipped"] == 0
    assert result["median_hours"] == 12.0
    assert result["p90_hours"] == 12.0
    assert result["tickets"][0]["ticket"] == "FT-1"
    assert result["tickets"][0]["time_to_pr_hours"] == 12.0
    assert result["tickets"][0]["plan_started_at"] == "2026-05-20T00:00:00Z"
    assert result["tickets"][0]["create_pr_finished_at"] == "2026-05-20T12:00:00Z"


def test_ttp_attended_split_from_planning_stamp(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    path = _write_stamped_ship_event(
        tmp_path,
        "FT-1",
        shipped_at="2026-05-20T10:00:00Z",
        plan_started="2026-05-20T00:00:00Z",
        create_pr_finished="2026-05-20T12:00:00Z",
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    record["flow_attribution"]["planning_started_at_iso"] = "2026-05-19T23:00:00Z"
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result = _compute_ttp(tmp_path)
    row = result["tickets"][0]
    assert row["time_to_pr_hours"] == 12.0
    assert row["planning_started_at"] == "2026-05-19T23:00:00Z"
    assert row["attended_hours"] == 1.0


def test_ttp_unparseable_planning_stamp_adds_no_attended_keys(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    path = _write_stamped_ship_event(
        tmp_path,
        "FT-1",
        shipped_at="2026-05-20T10:00:00Z",
        plan_started="2026-05-20T00:00:00Z",
        create_pr_finished="2026-05-20T12:00:00Z",
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    record["flow_attribution"]["planning_started_at_iso"] = "not-a-timestamp"
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result = _compute_ttp(tmp_path)
    row = result["tickets"][0]
    assert row["time_to_pr_hours"] == 12.0
    assert "attended_hours" not in row
    assert "planning_started_at" not in row


def test_ttp_excludes_not_attributed(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    # no state.json -> not attributed; must not be measured even with timestamps absent
    _write_ship_event(tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z")
    result = _compute_ttp(tmp_path)
    assert result["n_measured"] == 0
    assert result["n_skipped"] == 0
    assert result["tickets"] == []


def test_ttp_excludes_out_of_window(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(
        tmp_path, "FT-OLD", shipped_at="2026-05-13T23:59:59Z", observed_by_run_id="run-old"
    )
    _write_state(
        tmp_path,
        "FT-OLD",
        run_id="run-old",
        reflect_status="completed",
        plan_started_at_iso="2026-05-13T00:00:00Z",
        create_pr_finished_at_iso="2026-05-13T12:00:00Z",
    )
    result = _compute_ttp(tmp_path)
    assert result["n_measured"] == 0
    assert result["tickets"] == []


def test_ttp_skips_missing_timestamp(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(
        tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z", observed_by_run_id="run-1"
    )
    # attributed but plan.started_at_iso absent -> skip-and-record
    _write_state(
        tmp_path,
        "FT-1",
        run_id="run-1",
        reflect_status="completed",
        create_pr_finished_at_iso="2026-05-20T12:00:00Z",
    )
    result = _compute_ttp(tmp_path)
    assert result["n_measured"] == 0
    assert result["n_skipped"] == 1
    assert result["median_hours"] == 0.0
    assert result["skipped"][0]["ticket"] == "FT-1"
    assert "plan" in result["skipped"][0]["reason"]


def test_ttp_skips_negative_duration(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(
        tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z", observed_by_run_id="run-1"
    )
    # create_pr finishes before plan started -> negative duration -> skip
    _write_state(
        tmp_path,
        "FT-1",
        run_id="run-1",
        reflect_status="completed",
        plan_started_at_iso="2026-05-20T12:00:00Z",
        create_pr_finished_at_iso="2026-05-20T00:00:00Z",
    )
    result = _compute_ttp(tmp_path)
    assert result["n_measured"] == 0
    assert result["n_skipped"] == 1
    assert result["skipped"][0]["ticket"] == "FT-1"


def test_ttp_multi_percentile(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    # FT-A: 10h, FT-B: 20h -> median (p50) == 15.0 via linear interpolation
    _write_ship_event(
        tmp_path, "FT-A", shipped_at="2026-05-20T10:00:00Z", observed_by_run_id="run-a"
    )
    _write_state(
        tmp_path,
        "FT-A",
        run_id="run-a",
        reflect_status="completed",
        plan_started_at_iso="2026-05-20T00:00:00Z",
        create_pr_finished_at_iso="2026-05-20T10:00:00Z",
    )
    _write_ship_event(
        tmp_path, "FT-B", shipped_at="2026-05-21T10:00:00Z", observed_by_run_id="run-b"
    )
    _write_state(
        tmp_path,
        "FT-B",
        run_id="run-b",
        reflect_status="completed",
        plan_started_at_iso="2026-05-21T00:00:00Z",
        create_pr_finished_at_iso="2026-05-21T20:00:00Z",
    )
    result = _compute_ttp(tmp_path)
    assert result["n_measured"] == 2
    assert result["median_hours"] == 15.0
    # sorted by (time_to_pr_hours, ticket)
    assert [t["ticket"] for t in result["tickets"]] == ["FT-A", "FT-B"]
    assert result["tickets"][0]["time_to_pr_hours"] == 10.0
    assert result["tickets"][1]["time_to_pr_hours"] == 20.0


def test_ttp_empty(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    result = _compute_ttp(tmp_path)
    assert result["n_measured"] == 0
    assert result["median_hours"] == 0.0
    assert result["p90_hours"] == 0.0
    assert result["tickets"] == []


def test_ttp_non_dict_plan_stage_skips_not_crashes(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(
        tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z", observed_by_run_id="run-1"
    )
    # attributed via flow, but state carries a null `plan` stage (not a dict)
    state = {
        "schema_version": 1,
        "ticket": "FT-1",
        "run_id": "run-1",
        "backend": "jira",
        "stages": {
            "reflect": {"status": "completed"},
            "plan": None,
            "create_pr": {"finished_at_iso": "2026-05-20T12:00:00Z"},
        },
    }
    state_dir = tmp_path / ".flow" / "runs" / "FT-1"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    result = _compute_ttp(tmp_path)
    assert result["n_measured"] == 0
    assert result["n_skipped"] == 1


def test_ttp_cli_happy(tmp_path: Path, capsys) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(
        tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z", observed_by_run_id="run-1"
    )
    _write_state(
        tmp_path,
        "FT-1",
        run_id="run-1",
        reflect_status="completed",
        plan_started_at_iso="2026-05-20T00:00:00Z",
        create_pr_finished_at_iso="2026-05-20T12:00:00Z",
    )
    rc = metric.cli_main(
        [
            "time-to-pr",
            "--namespace",
            "demo",
            "--workspace-root",
            str(tmp_path),
            "--since",
            "2026-05-14",
            "--until",
            "2026-05-28",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["n_measured"] == 1
    assert payload["median_hours"] == 12.0


def test_ttp_cli_namespace_required(tmp_path: Path, capsys) -> None:
    rc = metric.cli_main(["time-to-pr"])
    assert rc == 1
    assert "namespace" in capsys.readouterr().err


# ─── flow_attribution stamp (forward-only, state.json reaped) ─────────────────


def _write_stamped_ship_event(
    root: Path,
    ticket: str,
    *,
    shipped_at: str,
    plan_started: str,
    create_pr_finished: str,
    observed_by_run_id: str = "abcdef0123456789",
    namespace: str = "demo",
) -> Path:
    record = {
        "ticket": ticket,
        "shipped_at": shipped_at,
        "evidence": {"merged": True},
        "observed_at": "2026-05-20T10:00:00Z",
        "observed_by_run_id": observed_by_run_id,
        "flow_attribution": {
            "plan_started_at_iso": plan_started,
            "create_pr_finished_at_iso": create_pr_finished,
        },
    }
    ship_dir = root / ".flow" / namespace / "ship-events"
    ship_dir.mkdir(parents=True, exist_ok=True)
    path = ship_dir / f"{ticket}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def test_classify_via_flow_from_stamp_no_state(tmp_path: Path) -> None:
    """A well-formed stamp attributes via-flow with NO state.json on disk."""
    _seed_workspace(tmp_path)
    _write_stamped_ship_event(
        tmp_path,
        "FT-1",
        shipped_at="2026-05-20T10:00:00Z",
        plan_started="2026-05-20T00:00:00Z",
        create_pr_finished="2026-05-20T12:00:00Z",
    )
    event = metric.load_ship_events(tmp_path, "demo")[0]
    assert metric.classify_attribution(tmp_path, event) == metric.ATTR_VIA_FLOW


def test_classify_malformed_stamp_falls_back_not_attributed(tmp_path: Path) -> None:
    """A stamp with an unparseable iso field falls back to the legacy join (no state -> not
    attributed)."""
    _seed_workspace(tmp_path)
    _write_stamped_ship_event(
        tmp_path,
        "FT-1",
        shipped_at="2026-05-20T10:00:00Z",
        plan_started="not-a-date",
        create_pr_finished="2026-05-20T12:00:00Z",
    )
    event = metric.load_ship_events(tmp_path, "demo")[0]
    assert metric.classify_attribution(tmp_path, event) == metric.ATTR_NOT_ATTRIBUTED


def test_ttp_measures_from_stamp_no_state(tmp_path: Path) -> None:
    """REGRESSION GUARD: a stamped event with NO state.json must MEASURE from the stamp.

    Without the restructure of the unconditional state.json read, this raises
    FileNotFoundError and aborts the whole command.
    """
    _seed_workspace(tmp_path)
    _write_stamped_ship_event(
        tmp_path,
        "FT-1",
        shipped_at="2026-05-20T10:00:00Z",
        plan_started="2026-05-20T00:00:00Z",
        create_pr_finished="2026-05-20T12:00:00Z",
    )
    result = _compute_ttp(tmp_path)
    assert result["n_measured"] == 1
    assert result["n_skipped"] == 0
    assert result["median_hours"] == 12.0
    assert result["tickets"][0]["ticket"] == "FT-1"
    assert result["tickets"][0]["time_to_pr_hours"] == 12.0
    assert result["tickets"][0]["plan_started_at"] == "2026-05-20T00:00:00Z"
    assert result["tickets"][0]["create_pr_finished_at"] == "2026-05-20T12:00:00Z"


# ─── percentile() ────────────────────────────────────────────────────────────


def test_percentile_median_of_odd_list() -> None:
    assert metric.percentile([10, 20, 30, 40, 50], 50) == 30.0


def test_percentile_p90_interpolated() -> None:
    # rank = 4 * 0.9 = 3.6 -> 40 + 0.6 * (50 - 40) = 46
    assert metric.percentile([10, 20, 30, 40, 50], 90) == 46.0


def test_percentile_empty_returns_zero() -> None:
    assert metric.percentile([], 50) == 0.0


def test_percentile_single_element() -> None:
    assert metric.percentile([7.5], 90) == 7.5


def test_percentile_pct_100_no_index_error() -> None:
    assert metric.percentile([1.0, 2.0, 3.0], 100) == 3.0


def test_percentile_does_not_mutate_caller_list() -> None:
    values = [30.0, 10.0, 20.0]
    metric.percentile(values, 50)
    assert values == [30.0, 10.0, 20.0]
