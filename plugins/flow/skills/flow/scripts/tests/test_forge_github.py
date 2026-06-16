from __future__ import annotations

import json
import subprocess

import pytest

from forge import NotSupported
from forge_github import GitHubAdapter

Recorder = list[list[str]]


def _is_graphql(args: list[str]) -> bool:
    return args[:3] == ["gh", "api", "graphql"]


def _graphql_mutation(args: list[str]) -> str:
    """'resolve' / 'reply' / 'read' — keyed on the query text in the argv."""
    blob = " ".join(args)
    if "resolveReviewThread" in blob:
        return "resolve"
    if "addPullRequestReviewThreadReply" in blob:
        return "reply"
    return "read"


def _adapter(responses: dict | None = None) -> tuple[GitHubAdapter, Recorder]:
    """GitHubAdapter with a fake runner that pattern-matches argv prefixes.

    `responses` keys: 'list' (json str), 'create' (stdout), 'view' (json str),
    'repo_view' (json str), 'threads'/'resolve'/'reply' (graphql json strs).
    """
    responses = responses or {}
    calls: Recorder = []

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, responses.get("list", "[]"), "")
        if args[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(args, 0, responses.get("create", ""), "")
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(args, 0, responses.get("view", "{}"), "")
        if args[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(
                args, 0, responses.get("repo_view", '{"nameWithOwner":"o/r"}'), ""
            )
        if _is_graphql(args):
            kind = _graphql_mutation(args)
            key = {"read": "threads", "resolve": "resolve", "reply": "reply"}[kind]
            return subprocess.CompletedProcess(args, 0, responses.get(key, "{}"), "")
        if args[:3] in (["gh", "pr", "ready"], ["gh", "pr", "merge"]):
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["git", "push", "origin"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", f"unexpected {args}")

    return GitHubAdapter({"workspace_root": "."}, runner=run), calls


def _ran(calls: Recorder, prefix: list[str]) -> bool:
    return any(c[: len(prefix)] == prefix for c in calls)


def _thread_node(
    *,
    tid: str = "T1",
    resolved: bool = False,
    path: str = "src/a.py",
    line: int | None = 10,
    body: str = "**Issue title**\nfix this",
    author: str | None = "coderabbitai",
    state: str | None = "CHANGES_REQUESTED",
) -> dict:
    comment: dict = {"body": body}
    comment["author"] = {"login": author} if author is not None else None
    comment["pullRequestReview"] = {"state": state} if state is not None else None
    return {
        "id": tid,
        "isResolved": resolved,
        "path": path,
        "line": line,
        "comments": {"nodes": [comment]},
    }


def _threads_response(nodes: list[dict]) -> str:
    return json.dumps(
        {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}}
    )


def _resolve_response(is_resolved: bool) -> str:
    return json.dumps({"data": {"resolveReviewThread": {"thread": {"isResolved": is_resolved}}}})


def test_detect_pr_parses_first_item():
    listing = json.dumps(
        [
            {
                "number": 7,
                "url": "https://github.com/o/r/pull/7",
                "isDraft": True,
                "baseRefName": "main",
                "headRefName": "feature/flow-x",
                "state": "OPEN",
            }
        ]
    )
    fg, _ = _adapter({"list": listing})
    pr = fg.detect_pr("feature/flow-x")
    assert pr is not None
    assert pr["number"] == 7
    assert pr["id"] == "7"
    assert pr["draft"] is True
    assert pr["url"].endswith("/pull/7")


def test_detect_pr_none_when_empty():
    fg, _ = _adapter({"list": "[]"})
    assert fg.detect_pr("feature/flow-x") is None


def test_pr_info_parses_object():
    view = json.dumps(
        {
            "number": 7,
            "url": "https://github.com/o/r/pull/7",
            "isDraft": False,
            "baseRefName": "main",
            "headRefName": "feature/flow-x",
            "state": "OPEN",
        }
    )
    fg, _ = _adapter({"view": view})
    pr = fg.pr_info("7")
    assert pr is not None
    assert pr["number"] == 7
    assert pr["id"] == "7"
    assert pr["head"] == "feature/flow-x"
    assert pr["state"] == "OPEN"


def test_pr_info_reads_merged_state():
    # Unlike detect_pr (open-only), pr_info reads ANY state so revise can detect MERGED.
    view = json.dumps(
        {
            "number": 7,
            "url": "https://github.com/o/r/pull/7",
            "isDraft": False,
            "baseRefName": "main",
            "headRefName": "feature/flow-x",
            "state": "MERGED",
        }
    )
    fg, _ = _adapter({"view": view})
    pr = fg.pr_info("7")
    assert pr is not None
    assert pr["state"] == "MERGED"


def test_pr_info_none_when_empty():
    fg, _ = _adapter({"view": "{}"})
    assert fg.pr_info("7") is None


def test_pr_info_none_when_null():
    fg, _ = _adapter({"view": "null"})
    assert fg.pr_info("7") is None


def test_pr_info_none_on_garbage():
    fg, _ = _adapter({"view": "not json"})
    assert fg.pr_info("7") is None


def test_open_pr_omits_draft_flag_when_ready():
    fg, calls = _adapter({"create": "https://github.com/o/r/pull/42\n"})
    pr = fg.open_pr("main", "feature/flow-x", "feat: x", "body", draft=False)
    assert pr["number"] == 42
    create = next(c for c in calls if c[:3] == ["gh", "pr", "create"])
    assert "--draft" not in create


def test_open_pr_passes_draft_flag():
    fg, calls = _adapter({"create": "https://github.com/o/r/pull/9\n"})
    fg.open_pr("main", "feature/flow-x", "feat: x", "body", draft=True)
    create = next(c for c in calls if c[:3] == ["gh", "pr", "create"])
    assert "--draft" in create


def test_open_pr_number_from_last_stdout_line():
    fg, _ = _adapter({"create": "Warning: foo\nhttps://github.com/o/r/pull/13\n"})
    pr = fg.open_pr("main", "feature/flow-x", "t", "b", draft=False)
    assert pr["number"] == 13


def test_open_pr_malformed_url_raises():
    from forge import ForgeError

    fg, _ = _adapter({"create": "https://github.com/o/r/pull/not-a-number\n"})
    with pytest.raises(ForgeError, match="cannot parse PR number"):
        fg.open_pr("main", "feature/flow-x", "t", "b", draft=False)


def test_detect_pr_malformed_url_without_number_raises():
    from forge import ForgeError

    listing = json.dumps([{"url": "https://github.com/o/r/pull/oops", "state": "OPEN"}])
    fg, _ = _adapter({"list": listing})
    with pytest.raises(ForgeError, match="cannot parse PR number"):
        fg.detect_pr("feature/flow-x")


def test_ci_rollup_green():
    view = json.dumps(
        {"statusCheckRollup": [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]}
    )
    fg, _ = _adapter({"view": view})
    assert fg.ci_rollup("7")["status"] == "green"


def test_ci_rollup_failed():
    view = json.dumps(
        {
            "statusCheckRollup": [
                {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"},
            ]
        }
    )
    fg, _ = _adapter({"view": view})
    assert fg.ci_rollup("7")["status"] == "failed"


def test_ci_rollup_pending_when_running():
    view = json.dumps(
        {"statusCheckRollup": [{"name": "ci", "status": "IN_PROGRESS", "conclusion": ""}]}
    )
    fg, _ = _adapter({"view": view})
    assert fg.ci_rollup("7")["status"] == "pending"


def test_ci_rollup_pending_when_empty():
    fg, _ = _adapter({"view": json.dumps({"statusCheckRollup": []})})
    assert fg.ci_rollup("7")["status"] == "pending"


def test_ci_rollup_status_context_shape():
    view = json.dumps({"statusCheckRollup": [{"context": "buildkite", "state": "SUCCESS"}]})
    fg, _ = _adapter({"view": view})
    assert fg.ci_rollup("7")["status"] == "green"


def test_ci_rollup_status_context_pending_is_not_failed():
    # A legacy StatusContext (no `status` field) carrying a non-terminal `state`
    # must read as pending, never failed (else it trips a premature fix cycle).
    for pending_state in ("PENDING", "EXPECTED", ""):
        view = json.dumps({"statusCheckRollup": [{"context": "legacy", "state": pending_state}]})
        fg, _ = _adapter({"view": view})
        assert fg.ci_rollup("7")["status"] == "pending", pending_state


def test_ci_rollup_status_context_failure():
    view = json.dumps({"statusCheckRollup": [{"context": "legacy", "state": "FAILURE"}]})
    fg, _ = _adapter({"view": view})
    assert fg.ci_rollup("7")["status"] == "failed"


@pytest.mark.parametrize("verdict", ["CANCELLED", "STALE", "NEUTRAL", "SKIPPED"])
def test_ci_rollup_superseded_verdict_is_pending(verdict):
    # A COMPLETED check with a superseded/terminal-non-failure verdict (e.g. a
    # CANCELLED duplicate concurrent run) must read as pending, never failed.
    view = json.dumps(
        {"statusCheckRollup": [{"name": "ci", "status": "COMPLETED", "conclusion": verdict}]}
    )
    fg, _ = _adapter({"view": view})
    assert fg.ci_rollup("7")["status"] == "pending", verdict


def test_ci_rollup_mixed_success_and_cancelled_is_pending():
    # The flow-483 PR #120 shape: a winning SUCCESS run plus a superseded
    # CANCELLED duplicate -> pending, not failed.
    view = json.dumps(
        {
            "statusCheckRollup": [
                {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "ci", "status": "COMPLETED", "conclusion": "CANCELLED"},
            ]
        }
    )
    fg, _ = _adapter({"view": view})
    assert fg.ci_rollup("7")["status"] == "pending"


def test_mark_ready_merge_delete_argv():
    fg, calls = _adapter()
    fg.mark_ready("7")
    fg.merge("7", squash=True)
    fg.delete_branch("feature/flow-x")
    assert _ran(calls, ["gh", "pr", "ready", "7"])
    assert _ran(calls, ["gh", "pr", "merge", "7", "--squash"])
    assert _ran(calls, ["git", "push", "origin", "--delete", "feature/flow-x"])


def test_capabilities_review_threads_on():
    fg, _ = _adapter()
    caps = {c["name"]: c["supported"] for c in fg.capabilities}
    assert caps["review_threads"] is True
    assert caps["ci_rollup"] is True
    assert caps["squash_merge"] is True
    assert caps["default_reviewers"] is False


def test_set_default_reviewers_raises_not_supported():
    fg, calls = _adapter()
    with pytest.raises(NotSupported):
        fg.set_default_reviewers("7")
    assert calls == []  # no host call made


def test_review_threads_normalizes_changes_requested_as_major():
    node = _thread_node(
        tid="T9",
        path="src/x.py",
        line=42,
        body="**Potential issue**\nguard the null",
        author="coderabbitai",
        state="CHANGES_REQUESTED",
    )
    fg, _ = _adapter({"threads": _threads_response([node])})
    threads = fg.review_threads("7")
    assert len(threads) == 1
    t = threads[0]
    assert t["id"] == "T9"
    assert t["file"] == "src/x.py"
    assert t["line"] == 42
    assert t["severity"] == "major"
    assert t["title"] == "**Potential issue**"
    assert t["body"].startswith("**Potential issue**")
    assert t["author"] == "coderabbitai"
    assert t["resolved"] is False
    assert t["parent_id"] is None


def test_review_threads_commented_is_minor():
    node = _thread_node(state="COMMENTED")
    fg, _ = _adapter({"threads": _threads_response([node])})
    assert fg.review_threads("7")[0]["severity"] == "minor"


def test_review_threads_drops_resolved():
    nodes = [
        _thread_node(tid="open1", resolved=False),
        _thread_node(tid="done1", resolved=True),
    ]
    fg, _ = _adapter({"threads": _threads_response(nodes)})
    threads = fg.review_threads("7")
    assert [t["id"] for t in threads] == ["open1"]


def test_review_threads_null_author_and_review_does_not_crash():
    node = _thread_node(author=None, state=None)
    fg, _ = _adapter({"threads": _threads_response([node])})
    t = fg.review_threads("7")[0]
    assert t["author"] == ""
    assert t["severity"] == "minor"


def test_review_threads_passes_typed_number_and_owner_repo():
    node = _thread_node()
    fg, calls = _adapter({"threads": _threads_response([node])})
    fg.review_threads("7")
    gql = next(c for c in calls if _is_graphql(c) and _graphql_mutation(c) == "read")
    assert "-F" in gql and "number=7" in gql
    assert "owner=o" in gql and "repo=r" in gql


def test_resolve_thread_true_when_isresolved():
    fg, _ = _adapter({"resolve": _resolve_response(True)})
    assert fg.resolve_thread("7", "T1") is True


def test_resolve_thread_false_when_not_resolved():
    fg, _ = _adapter({"resolve": _resolve_response(False)})
    assert fg.resolve_thread("7", "T1") is False


def test_post_reply_issues_reply_mutation():
    fg, calls = _adapter()
    fg.post_reply("7", "T1", "thanks, fixed")
    reply = next(c for c in calls if _is_graphql(c) and _graphql_mutation(c) == "reply")
    assert "pullRequestReviewThreadId=T1" in reply
    assert "body=thanks, fixed" in reply


def _retry_adapter(
    list_returns: list[subprocess.CompletedProcess[str]],
    create_returns: list[subprocess.CompletedProcess[str]] | None = None,
) -> tuple[GitHubAdapter, Recorder, list[float]]:
    """Counter-driven fake runner: each `gh pr list` / `gh pr create` call pops the
    next queued CompletedProcess (last one repeats if exhausted). Records every argv
    and every sleep delay so fail-then-succeed retry behaviour is drivable.
    """
    calls: Recorder = []
    sleeps: list[float] = []
    list_i = [0]
    create_i = [0]
    create_q = create_returns or []

    def _pop(queue: list, idx: list[int], args: list[str]) -> subprocess.CompletedProcess[str]:
        cp = queue[min(idx[0], len(queue) - 1)]
        idx[0] += 1
        return subprocess.CompletedProcess(args, cp.returncode, cp.stdout, cp.stderr)

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:3] == ["gh", "pr", "list"]:
            return _pop(list_returns, list_i, args)
        if args[:3] == ["gh", "pr", "create"]:
            return _pop(create_q, create_i, args)
        return subprocess.CompletedProcess(args, 0, "", "")

    def sleep(delay: float) -> None:
        sleeps.append(delay)

    adapter = GitHubAdapter({"workspace_root": "."}, runner=run, sleep=sleep)
    return adapter, calls, sleeps


def _cp(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _graphql_retry_adapter(
    graphql_returns: list[subprocess.CompletedProcess[str]],
    repo_view: str = '{"nameWithOwner":"o/r"}',
) -> tuple[GitHubAdapter, Recorder, list[float]]:
    """Counter-driven runner for the graphql ops. `gh repo view` always succeeds
    first try (single success), so a graphql fail-then-succeed records exactly one
    sleep regardless of the resolver call review_threads makes first.
    """
    calls: Recorder = []
    sleeps: list[float] = []
    gi = [0]

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(args, 0, repo_view, "")
        if _is_graphql(args):
            cp = graphql_returns[min(gi[0], len(graphql_returns) - 1)]
            gi[0] += 1
            return subprocess.CompletedProcess(args, cp.returncode, cp.stdout, cp.stderr)
        return subprocess.CompletedProcess(args, 0, "", "")

    def sleep(delay: float) -> None:
        sleeps.append(delay)

    return GitHubAdapter({"workspace_root": "."}, runner=run, sleep=sleep), calls, sleeps


def test_review_threads_retries_then_succeeds():
    ok = _threads_response([_thread_node(tid="T1")])
    fg, calls, sleeps = _graphql_retry_adapter(
        graphql_returns=[_cp(1, "", "GraphQL: HTTP 401 (transient)"), _cp(0, ok, "")]
    )
    threads = fg.review_threads("7")
    assert [t["id"] for t in threads] == ["T1"]
    gql = [c for c in calls if _is_graphql(c)]
    assert len(gql) == 2
    assert len(sleeps) == 1


def test_resolve_thread_retries_then_succeeds():
    fg, calls, sleeps = _graphql_retry_adapter(
        graphql_returns=[
            _cp(1, "", "GraphQL: HTTP 401 (transient)"),
            _cp(0, _resolve_response(True), ""),
        ]
    )
    assert fg.resolve_thread("7", "T1") is True
    gql = [c for c in calls if _is_graphql(c)]
    assert len(gql) == 2
    assert len(sleeps) == 1


def test_post_reply_not_retried_on_failure():
    from forge import ForgeError

    fg, calls, sleeps = _graphql_retry_adapter(graphql_returns=[_cp(1, "", "reply blew up")])
    with pytest.raises(ForgeError):
        fg.post_reply("7", "T1", "body")
    gql = [c for c in calls if _is_graphql(c)]
    assert len(gql) == 1
    assert sleeps == []


def test_detect_pr_retries_then_succeeds():
    listing = json.dumps([{"number": 7, "url": "https://github.com/o/r/pull/7"}])
    fg, calls, sleeps = _retry_adapter(
        list_returns=[
            _cp(1, "", "GraphQL: Something went wrong (HTTP 502)"),
            _cp(0, listing, ""),
        ]
    )
    pr = fg.detect_pr("feature/flow-x")
    assert pr is not None and pr["number"] == 7
    list_calls = [c for c in calls if c[:3] == ["gh", "pr", "list"]]
    assert len(list_calls) == 2
    assert len(sleeps) == 1


def test_detect_pr_retries_exhausted_raises():
    from forge import ForgeError

    fg, calls, _ = _retry_adapter(list_returns=[_cp(1, "", "GraphQL: HTTP 502 bad gateway")])
    with pytest.raises(ForgeError) as exc:
        fg.detect_pr("feature/flow-x")
    list_calls = [c for c in calls if c[:3] == ["gh", "pr", "list"]]
    assert len(list_calls) == 3
    assert str(exc.value).startswith("gh pr list failed:")


def test_detect_pr_no_retry_on_happy_path():
    listing = json.dumps([{"number": 7, "url": "https://github.com/o/r/pull/7"}])
    fg, calls, sleeps = _retry_adapter(list_returns=[_cp(0, listing, "")])
    pr = fg.detect_pr("feature/flow-x")
    assert pr is not None and pr["number"] == 7
    list_calls = [c for c in calls if c[:3] == ["gh", "pr", "list"]]
    assert len(list_calls) == 1
    assert sleeps == []


def test_open_pr_create_failure_not_retried():
    from forge import ForgeError

    fg, calls, sleeps = _retry_adapter(
        list_returns=[_cp(0, "[]", "")],
        create_returns=[_cp(1, "", "create blew up")],
    )
    with pytest.raises(ForgeError):
        fg.open_pr("main", "feature/flow-x", "feat: x", "body", draft=False)
    create_calls = [c for c in calls if c[:3] == ["gh", "pr", "create"]]
    assert len(create_calls) == 1
    assert not _ran(calls, ["gh", "pr", "list"])
    assert sleeps == []
