from __future__ import annotations

import json
import subprocess

import pytest

from forge import ForgeConfigError
from forge_bitbucket import BitbucketAdapter

CONFIG = {"workspace": "ws", "repo_slug": "rs", "workspace_root": "."}


def _adapter(handler) -> tuple[BitbucketAdapter, list[list[str]]]:
    calls: list[list[str]] = []

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        out = handler(args)
        return subprocess.CompletedProcess(args, 0, out, "")

    return BitbucketAdapter(CONFIG, runner=run), calls


def _api_path(args: list[str]) -> str:
    return args[2] if args[:2] == ["bkt", "api"] else ""


def test_requires_workspace_and_repo():
    with pytest.raises(ForgeConfigError):
        BitbucketAdapter({"workspace_root": "."})


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
        lambda a: json.dumps(listing) if "pullrequests?state=OPEN" in _api_path(a) else "null"
    )
    pr = fg.detect_pr("feature/flow-x")
    assert pr is not None
    assert pr["id"] == "9"
    assert pr["draft"] is True
    assert pr["head"] == "feature/flow-x"
    assert pr["base"] == "main"


def test_detect_pr_none_when_no_match():
    listing = {"values": [{"id": 1, "source": {"branch": {"name": "other"}}}]}
    fg, _ = _adapter(lambda a: json.dumps(listing))
    assert fg.detect_pr("feature/flow-x") is None


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
        path = _api_path(args)
        if "page=1" in path:
            return json.dumps(page1)
        if "page=2" in path:
            return json.dumps(page2)
        return "null"

    fg, calls = _adapter(h)
    pr = fg.detect_pr("feature/flow-x")
    assert pr is not None
    assert pr["id"] == "9"
    assert len([c for c in calls if "pullrequests?state=OPEN" in _api_path(c)]) == 2


def test_detect_pr_no_match_stops_at_last_page():
    listing = {"values": [{"id": 1, "source": {"branch": {"name": "other"}}}]}  # no `next`
    fg, calls = _adapter(lambda a: json.dumps(listing))
    assert fg.detect_pr("feature/flow-x") is None
    assert len(calls) == 1


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
        lambda a: json.dumps(_pr_view()) if _api_path(a).endswith("/pullrequests/9") else "null"
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
            if _api_path(a).endswith("/pullrequests/9")
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
    post = next(c for c in calls if "-X" in c and "POST" in c)
    payload = json.loads(post[post.index("-d") + 1])
    assert payload["draft"] is True
    assert payload["source"]["branch"]["name"] == "feature/flow-x"
    assert payload["destination"]["branch"]["name"] == "main"


def _checks(state: str):
    def h(args):
        if args[:3] == ["bkt", "pr", "checks"]:
            return f"  Pipeline    {state}\n  CodeRabbit  SUCCESSFUL\n"
        return "null"

    return h


def test_ci_rollup_green():
    fg, _ = _adapter(_checks("SUCCESSFUL"))
    assert fg.ci_rollup("9")["status"] == "green"


def test_ci_rollup_failed():
    fg, _ = _adapter(_checks("FAILED"))
    assert fg.ci_rollup("9")["status"] == "failed"


def test_ci_rollup_pending_inprogress():
    fg, _ = _adapter(_checks("INPROGRESS"))
    assert fg.ci_rollup("9")["status"] == "pending"


def test_ci_rollup_pending_when_no_pipeline_line():
    fg, _ = _adapter(lambda a: "  CodeRabbit  SUCCESSFUL\n")
    assert fg.ci_rollup("9")["status"] == "pending"


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
        path = _api_path(args)
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
        path = _api_path(args)
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


def _bot_checks(coderabbit_state: str | None):
    # `bkt pr checks` output with the pipeline still in progress, so the test
    # proves bot_review_present keys on the CodeRabbit line, not the pipeline.
    cr = f"  CodeRabbit  {coderabbit_state}\n" if coderabbit_state else ""

    def h(args):
        if args[:3] == ["bkt", "pr", "checks"]:
            return f"  Pipeline    INPROGRESS\n{cr}"
        return "null"

    return h


def test_bot_review_present_true_when_check_terminal():
    # Terminal CodeRabbit check = review done, regardless of finding count. On a
    # CLEAN review CR posts no "Actionable comments posted" comment, so the gate
    # must rely on the check-state, not a comment marker (flow-arva).
    fg, _ = _adapter(_bot_checks("SUCCESSFUL"))
    assert fg.bot_review_present("9") is True


def test_bot_review_present_false_when_check_inprogress():
    fg, _ = _adapter(_bot_checks("INPROGRESS"))
    assert fg.bot_review_present("9") is False


def test_bot_review_present_false_when_check_absent():
    # CR not registered yet: must not read "no line" as done (the bug: an empty
    # thread list at CI-green looked review-clean before CR ran).
    fg, _ = _adapter(_bot_checks(None))
    assert fg.bot_review_present("9") is False


def test_post_reply_parent_id_is_int():
    fg, calls = _adapter(lambda a: "null")
    fg.post_reply("9", "1", "Fixed in abc123.")
    post = next(c for c in calls if "-d" in c)
    payload = json.loads(post[post.index("-d") + 1])
    assert payload["parent"]["id"] == 1
    assert payload["content"]["raw"].startswith("Fixed in")


def test_resolve_thread_judges_by_resolution_not_resolved_flag():
    # The resolve POST returns a comment_resolution object with NO top-level
    # resolved flag; success must be judged by re-fetching .resolution != null.
    def h(args):
        path = _api_path(args)
        if path.endswith("/resolve"):
            return json.dumps({"type": "comment_resolution"})  # no `resolved` key
        if path.endswith("/comments/1"):
            return json.dumps({"id": 1, "resolution": {"type": "comment_resolution"}})
        return "null"

    fg, _ = _adapter(h)
    assert fg.resolve_thread("9", "1") is True


def test_resolve_thread_false_when_still_unresolved():
    def h(args):
        path = _api_path(args)
        if path.endswith("/resolve"):
            return json.dumps({"type": "comment_resolution"})
        if path.endswith("/comments/1"):
            return json.dumps({"id": 1, "resolution": None})
        return "null"

    fg, _ = _adapter(h)
    assert fg.resolve_thread("9", "1") is False


def _payload_for_path(calls: list[list[str]], path: str) -> dict:
    # select by API path, not the first -d: mark_ready's -d precedes merge's.
    c = next(c for c in calls if _api_path(c) == path)
    return json.loads(c[c.index("-d") + 1])


def _ran_prefix(calls: list[list[str]], prefix: list[str]) -> bool:
    return any(c[: len(prefix)] == prefix for c in calls)


def test_mark_ready_merge_delete_argv():
    fg, calls = _adapter(lambda a: "null")
    fg.mark_ready("9")
    fg.merge("9", squash=True)
    fg.delete_branch("feature/flow-x")

    base = "2.0/repositories/ws/rs"

    ready = next(c for c in calls if _api_path(c) == f"{base}/pullrequests/9")
    assert ready[ready.index("-X") + 1] == "PUT"
    assert _payload_for_path(calls, f"{base}/pullrequests/9") == {"draft": False}

    merge = next(c for c in calls if _api_path(c) == f"{base}/pullrequests/9/merge")
    assert merge[merge.index("-X") + 1] == "POST"
    assert _payload_for_path(calls, f"{base}/pullrequests/9/merge") == {"merge_strategy": "squash"}

    assert _ran_prefix(calls, ["git", "push", "origin", "--delete", "feature/flow-x"])


def test_merge_no_squash_emits_empty_payload():
    # squash=False sends {} (still carried as -d "{}" since {} is not None).
    fg, calls = _adapter(lambda a: "null")
    fg.merge("9", squash=False)
    assert _payload_for_path(calls, "2.0/repositories/ws/rs/pullrequests/9/merge") == {}


def test_capabilities_all_supported():
    fg, _ = _adapter(lambda a: "null")
    assert all(c["supported"] for c in fg.capabilities)


def test_set_default_reviewers_filters_author_and_puts():
    base = "2.0/repositories/ws/rs"
    me = {"account_id": "AUTHOR", "uuid": "{author-uuid}"}
    default_reviewers = {
        "values": [
            {"account_id": "AUTHOR", "uuid": "{author-uuid}"},  # dropped (author)
            {"account_id": "R1", "uuid": "{r1-uuid}"},
            {"account_id": "R2", "uuid": "{r2-uuid}"},
        ]
    }

    def handler(a):
        path = _api_path(a)
        if path == "2.0/user":
            return json.dumps(me)
        if path == f"{base}/default-reviewers":
            return json.dumps(default_reviewers)
        if path == f"{base}/pullrequests/9":
            return json.dumps({"id": 9})  # PUT echo
        return "null"

    fg, calls = _adapter(handler)
    fg.set_default_reviewers("9")

    # GET /user then GET default-reviewers then PUT the PR, author filtered out.
    assert _api_path(calls[0]) == "2.0/user"
    assert _api_path(calls[1]) == f"{base}/default-reviewers"
    put = next(c for c in calls if _api_path(c) == f"{base}/pullrequests/9")
    assert put[put.index("-X") + 1] == "PUT"
    payload = json.loads(put[put.index("-d") + 1])
    assert payload == {"reviewers": [{"uuid": "{r1-uuid}"}, {"uuid": "{r2-uuid}"}]}


def test_set_default_reviewers_empty_when_only_author():
    base = "2.0/repositories/ws/rs"

    def handler(a):
        path = _api_path(a)
        if path == "2.0/user":
            return json.dumps({"account_id": "AUTHOR", "uuid": "{a}"})
        if path == f"{base}/default-reviewers":
            return json.dumps({"values": [{"account_id": "AUTHOR", "uuid": "{a}"}]})
        return json.dumps({"id": 9})

    fg, calls = _adapter(handler)
    fg.set_default_reviewers("9")
    payload = _payload_for_path(calls, f"{base}/pullrequests/9")
    assert payload == {"reviewers": []}
