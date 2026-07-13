from __future__ import annotations

import json
from pathlib import Path

import lifecycle_cli


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _fresh() -> dict[str, object]:
    return {
        "target_exists": True,
        "ticket_state": "open",
        "run_state": "none",
        "lease_state": "free",
        "pr_state": "none",
    }


def test_reduce_reads_normalized_evidence_and_emits_compact_json(tmp_path: Path, capsys) -> None:
    evidence = _write(tmp_path / "evidence.json", _fresh())

    assert lifecycle_cli.cli_main(["reduce", "--evidence", str(evidence)]) == 0
    assert capsys.readouterr().out == '{"action":"start"}\n'


def test_reduce_surfaces_unknown_target_as_structured_error(tmp_path: Path, capsys) -> None:
    payload = _fresh()
    payload["target_exists"] = False
    evidence = _write(tmp_path / "evidence.json", payload)

    assert lifecycle_cli.cli_main(["reduce", "--evidence", str(evidence)]) == 3
    error = json.loads(capsys.readouterr().err)
    assert error == {"error": {"code": "unknown_target", "message": "target does not exist"}}


def test_reduce_rejects_relative_malformed_or_non_normalized_evidence(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "relative.json", _fresh())
    assert lifecycle_cli.cli_main(["reduce", "--evidence", "relative.json"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_evidence"

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    assert lifecycle_cli.cli_main(["reduce", "--evidence", str(malformed)]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_evidence"

    invalid = _fresh()
    invalid["run_state"] = "maybe"
    path = _write(tmp_path / "invalid.json", invalid)
    assert lifecycle_cli.cli_main(["reduce", "--evidence", str(path)]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_evidence"


def test_reduce_rejects_invalid_request_as_structured_error(tmp_path: Path, capsys) -> None:
    payload = _fresh()
    payload.update(run_state="healthy", request=True, scope_approved=True)
    evidence = _write(tmp_path / "evidence.json", payload)

    assert lifecycle_cli.cli_main(["reduce", "--evidence", str(evidence)]) == 4
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == "invalid_request"
    assert "approved scope" in error["error"]["message"]


def test_reduce_reports_argument_errors_as_structured_json(capsys) -> None:
    assert lifecycle_cli.cli_main(["reduce"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_evidence"


def test_coordinate_emits_closed_multi_target_disposition(tmp_path: Path, capsys) -> None:
    groupability = _write(
        tmp_path / "groupability.json",
        {
            "targets": [
                {"key": "FT-1", "live": True, "epic": False},
                {"key": "FT-2", "live": True, "epic": False},
            ],
            "coupling_verified": True,
        },
    )
    rc = lifecycle_cli.cli_main(
        [
            "coordinate",
            "--action",
            "start",
            "--action",
            "start",
            "--together",
            "--groupability-evidence",
            str(groupability),
        ]
    )

    assert rc == 0
    assert capsys.readouterr().out == '{"disposition":"together"}\n'


def test_coordinate_rejects_unattended_ambiguity(capsys) -> None:
    rc = lifecycle_cli.cli_main(
        ["coordinate", "--action", "start", "--action", "start", "--unattended"]
    )

    assert rc == 4
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == "invalid_request"
    assert "--together" in error["error"]["message"]


def test_coordinate_rejects_missing_or_malformed_groupability(tmp_path: Path, capsys) -> None:
    rc = lifecycle_cli.cli_main(
        ["coordinate", "--action", "start", "--action", "start", "--together"]
    )
    assert rc == 4
    assert "groupability evidence" in capsys.readouterr().err

    malformed = _write(
        tmp_path / "groupability.json",
        {"targets": [{"key": "FT-1", "live": True}], "coupling_verified": True},
    )
    rc = lifecycle_cli.cli_main(
        [
            "coordinate",
            "--action",
            "start",
            "--action",
            "start",
            "--together",
            "--groupability-evidence",
            str(malformed),
        ]
    )
    assert rc == 2
    assert "invalid_evidence" in capsys.readouterr().err
