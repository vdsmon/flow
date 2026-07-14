from __future__ import annotations

import json
from pathlib import Path

import public_commands_cli


def _write_jira_workspace(root: Path, project_key: str = "FT") -> None:
    (root / ".flow").mkdir(parents=True)
    (root / ".flow" / "workspace.toml").write_text(
        f'[tracker]\nbackend = "jira"\n[tracker.jira]\nproject_key = "{project_key}"\n',
        encoding="utf-8",
    )


def test_route_emits_deterministic_command_contract(tmp_path, capsys) -> None:
    _write_jira_workspace(tmp_path)
    rc = public_commands_cli.cli_main(
        [
            "route",
            "--workspace-root",
            str(tmp_path),
            "--",
            "FT-12",
            "--verify",
            "full",
        ]
    )

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {
        "command_id": "target",
        "effect": "confirm",
        "kind": "command",
        "options": ["--verify"],
        "option_values": {"--verify": ["full"]},
        "positionals": ["FT-12"],
        "reference": "references/command-target.md",
        "topic": None,
        "workspace": "required",
    }


def test_route_payload_preserves_repeated_agent_route_values(tmp_path, capsys) -> None:
    _write_jira_workspace(tmp_path)
    rc = public_commands_cli.cli_main(
        [
            "route",
            "--workspace-root",
            str(tmp_path),
            "--",
            "FT-12",
            "--route",
            "planner=codex,gpt-5.6-sol,xhigh",
            "--route=implementer=claude_code,sonnet,high",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["options"] == ["--route", "--route"]
    assert payload["option_values"] == {
        "--route": [
            "planner=codex,gpt-5.6-sol,xhigh",
            "implementer=claude_code,sonnet,high",
        ]
    }


def test_route_emits_scoped_help_contract(capsys) -> None:
    assert public_commands_cli.cli_main(["route", "--", "help", "memory"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "command_id": "help",
        "effect": "read",
        "kind": "help",
        "options": [],
        "option_values": {},
        "positionals": ["memory"],
        "reference": "references/command-target.md",
        "topic": "memory",
        "workspace": "none",
    }


def test_route_accepts_explicit_empty_token_list_as_cockpit(capsys) -> None:
    assert public_commands_cli.cli_main(["route", "--"]) == 0
    assert json.loads(capsys.readouterr().out)["command_id"] == "cockpit"


def test_route_rejects_removed_command(capsys) -> None:
    rc = public_commands_cli.cli_main(["route", "--", "resume"])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "unknown command or target 'resume'" in captured.err


def test_route_requires_public_tokens_after_separator(capsys) -> None:
    rc = public_commands_cli.cli_main(["route"])
    assert rc == 2
    assert "pass public command tokens after --" in capsys.readouterr().err


def test_route_derives_jira_and_beads_patterns_from_workspace(tmp_path, capsys) -> None:
    jira = tmp_path / "jira"
    (jira / ".flow").mkdir(parents=True)
    (jira / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "jira"\n[tracker.jira]\nproject_key = "MY-PROJ"\n',
        encoding="utf-8",
    )
    assert (
        public_commands_cli.cli_main(["route", "--workspace-root", str(jira), "--", "MY-PROJ-42"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["command_id"] == "target"

    beads = tmp_path / "beads"
    (beads / ".flow").mkdir(parents=True)
    (beads / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "beads"\n[tracker.beads]\nprefix = "flow"\n',
        encoding="utf-8",
    )
    assert (
        public_commands_cli.cli_main(["route", "--workspace-root", str(beads), "--", "flow-a1ti.2"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["command_id"] == "target"


def test_route_rejects_relative_workspace(tmp_path, capsys) -> None:
    del tmp_path
    assert public_commands_cli.cli_main(["route", "--workspace-root", ".", "--", "FT-1"]) == 2
    assert "absolute path" in capsys.readouterr().err


def test_help_renders_logical_flow_and_scoped_topic(capsys) -> None:
    assert public_commands_cli.cli_main(["help", "memory"]) == 0
    output = capsys.readouterr().out
    assert "Flow memory commands" in output
    assert "FLOW memory search [<query>]" in output
    assert "\n  FLOW\n" not in output
    assert "FLOW ticket create" not in output
    assert "/flow" not in output
    assert "$flow:flow" not in output


def test_help_rejects_unknown_topic_without_argparse_exit(capsys) -> None:
    assert public_commands_cli.cli_main(["help", "recall"]) == 2
    assert "unknown help topic 'recall'" in capsys.readouterr().err
