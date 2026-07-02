"""Tests for friction_escalate.py.

Seeds a real workspace matching test_friction_recurrence.py's pattern
(`.flow/workspace.toml` + `.flow/<namespace>/{friction,knowledge}.jsonl`) plus
the `[maintainer]` marker from test_flow_beads_create.py for the escalate()
integration tests.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import friction_escalate as fe

Recorder = list[tuple[list[str], Path]]


# --- workspace seeding --------------------------------------------------------


def _seed_workspace(root: Path, *, maintainer: bool = False, namespace: str = "demo") -> None:
    flow = root / ".flow"
    (flow / namespace).mkdir(parents=True, exist_ok=True)
    marker = "[maintainer]\nself_target = true\n\n" if maintainer else ""
    (flow / "workspace.toml").write_text(
        f'{marker}[tracker]\nbackend = "beads"\n\n[memory]\nnamespace = "{namespace}"\n',
        encoding="utf-8",
    )


def _seed_evolve(root: Path, body: str) -> None:
    path = root / ".flow" / "workspace.toml"
    path.write_text(path.read_text(encoding="utf-8") + body, encoding="utf-8")


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e, sort_keys=True) for e in entries]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


# --- select_escalations() fixtures --------------------------------------------


def _fix(ts: str, sha: str) -> dict:
    return {"id": f"m-{ts}", "ticket": "T-fix", "ts": ts, "fix_sha": sha}


def _recur(
    ts: str,
    *,
    stage: str = "implement",
    type_: str = "RETRY",
    ticket: str = "T-1",
    run_id: str = "run-1",
) -> dict:
    return {
        "id": f"f-{ts}-{run_id}",
        "run_id": run_id,
        "ticket": ticket,
        "ts": ts,
        "stage": stage,
        "type": type_,
    }


def _sig_class(anchor: str, fixes: list[dict], recurrences: list[dict]) -> dict:
    return {
        "cluster_key": "signature",
        "anchor": anchor,
        "fixes": fixes,
        "recurrences": recurrences,
        "post_fix_count": len(recurrences),
    }


# --- select_escalations: latest-fix vs earliest-fix headline test -------------


def test_latest_fix_not_earliest_suppresses_escalation():
    """A class with many recurrences since the EARLIEST fix but few since the
    LATEST fix must not escalate: the latest claimed fix is the one being
    graded, not the aggregate history the detector's post_fix_count reports."""
    fixes = [
        _fix("2026-01-01T00:00:00.000Z", "aaa1111"),
        _fix("2026-02-01T00:00:00.000Z", "bbb2222"),
    ]
    recurrences = (
        [
            _recur(f"2026-01-{d:02d}T00:00:00.000Z") for d in range(10, 18)
        ]  # 8, between the two fixes
        + [
            _recur("2026-02-02T00:00:00.000Z"),
            _recur("2026-02-03T00:00:00.000Z"),
        ]  # 2, after latest
    )
    payload = {"signature_classes": [_sig_class("flaky_anchor", fixes, recurrences)]}
    assert fe.select_escalations(payload, k=3, exempt=set()) == []


def test_high_recurrence_after_latest_fix_escalates():
    fixes = [
        _fix("2026-01-01T00:00:00.000Z", "aaa1111"),
        _fix("2026-02-01T00:00:00.000Z", "bbb2222"),
    ]
    recurrences = [_recur(f"2026-02-{d:02d}T00:00:00.000Z") for d in range(2, 6)]  # 4, after latest
    payload = {"signature_classes": [_sig_class("stubborn_anchor", fixes, recurrences)]}
    out = fe.select_escalations(payload, k=3, exempt=set())
    assert len(out) == 1
    assert out[0]["anchor"] == "stubborn_anchor"
    assert out[0]["count"] == 4
    assert out[0]["latest_fix_ts"] == "2026-02-01T00:00:00.000Z"
    assert out[0]["latest_fix_sha"] == "bbb2222"


def test_k_boundary_exact_escalates_one_under_does_not():
    fix = [_fix("2026-01-01T00:00:00.000Z", "aaa1111")]
    exactly_k = [_recur(f"2026-01-{d:02d}T00:00:00.000Z") for d in range(2, 5)]  # 3
    one_under = [_recur(f"2026-01-{d:02d}T00:00:00.000Z") for d in range(2, 4)]  # 2
    payload = {
        "signature_classes": [
            _sig_class("at_boundary", fix, exactly_k),
            _sig_class("below_boundary", fix, one_under),
        ]
    }
    out = fe.select_escalations(payload, k=3, exempt=set())
    assert [c["anchor"] for c in out] == ["at_boundary"]


def test_exempt_anchor_dropped_even_over_threshold():
    fix = [_fix("2026-01-01T00:00:00.000Z", "aaa1111")]
    recurrences = [_recur(f"2026-01-{d:02d}T00:00:00.000Z") for d in range(2, 8)]  # 6
    payload = {"signature_classes": [_sig_class("planned_files", fix, recurrences)]}
    assert fe.select_escalations(payload, k=3, exempt={"planned_files"}) == []


def test_empty_payload_returns_empty_list():
    assert fe.select_escalations({"signature_classes": []}, k=3, exempt=set()) == []
    assert fe.select_escalations({}, k=3, exempt=set()) == []


def test_fix_missing_ts_skipped_no_anchor_to_grade_against():
    fixes = [{"id": "m1", "ticket": "T-fix", "ts": "", "fix_sha": None}]
    recurrences = [_recur("2026-01-02T00:00:00.000Z")]
    payload = {"signature_classes": [_sig_class("orphan_anchor", fixes, recurrences)]}
    assert fe.select_escalations(payload, k=1, exempt=set()) == []


def test_sorted_by_descending_count():
    fix = [_fix("2026-01-01T00:00:00.000Z", "aaa1111")]
    hot = [_recur(f"2026-01-{d:02d}T00:00:00.000Z") for d in range(2, 10)]  # 8
    warm = [_recur(f"2026-01-{d:02d}T00:00:00.000Z") for d in range(2, 6)]  # 4
    payload = {
        "signature_classes": [
            _sig_class("warm_anchor", fix, warm),
            _sig_class("hot_anchor", fix, hot),
        ]
    }
    out = fe.select_escalations(payload, k=3, exempt=set())
    assert [c["anchor"] for c in out] == ["hot_anchor", "warm_anchor"]


# --- config readers ------------------------------------------------------------


def test_escalation_k_default_when_absent(tmp_path: Path):
    _seed_workspace(tmp_path)
    assert fe.escalation_k(tmp_path) == 3


def test_escalation_k_default_when_no_workspace(tmp_path: Path):
    assert fe.escalation_k(tmp_path) == 3


def test_escalation_k_override(tmp_path: Path):
    _seed_workspace(tmp_path)
    _seed_evolve(tmp_path, "\n[evolve]\nrecurrence_escalation_k = 5\n")
    assert fe.escalation_k(tmp_path) == 5


def test_escalation_k_wrong_type_falls_back_to_default(tmp_path: Path):
    _seed_workspace(tmp_path)
    _seed_evolve(tmp_path, '\n[evolve]\nrecurrence_escalation_k = "five"\n')
    assert fe.escalation_k(tmp_path) == 3


def test_exempt_anchors_default_when_absent(tmp_path: Path):
    _seed_workspace(tmp_path)
    assert fe.exempt_anchors(tmp_path) == {"planned_files"}


def test_exempt_anchors_default_when_no_workspace(tmp_path: Path):
    assert fe.exempt_anchors(tmp_path) == {"planned_files"}


def test_exempt_anchors_override(tmp_path: Path):
    _seed_workspace(tmp_path)
    _seed_evolve(tmp_path, '\n[evolve]\nrecurrence_exempt_anchors = ["foo_anchor", "bar_anchor"]\n')
    assert fe.exempt_anchors(tmp_path) == {"foo_anchor", "bar_anchor"}


def test_exempt_anchors_explicit_empty_list_means_no_exemptions(tmp_path: Path):
    _seed_workspace(tmp_path)
    _seed_evolve(tmp_path, "\n[evolve]\nrecurrence_exempt_anchors = []\n")
    assert fe.exempt_anchors(tmp_path) == set()


def test_exempt_anchors_wrong_type_falls_back_to_default(tmp_path: Path):
    _seed_workspace(tmp_path)
    _seed_evolve(tmp_path, '\n[evolve]\nrecurrence_exempt_anchors = "not-a-list"\n')
    assert fe.exempt_anchors(tmp_path) == {"planned_files"}


# --- escalate(): maintainer gate + create_bead wiring -------------------------


def _runner(
    list_by_label: dict[str, list[dict]] | None = None, create_id: str = "flow-new"
) -> tuple[Callable[..., subprocess.CompletedProcess[str]], Recorder]:
    by_label = dict(list_by_label or {})
    calls: Recorder = []

    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append((args, cwd))
        if len(args) >= 2 and args[1] == "list":
            label = args[args.index("-l") + 1] if "-l" in args else ""
            return subprocess.CompletedProcess(args, 0, json.dumps(by_label.get(label, [])), "")
        return subprocess.CompletedProcess(args, 0, json.dumps({"id": create_id}), "")

    return run, calls


def _seed_recurring_class(tmp_path: Path, namespace: str = "demo") -> None:
    machinery = [
        {
            "id": "fix-1",
            "ts": "2026-01-01T00:00:00.000Z",
            "ticket": "T-fix1",
            "type": "LEARNED",
            "body": "MACHINERY: escalate_anchor patched first try. Fix (commit aaa1111).",
        },
        {
            "id": "fix-2",
            "ts": "2026-02-01T00:00:00.000Z",
            "ticket": "T-fix2",
            "type": "LEARNED",
            "body": "MACHINERY: escalate_anchor patched again. Fix (commit bbb2222).",
        },
    ]
    friction = [
        {
            "id": f"f-{i}",
            "ts": f"2026-02-{i + 2:02d}T00:00:00.000Z",
            "run_id": f"run-{i}",
            "ticket": f"T-{i}",
            "stage": "implement",
            "type": "RETRY",
            "severity": "major",
            "body": "escalate_anchor fired again",
        }
        for i in range(3)
    ]
    _write_jsonl(tmp_path / ".flow" / namespace / "knowledge.jsonl", machinery)
    _write_jsonl(tmp_path / ".flow" / namespace / "friction.jsonl", friction)


def test_escalate_files_bead_with_recurrent_label_and_dedup_key(tmp_path: Path):
    _seed_workspace(tmp_path, maintainer=True)
    _seed_recurring_class(tmp_path)
    run, calls = _runner()

    result = fe.escalate(tmp_path, runner=run)

    assert result["maintainer"] is True
    assert result["k"] == 3
    assert result["exempt"] == ["planned_files"]
    assert len(result["filed"]) == 1
    assert result["filed"][0]["anchor"] == "escalate_anchor"
    assert result["filed"][0]["key"] == "flow-new"
    assert result["deduped"] == []
    assert result["errors"] == []

    create_args = next(c[0] for c in calls if c[0][:2] == ["bd", "create"])
    stamped = create_args[create_args.index("--labels") + 1]
    assert "recurrent" in stamped.split(",")
    # propose-only is LOCKED, not merely true: an evolve label would make the
    # bead drain-eligible, and any verb beyond list/create would be a mutation.
    assert "evolve" not in stamped.split(",")
    assert {c[0][1] for c in calls} <= {"list", "create"}

    import flow_beads_create as fbc

    dedup_key = "recurrence-escalation-escalate_anchor"
    evid = f"evid:{fbc.fingerprint(dedup_key)}"
    list_call = next(c[0] for c in calls if c[0][1] == "list")
    assert list_call[list_call.index("-l") + 1] == evid


def test_escalate_dedup_hit_routes_to_deduped_no_create(tmp_path: Path):
    _seed_workspace(tmp_path, maintainer=True)
    _seed_recurring_class(tmp_path)

    import flow_beads_create as fbc

    dedup_key = "recurrence-escalation-escalate_anchor"
    evid = f"evid:{fbc.fingerprint(dedup_key)}"
    run, calls = _runner(list_by_label={evid: [{"id": "flow-old"}]})

    result = fe.escalate(tmp_path, runner=run)

    assert result["filed"] == []
    assert result["deduped"] == [{"anchor": "escalate_anchor", "existing_key": "flow-old"}]
    assert not any(c[0][:2] == ["bd", "create"] for c in calls)


def test_escalate_not_maintainer_is_dormant_no_op(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("maintainer._global_config_path", lambda: tmp_path / "absent.toml")
    # deliberately no [memory] section either, to prove the dormant short-circuit
    # fires before any namespace/analyze read is attempted.
    (tmp_path / ".flow").mkdir(parents=True)
    (tmp_path / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "beads"\n', encoding="utf-8"
    )

    result = fe.escalate(tmp_path)

    assert result == {
        "maintainer": False,
        "k": 0,
        "exempt": [],
        "candidates": 0,
        "filed": [],
        "deduped": [],
        "errors": [],
    }


# --- CLI -----------------------------------------------------------------------


def test_cli_dormant_prints_json_exit_0(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setattr("maintainer._global_config_path", lambda: tmp_path / "absent.toml")
    (tmp_path / ".flow").mkdir(parents=True)
    (tmp_path / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "beads"\n', encoding="utf-8"
    )
    rc = fe.cli_main(["escalate", "--workspace-root", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["maintainer"] is False


def test_cli_memory_config_error_exit_4(tmp_path: Path, capsys):
    # maintainer-marked but missing [memory] -> resolve_namespace raises inside escalate()
    (tmp_path / ".flow").mkdir(parents=True)
    (tmp_path / ".flow" / "workspace.toml").write_text(
        "[maintainer]\nself_target = true\n", encoding="utf-8"
    )
    rc = fe.cli_main(["escalate", "--workspace-root", str(tmp_path)])
    assert rc == 4
    assert "workspace.toml" in capsys.readouterr().err
