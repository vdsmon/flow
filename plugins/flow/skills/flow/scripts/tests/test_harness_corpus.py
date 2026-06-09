from __future__ import annotations

import json
import types

import pytest

import harness_corpus
from harness_corpus import DECIDERS, CorpusError, load_corpus, replay, run_case

_CASES = load_corpus()

# ─── corpus file: shape + standing replay freeze ─────────────────────────────


def test_corpus_loads_and_validates():
    assert _CASES
    for case in _CASES:
        assert case["case_id"]
        assert case["split"] in ("held_in", "held_out")
        assert case["decider"] in DECIDERS
        assert isinstance(case["args"], dict)
        assert "expected" in case


@pytest.mark.parametrize("case", _CASES, ids=[c["case_id"] for c in _CASES])
def test_every_case_replays_green(case):
    actual = run_case(case)
    assert actual == case["expected"], f"case {case['case_id']} regressed: {actual!r}"


def test_split_coverage():
    seen = {(c["decider"], c["split"]) for c in _CASES}
    for decider in DECIDERS:
        for split in ("held_in", "held_out"):
            assert (decider, split) in seen, f"{decider} missing a {split} case"


# ─── load_corpus validation rejections ───────────────────────────────────────


def _case(**over):
    case = {
        "case_id": "hot-stub",
        "split": "held_in",
        "decider": "triage.is_hot_change",
        "args": {"files": []},
        "expected": False,
    }
    case.update(over)
    return case


def _corpus_path(tmp_path, payload):
    p = tmp_path / "corpus.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("wrong_schema_version", {"schema_version": 2, "cases": [_case()]}),
        ("unknown_decider", {"schema_version": 1, "cases": [_case(decider="nope.decide")]}),
        ("bad_split", {"schema_version": 1, "cases": [_case(split="holdout")]}),
        (
            "duplicate_case_id",
            {"schema_version": 1, "cases": [_case(), _case()]},
        ),
        ("empty_case_id", {"schema_version": 1, "cases": [_case(case_id="")]}),
        ("non_dict_args", {"schema_version": 1, "cases": [_case(args=[1])]}),
        ("cases_not_list", {"schema_version": 1, "cases": {}}),
    ],
)
def test_load_corpus_rejects(tmp_path, name, payload):
    with pytest.raises(CorpusError):
        load_corpus(_corpus_path(tmp_path, payload))


def test_load_corpus_rejects_missing_expected(tmp_path):
    case = _case()
    del case["expected"]
    with pytest.raises(CorpusError):
        load_corpus(_corpus_path(tmp_path, {"schema_version": 1, "cases": [case]}))


# ─── run_case / replay mechanics ─────────────────────────────────────────────


def test_set_args_marshaling():
    case = {
        "case_id": "marshal",
        "split": "held_in",
        "decider": "evolve_select.partition",
        "args": {
            "candidates": [
                {"id": "flow-a", "priority": 1, "labels": [], "issue_type": "task"},
            ],
            "inflight_keys": ["flow-a"],
            "hot_inflight": False,
            "open_pr_count": 0,
        },
        "expected": None,
    }
    result = run_case(case)
    assert result["skipped_in_flight"] == ["flow-a"]
    assert result["launch"] == []


def test_resolve_injection():
    resolved = []

    def fake_resolve(name):
        resolved.append(name)
        return types.SimpleNamespace(is_hot_change=lambda files: "SENTINEL")

    case = _case(args={"files": ["x.py"]})
    assert run_case(case, resolve=fake_resolve) == "SENTINEL"
    assert resolved == ["triage"]


def test_replay_reports_mismatch():
    good = _case()
    bad = _case(case_id="hot-bad", args={"files": ["lease.py"]}, expected=False)
    rows = replay([good, bad])
    assert rows[0] == {
        "case_id": "hot-stub",
        "split": "held_in",
        "decider": "triage.is_hot_change",
        "ok": True,
    }
    assert rows[1]["ok"] is False
    assert rows[1]["actual"] is True
    assert rows[1]["expected"] is False


def test_replay_accepts_resolve():
    def fake_resolve(name):
        return types.SimpleNamespace(is_hot_change=lambda files: False)

    rows = replay([_case()], resolve=fake_resolve)
    assert rows[0]["ok"] is True


def test_default_corpus_path_is_sibling():
    assert harness_corpus._default_path().name == "harness_corpus.json"
    assert harness_corpus._default_path().parent.name == "scripts"
