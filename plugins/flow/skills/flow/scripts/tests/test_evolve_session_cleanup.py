from __future__ import annotations

import json
import os

import evolve_session_cleanup as esc
import lease

NOW = "2026-06-08T22:00:00Z"
NOW_EPOCH = 1780956000.0  # epoch of NOW (UTC)


# ─── fixtures ────────────────────────────────────────────────────────────────


def _worktree_cwd(repo, key, slug="wip"):
    return repo / ".flow" / "worktrees" / f"feat-{key}-{slug}"


def _run_dir(cwd, key):
    return cwd / ".flow" / "runs" / key


def _write_lease(run_dir, *, expired: bool = False, corrupt: bool = False) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    if corrupt:
        lease.run_lock_path(run_dir).write_text("{not json", encoding="utf-8")
        return
    now = "2020-01-01T00:00:00Z" if expired else NOW
    ttl = 1 if expired else 3600
    lease.acquire(
        run_dir,
        "run-test",
        ttl,
        now,
        stage="reflect",
        current_boot="boot-A",
        hostname="host-1",
        cwd=str(run_dir),
    )


_transcript_counter = [0]


def _transcript(tmp_path, *, fresh: bool = False, idle_secs: float | None = None) -> str:
    """A transcript file whose mtime is fresh (moments ago), idle (well past threshold),
    or idle by an explicit number of seconds (`idle_secs`, for boundary tests).

    Each call gets a unique filename so an overridden default never clobbers the
    mtime of a transcript an earlier call set.
    """
    _transcript_counter[0] += 1
    path = tmp_path / f"transcript-{_transcript_counter[0]}.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    if idle_secs is not None:
        mtime = NOW_EPOCH - idle_secs
    else:
        mtime = NOW_EPOCH - 5 if fresh else NOW_EPOCH - 10_000
    os.utime(path, (mtime, mtime))
    return str(path)


def _record(repo, tmp_path, *, key="flow-abc", **overrides):
    # a drain-launched bg run records cwd == repo root and the key in `intent`
    cwd = overrides.pop("cwd", None) or str(repo)
    intent = overrides.pop("intent", None)
    if intent is None:
        intent = f"/flow {key} --auto"
    defaults = dict(
        job_id="abc12345",
        job_dir=str(tmp_path / "jobs" / "abc12345"),
        session_id="abc12345-0000-0000-0000-000000000000",
        state="done",
        tempo="idle",
        cwd=cwd,
        intent=intent,
        link_scan_path=_transcript(tmp_path, fresh=False),
    )
    defaults.update(overrides)
    return esc.JobRecord(**defaults)


def _classify(repo, records, *, self_job=None, status="closed", threshold=300):
    return esc.classify(
        records,
        repo,
        NOW,
        self_job=self_job,
        idle_threshold_secs=threshold,
        bead_status=lambda key: status,
    )


def _setup_happy(tmp_path):
    """A repo + one fully-qualifying record (terminal bead supplied by the caller).

    The lease is EXPIRED (session ended) and the transcript is idle, so the only
    remaining gate is the bead status the caller chooses.
    """
    repo = tmp_path / "flow"
    repo.mkdir()
    rec = _record(repo, tmp_path)
    _write_lease(_run_dir(_worktree_cwd(repo, "flow-abc"), "flow-abc"), expired=True)
    return repo, rec


# ─── classify core: happy path + one negative per guard ──────────────────────


def test_happy_path_terminal_bead_is_stoppable(tmp_path):
    repo, rec = _setup_happy(tmp_path)
    for status in ("closed", "blocked", "deferred"):
        out = _classify(repo, [rec], status=status)
        assert len(out["stoppable"]) == 1, status
        entry = out["stoppable"][0]
        assert entry["session_id"] == rec.session_id
        assert entry["job_id"] == rec.job_id  # the `claude stop` handle
        assert entry["key"] == "flow-abc"
        assert entry["cwd"] == rec.cwd
        assert entry["job_dir"] == rec.job_dir


def test_bead_open_skips(tmp_path):
    repo, rec = _setup_happy(tmp_path)
    out = _classify(repo, [rec], status="open")
    assert out["stoppable"] == []
    assert out["skipped"][0]["reason"].startswith("bead flow-abc not terminal")


def test_lease_live_skips(tmp_path):
    repo, rec = _setup_happy(tmp_path)
    _write_lease(_run_dir(_worktree_cwd(repo, "flow-abc"), "flow-abc"))  # live (not expired)
    out = _classify(repo, [rec])
    assert out["stoppable"] == []
    assert out["skipped"][0]["reason"] == "lease is live"


def test_lease_corrupt_skips(tmp_path):
    repo = tmp_path / "flow"
    repo.mkdir()
    rec = _record(repo, tmp_path)
    _write_lease(_run_dir(_worktree_cwd(repo, "flow-abc"), "flow-abc"), corrupt=True)
    out = _classify(repo, [rec])
    assert out["stoppable"] == []
    assert out["skipped"][0]["reason"] == "lease is corrupt"


def test_lease_reboot_clearable_proceeds(tmp_path, monkeypatch):
    # an expired lease from a previous boot on THIS host is non-live → proceeds;
    # classify passes boot/hostname so it reads expired_reboot_clearable, not
    # expired_foreign (display accuracy; neither state is a skip).
    repo = tmp_path / "flow"
    repo.mkdir()
    rec = _record(repo, tmp_path)
    run_dir = _run_dir(_worktree_cwd(repo, "flow-abc"), "flow-abc")
    lease.acquire(
        run_dir,
        "run-test",
        1,
        "2020-01-01T00:00:00Z",
        stage="reflect",
        current_boot="boot-OLD",
        hostname=lease.hostname(),
        cwd=str(run_dir),
    )
    monkeypatch.setattr(lease, "boot_id", lambda runner=None: "boot-NEW")
    out = _classify(repo, [rec], status="closed")
    assert len(out["stoppable"]) == 1
    assert "lease expired_reboot_clearable" in out["stoppable"][0]["reason"]


def test_fresh_transcript_mtime_skips(tmp_path):
    # state=done, tempo=idle, bead terminal, lease expired, but the transcript was written moments
    # ago → still mid-reflect (tempo lags) → skip. The load-bearing case.
    repo = tmp_path / "flow"
    repo.mkdir()
    rec = _record(repo, tmp_path, link_scan_path=_transcript(tmp_path, fresh=True))
    _write_lease(_run_dir(_worktree_cwd(repo, "flow-abc"), "flow-abc"), expired=True)
    out = _classify(repo, [rec])
    assert out["stoppable"] == []
    assert out["skipped"][0]["reason"] == "transcript not provably idle"


def test_missing_transcript_path_skips(tmp_path):
    repo = tmp_path / "flow"
    repo.mkdir()
    rec = _record(repo, tmp_path, link_scan_path="")
    _write_lease(_run_dir(_worktree_cwd(repo, "flow-abc"), "flow-abc"), expired=True)
    out = _classify(repo, [rec])
    assert out["stoppable"] == []
    assert out["skipped"][0]["reason"] == "transcript not provably idle"


def test_unreadable_transcript_path_skips(tmp_path):
    repo = tmp_path / "flow"
    repo.mkdir()
    rec = _record(repo, tmp_path, link_scan_path=str(tmp_path / "does-not-exist.jsonl"))
    _write_lease(_run_dir(_worktree_cwd(repo, "flow-abc"), "flow-abc"), expired=True)
    out = _classify(repo, [rec])
    assert out["stoppable"] == []
    assert out["skipped"][0]["reason"] == "transcript not provably idle"


def test_tempo_not_idle_skips(tmp_path):
    # tempo is the one hard activity signal. Never stop a session reporting work, regardless of
    # state.
    repo, _ = _setup_happy(tmp_path)
    rec = _record(tmp_path / "flow", tmp_path, tempo="active")
    out = _classify(repo, [rec])
    assert out["stoppable"] == []
    assert out["skipped"][0]["reason"] == "tempo not idle (tempo='active')"


def test_stale_working_state_is_stoppable_via_override(tmp_path):
    # the PRIMARY case: a finished bg run rests at state='working' (a session_cron keepalive or a
    # daemon that never flips the field). With a long-idle transcript (past the stale threshold), a
    # dead lease, and a terminal bead, it is stoppable: `state` is an unreliable proxy, overridden
    # by the three direct signals.
    repo, _ = _setup_happy(tmp_path)
    rec = _record(
        tmp_path / "flow",
        tmp_path,
        state="working",
        link_scan_path=_transcript(tmp_path, idle_secs=10_000),
    )
    out = _classify(repo, [rec])
    assert len(out["stoppable"]) == 1
    entry = out["stoppable"][0]
    assert entry["job_id"] == rec.job_id
    assert "stale-working" in entry["reason"]


def test_stale_working_idle_only_past_short_threshold_skips(tmp_path):
    # idle past the normal threshold (300s) but NOT the longer stale threshold (600s):
    # too soon to override a still-'working' state field → skip (fail-safe).
    repo, _ = _setup_happy(tmp_path)
    rec = _record(
        tmp_path / "flow",
        tmp_path,
        state="working",
        link_scan_path=_transcript(tmp_path, idle_secs=450),
    )
    out = _classify(repo, [rec])
    assert out["stoppable"] == []
    assert "stale threshold" in out["skipped"][0]["reason"]


def test_blocked_tempo_terminal_bead_is_stoppable(tmp_path):
    # a bg run that DIED in tempo=blocked (rate limit, permission ask, auth outage)
    # is eligible once the three doneness signals hold: dead lease, transcript idle
    # past the stale threshold, terminal bead. state=blocked is not a clean terminal,
    # so the longer stale bar applies.
    repo, _ = _setup_happy(tmp_path)
    for status in ("closed", "blocked", "deferred"):
        rec = _record(
            tmp_path / "flow",
            tmp_path,
            state="blocked",
            tempo="blocked",
            link_scan_path=_transcript(tmp_path, idle_secs=10_000),
        )
        out = _classify(repo, [rec], status=status)
        assert len(out["stoppable"]) == 1, status
        entry = out["stoppable"][0]
        assert entry["job_id"] == rec.job_id
        assert "stale-blocked" in entry["reason"]


def test_blocked_tempo_open_bead_skips(tmp_path):
    # a genuine needs-input run blocked on a permission ask: bead still open → skip.
    # the terminal-bead gate separates a dead-blocked zombie from a live one.
    repo, _ = _setup_happy(tmp_path)
    rec = _record(
        tmp_path / "flow",
        tmp_path,
        state="blocked",
        tempo="blocked",
        link_scan_path=_transcript(tmp_path, idle_secs=10_000),
    )
    out = _classify(repo, [rec], status="open")
    assert out["stoppable"] == []
    assert out["skipped"][0]["reason"].startswith("bead flow-abc not terminal")


def test_blocked_tempo_idle_only_past_short_threshold_skips(tmp_path):
    # tempo=blocked idle past the short threshold (300s) but not the stale one (600s):
    # too soon to override the stale field → skip (fail-safe).
    repo, _ = _setup_happy(tmp_path)
    rec = _record(
        tmp_path / "flow",
        tmp_path,
        state="blocked",
        tempo="blocked",
        link_scan_path=_transcript(tmp_path, idle_secs=450),
    )
    out = _classify(repo, [rec])
    assert out["stoppable"] == []
    assert "stale threshold" in out["skipped"][0]["reason"]


def test_clean_terminal_state_uses_short_threshold(tmp_path):
    # a clean state='done' run idle past the SHORT threshold (but not the stale one) is stoppable:
    # the longer bar applies only when overriding a non-terminal state.
    repo, _ = _setup_happy(tmp_path)
    rec = _record(
        tmp_path / "flow",
        tmp_path,
        state="done",
        link_scan_path=_transcript(tmp_path, idle_secs=450),
    )
    out = _classify(repo, [rec])
    assert len(out["stoppable"]) == 1
    assert out["stoppable"][0]["reason"].startswith("done/idle")


def test_just_finished_session_stoppable_under_short_bars(tmp_path):
    # contract lock (flow-opnl): the drain's post-`done` final session sweep passes short
    # --idle-threshold-secs AND --stale-idle-threshold-secs so a freshly-finished bg run (state
    # stuck at working/blocked, transcript idle only ~60s) is reaped instead of leaking as a panel.
    # At `done` every lease is freed, so the three hard signals (lease non-live, tempo idle/blocked,
    # bead terminal) already prove doneness and the long 600s stale bar is unnecessary. This pins
    # that the CLI flags reach the stale-state transcript-idle gate; it breaks if the flag is ever
    # removed.
    repo, _ = _setup_happy(tmp_path)
    for state, tempo in (("working", "idle"), ("blocked", "blocked")):
        rec = _record(
            tmp_path / "flow",
            tmp_path,
            state=state,
            tempo=tempo,
            link_scan_path=_transcript(tmp_path, idle_secs=60),
        )
        out = esc.classify(
            [rec],
            repo,
            NOW,
            self_job=None,
            idle_threshold_secs=45,
            stale_idle_threshold_secs=45,
            bead_status=lambda key: "closed",
        )
        assert len(out["stoppable"]) == 1, (state, tempo)
        assert out["stoppable"][0]["job_id"] == rec.job_id


def test_intent_not_a_flow_launch_skips(tmp_path):
    # a foreign / non-flow job (e.g. intent "ft-1121") has no /flow <key> --auto
    repo = tmp_path / "flow"
    repo.mkdir()
    rec = _record(repo, tmp_path, intent="ft-1121 status check")
    out = _classify(repo, [rec])
    assert out["stoppable"] == []
    assert out["skipped"][0]["reason"] == "intent is not a /flow <key> --auto launch"


def test_cwd_not_repo_root_skips(tmp_path):
    # a flow job whose cwd is another project (not THIS repo's root)
    repo = tmp_path / "flow"
    repo.mkdir()
    rec = _record(repo, tmp_path, cwd=str(tmp_path / "other-project"))
    out = _classify(repo, [rec])
    assert out["stoppable"] == []
    assert out["skipped"][0]["reason"] == "cwd is not this repo's root"


def test_absent_worktree_proceeds(tmp_path):
    # the worktree was already reaped (common post-reap case) → lease "absent"
    # (non-live) → cleanup PROCEEDS, not skip. Treating absent as skip would mean
    # cleanup never fires once reap tears down the worktree.
    repo = tmp_path / "flow"
    repo.mkdir()
    rec = _record(repo, tmp_path)  # no lease / worktree written for flow-abc
    out = _classify(repo, [rec], status="closed")
    assert len(out["stoppable"]) == 1
    assert "lease absent" in out["stoppable"][0]["reason"]


def test_self_job_skips_even_when_qualified(tmp_path):
    repo, rec = _setup_happy(tmp_path)
    out = _classify(repo, [rec], self_job=rec.job_id)
    assert out["stoppable"] == []
    assert out["skipped"][0]["reason"] == "self-job"


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _jobs_root_with(tmp_path, *records_json):
    root = tmp_path / "jobs"
    root.mkdir(parents=True, exist_ok=True)
    for i, payload in enumerate(records_json):
        d = root / f"{i:08x}"  # an 8-hex job dir basename, distinct per record
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(payload, encoding="utf-8")
    return root


def test_enumerate_jobs_skips_state_json_vanished_after_glob(tmp_path):
    # the glob->read race: the drain's own A2 step rm -rf's job dirs each turn,
    # so a state.json can vanish between the glob and the read. A dangling
    # symlink makes read_text raise the same OSError the race does; the record
    # is omitted (cannot classify), the sibling still enumerates.
    jobs_root = _jobs_root_with(tmp_path, json.dumps({"sessionId": "s-1", "tempo": "idle"}))
    racy = jobs_root / "deadbeef"
    racy.mkdir()
    (racy / "state.json").symlink_to(jobs_root / "gone" / "state.json")
    records = esc._enumerate_jobs(jobs_root)
    assert [r.job_id for r in records] == ["00000000"]


def test_cli_non_maintainer_exit_4(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("maintainer._global_config_path", lambda: tmp_path / "absent.toml")
    plain = tmp_path / "proj"
    (plain / ".flow").mkdir(parents=True)
    (plain / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "beads"\n', encoding="utf-8"
    )
    rc = esc.cli_main(["--workspace-root", str(plain), "--jobs-root", str(tmp_path / "jobs")])
    assert rc == 4
    assert "nothing to clean" in capsys.readouterr().err


def test_cli_happy_path_prints_expected_json(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "flow"
    repo.mkdir()
    monkeypatch.setattr(esc, "resolve_maintainer_repo", lambda ws: repo)
    monkeypatch.setattr(esc, "_bd_status_lookup", lambda: lambda key: "closed")

    # the lease lives in the worktree; the job records cwd == repo root + the key in intent
    _write_lease(_run_dir(_worktree_cwd(repo, "flow-abc"), "flow-abc"), expired=True)
    transcript = _transcript(tmp_path, fresh=False)
    state = json.dumps(
        {
            "state": "done",
            "tempo": "idle",
            "cwd": str(repo),
            "intent": "/flow flow-abc --auto",
            "sessionId": "deadbeef-0000-0000-0000-000000000000",
            "linkScanPath": transcript,
        }
    )
    # an empty/malformed sibling state.json must be skipped, not crash enumeration
    jobs_root = _jobs_root_with(tmp_path, state, "", "{not json")

    rc = esc.cli_main(["--workspace-root", str(repo), "--jobs-root", str(jobs_root), "--now", NOW])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["stoppable"]) == 1
    entry = out["stoppable"][0]
    assert entry["session_id"] == "deadbeef-0000-0000-0000-000000000000"
    assert entry["key"] == "flow-abc"


def test_cli_self_job_threads_through(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "flow"
    repo.mkdir()
    monkeypatch.setattr(esc, "resolve_maintainer_repo", lambda ws: repo)
    monkeypatch.setattr(esc, "_bd_status_lookup", lambda: lambda key: "closed")

    _write_lease(_run_dir(_worktree_cwd(repo, "flow-abc"), "flow-abc"), expired=True)
    transcript = _transcript(tmp_path, fresh=False)
    state = json.dumps(
        {
            "state": "done",
            "tempo": "idle",
            "cwd": str(repo),
            "intent": "/flow flow-abc --auto",
            "sessionId": "deadbeef-0000-0000-0000-000000000000",
            "linkScanPath": transcript,
        }
    )
    jobs_root = _jobs_root_with(tmp_path, state)
    self_basename = sorted(p.name for p in jobs_root.iterdir())[0]

    rc = esc.cli_main(
        [
            "--workspace-root",
            str(repo),
            "--jobs-root",
            str(jobs_root),
            "--now",
            NOW,
            "--self-job",
            self_basename,
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["stoppable"] == []
    assert out["skipped"][0]["reason"] == "self-job"


def test_cli_stale_threshold_flag_threads_through(tmp_path, monkeypatch, capsys):
    # a state='working' job (the common finished-bg-run case) idle past the stale
    # threshold is stoppable, and the stoppable entry carries job_id (the stop handle).
    repo = tmp_path / "flow"
    repo.mkdir()
    monkeypatch.setattr(esc, "resolve_maintainer_repo", lambda ws: repo)
    monkeypatch.setattr(esc, "_bd_status_lookup", lambda: lambda key: "closed")

    _write_lease(_run_dir(_worktree_cwd(repo, "flow-abc"), "flow-abc"), expired=True)
    transcript = _transcript(tmp_path, idle_secs=10_000)
    state = json.dumps(
        {
            "state": "working",
            "tempo": "idle",
            "cwd": str(repo),
            "intent": "/flow flow-abc --auto",
            "sessionId": "deadbeef-0000-0000-0000-000000000000",
            "linkScanPath": transcript,
        }
    )
    jobs_root = _jobs_root_with(tmp_path, state)
    job_basename = sorted(p.name for p in jobs_root.iterdir())[0]

    rc = esc.cli_main(
        [
            "--workspace-root",
            str(repo),
            "--jobs-root",
            str(jobs_root),
            "--now",
            NOW,
            "--stale-idle-threshold-secs",
            "600",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["stoppable"]) == 1
    assert out["stoppable"][0]["job_id"] == job_basename
