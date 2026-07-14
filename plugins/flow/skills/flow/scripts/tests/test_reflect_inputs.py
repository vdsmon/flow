"""Tests for reflect_inputs.py, the reflect-stage input bundler."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import flow_friction
import harness_corpus
import memory_append
import reflect_inputs
import state
import ticket_frontmatter


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    _git(["init", "--initial-branch=main"], tmp_path)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "test"], tmp_path)
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "README.md"], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)
    return tmp_path


def _seed_state(ticket_dir: Path, head_sha: str, stages: list[str] | None = None) -> None:
    state.init(ticket_dir, "FT-1", "jira", stages or ["ticket", "plan", "implement"])
    state.begin_stage(ticket_dir, "ticket", head_sha)


# ─── bundle() ────────────────────────────────────────────────────────────────


def test_bundle_includes_state_and_ticket_and_run_id(tmp_repo: Path, tmp_path: Path) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["ticket"] == "FT-1"
    assert "run_id" in payload
    assert "state" in payload
    assert payload["state"]["ticket"] == "FT-1"


def test_bundle_reads_frontmatter_when_provided(tmp_repo: Path, tmp_path: Path) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    fm_path = tmp_path / "FT-1.md"
    ticket_frontmatter.update(fm_path, {"ticket": "FT-1", "status": "in_progress"})
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo, ticket_frontmatter_path=fm_path)
    assert payload["ticket_frontmatter"]["status"] == "in_progress"


def test_bundle_omits_frontmatter_when_not_provided(tmp_repo: Path, tmp_path: Path) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["ticket_frontmatter"] == {}


def test_bundle_final_diff_via_diff_extract(tmp_repo: Path, tmp_path: Path) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    (tmp_repo / "a.py").write_text("hi\n", encoding="utf-8")
    _git(["add", "a.py"], tmp_repo)
    _git(["commit", "-m", "add"], tmp_repo)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["final_diff"] is not None
    assert payload["final_diff"]["files_touched"] == ["a.py"]


def test_bundle_diff_null_when_ticket_stage_never_started(tmp_repo: Path, tmp_path: Path) -> None:
    ticket_dir = tmp_path / "runs" / "FT-1"
    state.init(ticket_dir, "FT-1", "jira", ["ticket", "plan"])
    # No begin_stage call -> no started_at_sha.
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["final_diff"] is None


def test_bundle_includes_subagent_reports_when_output_path_set(
    tmp_repo: Path, tmp_path: Path
) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    report_path = ticket_dir / "stages" / "implement.out"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("subagent report body\n", encoding="utf-8")
    state.finish_stage(
        ticket_dir,
        "ticket",
        "completed",
        head,
        output_path=str(report_path),
    )
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    reports = payload["subagent_reports"]
    assert any(r["body"] == "subagent report body\n" for r in reports)


def test_bundle_missing_report_file_gives_null_body_no_warning(
    tmp_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # An inline stage may record an output_path without ever writing the file;
    # an absent report is normal -> null body, and NOT a warning.
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    missing = ticket_dir / "stages" / "implement.out"
    state.finish_stage(ticket_dir, "ticket", "completed", head, output_path=str(missing))
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    reports = payload["subagent_reports"]
    assert any(r["body"] is None for r in reports)
    captured = capsys.readouterr()
    assert "unreadable" not in captured.err


def test_bundle_real_read_error_still_warns(
    tmp_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A genuine read failure (here: output_path points at a directory) is not a
    # normal absent report and must still warn.
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    as_dir = ticket_dir / "stages" / "implement.out"
    as_dir.mkdir(parents=True, exist_ok=True)
    state.finish_stage(ticket_dir, "ticket", "completed", head, output_path=str(as_dir))
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert any(r["body"] is None for r in payload["subagent_reports"])
    captured = capsys.readouterr()
    assert "unreadable" in captured.err


def test_bundle_skips_stages_with_no_output_path(tmp_repo: Path, tmp_path: Path) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    # state had 3 stages, none with output_path -> reports empty.
    assert payload["subagent_reports"] == []


def test_bundle_missing_state_raises(tmp_repo: Path, tmp_path: Path) -> None:
    ticket_dir = tmp_path / "runs" / "missing"
    with pytest.raises(FileNotFoundError):
        reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_emits_json(tmp_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    rc = reflect_inputs.cli_main(
        [
            "--ticket",
            "FT-1",
            "--ticket-dir",
            str(ticket_dir),
            "--cwd",
            str(tmp_repo),
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ticket"] == "FT-1"


def test_cli_missing_state_returns_1(
    tmp_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = reflect_inputs.cli_main(
        [
            "--ticket",
            "FT-1",
            "--ticket-dir",
            str(tmp_path / "no-such"),
            "--cwd",
            str(tmp_repo),
        ]
    )
    assert rc == 1
    assert "state.json" in capsys.readouterr().err


def test_cli_includes_frontmatter_when_flagged(
    tmp_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    fm_path = tmp_path / "FT-1.md"
    ticket_frontmatter.update(fm_path, {"ticket": "FT-1", "status": "x"})
    rc = reflect_inputs.cli_main(
        [
            "--ticket",
            "FT-1",
            "--ticket-dir",
            str(ticket_dir),
            "--cwd",
            str(tmp_repo),
            "--ticket-frontmatter",
            str(fm_path),
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ticket_frontmatter"]["status"] == "x"


def test_bundle_includes_friction_for_this_run_only(tmp_repo: Path, tmp_path: Path) -> None:
    (tmp_repo / ".flow").mkdir(exist_ok=True)
    (tmp_repo / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "jira"\n[memory]\nnamespace = "demo"\n', encoding="utf-8"
    )
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    ticket_state = state.read(ticket_dir)[0]
    assert ticket_state is not None
    run_id = ticket_state.run_id
    flow_friction.append(
        tmp_repo, "FT-1", run_id, "implement", "RECONCILE", "expanded planned_files"
    )
    flow_friction.append(tmp_repo, "FT-1", "other-run", "ticket", "RETRY", "noise from another run")
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    types = [f["type"] for f in payload["friction"]]
    assert "RECONCILE" in types
    assert "RETRY" not in types  # different run_id is excluded


# ─── harness_eval block ──────────────────────────────────────────────────────


def test_bundle_includes_harness_eval_block(tmp_repo: Path, tmp_path: Path) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    block = payload["harness_eval"]
    assert block["available"] is True
    assert Path(block["eval_path"]).is_file()
    assert Path(block["corpus_path"]).is_file()
    cases = harness_corpus.load_corpus(Path(block["corpus_path"]))
    expected_counts = {
        "held_in": sum(1 for c in cases if c["split"] == "held_in"),
        "held_out": sum(1 for c in cases if c["split"] == "held_out"),
    }
    assert block["case_counts"] == expected_counts
    json.dumps(payload)


def test_harness_eval_block_unavailable_when_files_missing(tmp_path: Path) -> None:
    empty = tmp_path / "empty-scripts"
    empty.mkdir()
    block = reflect_inputs._harness_eval_block(scripts_dir=empty)
    assert block["available"] is False
    assert block["reason"]


def test_harness_eval_block_tolerant_of_corpus_error(
    tmp_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)

    def _boom(path: object = None) -> list[dict[str, object]]:
        raise harness_corpus.CorpusError("corpus exploded")

    monkeypatch.setattr(reflect_inputs.harness_corpus, "load_corpus", _boom)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["harness_eval"]["available"] is False
    assert "corpus exploded" in payload["harness_eval"]["reason"]
    assert payload["ticket"] == "FT-1"
    assert "state" in payload
    assert "friction" in payload


# ─── reflect_config ──────────────────────────────────────────────────────────


def _write_workspace(tmp_repo: Path, reflect_block: str = "") -> None:
    (tmp_repo / ".flow").mkdir(exist_ok=True)
    (tmp_repo / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "jira"\n[memory]\nnamespace = "demo"\n' + reflect_block,
        encoding="utf-8",
    )


def test_reflect_config_defaults_when_no_block(tmp_repo: Path, tmp_path: Path) -> None:
    _write_workspace(tmp_repo)
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["reflect_config"] == {"machinery": False, "claude_memory": True}


def test_reflect_config_defaults_when_no_workspace_toml(tmp_repo: Path, tmp_path: Path) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["reflect_config"] == {"machinery": False, "claude_memory": True}


def test_reflect_config_block_overrides(tmp_repo: Path, tmp_path: Path) -> None:
    _write_workspace(tmp_repo, "[reflect]\nmachinery = true\nclaude_memory = false\n")
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["reflect_config"] == {"machinery": True, "claude_memory": False}


def test_reflect_config_partial_block_keeps_other_default(tmp_repo: Path, tmp_path: Path) -> None:
    # only machinery set -> claude_memory stays at its default true.
    _write_workspace(tmp_repo, "[reflect]\nmachinery = true\n")
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["reflect_config"] == {"machinery": True, "claude_memory": True}


def test_reflect_config_non_bool_value_ignored(tmp_repo: Path, tmp_path: Path) -> None:
    # a non-bool override is ignored, the default holds (guards against TOML typos).
    _write_workspace(tmp_repo, '[reflect]\nmachinery = "yes"\n')
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["reflect_config"]["machinery"] is False


# ─── recalled_entries ────────────────────────────────────────────────────────


def _write_recall_log(ticket_dir: Path, lines: list[dict]) -> Path:
    ticket_dir.mkdir(parents=True, exist_ok=True)
    log = ticket_dir / "recall-log.jsonl"
    with log.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    return log


def test_recalled_entries_joins_ids_to_knowledge_bodies(tmp_repo: Path, tmp_path: Path) -> None:
    _write_workspace(tmp_repo)
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    e1 = memory_append.append(tmp_repo, "FACT", "first body", "br", "FT-1")
    e2 = memory_append.append(tmp_repo, "LEARNED", "second body", "br", "FT-1")
    _write_recall_log(ticket_dir, [{"returned_ids": [e1["id"], e2["id"]]}])
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    recalled = payload["recalled_entries"]
    by_id = {r["id"]: r for r in recalled}
    assert e1["id"] in by_id
    assert e2["id"] in by_id
    assert by_id[e1["id"]]["body"] == "first body"
    assert by_id[e2["id"]]["type"] == "LEARNED"
    assert by_id[e1["id"]]["ts"] == e1["ts"]
    assert by_id[e1["id"]]["branch"] == "br"
    assert by_id[e1["id"]]["ticket"] == "FT-1"


def test_recalled_entries_dedups_first_seen_order(tmp_repo: Path, tmp_path: Path) -> None:
    _write_workspace(tmp_repo)
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    a = memory_append.append(tmp_repo, "FACT", "alpha", "br", "FT-1")
    b = memory_append.append(tmp_repo, "FACT", "beta", "br", "FT-1")
    _write_recall_log(
        ticket_dir,
        [
            {"returned_ids": [a["id"], b["id"]]},
            {"returned_ids": [b["id"], a["id"]]},
        ],
    )
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    ids = [r["id"] for r in payload["recalled_entries"]]
    assert ids == [a["id"], b["id"]]


def test_recalled_entries_keeps_superseded_flagged(tmp_repo: Path, tmp_path: Path) -> None:
    """A recalled entry superseded before reflect stays in the bundle flagged
    `superseded: true` so `--used-ids` can still name it (the recall-usage
    denominator counts every surfaced id)."""
    _write_workspace(tmp_repo)
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    a = memory_append.append(tmp_repo, "FACT", "old truth", "br", "FT-1")
    b = memory_append.append(tmp_repo, "FACT", "live truth", "br", "FT-1")
    memory_append.append(tmp_repo, "FACT", "new truth", "br", "FT-1", supersedes=a["id"])
    _write_recall_log(ticket_dir, [{"returned_ids": [a["id"], b["id"]]}])
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    by_id = {r["id"]: r for r in payload["recalled_entries"]}
    assert by_id[a["id"]]["superseded"] is True
    assert by_id[a["id"]]["body"] == "old truth"
    assert "superseded" not in by_id[b["id"]]


def test_recalled_entries_drops_unjoinable_id(tmp_repo: Path, tmp_path: Path) -> None:
    _write_workspace(tmp_repo)
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    e1 = memory_append.append(tmp_repo, "FACT", "present body", "br", "FT-1")
    _write_recall_log(ticket_dir, [{"returned_ids": [e1["id"], "ghost-id-not-in-knowledge"]}])
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    ids = [r["id"] for r in payload["recalled_entries"]]
    assert ids == [e1["id"]]
    assert "ghost-id-not-in-knowledge" not in ids


def test_recalled_entries_empty_when_no_recall_log(tmp_repo: Path, tmp_path: Path) -> None:
    _write_workspace(tmp_repo)
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    memory_append.append(tmp_repo, "FACT", "body", "br", "FT-1")
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["recalled_entries"] == []


def test_recalled_entries_empty_when_namespace_unresolvable(tmp_repo: Path, tmp_path: Path) -> None:
    # No workspace.toml -> namespace resolution fails; recalled_entries degrades to
    # [] but the rest of the bundle still populates.
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    _write_recall_log(ticket_dir, [{"returned_ids": ["whatever-id"]}])
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["recalled_entries"] == []
    assert payload["ticket"] == "FT-1"
    assert "state" in payload


def test_recalled_entries_tolerates_malformed_log_line_no_sidecar(
    tmp_repo: Path, tmp_path: Path
) -> None:
    _write_workspace(tmp_repo)
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    e1 = memory_append.append(tmp_repo, "FACT", "good body", "br", "FT-1")
    ticket_dir.mkdir(parents=True, exist_ok=True)
    log = ticket_dir / "recall-log.jsonl"
    with log.open("w", encoding="utf-8") as fh:
        fh.write("{ this is not valid json\n")
        fh.write(json.dumps({"returned_ids": [e1["id"]]}) + "\n")
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    ids = [r["id"] for r in payload["recalled_entries"]]
    assert ids == [e1["id"]]
    sidecars = list(ticket_dir.glob("recall-log.jsonl.quarantine*"))
    assert sidecars == []


# ─── friction_recurrence ─────────────────────────────────────────────────────


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e, sort_keys=True) for e in entries]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _friction_entry(
    *,
    id_: str,
    ts: str,
    run_id: str = "run-1",
    stage: str = "implement",
    type_: str = "RETRY",
    ticket: str = "FT-1",
    body: str = "",
) -> dict:
    return {
        "id": id_,
        "ts": ts,
        "run_id": run_id,
        "ticket": ticket,
        "stage": stage,
        "type": type_,
        "severity": "major",
        "body": body,
    }


def _machinery_entry(*, id_: str, ts: str, ticket: str = "T-fix", body: str) -> dict:
    return {"id": id_, "ts": ts, "ticket": ticket, "type": "LEARNED", "body": body}


def test_bundle_recurrence_block_present_for_recurring_class(
    tmp_repo: Path, tmp_path: Path
) -> None:
    _write_workspace(tmp_repo)
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    machinery = [
        _machinery_entry(
            id_="fix-1",
            ts="2026-06-01T00:00:00.000Z",
            body="MACHINERY: signature_bug was patched. Fix (commit abc1234).",
        )
    ]
    friction = [
        _friction_entry(
            id_="f-1",
            ts="2026-06-02T00:00:00.000Z",
            run_id="run-x",
            body="signature_bug fired again",
        )
    ]
    _write_jsonl(tmp_repo / ".flow" / "demo" / "knowledge.jsonl", machinery)
    _write_jsonl(tmp_repo / ".flow" / "demo" / "friction.jsonl", friction)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    recurrence = payload["friction_recurrence"]
    assert recurrence != []
    entry = recurrence[0]
    assert entry["anchor"] == "signature_bug"
    assert entry["fired_count"] == 1
    assert entry["last_fix_sha"] == "abc1234"
    assert isinstance(entry["runs_ago"], int)
    assert entry["runs_ago"] == 1


def test_bundle_recurrence_empty_when_no_recurrence(tmp_repo: Path, tmp_path: Path) -> None:
    _write_workspace(tmp_repo)
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    machinery = [
        _machinery_entry(
            id_="fix-2",
            ts="2026-06-01T00:00:00.000Z",
            body="MACHINERY: stale_flag cleaned up. Fix (commit bbbbbbb).",
        )
    ]
    friction = [
        _friction_entry(
            id_="f-early",
            ts="2026-05-01T00:00:00.000Z",
            body="stale_flag lingering before the fix",
        )
    ]
    _write_jsonl(tmp_repo / ".flow" / "demo" / "knowledge.jsonl", machinery)
    _write_jsonl(tmp_repo / ".flow" / "demo" / "friction.jsonl", friction)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["friction_recurrence"] == []


def test_bundle_recurrence_runs_ago_counts_distinct_runids_globally(
    tmp_repo: Path, tmp_path: Path
) -> None:
    _write_workspace(tmp_repo)
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    fix_ts = "2026-06-01T00:00:00.000Z"
    machinery = [
        _machinery_entry(
            id_="fix-3",
            ts=fix_ts,
            body="MACHINERY: recur_multi_token issue. Fix (commit ccc1111).",
        )
    ]
    friction = [
        _friction_entry(
            id_="f-a",
            ts="2026-06-02T00:00:00.000Z",
            run_id="run-a",
            body="recur_multi_token fired again",
        ),
        _friction_entry(
            id_="f-b",
            ts="2026-06-03T00:00:00.000Z",
            run_id="run-b",
            body="recur_multi_token fired again",
        ),
        _friction_entry(
            id_="f-unrelated",
            ts="2026-06-04T00:00:00.000Z",
            run_id="run-c",
            body="unrelated_token forms no class",
        ),
        _friction_entry(
            id_="f-a-repeat",
            ts="2026-06-05T00:00:00.000Z",
            run_id="run-a",
            body="recur_multi_token fired yet again",
        ),
    ]
    _write_jsonl(tmp_repo / ".flow" / "demo" / "knowledge.jsonl", machinery)
    _write_jsonl(tmp_repo / ".flow" / "demo" / "friction.jsonl", friction)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    recurrence = [c for c in payload["friction_recurrence"] if c["anchor"] == "recur_multi_token"]
    assert len(recurrence) == 1
    assert recurrence[0]["runs_ago"] == 3


def test_bundle_recurrence_last_fix_sha_is_most_recent(tmp_repo: Path, tmp_path: Path) -> None:
    _write_workspace(tmp_repo)
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    machinery = [
        _machinery_entry(
            id_="fix-old",
            ts="2026-06-01T00:00:00.000Z",
            body="MACHINERY: double_fix_token issue. Fix (commit aaa1111).",
        ),
        _machinery_entry(
            id_="fix-new",
            ts="2026-06-02T00:00:00.000Z",
            body="MACHINERY: double_fix_token issue, take two. Fix (commit bbb2222).",
        ),
    ]
    friction = [
        _friction_entry(
            id_="f-post",
            ts="2026-06-03T00:00:00.000Z",
            body="double_fix_token fired again",
        )
    ]
    _write_jsonl(tmp_repo / ".flow" / "demo" / "knowledge.jsonl", machinery)
    _write_jsonl(tmp_repo / ".flow" / "demo" / "friction.jsonl", friction)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    recurrence = [c for c in payload["friction_recurrence"] if c["anchor"] == "double_fix_token"]
    assert len(recurrence) == 1
    assert recurrence[0]["last_fix_sha"] == "bbb2222"


def test_bundle_recurrence_last_fix_sha_null_when_evidence_lost(
    tmp_repo: Path, tmp_path: Path
) -> None:
    _write_workspace(tmp_repo)
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    machinery = [
        _machinery_entry(
            id_="fix-silent",
            ts="2026-06-01T00:00:00.000Z",
            ticket="T-silent",
            body="MACHINERY: silent_fix_token issue resolved, evidence lost.",
        )
    ]
    friction = [
        _friction_entry(
            id_="f-post",
            ts="2026-06-02T00:00:00.000Z",
            body="silent_fix_token fired again",
        )
    ]
    _write_jsonl(tmp_repo / ".flow" / "demo" / "knowledge.jsonl", machinery)
    _write_jsonl(tmp_repo / ".flow" / "demo" / "friction.jsonl", friction)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    recurrence = [c for c in payload["friction_recurrence"] if c["anchor"] == "silent_fix_token"]
    assert len(recurrence) == 1
    assert recurrence[0]["last_fix_sha"] is None


def test_bundle_friction_section_unchanged_with_recurrence(tmp_repo: Path, tmp_path: Path) -> None:
    _write_workspace(tmp_repo)
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    ticket_state = state.read(ticket_dir)[0]
    assert ticket_state is not None
    run_id = ticket_state.run_id
    machinery = [
        _machinery_entry(
            id_="fix-4",
            ts="2026-06-01T00:00:00.000Z",
            body="MACHINERY: recur_token patched. Fix (commit cafefeed).",
        )
    ]
    friction = [
        _friction_entry(
            id_="f-this-run",
            ts="2026-06-02T00:00:00.000Z",
            run_id=run_id,
            body="unrelated friction for this run",
        ),
        _friction_entry(
            id_="f-other-1",
            ts="2026-06-03T00:00:00.000Z",
            run_id="other-run-1",
            body="recur_token fired again",
        ),
        _friction_entry(
            id_="f-other-2",
            ts="2026-06-04T00:00:00.000Z",
            run_id="other-run-2",
            body="recur_token fired again",
        ),
    ]
    _write_jsonl(tmp_repo / ".flow" / "demo" / "knowledge.jsonl", machinery)
    _write_jsonl(tmp_repo / ".flow" / "demo" / "friction.jsonl", friction)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert [f["id"] for f in payload["friction"]] == ["f-this-run"]
    recurrence = [c for c in payload["friction_recurrence"] if c["anchor"] == "recur_token"]
    assert len(recurrence) == 1
    assert recurrence[0]["fired_count"] == 2


def test_bundle_recurrence_degrades_to_empty_on_no_workspace(
    tmp_repo: Path, tmp_path: Path
) -> None:
    # No workspace.toml -> namespace resolution fails; friction_recurrence
    # degrades to [] but the rest of the bundle still populates.
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["friction_recurrence"] == []
    assert payload["ticket"] == "FT-1"
    assert "state" in payload


def test_bundle_recurrence_degrades_to_empty_on_detector_crash(
    tmp_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The detector is a separately-evolving module: ANY exception it raises
    # must degrade this closing-stage enrichment to [], never kill the bundle.
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)

    def _boom(*args: object, **kwargs: object) -> dict:
        raise ValueError("detector edge case")

    monkeypatch.setattr(reflect_inputs.friction_recurrence, "analyze", _boom)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["friction_recurrence"] == []
    assert payload["ticket"] == "FT-1"
    assert "state" in payload


def test_bundle_recurrence_capped_worst_first(
    tmp_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)

    classes = [
        {
            "cluster_key": "signature",
            "anchor": f"anchor_{i:02d}",
            "post_fix_count": i,
            "fixes": [{"ts": "2026-06-01T00:00:00.000Z", "fix_sha": None}],
        }
        for i in range(1, 21)
    ]
    monkeypatch.setattr(
        reflect_inputs.friction_recurrence,
        "analyze",
        lambda *a, **k: {"signature_classes": classes},
    )
    monkeypatch.setattr(reflect_inputs._memory_paths, "resolve_namespace", lambda cwd: "demo")
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    got = payload["friction_recurrence"]
    assert len(got) == reflect_inputs._RECURRENCE_CAP
    assert got[0]["fired_count"] == 20
    assert got[-1]["fired_count"] == 20 - reflect_inputs._RECURRENCE_CAP + 1


# ─── label_facets ────────────────────────────────────────────────────────────


def test_bundle_label_facets_default_empty(tmp_repo: Path, tmp_path: Path) -> None:
    _write_workspace(tmp_repo)
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["label_facets"] == []


def test_bundle_label_facets_set(tmp_repo: Path, tmp_path: Path) -> None:
    _write_workspace(tmp_repo, 'label_facets = ["form"]\n')
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["label_facets"] == ["form"]


def test_bundle_label_facets_empty_when_no_workspace_toml(tmp_repo: Path, tmp_path: Path) -> None:
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["label_facets"] == []


def test_bundle_label_facets_empty_when_malformed(tmp_repo: Path, tmp_path: Path) -> None:
    (tmp_repo / ".flow").mkdir(exist_ok=True)
    (tmp_repo / ".flow" / "workspace.toml").write_text(
        "this is not = valid [ toml", encoding="utf-8"
    )
    head = _git(["rev-parse", "HEAD"], tmp_repo).strip()
    ticket_dir = tmp_path / "runs" / "FT-1"
    _seed_state(ticket_dir, head)
    payload = reflect_inputs.bundle("FT-1", ticket_dir, tmp_repo)
    assert payload["label_facets"] == []


def test_immutable_routed_envelope_binds_payload_source_route_and_generation(
    tmp_path: Path,
) -> None:
    output = tmp_path / "reflection-input.json"
    envelope = reflect_inputs.write_immutable_envelope(
        {"ticket": "F-1", "final_diff": {"patch": "x"}},
        output,
        source_sha="a" * 40,
        route_digest="b" * 64,
        stage_generation=3,
    )
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted == envelope
    assert envelope["schema"] == "flow.reflection-input-bundle/v1"
    assert envelope["stage_generation"] == 3
    assert len(envelope["digest"]) == 64
    assert output.stat().st_mode & 0o222 == 0
