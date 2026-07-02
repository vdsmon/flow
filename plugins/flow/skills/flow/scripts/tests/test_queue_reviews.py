from __future__ import annotations

from typing import ClassVar

import pytest

import queue_reviews
from forge import ForgeError, NotSupported


class _FakeForge:
    """Records calls; scripts responses. Mirrors the Forge surface queue_reviews uses.

    `prs` maps an EXACT head ref -> the PullRequest detect_pr returns (None = no PR).
    `threads` maps a pr_id -> the review_threads list.
    """

    backend = "github"
    capabilities: ClassVar[list] = []

    def __init__(self, *, prs=None, threads=None, fail_threads_on=None, fail_detect_on=None):
        self.calls: list[tuple] = []
        self._prs = prs or {}
        self._threads = threads or {}
        self._fail_threads_on = fail_threads_on or set()
        self._fail_detect_on = fail_detect_on or set()

    def detect_pr(self, branch):
        self.calls.append(("detect_pr", branch))
        if branch in self._fail_detect_on:
            raise ForgeError(f"detect failed for {branch}")
        return self._prs.get(branch)

    def review_threads(self, pr_id):
        self.calls.append(("review_threads", pr_id))
        if pr_id in self._fail_threads_on:
            raise NotSupported("no review threads on this host")
        return self._threads.get(pr_id, [])


def _pr(number, *, state="OPEN"):
    return {
        "id": str(number),
        "url": f"https://example/pr/{number}",
        "number": number,
        "draft": False,
        "base": "main",
        "head": f"feat/flow-{number}",
        "state": state,
    }


def _thread(tid, severity, *, resolved=False, title="t"):
    return {
        "id": tid,
        "file": None,
        "line": None,
        "severity": severity,
        "title": title,
        "body": "b",
        "resolved": resolved,
        "author": "a",
        "parent_id": None,
    }


def test_slugged_ref_resolves_and_flags_major():
    # DISCRIMINATING: feed the SLUGGED head ref, not the bare feat/<key>. A bare-key
    # resolution (detect_pr("feat/flow-kx17.5")) would NOT match this ref and silently
    # flag nothing.
    ref = "feat/flow-kx17.5-queue-surfacing"
    fake = _FakeForge(
        prs={ref: _pr(310)},
        threads={"310": [_thread("rt1", "major")]},
    )
    results = queue_reviews.flag_parked_reviews(["flow-kx17.5"], [ref], fake)

    assert ("detect_pr", ref) in fake.calls
    assert len(results) == 1
    r = results[0]
    assert r["key"] == "flow-kx17.5"
    assert r["pr_id"] == "310"
    assert r["pr_url"] == "https://example/pr/310"
    assert r["unresolved_major"] == 1
    assert r["threads"] == [{"id": "rt1", "severity": "major", "title": "t"}]


def test_resolved_and_minor_threads_not_flagged():
    # only resolved threads + a leftover bot minor -> NOT flagged. proves the plain-comment
    # floor is NOT applied at this surfacing layer.
    ref = "feat/flow-abc-slug"
    fake = _FakeForge(
        prs={ref: _pr(100)},
        threads={
            "100": [
                _thread("a", "major", resolved=True),
                _thread("b", "minor"),
                _thread("c", "nit"),
            ]
        },
    )
    results = queue_reviews.flag_parked_reviews(["flow-abc"], [ref], fake)
    assert results == []


def test_no_matching_pr_ref_skipped():
    # parked key with no matching pr_refs entry -> skipped, no crash, no detect_pr.
    fake = _FakeForge(prs={}, threads={})
    results = queue_reviews.flag_parked_reviews(["flow-xyz"], ["feat/flow-other-slug"], fake)
    assert results == []
    assert all(c[0] != "detect_pr" for c in fake.calls)


def test_detect_pr_none_skipped():
    # ref present but the PR has been closed/merged (detect_pr -> None) -> skipped.
    ref = "feat/flow-gone-slug"
    fake = _FakeForge(prs={ref: None}, threads={})
    results = queue_reviews.flag_parked_reviews(["flow-gone"], [ref], fake)
    assert results == []


def test_forge_error_on_one_key_does_not_drop_others():
    # the failing key is FIRST to prove the loop continues past it.
    ref_bad = "feat/flow-bad-slug"
    ref_good = "feat/flow-good-slug"
    fake = _FakeForge(
        prs={ref_bad: _pr(1), ref_good: _pr(2)},
        threads={"2": [_thread("g", "critical")]},
        fail_threads_on={"1"},
    )
    results = queue_reviews.flag_parked_reviews(
        ["flow-bad", "flow-good"], [ref_bad, ref_good], fake
    )
    keys = {r["key"] for r in results}
    assert keys == {"flow-good"}


def test_not_supported_swallowed():
    ref = "feat/flow-nohost-slug"
    fake = _FakeForge(prs={ref: _pr(5)}, threads={}, fail_threads_on={"5"})
    results = queue_reviews.flag_parked_reviews(["flow-nohost"], [ref], fake)
    assert results == []


def test_detect_pr_error_swallowed():
    ref = "feat/flow-derr-slug"
    fake = _FakeForge(prs={}, threads={}, fail_detect_on={ref})
    results = queue_reviews.flag_parked_reviews(["flow-derr"], [ref], fake)
    assert results == []


def test_non_forge_error_on_one_key_does_not_drop_others():
    # a non-ForgeError leaking from an adapter (unexpected payload -> KeyError,
    # raw JSON parse error, ...) must be swallowed per-key like a ForgeError, or
    # the always-exit-0 status contract breaks with a traceback.
    ref_bad = "feat/flow-boom-slug"
    ref_good = "feat/flow-good-slug"

    class _Boom(_FakeForge):
        def review_threads(self, pr_id):
            if pr_id == "1":
                raise KeyError("unexpected payload shape")
            return super().review_threads(pr_id)

    fake = _Boom(
        prs={ref_bad: _pr(1), ref_good: _pr(2)},
        threads={"2": [_thread("g", "critical")]},
    )
    results = queue_reviews.flag_parked_reviews(
        ["flow-boom", "flow-good"], [ref_bad, ref_good], fake
    )
    assert {r["key"] for r in results} == {"flow-good"}


def test_detect_pr_payload_without_id_skipped():
    # a payload missing `id` cannot be probed for threads; skip the key instead
    # of raising KeyError out of the enrichment.
    ref = "feat/flow-noid-slug"
    pr = _pr(3)
    del pr["id"]
    fake = _FakeForge(prs={ref: pr}, threads={})
    results = queue_reviews.flag_parked_reviews(["flow-noid"], [ref], fake)
    assert results == []
    assert all(c[0] != "review_threads" for c in fake.calls)


@pytest.mark.parametrize(
    "severity,flagged",
    [
        ("critical", True),
        ("major", True),
        ("minor", False),
        ("nit", False),
        ("unknown", False),
    ],
)
def test_severity_floor_is_major_plus(severity, flagged):
    ref = "feat/flow-sev-slug"
    fake = _FakeForge(prs={ref: _pr(9)}, threads={"9": [_thread("x", severity)]})
    results = queue_reviews.flag_parked_reviews(["flow-sev"], [ref], fake)
    assert (len(results) == 1) is flagged


def test_cli_no_forge_block_emits_empty(tmp_path, capsys):
    import json

    (tmp_path / ".flow").mkdir()
    (tmp_path / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "beads"\n', encoding="utf-8"
    )
    rc = queue_reviews.cli_main(
        ["--workspace-root", str(tmp_path), "--keys", "flow-x", "--pr-refs", "feat/flow-x-s"]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == []


def test_cli_with_fake_factory(tmp_path, capsys):
    import json

    (tmp_path / ".flow").mkdir()
    (tmp_path / ".flow" / "workspace.toml").write_text(
        '[forge]\nbackend = "github"\n[forge.github]\n', encoding="utf-8"
    )
    ref = "feat/flow-kx17.5-queue-surfacing"
    fake = _FakeForge(prs={ref: _pr(42)}, threads={"42": [_thread("m", "major")]})
    rc = queue_reviews.cli_main(
        ["--workspace-root", str(tmp_path), "--keys", "flow-kx17.5", "--pr-refs", ref],
        forge_factory=lambda _cfg: fake,
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 1
    assert out[0]["key"] == "flow-kx17.5"
    assert out[0]["pr_id"] == "42"
