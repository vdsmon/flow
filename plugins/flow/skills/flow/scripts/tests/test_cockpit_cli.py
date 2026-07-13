from __future__ import annotations

import json
from pathlib import Path

import cockpit_cli


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _evidence() -> dict[str, object]:
    return {
        "runs": [
            {
                "ticket": "FT-2",
                "next_or_blocked": "plan:pending",
                "lease": "free",
                "completed": 1,
                "total_stages": 7,
            }
        ],
        "deferred": [{"target": "FT-3", "question": "Choose the API", "state": "deferred"}],
        "pending": [{"target": "FT-4", "operation": "transition"}],
        "feedback": [{"target": "FT-5", "pr": "17", "actionable_count": 2}],
        "maintenance": [
            {
                "label": "nightly schedule",
                "detail": "last fire failed",
                "next_command": "FLOW maintain evolution audit",
            }
        ],
    }


def test_render_constructs_cockpit_dataclasses_and_emits_text(tmp_path: Path, capsys) -> None:
    evidence = _write(tmp_path / "cockpit.json", _evidence())

    assert cockpit_cli.cli_main(["render", "--evidence", str(evidence)]) == 0
    output = capsys.readouterr().out
    assert "Needs attention" in output
    assert "Active" in output
    assert "FLOW pr:17" in output
    assert "Maintainer health" in output
    assert output.endswith("\n")


def test_render_json_is_compact_sorted_snapshot(tmp_path: Path, capsys) -> None:
    evidence = _write(tmp_path / "cockpit.json", _evidence())

    assert cockpit_cli.cli_main(["render", "--evidence", str(evidence), "--json"]) == 0
    output = capsys.readouterr().out
    assert "\n" not in output.rstrip("\n")
    snapshot = json.loads(output)
    assert snapshot["pending_mutations"] == 1
    assert snapshot["pending_targets"] == ["FT-4"]
    assert snapshot["next_commands"][0] == 'FLOW FT-3 --request "<answer>"'


def test_render_rejects_relative_or_invalid_evidence_with_structured_error(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "relative.json", _evidence())
    assert cockpit_cli.cli_main(["render", "--evidence", "relative.json"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_evidence"

    payload = _evidence()
    payload["feedback"] = [{"target": "FT-5", "pr": "17"}]
    evidence = _write(tmp_path / "invalid.json", payload)
    assert cockpit_cli.cli_main(["render", "--evidence", str(evidence)]) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == "invalid_evidence"
    assert "actionable_count" in error["error"]["message"]


def test_render_reports_argument_errors_as_structured_json(capsys) -> None:
    assert cockpit_cli.cli_main(["render"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_evidence"
