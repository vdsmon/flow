from __future__ import annotations

import json
from datetime import UTC, datetime

import run_report as rr
import state


def _state(*, active: bool = False) -> state.TicketState:
    return state.TicketState(
        schema_version=1,
        ticket="flow-x",
        run_id="run-1",
        backend="beads",
        started_at="2026-07-13T10:00:00+00:00",
        stages={
            "plan": state.StageRecord(
                status="completed",
                started_at_iso="2026-07-13T10:00:00+00:00",
                finished_at_iso="2026-07-13T10:10:00+00:00",
            ),
            "implement": state.StageRecord(
                status="in_progress" if active else "completed",
                started_at_iso="2026-07-13T10:40:00+00:00",
                finished_at_iso=None if active else "2026-07-13T11:00:00+00:00",
            ),
        },
    )


def test_analyze_ranks_between_stage_wait_without_assigning_blame():
    report = rr.analyze(
        _state(),
        [
            {
                "run_id": "run-1",
                "ts": "2026-07-13T10:45:00Z",
                "stage": "implement",
                "type": "RETRY",
                "severity": "minor",
                "body": "first test command used the wrong target",
            },
            {
                "run_id": "other-run",
                "stage": "plan",
                "type": "BLOCKER",
                "body": "unrelated",
            },
        ],
        now=datetime(2026, 7, 13, 12, tzinfo=UTC),
    )

    assert report["total_seconds"] == 3600
    assert report["top_time"][0] == {
        "kind": "gap",
        "label": "wait after plan before implement",
        "seconds": 1800,
        "percent": 50.0,
    }
    assert [item["seconds"] for item in report["top_time"]] == [1800, 1200, 600]
    assert report["friction"]["count"] == 1
    assert report["friction"]["by_type"] == {"RETRY": 1}


def test_active_stage_uses_now_as_its_effective_end():
    report = rr.analyze(
        _state(active=True),
        [],
        now=datetime(2026, 7, 13, 11, 10, tzinfo=UTC),
    )

    implement = next(row for row in report["stages"] if row["stage"] == "implement")
    assert report["total_seconds"] == 4200
    assert implement["seconds"] == 1800


def test_render_text_is_concise_when_no_friction_was_recorded():
    report = rr.analyze(
        _state(),
        [],
        now=datetime(2026, 7, 13, 12, tzinfo=UTC),
    )

    rendered = rr.render_text(report)

    assert "Run time: 1h 0m" in rendered
    assert "wait after plan before implement: 30m 0s (50.0%)" in rendered
    assert "Friction: none recorded." in rendered


def test_cli_reads_run_scoped_friction_and_publishes_receipt(tmp_path, capsys):
    workspace = tmp_path
    ticket_dir = workspace / ".flow" / "runs" / "flow-x"
    state._write(ticket_dir, _state())
    (workspace / ".flow" / "workspace.toml").write_text(
        '[memory]\nnamespace = "flow"\n', encoding="utf-8"
    )
    friction_path = workspace / ".flow" / "flow" / "friction.jsonl"
    friction_path.parent.mkdir(parents=True)
    friction_path.write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "ts": "2026-07-13T10:45:00Z",
                "stage": "implement",
                "type": "RECONCILE",
                "severity": "minor",
                "body": "one planned file was missing",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output = ticket_dir / "run-report.json"

    rc = rr.cli_main(
        [
            "--workspace-root",
            str(workspace),
            "--ticket-dir",
            str(ticket_dir),
            "--json",
            "--output",
            str(output),
        ]
    )

    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert printed == persisted
    assert printed["friction"]["by_stage"] == {"implement": 1}
