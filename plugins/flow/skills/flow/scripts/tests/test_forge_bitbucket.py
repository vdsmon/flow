from __future__ import annotations

import json
import subprocess

import pytest

from forge import ForgeConfigError, ForgeError
from forge_bitbucket import BitbucketAdapter

CONFIG = {"workspace": "ws", "repo_slug": "rs", "workspace_root": "."}


def _adapter(handler, rc: int = 0, stderr: str = "") -> tuple[BitbucketAdapter, list[list[str]]]:
    calls: list[list[str]] = []

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        out = handler(args)
        return subprocess.CompletedProcess(args, rc, out, stderr)

    return BitbucketAdapter(CONFIG, runner=run), calls


def _proxy_path(args: list[str]) -> str:
    return args[3] if args[:2] == ["brinta-ai", "bitbucket"] else ""


def test_requires_workspace_and_repo():
    with pytest.raises(ForgeConfigError):
        BitbucketAdapter({"workspace_root": "."})


def test_api_calls_go_through_brinta_ai_with_2_0_prefix_stripped():
    fg, calls = _adapter(lambda a: "null")
    fg.detect_pr("feat/flow-x")
    assert calls, "no proxy call made"
    assert calls[0][:3] == ["brinta-ai", "bitbucket", "GET"]
    path = _proxy_path(calls[0])
    assert path.startswith("repositories/ws/rs/pullrequests?"), path
    assert "2.0/" not in path


def test_bodyless_success_returns_none_instead_of_json_error():
    fg, _ = _adapter(lambda a: "(HTTP 204, no body)\n")
    # update_pr_body returns None on success; must not raise on the sentinel line.
    assert fg.update_pr_body("3", "new body") is None


def test_proxy_failure_raises_forge_error_with_stderr():
    fg, _ = _adapter(lambda a: '{"error": {"message": "nope"}}', rc=1, stderr="HTTP 403")
    with pytest.raises(ForgeError, match="HTTP 403"):
        fg.pr_info("3")


def test_detect_pr_filters_by_source_branch():
    listing = {
        "values": [
            {"id": 1, "source": {"branch": {"name": "other"}}},
            {
                "id": 9,
                "source": {"branch": {"name": "feature/flow-x"}},
                "destination": {"branch": {"name": "main"}},
                "links": {"html": {"href": "https://bitbucket.org/ws/rs/pull-requests/9"}},
                "draft": True,
                "state": "OPEN",
            },
        ]
    }
    fg, _ = _adapter(
        lambda a: json.dumps(listing) if "state%20%3D%20%22OPEN%22" in _proxy_path(a) else "null"
    )
    pr = fg.detect_pr("feature/flow-x")
    assert pr is not None
    assert pr["id"] == "9"
    assert pr["draft"] is True
    assert pr["head"] == "feature/flow-x"
    assert pr["base"] == "main"


def test_detect_pr_sends_server_side_branch_and_state_filter():
    # Without `q=source.branch.name=...` the merged-state probe walked every PR in the
    # repo (3100+ witnessed). State must ride INSIDE q, never as the state= param:
    # Bitbucket ignores the param when q is present (live control returned an OPEN PR
    # from state=MERGED&q=<branch>, which finalize would have taken as merged proof).
    fg, calls = _adapter(lambda _a: json.dumps({"values": []}))
    assert fg.detect_pr("feature/flow-x", state="merged") is None
    (call,) = calls
    path = _proxy_path(call)
    assert "q=source.branch.name%20%3D%20%22feature%2Fflow-x%22" in path
    assert "state%20%3D%20%22MERGED%22" in path
    assert "state=MERGED&" not in path


def test_detect_pr_rejects_wrong_state_item_from_filtered_listing():
    # The client-side state check is the backstop for the param-ignored quirk: a
    # listing that leaks an OPEN item into a merged probe must not become merged proof.
    listing = {
        "values": [
            {
                "id": 9,
                "source": {"branch": {"name": "feature/flow-x"}},
                "destination": {"branch": {"name": "main"}},
                "state": "OPEN",
            }
        ]
    }
    fg, _ = _adapter(lambda _a: json.dumps(listing))
    assert fg.detect_pr("feature/flow-x", state="merged") is None


def test_detect_pr_selects_merged_state_and_emits_head_sha():
    listing = {
        "values": [
            {
                "id": 9,
                "source": {
                    "branch": {"name": "feature/flow-x"},
                    "commit": {"hash": "merged-head"},
                },
                "destination": {"branch": {"name": "main"}},
                "state": "MERGED",
            }
        ]
    }
    fg, calls = _adapter(lambda _a: json.dumps(listing))

    pr = fg.detect_pr("feature/flow-x", state="merged")

    assert pr is not None
    assert pr["head_sha"] == "merged-head"
    assert any("state%20%3D%20%22MERGED%22" in _proxy_path(call) for call in calls)


def test_source_url_is_commit_pinned_and_encodes_path():
    fg, calls = _adapter(lambda _a: "null")

    url = fg.source_url("9", "abc123", "src/a b.py", 10, 12)

    assert url == "https://bitbucket.org/ws/rs/src/abc123/src/a%20b.py#lines-10:12"
    assert calls == []


def test_detect_pr_follows_pagination():
    # >50 open PRs push the run's PR past page 1; detect_pr must follow `next`
    # (like _fetch_all_comments) or create_pr's resume idempotency breaks.
    page1 = {"values": [{"id": 1, "source": {"branch": {"name": "other"}}}], "next": "page2"}
    page2 = {
        "values": [
            {
                "id": 9,
                "source": {"branch": {"name": "feature/flow-x"}},
                "destination": {"branch": {"name": "main"}},
                "links": {"html": {"href": "https://bitbucket.org/ws/rs/pull-requests/9"}},
                "state": "OPEN",
            }
        ]
    }

    def h(args):
        path = _proxy_path(args)
        if "page=1" in path:
            return json.dumps(page1)
        if "page=2" in path:
            return json.dumps(page2)
        return "null"

    fg, calls = _adapter(h)
    pr = fg.detect_pr("feature/flow-x")
    assert pr is not None
    assert pr["id"] == "9"
    assert len([c for c in calls if "state%20%3D%20%22OPEN%22" in _proxy_path(c)]) == 2


def test_detect_pr_no_match_stops_at_last_page():
    listing = {"values": [{"id": 1, "source": {"branch": {"name": "other"}}}]}  # no `next`
    fg, calls = _adapter(lambda a: json.dumps(listing))
    assert fg.detect_pr("feature/flow-x") is None
    assert len(calls) == 1


def test_list_authored_filters_current_user_follows_pages_and_sorts_newest_first():
    page1 = {
        "values": [
            {
                "id": 7,
                "title": "Older",
                "updated_on": "2026-07-27T12:00:00Z",
                "author": {"uuid": "{me}"},
                "links": {"html": {"href": "https://bitbucket.org/ws/rs/pull-requests/7"}},
            },
            {"id": 6, "author": {"uuid": "{someone-else}"}},
        ],
        "next": "page2",
    }
    page2 = {
        "values": [
            {
                "id": 8,
                "title": "Newest",
                "updated_on": "2026-07-28T12:00:00Z",
                "author": {"uuid": "{me}"},
                "draft": True,
                "links": {"html": {"href": "https://bitbucket.org/ws/rs/pull-requests/8"}},
            }
        ]
    }

    def h(args):
        path = _proxy_path(args)
        if path == "user":
            return json.dumps({"uuid": "{me}"})
        if "page=1" in path:
            return json.dumps(page1)
        if "page=2" in path:
            return json.dumps(page2)
        return "null"

    fg, calls = _adapter(h)
    prs = fg.list_authored()
    assert [pr["number"] for pr in prs] == [8, 7]
    assert prs[0]["title"] == "Newest"
    assert prs[0]["updated_at"] == "2026-07-28T12:00:00Z"
    list_calls = [call for call in calls if "pullrequests?state=OPEN" in _proxy_path(call)]
    assert len(list_calls) == 2


def test_list_authored_requires_current_user_uuid():
    fg, _ = _adapter(lambda _args: json.dumps({"display_name": "No UUID"}))
    with pytest.raises(ForgeError, match="no uuid"):
        fg.list_authored()


def _pr_view(state: str = "OPEN") -> dict:
    return {
        "id": 9,
        "source": {"branch": {"name": "feature/flow-x"}},
        "destination": {"branch": {"name": "main"}},
        "links": {"html": {"href": "https://bitbucket.org/ws/rs/pull-requests/9"}},
        "draft": False,
        "state": state,
    }


def test_pr_info_reads_pr_by_id():
    fg, _ = _adapter(
        lambda a: json.dumps(_pr_view()) if _proxy_path(a).endswith("/pullrequests/9") else "null"
    )
    pr = fg.pr_info("9")
    assert pr is not None
    assert pr["id"] == "9"
    assert pr["head"] == "feature/flow-x"
    assert pr["base"] == "main"
    assert pr["state"] == "OPEN"


def test_pr_info_reads_merged_state():
    # pr_info reads ANY state (revise detects MERGED), unlike open-only detect_pr.
    fg, _ = _adapter(
        lambda a: (
            json.dumps(_pr_view(state="MERGED"))
            if _proxy_path(a).endswith("/pullrequests/9")
            else "null"
        )
    )
    pr = fg.pr_info("9")
    assert pr is not None
    assert pr["state"] == "MERGED"


def test_pr_info_none_when_absent():
    fg, _ = _adapter(lambda a: "null")
    assert fg.pr_info("9") is None


def test_open_pr_posts_payload():
    created = {
        "id": 42,
        "source": {"branch": {"name": "feature/flow-x"}},
        "destination": {"branch": {"name": "main"}},
        "links": {"html": {"href": "https://bitbucket.org/ws/rs/pull-requests/42"}},
        "draft": True,
        "state": "OPEN",
    }
    fg, calls = _adapter(lambda a: json.dumps(created))
    pr = fg.open_pr("main", "feature/flow-x", "feat: x", "body", draft=True)
    assert pr["number"] == 42
    assert calls[0][2] == "POST"
    payload = json.loads(calls[0][4])
    assert payload["draft"] is True
    assert payload["source"]["branch"]["name"] == "feature/flow-x"
    assert payload["destination"]["branch"]["name"] == "main"


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


def _comment(cid, *, raw, resolved=False, author="coderabbit", inline=True, parent=None):
    c = {
        "id": cid,
        "user": {"display_name": author},
        "content": {"raw": raw},
        "resolution": {"type": "comment_resolution"} if resolved else None,
    }
    if inline:
        c["inline"] = {"path": "a.py", "to": 12}
    if parent is not None:
        c["parent"] = {"id": parent}
    return c


def test_review_threads_filters_and_normalizes_with_pagination():
    page1 = {
        "values": [
            _comment(1, raw="**Critical fix**\nPotential issue here"),
            _comment(2, raw="**done**\nPotential issue", resolved=True),  # dropped: resolved
            _comment(3, raw="**human note**", author="someone"),  # dropped: not coderabbit
        ],
        "next": "page2",
    }
    page2 = {
        "values": [
            _comment(4, raw="Walkthrough summary"),  # dropped: not actionable
            _comment(5, raw="**Minor nit**\nsuggestion: rename"),
        ]
    }

    def h(args):
        path = _proxy_path(args)
        if "page=1" in path:
            return json.dumps(page1)
        if "page=2" in path:
            return json.dumps(page2)
        return "null"

    fg, _ = _adapter(h)
    threads = fg.review_threads("9")
    ids = sorted(t["id"] for t in threads)
    assert ids == ["1", "5"]  # only unresolved actionable coderabbit findings
    by_id = {t["id"]: t for t in threads}
    assert by_id["1"]["severity"] == "critical"
    assert by_id["1"]["title"] == "Critical fix"
    assert by_id["5"]["severity"] == "minor"
    assert by_id["1"]["file"] == "a.py"
    assert by_id["1"]["line"] == 12


# Real CodeRabbit inline header bytes captured from PR #2867 (real CR bytes).
_CR_MAJOR_HEADER = (
    "_\U0001f3af Functional Correctness_ | _\U0001f7e0 Major_ | _⚡ Quick win_\n\n"
    "**Narrow the `ValueError` scope in source resolution.**\n…"
)
_CR_MINOR_HEADER = (
    "_\U0001f4d0 Maintainability & Code Quality_ | _\U0001f7e1 Minor_ | _⚡ Quick win_\n\n"
    "**Remove ticket IDs from test docstrings/comments.**\n…"
)


def test_review_threads_surfaces_coderabbit_emoji_pipe_format():
    page = {"values": [_comment(1, raw=_CR_MAJOR_HEADER)]}

    def h(args):
        path = _proxy_path(args)
        if "page=1" in path:
            return json.dumps(page)
        return "null"

    fg, _ = _adapter(h)
    threads = fg.review_threads("9")
    assert len(threads) == 1
    t = threads[0]
    assert t["severity"] == "major"
    assert t["title"] == "Narrow the `ValueError` scope in source resolution."


def test_is_actionable_inline_recognizes_emoji_pipe_metadata():
    from forge_bitbucket import _is_actionable_inline

    assert _is_actionable_inline(_comment(1, raw=_CR_MAJOR_HEADER)) is True
    assert _is_actionable_inline(_comment(2, raw=_CR_MINOR_HEADER)) is True


def test_is_actionable_inline_old_format_still_actionable():
    from forge_bitbucket import _is_actionable_inline

    assert _is_actionable_inline(_comment(1, raw="**X**\nPotential issue here")) is True
    assert _is_actionable_inline(_comment(2, raw="**X**\nsuggestion: rename")) is True


def test_is_actionable_inline_excludes_walkthrough_summary():
    from forge_bitbucket import _is_actionable_inline

    assert _is_actionable_inline(_comment(1, raw="Walkthrough summary")) is False
    assert _is_actionable_inline(_comment(2, raw="Actionable comments posted: 2")) is False


def test_is_actionable_inline_rejects_non_actionable_inline():
    from forge_bitbucket import _is_actionable_inline

    assert _is_actionable_inline(_comment(1, raw="just some plain prose, nothing here")) is False
    assert _is_actionable_inline(_comment(2, raw="**bold only, no pipe**")) is False


def test_post_reply_parent_id_is_int():
    fg, calls = _adapter(lambda a: "null")
    fg.post_reply("9", "1", "Fixed in abc123.")
    payload = json.loads(calls[0][4])
    assert payload["parent"]["id"] == 1
    assert payload["content"]["raw"].startswith("Fixed in")


def test_resolve_thread_judges_by_resolution_not_resolved_flag():
    # The resolve POST returns a comment_resolution object with NO top-level
    # resolved flag; success must be judged by re-fetching .resolution != null.
    def h(args):
        path = _proxy_path(args)
        if path.endswith("/resolve"):
            return json.dumps({"type": "comment_resolution"})  # no `resolved` key
        if path.endswith("/comments/1"):
            return json.dumps({"id": 1, "resolution": {"type": "comment_resolution"}})
        return "null"

    fg, _ = _adapter(h)
    assert fg.resolve_thread("9", "1") is True


def test_resolve_thread_false_when_still_unresolved():
    def h(args):
        path = _proxy_path(args)
        if path.endswith("/resolve"):
            return json.dumps({"type": "comment_resolution"})
        if path.endswith("/comments/1"):
            return json.dumps({"id": 1, "resolution": None})
        return "null"

    fg, _ = _adapter(h)
    assert fg.resolve_thread("9", "1") is False


def _payload_for_path(calls: list[list[str]], path: str) -> dict:
    # select by API path, not the first payload: mark_ready's precedes merge's.
    c = next(c for c in calls if _proxy_path(c) == path)
    return json.loads(c[4])


def _ran_prefix(calls: list[list[str]], prefix: list[str]) -> bool:
    return any(c[: len(prefix)] == prefix for c in calls)


def test_mark_ready_merge_delete_argv():
    fg, calls = _adapter(lambda a: "null")
    fg.mark_ready("9")
    fg.merge("9", squash=True)
    fg.delete_branch("feature/flow-x")

    base = "repositories/ws/rs"

    ready = next(c for c in calls if _proxy_path(c) == f"{base}/pullrequests/9")
    assert ready[2] == "PUT"
    assert _payload_for_path(calls, f"{base}/pullrequests/9") == {"draft": False}

    merge = next(c for c in calls if _proxy_path(c) == f"{base}/pullrequests/9/merge")
    assert merge[2] == "POST"
    assert _payload_for_path(calls, f"{base}/pullrequests/9/merge") == {"merge_strategy": "squash"}

    assert _ran_prefix(calls, ["git", "push", "origin", "--delete", "feature/flow-x"])


def test_update_pr_body_puts_description():
    fg, calls = _adapter(lambda a: "null")
    fg.update_pr_body("9", "## Evidence\ngreen")

    path = "repositories/ws/rs/pullrequests/9"
    put = next(c for c in calls if _proxy_path(c) == path)
    assert put[2] == "PUT"
    assert _payload_for_path(calls, path) == {"description": "## Evidence\ngreen"}


def test_merge_no_squash_emits_empty_payload():
    # squash=False sends {} (still carried as an argument since {} is not None).
    fg, calls = _adapter(lambda a: "null")
    fg.merge("9", squash=False)
    assert _payload_for_path(calls, "repositories/ws/rs/pullrequests/9/merge") == {}


def test_set_default_reviewers_filters_author_and_puts():
    base = "repositories/ws/rs"
    me = {"account_id": "AUTHOR", "uuid": "{author-uuid}"}
    default_reviewers = {
        "values": [
            {"account_id": "AUTHOR", "uuid": "{author-uuid}"},  # dropped (author)
            {"account_id": "R1", "uuid": "{r1-uuid}"},
            {"account_id": "R2", "uuid": "{r2-uuid}"},
        ]
    }

    def handler(a):
        path = _proxy_path(a)
        if path == "user":
            return json.dumps(me)
        if path == f"{base}/default-reviewers":
            return json.dumps(default_reviewers)
        if path == f"{base}/pullrequests/9":
            return json.dumps({"id": 9})  # PUT echo
        return "null"

    fg, calls = _adapter(handler)
    fg.set_default_reviewers("9")

    # GET /user then GET default-reviewers then PUT the PR, author filtered out.
    assert _proxy_path(calls[0]) == "user"
    assert _proxy_path(calls[1]) == f"{base}/default-reviewers"
    put = next(c for c in calls if _proxy_path(c) == f"{base}/pullrequests/9")
    assert put[2] == "PUT"
    payload = json.loads(put[4])
    assert payload == {"reviewers": [{"uuid": "{r1-uuid}"}, {"uuid": "{r2-uuid}"}]}


def test_set_default_reviewers_empty_when_only_author():
    base = "repositories/ws/rs"

    def handler(a):
        path = _proxy_path(a)
        if path == "user":
            return json.dumps({"account_id": "AUTHOR", "uuid": "{a}"})
        if path == f"{base}/default-reviewers":
            return json.dumps({"values": [{"account_id": "AUTHOR", "uuid": "{a}"}]})
        return json.dumps({"id": 9})

    fg, calls = _adapter(handler)
    fg.set_default_reviewers("9")
    payload = _payload_for_path(calls, f"{base}/pullrequests/9")
    assert payload == {"reviewers": []}
