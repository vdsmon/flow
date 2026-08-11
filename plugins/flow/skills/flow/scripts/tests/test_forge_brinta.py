from __future__ import annotations

import json
import subprocess

import pytest

from forge import ForgeConfigError, ForgeError, make_forge
from forge_brinta import BrintaAdapter
from init import _reconcile_forge_backend

CONFIG = {"workspace": "ws", "repo_slug": "rs", "workspace_root": "."}


def _adapter(handler, rc: int = 0, stderr: str = "") -> tuple[BrintaAdapter, list[list[str]]]:
    calls: list[list[str]] = []

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        out = handler(args)
        return subprocess.CompletedProcess(args, rc, out, stderr)

    return BrintaAdapter(CONFIG, runner=run), calls


def _proxy_path(args: list[str]) -> str:
    return args[3] if args[:2] == ["brinta-ai", "bitbucket"] else ""


def test_factory_constructs_brinta_backend():
    forge = make_forge({"backend": "brinta", **CONFIG})
    assert isinstance(forge, BrintaAdapter)
    assert forge.backend == "brinta"


def test_requires_workspace_and_repo():
    with pytest.raises(ForgeConfigError):
        BrintaAdapter({"workspace_root": "."})


def test_api_calls_go_through_brinta_ai_with_2_0_prefix_stripped():
    fg, calls = _adapter(lambda a: "null")
    fg.detect_pr("feat/flow-x")
    assert calls, "no proxy call made"
    assert calls[0][:3] == ["brinta-ai", "bitbucket", "GET"]
    path = _proxy_path(calls[0])
    assert path.startswith("repositories/ws/rs/pullrequests?"), path
    assert "2.0/" not in path


def test_detect_pr_normalizes_like_bitbucket():
    listing = {
        "values": [
            {
                "id": 9,
                "source": {"branch": {"name": "feat/flow-x"}},
                "destination": {"branch": {"name": "main"}},
                "links": {"html": {"href": "https://bitbucket.org/ws/rs/pull-requests/9"}},
                "draft": False,
                "state": "OPEN",
            }
        ]
    }
    fg, _ = _adapter(lambda a: json.dumps(listing))
    pr = fg.detect_pr("feat/flow-x")
    assert pr is not None
    assert pr["id"] == "9"
    assert pr["head"] == "feat/flow-x"


def test_open_pr_posts_payload_as_json_argument():
    created = {
        "id": 3,
        "source": {"branch": {"name": "feat/b"}},
        "destination": {"branch": {"name": "main"}},
        "links": {"html": {"href": "u"}},
        "state": "OPEN",
    }
    fg, calls = _adapter(lambda a: json.dumps(created))
    fg.open_pr("main", "feat/b", "t", "body", draft=True)
    assert calls[0][2] == "POST"
    payload = json.loads(calls[0][4])
    assert payload["source"]["branch"]["name"] == "feat/b"
    assert payload["draft"] is True


def test_bodyless_success_returns_none_instead_of_json_error():
    fg, _ = _adapter(lambda a: "(HTTP 204, no body)\n")
    # update_pr_body returns None on success; must not raise on the sentinel line.
    assert fg.update_pr_body("3", "new body") is None


def test_proxy_failure_raises_forge_error_with_stderr():
    fg, _ = _adapter(lambda a: '{"error": {"message": "nope"}}', rc=1, stderr="HTTP 403")
    with pytest.raises(ForgeError, match="HTTP 403"):
        fg.pr_info("3")


def test_ci_rollup_reads_commit_statuses():
    statuses = {
        "values": [
            {"key": "BITBUCKET-PIPELINES", "name": "Pipeline #7", "state": "SUCCESSFUL"},
            {"key": "coderabbit", "name": "CodeRabbit", "state": "INPROGRESS"},
        ]
    }
    fg, calls = _adapter(lambda a: json.dumps(statuses))
    rollup = fg.ci_rollup("9")
    assert rollup["status"] == "green"
    assert "/pullrequests/9/statuses" in _proxy_path(calls[0])


def test_ci_rollup_pending_when_no_pipeline_entry():
    fg, _ = _adapter(lambda a: json.dumps({"values": []}))
    assert fg.ci_rollup("9")["status"] == "pending"


def test_ci_rollup_failed_states():
    statuses = {"values": [{"key": "BITBUCKET-PIPELINES", "name": "p", "state": "FAILED"}]}
    fg, _ = _adapter(lambda a: json.dumps(statuses))
    assert fg.ci_rollup("9")["status"] == "failed"


def test_bot_review_present_only_on_terminal_coderabbit_state():
    inprogress = {"values": [{"key": "x", "name": "CodeRabbit", "state": "INPROGRESS"}]}
    done = {"values": [{"key": "x", "name": "CodeRabbit", "state": "SUCCESSFUL"}]}
    fg, _ = _adapter(lambda a: json.dumps(inprogress))
    assert fg.bot_review_present("9") is False
    fg, _ = _adapter(lambda a: json.dumps(done))
    assert fg.bot_review_present("9") is True


# ─── init reconcile: the brinta opt-in survives reconfigure ──────────────────

DERIVED_BB = (
    '[forge]\nbackend = "bitbucket"\n\n[forge.bitbucket]\nworkspace = "ws"\nrepo_slug = "rs"\n'
)


def test_reconcile_keeps_brinta_backend_with_refreshed_coordinates():
    stored = '[forge]\nbackend = "brinta"\n\n[forge.brinta]\nworkspace = "old"\nrepo_slug = "old"\n'
    out = _reconcile_forge_backend(DERIVED_BB, stored)
    assert 'backend = "brinta"' in out
    assert "[forge.brinta]" in out
    assert 'workspace = "ws"' in out  # coordinates come from the remote, not the stored block
    assert "bitbucket]" not in out


def test_reconcile_leaves_plain_bitbucket_untouched():
    stored = (
        '[forge]\nbackend = "bitbucket"\n\n[forge.bitbucket]\nworkspace = "ws"\nrepo_slug = "rs"\n'
    )
    assert _reconcile_forge_backend(DERIVED_BB, stored) == DERIVED_BB


def test_reconcile_drops_brinta_on_github_move():
    derived_gh = '[forge]\nbackend = "github"\n\n[forge.github]\n'
    stored = '[forge]\nbackend = "brinta"\n\n[forge.brinta]\nworkspace = "ws"\nrepo_slug = "rs"\n'
    assert _reconcile_forge_backend(derived_gh, stored) == derived_gh


def test_reconcile_passes_through_when_nothing_stored():
    assert _reconcile_forge_backend(DERIVED_BB, None) == DERIVED_BB
    assert _reconcile_forge_backend(DERIVED_BB, "") == DERIVED_BB
