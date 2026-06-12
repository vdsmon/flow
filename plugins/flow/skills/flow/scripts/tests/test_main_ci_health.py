from __future__ import annotations

import json
import subprocess
from pathlib import Path

import main_ci_health as mch


def _run(rc: int, out: str = "", err: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)


# ---- classify_main_ci (pure, REST-shaped lowercase entries) ----


def test_green_all_success():
    runs = [{"name": "lint-and-test", "status": "completed", "conclusion": "success"}]
    assert mch.classify_main_ci(runs)["status"] == "green"


def test_one_failure_is_failed():
    runs = [
        {"name": "lint-and-test", "status": "completed", "conclusion": "success"},
        {"name": "deploy", "status": "completed", "conclusion": "failure"},
    ]
    out = mch.classify_main_ci(runs)
    assert out["status"] == "failed"
    assert out["failing_checks"] == ["deploy"]


def test_in_progress_is_pending():
    runs = [{"name": "lint-and-test", "status": "in_progress", "conclusion": None}]
    assert mch.classify_main_ci(runs)["status"] == "pending"


def test_cancelled_folds_to_pending():
    runs = [{"name": "lint-and-test", "status": "completed", "conclusion": "cancelled"}]
    assert mch.classify_main_ci(runs)["status"] == "pending"


def test_skipped_folds_to_pending():
    runs = [{"name": "lint-and-test", "status": "completed", "conclusion": "skipped"}]
    assert mch.classify_main_ci(runs)["status"] == "pending"


def test_neutral_folds_to_pending():
    runs = [{"name": "lint-and-test", "status": "completed", "conclusion": "neutral"}]
    assert mch.classify_main_ci(runs)["status"] == "pending"


def test_empty_check_runs_is_pending():
    assert mch.classify_main_ci([])["status"] == "pending"


def test_lowercase_status_uppercased_and_classified():
    # a completed-success entry must read green ONLY because status is uppercased to
    # COMPLETED before _classify_rollup (which compares raw status \!= "COMPLETED").
    runs = [{"name": "x", "status": "completed", "conclusion": "success"}]
    assert mch.classify_main_ci(runs)["status"] == "green"


# ---- probe (injected runner; never hits live gh) ----


def test_probe_green_with_sha():
    payload = json.dumps(
        [{"name": "lint-and-test", "status": "completed", "conclusion": "success"}]
    )

    def runner(args):
        if args[:2] == ["gh", "api"]:
            return _run(0, payload)
        return _run(0)

    out = mch.probe(Path("."), sha="abc123", runner=runner)
    assert out == {"status": "green", "sha": "abc123", "failing_checks": []}


def test_probe_failed_with_sha():
    payload = json.dumps(
        [{"name": "lint-and-test", "status": "completed", "conclusion": "failure"}]
    )
    out = mch.probe(Path("."), sha="dead", runner=lambda a: _run(0, payload))
    assert out["status"] == "failed"
    assert out["failing_checks"] == ["lint-and-test"]


def test_probe_gh_error_is_error_not_failed():
    # a failing gh runner (transient 401 / network) must read error → RESUME, never pause.
    out = mch.probe(Path("."), sha="abc", runner=lambda a: _run(1, "", "HTTP 401"))
    assert out == {"status": "error", "sha": "abc", "failing_checks": []}


def test_probe_resolves_sha_when_absent():
    def runner(args):
        if args[:2] == ["git", "rev-parse"]:
            return _run(0, "resolvedsha\n")
        if args[:2] == ["gh", "api"]:
            return _run(
                0, json.dumps([{"name": "t", "status": "completed", "conclusion": "success"}])
            )
        return _run(0)

    out = mch.probe(Path("."), runner=runner)
    assert out["status"] == "green"
    assert out["sha"] == "resolvedsha"


def test_probe_rev_parse_failure_is_error():
    def runner(args):
        if args[:2] == ["git", "rev-parse"]:
            return _run(128, "", "fatal")
        return _run(0)

    out = mch.probe(Path("."), runner=runner)
    assert out["status"] == "error"


def test_probe_retries_transient_then_succeeds():
    payload = json.dumps([{"name": "t", "status": "completed", "conclusion": "success"}])
    calls = {"n": 0}

    def runner(args):
        if args[:2] == ["gh", "api"]:
            calls["n"] += 1
            return _run(1) if calls["n"] == 1 else _run(0, payload)
        return _run(0)

    out = mch.probe(Path("."), sha="abc", runner=runner)
    assert out["status"] == "green"
    assert calls["n"] == 2
