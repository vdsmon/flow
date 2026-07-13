"""Tests for senses_deadman.py (the nightly ship-event divergence + health-digest producer).

Two axes to keep honest:

1. The alarm's `observed` count is the per-CLOSE observed bucket (a window close whose key has a
   ship-event file), NOT the in-window ship-event count keyed by shipped_at. The two diverge
   whenever an in-window ship event belongs to a bead that did not close in the window; only the
   bucket length may reach `decide_alarm`.
2. Every bd/git subprocess routes through the ONE injected Runner, so the upfront fetch is
   assertable; `is_shipped` routes through the tracker seam (a fake here), never the Runner.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import senses_deadman as sd

# ─── Fakes ───────────────────────────────────────────────────────────────────


def _cp(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _ship(state: str, commit_sha: str | None = None) -> dict:
    if commit_sha is None and state != "not_yet_observed":
        evidence: dict | None = None
    else:
        evidence = {"commit_sha": commit_sha}
    return {"state": state, "shipped_at": None, "evidence": evidence, "source": "none"}


class _FakeTracker:
    """Records every is_shipped probe so the tests can assert it is (not) called."""

    def __init__(self, ships: dict[str, dict]):
        self._ships = ships
        self.probed: list[str] = []

    def is_shipped(self, key: str) -> dict:
        self.probed.append(key)
        return self._ships.get(key, _ship("not_shipped"))


class _Runner:
    """One injectable seam for every bd/git call, recording argv for assertions."""

    def __init__(
        self,
        *,
        closed: list[dict] | None = None,
        open_beads: list[dict] | None = None,
        open_list_rc: int = 0,
        closed_rc: int = 0,
        bodies: dict[str, str] | None = None,
        default_head: str = "origin/main",
        log_out: str = "",
        create_rc: int = 0,
    ):
        self.calls: list[list[str]] = []
        self._closed = closed or []
        self._open = open_beads or []
        self._open_list_rc = open_list_rc
        self._closed_rc = closed_rc
        self._bodies = bodies or {}
        self._default_head = default_head
        self._log_out = log_out
        self._create_rc = create_rc

    def __call__(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        if args[:2] == ["git", "fetch"]:
            return _cp()
        if args[:2] == ["git", "symbolic-ref"]:
            return _cp(self._default_head + "\n")
        if args[:2] == ["git", "log"]:
            return _cp(self._log_out)
        if args[:3] == ["git", "show", "-s"]:
            sha = args[-1]
            body = self._bodies.get(sha)
            return _cp(body, 0) if body is not None else _cp("", 1)
        if args[:3] == ["bd", "list", "--status"]:
            if args[3] == "closed":
                if self._closed_rc != 0:
                    return _cp("", self._closed_rc)
                return _cp(json.dumps(self._closed))
            if args[3] == "open":
                if self._open_list_rc != 0:
                    return _cp("", self._open_list_rc)
                return _cp(json.dumps(self._open))
        if args[:2] == ["bd", "create"]:
            return _cp("", self._create_rc)
        return _cp("", 0)

    @property
    def fetch_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["git", "fetch"]]

    @property
    def create_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["bd", "create"]]


NOW = "2026-07-10T00:00:00Z"


def _lag_aged() -> str:
    # A close well outside the 24h lag so the probe path is taken.
    return "2026-07-05T00:00:00Z"


# ─── classify_closes ─────────────────────────────────────────────────────────


def _classify(closes, *, observed_keys=frozenset(), ships=None, bodies=None, lag_hours=24.0):
    tracker = _FakeTracker(ships or {})
    body_map = bodies or {}
    buckets = sd.classify_closes(
        closes,
        now_iso=NOW,
        lag_hours=lag_hours,
        observed_keys=set(observed_keys),
        is_shipped_fn=tracker.is_shipped,
        commit_body_fn=body_map.get,
    )
    return buckets, tracker


def test_within_lag_close_excluded_and_not_probed():
    recent = "2026-07-09T18:00:00Z"  # 6h before NOW, inside the 24h lag
    buckets, tracker = _classify([{"key": "flow-a", "closed_at": recent}])
    assert buckets["within_lag"] == ["flow-a"]
    assert buckets["missing"] == []
    assert tracker.probed == []  # lag path never reaches the tracker


def test_existing_ship_event_file_suppresses_probe():
    buckets, tracker = _classify(
        [{"key": "flow-a", "closed_at": _lag_aged()}], observed_keys={"flow-a"}
    )
    assert buckets["observed"] == ["flow-a"]
    assert tracker.probed == []


def test_indeterminate_is_unmerged_never_missing():
    buckets, _ = _classify(
        [{"key": "flow-a", "closed_at": _lag_aged()}],
        ships={"flow-a": _ship("indeterminate")},
    )
    assert buckets["unmerged"] == ["flow-a"]
    assert buckets["missing"] == []


def test_not_yet_observed_with_no_lead_is_missing():
    buckets, _ = _classify(
        [{"key": "flow-a", "closed_at": _lag_aged()}],
        ships={"flow-a": _ship("not_yet_observed", commit_sha="deadbee")},
        bodies={"deadbee": "flow-a: standalone commit, no cover\n"},
    )
    assert buckets["missing"] == ["flow-a"]
    assert buckets["covered"] == []


def test_covered_when_body_names_observed_lead():
    buckets, _ = _classify(
        [{"key": "flow-child", "closed_at": _lag_aged()}],
        observed_keys={"flow-lead"},
        ships={"flow-child": _ship("not_yet_observed", commit_sha="abc123")},
        bodies={"abc123": "feat: x\n\nCloses flow-lead / flow-child\n"},
    )
    assert buckets["missing"] == []
    assert buckets["covered"] == [{"key": "flow-child", "lead": "flow-lead"}]


def test_covered_but_lead_unobserved_stays_missing():
    # The named lead has no primary ship event, so the sense is genuinely dark.
    buckets, _ = _classify(
        [{"key": "flow-child", "closed_at": _lag_aged()}],
        observed_keys=set(),
        ships={"flow-child": _ship("not_yet_observed", commit_sha="abc123")},
        bodies={"abc123": "feat: x\n\nCloses flow-lead / flow-child\n"},
    )
    assert buckets["missing"] == ["flow-child"]
    assert buckets["covered"] == []


def test_cover_word_boundary_rejects_parent_key():
    # observed lead flow-a1ti must NOT match the child token flow-a1ti.2 in the body.
    buckets, _ = _classify(
        [{"key": "flow-child", "closed_at": _lag_aged()}],
        observed_keys={"flow-a1ti"},
        ships={"flow-child": _ship("not_yet_observed", commit_sha="abc123")},
        bodies={"abc123": "feat: x\n\nCloses flow-a1ti.2 / flow-child\n"},
    )
    assert buckets["missing"] == ["flow-child"]


def test_cover_self_reference_alone_does_not_cover():
    buckets, _ = _classify(
        [{"key": "flow-child", "closed_at": _lag_aged()}],
        observed_keys=set(),
        ships={"flow-child": _ship("not_yet_observed", commit_sha="abc123")},
        bodies={"abc123": "feat: x\n\nCloses flow-child\n"},
    )
    assert buckets["missing"] == ["flow-child"]


def test_git_show_failure_stays_missing_no_crash():
    buckets, _ = _classify(
        [{"key": "flow-child", "closed_at": _lag_aged()}],
        observed_keys={"flow-lead"},
        ships={"flow-child": _ship("not_yet_observed", commit_sha="missing-sha")},
        bodies={},  # commit_body_fn returns None
    )
    assert buckets["missing"] == ["flow-child"]


# ─── decide_alarm ────────────────────────────────────────────────────────────


def test_alarm_arm1_full_darkness():
    assert sd.decide_alarm(0, 2, min_missing=2, max_gap=5) is True


def test_alarm_arm1_below_floor_no_fire():
    assert sd.decide_alarm(0, 1, min_missing=2, max_gap=5) is False


def test_alarm_arm2_partial_darkness_with_observed():
    assert sd.decide_alarm(2, 5, min_missing=2, max_gap=5) is True


def test_no_alarm_when_observed_and_below_gap():
    assert sd.decide_alarm(3, 4, min_missing=2, max_gap=5) is False


def test_no_alarm_zero_closes():
    assert sd.decide_alarm(0, 0, min_missing=2, max_gap=5) is False


# ─── run_record_summary ──────────────────────────────────────────────────────


def test_run_record_absent_is_not_armed():
    summary = sd.run_record_summary([], now_iso=NOW)
    assert summary["armed"] is False


def test_run_record_latest_end_fail_surfaced():
    entries = [
        {"schedule": "nightly", "phase": "start", "ts": "2026-07-09T00:17:00Z", "outcome": ""},
        {"schedule": "nightly", "phase": "end", "ts": "2026-07-09T00:30:00Z", "outcome": "ok"},
        {"schedule": "nightly", "phase": "start", "ts": "2026-07-10T00:17:00Z", "outcome": ""},
        {"schedule": "nightly", "phase": "end", "ts": "2026-07-10T00:31:00Z", "outcome": "fail"},
    ]
    summary = sd.run_record_summary(entries, now_iso=NOW)
    assert summary["armed"] is True
    nightly = summary["schedules"]["nightly"]
    assert nightly["latest_outcome"] == "fail"
    assert nightly["latest_end"] == "2026-07-10T00:31:00Z"


# ─── deadman() end-to-end ────────────────────────────────────────────────────


def _seed_workspace(root: Path, *, maintainer: bool = True, namespace: str = "demo") -> None:
    flow = root / ".flow"
    flow.mkdir(parents=True, exist_ok=True)
    marker = "\n[maintainer]\nself_target = true\n" if maintainer else ""
    (flow / "workspace.toml").write_text(
        f'[tracker]\nbackend = "beads"\n\n[memory]\nnamespace = "{namespace}"\n{marker}',
        encoding="utf-8",
    )


def _seed_ship_event(root: Path, namespace: str, key: str, shipped_at: str) -> None:
    ship_dir = root / ".flow" / namespace / "ship-events"
    ship_dir.mkdir(parents=True, exist_ok=True)
    (ship_dir / f"{key}.json").write_text(
        json.dumps({"ticket": key, "shipped_at": shipped_at}), encoding="utf-8"
    )


def _install_tracker(monkeypatch, ships: dict[str, dict]) -> _FakeTracker:
    tracker = _FakeTracker(ships)
    monkeypatch.setattr(sd, "make_tracker", lambda config: tracker)
    return tracker


def _run(root: Path, runner: _Runner, **kwargs):
    kwargs.setdefault("now_iso", NOW)
    kwargs.setdefault("run_record_path", root / "nope")
    return sd.deadman(root, runner=runner, **kwargs)


def _dark_closes(keys):
    return [{"id": k, "closed_at": _lag_aged()} for k in keys]


def _dark_ships(keys):
    return {k: _ship("not_yet_observed", commit_sha=f"sha{k}") for k in keys}


def test_fetch_is_exactly_one_regardless_of_key_count(tmp_path, monkeypatch):
    keys = ("flow-a", "flow-b", "flow-c")
    _seed_workspace(tmp_path)
    _install_tracker(monkeypatch, _dark_ships(keys))
    runner = _Runner(closed=_dark_closes(keys), bodies={f"shaflow-{c}": "" for c in "abc"})
    _digest, code = _run(tmp_path, runner)
    assert len(runner.fetch_calls) == 1
    assert code == 1  # observed==0 and missing(3) >= min_missing(2)


def test_two_observed_counts_diverge_only_bucket_drives_alarm(tmp_path, monkeypatch):
    # An in-window ship event for flow-z that did NOT close in the window: it lifts the
    # informational shipped_at count but must not touch the alarm's observed bucket.
    keys = ("flow-a", "flow-b")
    _seed_workspace(tmp_path)
    _seed_ship_event(tmp_path, "demo", "flow-z", "2026-07-08T00:00:00Z")
    _install_tracker(monkeypatch, _dark_ships(keys))
    runner = _Runner(closed=_dark_closes(keys), bodies={"shaflow-a": "", "shaflow-b": ""})
    digest, code = _run(tmp_path, runner)
    assert digest["divergence"]["observed"] == 0  # bucket: no window close is observed
    assert code == 1  # arm1: observed==0 and missing(2) >= min_missing(2)


def test_p0_skipped_when_open_stem_bead_exists(tmp_path, monkeypatch):
    keys = ("flow-a", "flow-b")
    _seed_workspace(tmp_path)
    _install_tracker(monkeypatch, _dark_ships(keys))
    runner = _Runner(
        closed=_dark_closes(keys),
        bodies={"shaflow-a": "", "shaflow-b": ""},
        open_beads=[{"id": "flow-p", "title": "senses-deadman: 4 merged closes unobserved"}],
    )
    digest, _ = _run(tmp_path, runner)
    assert digest["filed"]["action"] == "skipped_open"
    assert runner.create_calls == []


def test_p0_list_failure_skips_filing(tmp_path, monkeypatch):
    keys = ("flow-a", "flow-b")
    _seed_workspace(tmp_path)
    _install_tracker(monkeypatch, _dark_ships(keys))
    runner = _Runner(
        closed=_dark_closes(keys), bodies={"shaflow-a": "", "shaflow-b": ""}, open_list_rc=1
    )
    digest, _ = _run(tmp_path, runner)
    assert digest["filed"]["action"] == "skipped_list_error"
    assert runner.create_calls == []


def test_p0_create_carries_priority_and_description(tmp_path, monkeypatch):
    keys = ("flow-a", "flow-b")
    _seed_workspace(tmp_path)
    _install_tracker(monkeypatch, _dark_ships(keys))
    runner = _Runner(closed=_dark_closes(keys), bodies={"shaflow-a": "", "shaflow-b": ""})
    digest, _ = _run(tmp_path, runner)
    assert digest["filed"]["action"] == "filed"
    assert len(runner.create_calls) == 1
    argv = runner.create_calls[0]
    assert argv[:2] == ["bd", "create"]
    assert "-p" in argv
    assert argv[argv.index("-p") + 1] == "P0"
    assert "-d" in argv


def test_dry_run_never_fetches_files_or_quarantines(tmp_path, monkeypatch):
    keys = ("flow-a", "flow-b")
    _seed_workspace(tmp_path)
    ship_dir = tmp_path / ".flow" / "demo" / "ship-events"
    ship_dir.mkdir(parents=True)
    (ship_dir / "flow-corrupt.json").write_text("{", encoding="utf-8")
    _install_tracker(monkeypatch, _dark_ships(keys))
    runner = _Runner(closed=_dark_closes(keys), bodies={"shaflow-a": "", "shaflow-b": ""})
    digest, code = _run(tmp_path, runner, dry_run=True)
    assert runner.create_calls == []
    assert runner.fetch_calls == []
    assert not (tmp_path / ".flow" / "demo" / "ship-events.quarantine").exists()
    assert "read-only dry-run" in digest["trend"]["unavailable"]
    assert code == 1  # still a detected divergence


def test_digest_absent_inputs_and_revert_rate_never_called(tmp_path, monkeypatch):
    _seed_workspace(tmp_path)
    _install_tracker(monkeypatch, {})

    called = {"revert": False}

    def _revert_spy(*a, **k):
        called["revert"] = True
        raise AssertionError("compute_revert_rate must never run")

    monkeypatch.setattr(sd.metric, "compute_revert_rate", _revert_spy)
    monkeypatch.setattr(sd.metric, "compute", lambda *a, **k: {"shipped": 3})
    monkeypatch.setattr(sd.metric, "compute_time_to_pr", lambda *a, **k: {"median_hours": 4.0})
    monkeypatch.setattr(
        sd.metric, "compute_friction_per_run", lambda *a, **k: {"events_per_run": 1.0}
    )
    monkeypatch.setattr(sd.metric, "compute_recall_hit_rate", lambda *a, **k: {"hit_rate": 0.5})

    runner = _Runner(closed=[])
    digest, code = _run(tmp_path, runner)

    assert called["revert"] is False
    assert set(digest) >= {"divergence", "freshness", "trend", "liveness"}
    assert digest["freshness"]["newest_ship_event"] == "absent"
    assert digest["liveness"]["armed"] is False
    assert code == 0  # zero closes: no alarm
    # No quarantine sidecar minted against the foreign run-record.
    assert not (tmp_path / "nope.quarantine").exists()
    assert list(tmp_path.glob("**/run-record.jsonl.quarantine*")) == []


def test_digest_surfaces_quarantine_line_count(tmp_path, monkeypatch):
    _seed_workspace(tmp_path)
    _install_tracker(monkeypatch, {})
    qpath = tmp_path / ".flow" / "demo" / "ship-events.quarantine"
    qpath.parent.mkdir(parents=True, exist_ok=True)
    qpath.write_text('{"reason":"x","raw":"a"}\n{"reason":"y","raw":"b"}\n', encoding="utf-8")
    runner = _Runner(closed=[])
    digest, _ = _run(tmp_path, runner)
    assert digest["freshness"]["quarantine_lines"] == 2


def test_render_digest_has_four_sections(tmp_path, monkeypatch):
    _seed_workspace(tmp_path)
    _install_tracker(monkeypatch, {})
    runner = _Runner(closed=[])
    digest, _ = _run(tmp_path, runner)
    md = sd.render_digest(digest)
    assert "Divergence" in md
    assert "Telemetry freshness" in md
    assert "Metric trend" in md
    assert "Loop liveness" in md


# ─── gate ────────────────────────────────────────────────────────────────────


def test_non_maintainer_exits_4_zero_bd(tmp_path, monkeypatch):
    _seed_workspace(tmp_path, maintainer=False)
    # Force the gate branch: a machine-local `~/.flow/config.toml` maintainer pointer would
    # otherwise resolve a bare workspace as maintainer.
    monkeypatch.setattr(sd, "resolve_maintainer_repo", lambda root: None)

    def _boom(*a, **k):
        raise AssertionError("no subprocess in the non-maintainer gate")

    monkeypatch.setattr(subprocess, "run", _boom)
    code = sd.cli_main(["--workspace-root", str(tmp_path)])
    assert code == 4


# ─── error path (exit 2) ─────────────────────────────────────────────────────


def test_tracker_error_mid_window_maps_to_exit_2(tmp_path, monkeypatch):
    # is_shipped raises TrackerError on a bd read failure; the documented exit-2 contract must hold
    # rather than a traceback escaping deadman.
    _seed_workspace(tmp_path)

    class _RaisingTracker:
        def is_shipped(self, key: str) -> dict:
            raise sd.TrackerError("bd show failed")

    monkeypatch.setattr(sd, "make_tracker", lambda config: _RaisingTracker())
    runner = _Runner(closed=_dark_closes(("flow-a",)))
    digest, code = _run(tmp_path, runner)
    assert code == 2
    assert "bd show failed" in digest["error"]


def test_bd_list_closed_failure_maps_to_exit_2(tmp_path, monkeypatch):
    # A failed close enumeration is a _GatherError: the verdict is unreliable, so exit 2 holds.
    _seed_workspace(tmp_path)
    _install_tracker(monkeypatch, {})
    runner = _Runner(closed_rc=1)
    digest, code = _run(tmp_path, runner)
    assert code == 2
    assert "bd list closed failed" in digest["error"]


def test_render_digest_error_short_circuits():
    md = sd.render_digest({"error": "bd list closed failed (rc=1)"})
    assert "ERROR: bd list closed failed (rc=1)" in md
    # No zero-count sections: an error night must not read as a healthy digest in the log.
    assert "### Divergence" not in md
