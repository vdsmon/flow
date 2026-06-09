from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import harness_eval
from harness_eval import EvalError, cli_main, replay_checkout, score_delta

SCRIPTS_DIR = Path(harness_eval.__file__).resolve().parent


def _row(case_id, *, split="held_in", ok=True, **extra):
    row = {"case_id": case_id, "split": split, "decider": "triage.is_hot_change", "ok": ok}
    row.update(extra)
    return row


def _case(case_id, *, decider="triage.is_hot_change", split="held_in", args=None, expected=False):
    return {
        "case_id": case_id,
        "split": split,
        "decider": decider,
        "args": args if args is not None else {"files": []},
        "expected": expected,
    }


def _stub_checkout(tmp_path, files):
    d = tmp_path / "stub-checkout"
    d.mkdir(exist_ok=True)
    for name, src in files.items():
        (d / name).write_text(src, encoding="utf-8")
    return d


# ─── score_delta (pure) ──────────────────────────────────────────────────────


def test_identical_all_pass_rows_non_regression():
    rows = [_row("a"), _row("b", split="held_out"), _row("c", split="held_out")]
    delta = score_delta(rows, rows)
    assert delta["non_regression"] is True
    assert delta["cases"] == 3
    held_in = delta["splits"]["held_in"]
    held_out = delta["splits"]["held_out"]
    assert held_in["cases"] == 1
    assert held_in["baseline_pass"] == 1
    assert held_in["baseline_fail"] == 0
    assert held_in["candidate_pass"] == 1
    assert held_in["candidate_fail"] == 0
    assert held_out["cases"] == 2
    assert held_out["candidate_pass"] == 2
    for split in delta["splits"].values():
        assert split["regressed"] == []
        assert split["improved"] == []
        assert split["detail"] == {}


def test_held_out_regression_flagged_held_in_clean():
    baseline = [_row("a"), _row("b", split="held_out")]
    candidate = [_row("a"), _row("b", split="held_out", ok=False, actual=1, expected=2)]
    delta = score_delta(baseline, candidate)
    assert delta["non_regression"] is False
    assert delta["splits"]["held_out"]["regressed"] == ["b"]
    assert delta["splits"]["held_in"]["regressed"] == []
    assert delta["splits"]["held_in"]["detail"] == {}


def test_improvement_listed_non_regression_holds():
    baseline = [_row("a", ok=False, actual=1, expected=2)]
    candidate = [_row("a")]
    delta = score_delta(baseline, candidate)
    assert delta["non_regression"] is True
    assert delta["splits"]["held_in"]["improved"] == ["a"]
    assert delta["splits"]["held_in"]["regressed"] == []


def test_both_fail_is_unchanged():
    baseline = [_row("a", ok=False, actual=1, expected=2)]
    candidate = [_row("a", ok=False, actual=3, expected=2)]
    delta = score_delta(baseline, candidate)
    assert delta["non_regression"] is True
    assert delta["splits"]["held_in"]["regressed"] == []
    assert delta["splits"]["held_in"]["improved"] == []


def test_case_set_mismatch_raises():
    with pytest.raises(EvalError):
        score_delta([_row("a")], [_row("a"), _row("b")])


def test_regressed_detail_carries_evidence():
    baseline = [_row("a"), _row("b")]
    candidate = [
        _row("a", ok=False, actual={"x": 1}, expected={"x": 2}),
        _row("b", ok=False, error="TypeError: boom"),
    ]
    delta = score_delta(baseline, candidate)
    detail = delta["splits"]["held_in"]["detail"]
    assert detail["a"] == {"actual": {"x": 1}, "expected": {"x": 2}}
    assert detail["b"] == {"error": "TypeError: boom"}


# ─── replay_checkout (subprocess driver) ─────────────────────────────────────


def test_replay_stub_checkout_all_ok(tmp_path):
    checkout = _stub_checkout(
        tmp_path, {"triage.py": "def is_hot_change(files):\n    return bool(files)\n"}
    )
    cases = [
        _case("c1", args={"files": []}, expected=False),
        _case("c2", split="held_out", args={"files": ["x"]}, expected=True),
    ]
    rows = replay_checkout(checkout, cases)
    assert [r["ok"] for r in rows] == [True, True]
    assert rows[0]["split"] == "held_in"
    assert rows[1]["split"] == "held_out"


def test_raising_decider_captured_not_fatal(tmp_path):
    checkout = _stub_checkout(
        tmp_path,
        {"triage.py": "def is_hot_change(files):\n    raise RuntimeError('kaput')\n"},
    )
    rows = replay_checkout(checkout, [_case("c1"), _case("c2", args={"files": ["a"]})])
    assert len(rows) == 2
    for row in rows:
        assert row["ok"] is False
        assert row["error"] == "RuntimeError: kaput"


def test_module_absent_from_checkout_is_import_error(tmp_path):
    checkout = _stub_checkout(
        tmp_path, {"triage.py": "def is_hot_change(files):\n    return False\n"}
    )
    rows = replay_checkout(checkout, [_case("c1", decider="evolve_drain.decide", args={})])
    assert rows[0]["ok"] is False
    assert rows[0]["error"].startswith("ModuleNotFoundError")


def test_set_args_marshaled_across_subprocess(tmp_path):
    checkout = _stub_checkout(
        tmp_path,
        {
            "evolve_select.py": (
                "def partition(inflight_keys=None, **kwargs):\n"
                "    assert isinstance(inflight_keys, set), type(inflight_keys).__name__\n"
                "    return sorted(inflight_keys)\n"
            )
        },
    )
    cases = [
        _case(
            "c1",
            decider="evolve_select.partition",
            args={"inflight_keys": ["b", "a"]},
            expected=["a", "b"],
        )
    ]
    rows = replay_checkout(checkout, cases)
    assert rows[0]["ok"] is True


def test_sibling_imports_resolve_in_target_checkout(tmp_path):
    checkout = _stub_checkout(
        tmp_path,
        {
            "triage.py": "SENTINEL = 'stub-triage-sentinel'\n",
            "evolve_self_merge.py": (
                "import triage\n\ndef decide(**kwargs):\n    return triage.SENTINEL\n"
            ),
        },
    )
    rows = replay_checkout(
        checkout,
        [_case("c1", decider="evolve_self_merge.decide", args={}, expected="stub-triage-sentinel")],
    )
    assert rows[0]["ok"] is True


def test_driver_nonzero_exit_raises(monkeypatch):
    fake = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
    monkeypatch.setattr(harness_eval.subprocess, "run", lambda *a, **k: fake)
    with pytest.raises(EvalError):
        replay_checkout(SCRIPTS_DIR, [])


def test_driver_garbage_stdout_raises(monkeypatch):
    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr="")
    monkeypatch.setattr(harness_eval.subprocess, "run", lambda *a, **k: fake)
    with pytest.raises(EvalError):
        replay_checkout(SCRIPTS_DIR, [])


# ─── cli_main ────────────────────────────────────────────────────────────────


def test_score_self_vs_self_is_clean(capsys):
    rc = cli_main(["score", "--candidate", str(SCRIPTS_DIR)])
    out = capsys.readouterr().out
    result = json.loads(out)
    assert rc == 0
    assert result["non_regression"] is True
    assert result["cases"] == 35
    assert result["baseline"] == str(SCRIPTS_DIR)
    assert result["candidate"] == str(SCRIPTS_DIR)


def test_score_sabotaged_candidate_exits_3(tmp_path, capsys):
    checkout = _stub_checkout(
        tmp_path, {"triage.py": "def is_hot_change(files):\n    return True\n"}
    )
    corpus = tmp_path / "corpus.json"
    corpus.write_text(
        json.dumps({"schema_version": 1, "cases": [_case("hot-none", expected=False)]}),
        encoding="utf-8",
    )
    rc = cli_main(
        [
            "score",
            "--candidate",
            str(checkout),
            "--baseline",
            str(SCRIPTS_DIR),
            "--corpus",
            str(corpus),
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert rc == 3
    assert result["non_regression"] is False
    assert result["splits"]["held_in"]["regressed"] == ["hot-none"]


def test_missing_candidate_dir_exits_1(tmp_path, capsys):
    rc = cli_main(["score", "--candidate", str(tmp_path / "nope")])
    assert rc == 1
    assert "candidate" in capsys.readouterr().err


def test_corrupt_corpus_exits_2(tmp_path, capsys):
    corpus = tmp_path / "corpus.json"
    corpus.write_text("not json", encoding="utf-8")
    rc = cli_main(["score", "--candidate", str(SCRIPTS_DIR), "--corpus", str(corpus)])
    assert rc == 2
    assert capsys.readouterr().err
