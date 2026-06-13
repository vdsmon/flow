"""Tests for metric.py arm-compare — per-arm flow-vs-control comparison.

Ship-events partition on the `arm` field (absent -> "flow"; legacy events read as
flow). Per arm {flow, control} the compute renders median_time_to_pr_hours,
interventions_per_pr, completion_rate, and reverts, then a pre-registered verdict
(flow wins iff it takes >=2 of the three axes, with a GUARD override: any flow-arm
revert with zero control-arm reverts forces flow_wins=false).

The history seam (`metric._status_history`) is monkeypatched with in-memory fakes
keyed by ticket, so these tests never shell `bd`.
"""

from __future__ import annotations

import json
from pathlib import Path

import _memory_paths
import metric


def _seed_workspace(root: Path, namespace: str = "demo", backend: str = "beads") -> None:
    flow = root / ".flow"
    (flow / namespace).mkdir(parents=True, exist_ok=True)
    (flow / ".initialized").write_text("", encoding="utf-8")
    (flow / "workspace.toml").write_text(
        f'[tracker]\nbackend = "{backend}"\n\n[memory]\nnamespace = "{namespace}"\n',
        encoding="utf-8",
    )


def _write_ship_event(
    root: Path,
    ticket: str,
    shipped_at: str,
    *,
    namespace: str = "demo",
    arm: str | None = None,
    stamp: tuple[str, str] | None = None,
    evidence: dict | None = None,
) -> None:
    sdir = _memory_paths.ship_events_dir(root, namespace)
    sdir.mkdir(parents=True, exist_ok=True)
    event: dict = {
        "ticket": ticket,
        "shipped_at": shipped_at,
        "observed_by_run_id": f"run-{ticket}",
    }
    if arm is not None:
        event["arm"] = arm
    if stamp is not None:
        event["flow_attribution"] = {
            "plan_started_at_iso": stamp[0],
            "create_pr_finished_at_iso": stamp[1],
        }
    if evidence is not None:
        event["evidence"] = evidence
    (sdir / f"{ticket}.json").write_text(json.dumps(event, sort_keys=True), encoding="utf-8")


def _patch_history(monkeypatch, histories: dict[str, list[tuple[str, str]] | None]) -> None:
    def fake(workspace_root, namespace, ticket):
        snaps = histories.get(ticket)
        if snaps is None:
            return None
        from _timeutil import parse_iso

        out = []
        for iso, status in snaps:
            dt = parse_iso(iso)
            if dt is not None:
                out.append((dt, status))
        out.sort(key=lambda p: p[0])
        return out

    monkeypatch.setattr(metric, "_status_history", fake)


SINCE = "2026-06-01T00:00:00Z"
UNTIL = "2026-06-08T00:00:00Z"


def _compute(root: Path, namespace: str = "demo") -> dict:
    return metric.compute_arm_compare(root, namespace, since_iso=SINCE, until_iso=UNTIL)


def test_absent_arm_buckets_as_flow(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    _patch_history(monkeypatch, {})
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z")  # no arm -> flow
    _write_ship_event(tmp_path, "FT-2", "2026-06-03T00:00:00Z", arm="control")
    result = _compute(tmp_path)
    assert result["total_ship_events"] == 2
    # FT-1 counts as a flow-arm event; FT-2 as control.
    assert result["flow"]["n_events"] == 1
    assert result["control"]["n_events"] == 1


def test_time_to_pr_flow_from_stamp_control_from_evidence(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    _patch_history(monkeypatch, {})
    # flow: stamp 10:00 -> 12:00 == 2h
    _write_ship_event(
        tmp_path,
        "FT-1",
        "2026-06-03T00:00:00Z",
        stamp=("2026-06-03T10:00:00Z", "2026-06-03T12:00:00Z"),
    )
    # control: evidence start 08:00 -> pr 14:00 == 6h
    _write_ship_event(
        tmp_path,
        "FT-2",
        "2026-06-03T00:00:00Z",
        arm="control",
        evidence={"start_ts": "2026-06-03T08:00:00Z", "pr_ts": "2026-06-03T14:00:00Z"},
    )
    result = _compute(tmp_path)
    assert result["flow"]["median_time_to_pr_hours"] == 2.0
    assert result["control"]["median_time_to_pr_hours"] == 6.0


def test_time_to_pr_median_over_multiple(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    _patch_history(monkeypatch, {})
    _write_ship_event(
        tmp_path,
        "FT-1",
        "2026-06-03T00:00:00Z",
        stamp=("2026-06-03T10:00:00Z", "2026-06-03T11:00:00Z"),  # 1h
    )
    _write_ship_event(
        tmp_path,
        "FT-2",
        "2026-06-03T00:00:00Z",
        stamp=("2026-06-03T10:00:00Z", "2026-06-03T13:00:00Z"),  # 3h
    )
    _write_ship_event(
        tmp_path,
        "FT-3",
        "2026-06-03T00:00:00Z",
        stamp=("2026-06-03T10:00:00Z", "2026-06-03T15:00:00Z"),  # 5h
    )
    result = _compute(tmp_path)
    assert result["flow"]["median_time_to_pr_hours"] == 3.0


def test_time_to_pr_missing_and_negative_skipped(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    _patch_history(monkeypatch, {})
    # good 2h
    _write_ship_event(
        tmp_path,
        "FT-1",
        "2026-06-03T00:00:00Z",
        stamp=("2026-06-03T10:00:00Z", "2026-06-03T12:00:00Z"),
    )
    # negative duration -> skip
    _write_ship_event(
        tmp_path,
        "FT-2",
        "2026-06-03T00:00:00Z",
        stamp=("2026-06-03T12:00:00Z", "2026-06-03T10:00:00Z"),
    )
    # no stamp / no evidence ts -> skip
    _write_ship_event(tmp_path, "FT-3", "2026-06-03T00:00:00Z")
    result = _compute(tmp_path)
    assert result["flow"]["median_time_to_pr_hours"] == 2.0
    assert len(result["flow"]["time_to_pr_skipped"]) == 2


def test_time_to_pr_null_when_none_measurable(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    _patch_history(monkeypatch, {})
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z")  # no ts
    result = _compute(tmp_path)
    assert result["flow"]["median_time_to_pr_hours"] is None


def test_interventions_per_pr_mean(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    _patch_history(monkeypatch, {})
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z", evidence={"interventions": 1})
    _write_ship_event(tmp_path, "FT-2", "2026-06-03T00:00:00Z", evidence={"interventions": 3})
    # absent field -> excluded from denominator
    _write_ship_event(tmp_path, "FT-3", "2026-06-03T00:00:00Z")
    result = _compute(tmp_path)
    assert result["flow"]["interventions_per_pr"] == 2.0  # (1+3)/2


def test_interventions_null_when_none_carry(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    _patch_history(monkeypatch, {})
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z")
    result = _compute(tmp_path)
    assert result["flow"]["interventions_per_pr"] is None


def test_completion_rate(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    _patch_history(monkeypatch, {})
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z", evidence={"outcome": "merged"})
    _write_ship_event(tmp_path, "FT-2", "2026-06-03T00:00:00Z", evidence={"outcome": "merged"})
    _write_ship_event(tmp_path, "FT-3", "2026-06-03T00:00:00Z", evidence={"outcome": "abandoned"})
    # field absent / unknown value -> excluded
    _write_ship_event(tmp_path, "FT-4", "2026-06-03T00:00:00Z", evidence={"outcome": "weird"})
    _write_ship_event(tmp_path, "FT-5", "2026-06-03T00:00:00Z")
    result = _compute(tmp_path)
    assert result["flow"]["completion_rate"] == 2 / 3


def test_completion_rate_null_when_none_carry(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    _patch_history(monkeypatch, {})
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z")
    result = _compute(tmp_path)
    assert result["flow"]["completion_rate"] is None


def test_verdict_axis_time_to_pr_favors_flow(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    _patch_history(monkeypatch, {})
    # flow faster (2h) than control (6h); other axes undecidable
    _write_ship_event(
        tmp_path,
        "FT-1",
        "2026-06-03T00:00:00Z",
        stamp=("2026-06-03T10:00:00Z", "2026-06-03T12:00:00Z"),
    )
    _write_ship_event(
        tmp_path,
        "FT-2",
        "2026-06-03T00:00:00Z",
        arm="control",
        evidence={"start_ts": "2026-06-03T08:00:00Z", "pr_ts": "2026-06-03T14:00:00Z"},
    )
    result = _compute(tmp_path)
    v = result["verdict"]
    assert v["time_to_pr"] == "flow"
    assert v["interventions_per_pr"] is None
    assert v["completion_rate"] is None
    assert v["favored_flow_count"] == 1
    assert v["flow_wins"] is False


def test_verdict_undecidable_axis_not_counted(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    _patch_history(monkeypatch, {})
    # interventions decidable for flow only -> axis undecidable
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z", evidence={"interventions": 0})
    _write_ship_event(
        tmp_path,
        "FT-2",
        "2026-06-03T00:00:00Z",
        arm="control",
        evidence={"start_ts": "2026-06-03T08:00:00Z", "pr_ts": "2026-06-03T09:00:00Z"},
    )
    result = _compute(tmp_path)
    assert result["verdict"]["interventions_per_pr"] is None


def test_verdict_flow_wins_two_axes(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    _patch_history(monkeypatch, {})
    # flow: 2h, 1 intervention, merged
    _write_ship_event(
        tmp_path,
        "FT-1",
        "2026-06-03T00:00:00Z",
        stamp=("2026-06-03T10:00:00Z", "2026-06-03T12:00:00Z"),
        evidence={"interventions": 1, "outcome": "merged"},
    )
    # control: 6h, 5 interventions, abandoned -> flow wins time + interventions + completion
    _write_ship_event(
        tmp_path,
        "FT-2",
        "2026-06-03T00:00:00Z",
        arm="control",
        evidence={
            "start_ts": "2026-06-03T08:00:00Z",
            "pr_ts": "2026-06-03T14:00:00Z",
            "interventions": 5,
            "outcome": "abandoned",
        },
    )
    result = _compute(tmp_path)
    v = result["verdict"]
    assert v["time_to_pr"] == "flow"
    assert v["interventions_per_pr"] == "flow"
    assert v["completion_rate"] == "flow"
    assert v["favored_flow_count"] == 3
    assert v["flow_wins"] is True
    assert v["guard_triggered"] is False


def test_guard_override_flow_revert_control_clean(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    # flow wins all measurable axes but has a revert; control has none -> guard
    _write_ship_event(
        tmp_path,
        "FT-1",
        "2026-06-03T00:00:00Z",
        stamp=("2026-06-03T10:00:00Z", "2026-06-03T12:00:00Z"),
        evidence={"interventions": 1, "outcome": "merged"},
    )
    _write_ship_event(
        tmp_path,
        "FT-2",
        "2026-06-03T00:00:00Z",
        arm="control",
        evidence={
            "start_ts": "2026-06-03T08:00:00Z",
            "pr_ts": "2026-06-03T14:00:00Z",
            "interventions": 5,
            "outcome": "abandoned",
        },
    )
    _patch_history(
        monkeypatch,
        {
            # flow bead reopened + reclosed after ship -> revert
            "FT-1": [
                ("2026-06-03T00:00:00Z", "closed"),
                ("2026-06-04T00:00:00Z", "open"),
                ("2026-06-05T00:00:00Z", "closed"),
            ],
            # control clean
            "FT-2": [
                ("2026-06-03T00:00:00Z", "closed"),
                ("2026-06-04T00:00:00Z", "closed"),
            ],
        },
    )
    result = _compute(tmp_path)
    assert result["flow"]["reverts"] == 1
    assert result["control"]["reverts"] == 0
    v = result["verdict"]
    assert v["guard_triggered"] is True
    assert v["flow_wins"] is False


def test_reverts_undecidable_skipped(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z")
    _patch_history(monkeypatch, {"FT-1": None})  # history_unavailable
    result = _compute(tmp_path)
    assert result["flow"]["reverts"] == 0
    assert len(result["flow"]["reverts_skipped"]) == 1


def test_h8s7_empty_corpus_fails_loud(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_workspace(tmp_path)
    _patch_history(monkeypatch, {})
    rc = metric.cli_main(
        [
            "arm-compare",
            "--namespace",
            "demo",
            "--workspace-root",
            str(tmp_path),
            "--since",
            "2026-06-01",
            "--until",
            "2026-06-08",
        ]
    )
    assert rc != 0
    err = capsys.readouterr().err
    ship_dir = str(_memory_paths.ship_events_dir(tmp_path, "demo"))
    assert ship_dir in err


def test_h8s7_stamps_resolved_root_and_total(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_workspace(tmp_path)
    _patch_history(monkeypatch, {})
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z")
    rc = metric.cli_main(
        [
            "arm-compare",
            "--namespace",
            "demo",
            "--workspace-root",
            str(tmp_path),
            "--since",
            "2026-06-01",
            "--until",
            "2026-06-08",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["resolved_workspace_root"] == str(tmp_path.resolve())
    assert payload["total_ship_events"] == 1


def test_cli_namespace_required(tmp_path: Path, capsys) -> None:
    rc = metric.cli_main(["arm-compare", "--workspace-root", str(tmp_path)])
    assert rc == 1
    assert "namespace" in capsys.readouterr().err


def test_cli_no_flow_dir(tmp_path: Path, capsys) -> None:
    rc = metric.cli_main(["arm-compare", "--namespace", "demo", "--workspace-root", str(tmp_path)])
    assert rc == 1
    assert "no .flow" in capsys.readouterr().err
