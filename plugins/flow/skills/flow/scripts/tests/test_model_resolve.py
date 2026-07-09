"""Contract tests for model_resolve.py: the per-stage model resolver.

Disposition is ON BY DEFAULT: a full-lane run downshifts each routable stage
(implement, e2e, code_review, review_loop) to `sonnet` unless the workspace pins a
per-stage model, sets the deprecated `[models] work_model` fallback, or opts out
(model in OFF_VALUES). A non-routable stage (e.g. `plan`) always inherits the session.
"express"/"light" always skip; any read error fails open (prints nothing).
"""

from __future__ import annotations

from pathlib import Path

import model_resolve

ROUTABLE = ("implement", "e2e", "code_review", "review_loop")


def _make_workspace(
    tmp_path: Path,
    *,
    lane: str | None = None,
    models_lines: list[str] | None = None,
    frontmatter: bool = True,
) -> Path:
    flow = tmp_path / ".flow"
    (flow / "tickets").mkdir(parents=True)

    lines = ["[tracker]", 'backend = "beads"', "", "[tracker.beads]", 'prefix = "test"']
    if models_lines is not None:
        lines += ["", "[models]", *models_lines]
    (flow / "workspace.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    if frontmatter:
        fm = ["+++", 'ticket = "test-1"', 'status = "in_progress"']
        if lane is not None:
            fm.append(f'lane = "{lane}"')
        fm += ["+++", "", "plan body"]
        (flow / "tickets" / "test-1.md").write_text("\n".join(fm) + "\n", encoding="utf-8")
    return tmp_path


def _resolve(ws: Path, stage: str) -> str:
    return model_resolve.resolve_stage_model(ws, "test-1", stage)


def test_default_on_all_routable_stages(tmp_path: Path) -> None:
    # no [models] block -> the default is on -> every routable stage is sonnet.
    ws = _make_workspace(tmp_path, models_lines=None)
    for stage in ROUTABLE:
        assert _resolve(ws, stage) == "sonnet", stage


def test_non_routable_stage_inherits(tmp_path: Path) -> None:
    # plan is not routable -> always empty (inherit the session), even with work_model set.
    ws = _make_workspace(tmp_path, models_lines=['work_model = "opus"'])
    assert _resolve(ws, "plan") == ""


def test_lane_full_defaults(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, lane="full", models_lines=None)
    assert _resolve(ws, "implement") == "sonnet"


def test_empty_models_block_defaults(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, models_lines=[])
    assert _resolve(ws, "e2e") == "sonnet"


def test_work_model_fallback_applies_to_all_routable(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, models_lines=['work_model = "opus"'])
    for stage in ROUTABLE:
        assert _resolve(ws, stage) == "opus", stage


def test_per_stage_pin_wins_over_work_model(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, models_lines=['work_model = "opus"', 'e2e = "sonnet"'])
    assert _resolve(ws, "implement") == "opus"  # falls back to work_model
    assert _resolve(ws, "e2e") == "sonnet"  # per-stage override wins


def test_per_stage_pin_without_work_model(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, models_lines=['implement = "opus"'])
    assert _resolve(ws, "implement") == "opus"  # explicit pin
    assert _resolve(ws, "e2e") == "sonnet"  # unset stage keeps the built-in default


def test_per_stage_opt_out(tmp_path: Path) -> None:
    # a per-stage OFF_VALUE opts that stage out; others keep the default.
    ws = _make_workspace(tmp_path, models_lines=['e2e = "off"'])
    assert _resolve(ws, "e2e") == ""
    assert _resolve(ws, "implement") == "sonnet"


def test_per_stage_opt_out_beats_work_model(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, models_lines=['work_model = "opus"', 'e2e = "none"'])
    assert _resolve(ws, "e2e") == ""
    assert _resolve(ws, "implement") == "opus"


def test_work_model_opt_out_disables_all_routable(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, models_lines=['work_model = "off"'])
    for stage in ROUTABLE:
        assert _resolve(ws, stage) == "", stage


def test_lane_express_skips(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, lane="express", models_lines=None)
    assert _resolve(ws, "implement") == ""


def test_lane_light_skips(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, lane="light", models_lines=['work_model = "opus"'])
    assert _resolve(ws, "e2e") == ""


def test_missing_frontmatter_defaults(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, models_lines=None, frontmatter=False)
    assert _resolve(ws, "implement") == "sonnet"


def test_missing_workspace_toml_keeps_default(tmp_path: Path) -> None:
    tickets = tmp_path / ".flow" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "test-1.md").write_text('+++\nlane = "full"\n+++\n', encoding="utf-8")
    assert model_resolve.resolve_stage_model(tmp_path, "test-1", "implement") == "sonnet"


def test_cli_prints_stage_model(tmp_path: Path, capsys) -> None:
    ws = _make_workspace(tmp_path, models_lines=None)
    rc = model_resolve.cli_main(
        ["--workspace-root", str(ws), "--ticket", "test-1", "--stage", "e2e"]
    )
    assert rc == 0
    assert capsys.readouterr().out.strip() == "sonnet"


def test_cli_prints_nothing_when_opted_out(tmp_path: Path, capsys) -> None:
    ws = _make_workspace(tmp_path, models_lines=['work_model = "off"'])
    rc = model_resolve.cli_main(
        ["--workspace-root", str(ws), "--ticket", "test-1", "--stage", "implement"]
    )
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_cli_prints_nothing_for_non_routable_stage(tmp_path: Path, capsys) -> None:
    ws = _make_workspace(tmp_path, models_lines=['work_model = "opus"'])
    rc = model_resolve.cli_main(
        ["--workspace-root", str(ws), "--ticket", "test-1", "--stage", "plan"]
    )
    assert rc == 0
    assert capsys.readouterr().out == ""
