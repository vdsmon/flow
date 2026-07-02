"""GitHub forge adapter (`gh` CLI).

Implements the `Forge` Protocol for GitHub workspaces. PR mechanics lift the logic
that lived gh-direct in `create_pr.py` (detect/open) plus the CI rollup semantics
from `evolve_reap.rollup_is_green`.

Review-thread ops normalize GitHub PR review threads via the GraphQL API
(`reviewThreads` read, `addPullRequestReviewThreadReply` / `resolveReviewThread`
mutations). `merge` / `mark_ready` / `delete_branch` are implemented too (Layer 2
calls them).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner
from forge import (
    CI_STATUS,
    THREAD_SEVERITY,
    Capability,
    CICheck,
    CIStatus,
    ForgeError,
    NotSupported,
    PullRequest,
    ReviewThread,
)

_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          path
          line
          comments(first: 1) {
            nodes {
              author { login }
              body
              pullRequestReview { state }
            }
          }
        }
      }
    }
  }
}
"""

_REPLY_MUTATION = """
mutation($pullRequestReviewThreadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(
    input: {pullRequestReviewThreadId: $pullRequestReviewThreadId, body: $body}
  ) {
    comment { id }
  }
}
"""

_RESOLVE_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { isResolved }
  }
}
"""


class GitHubAdapter:
    backend = "github"

    def __init__(
        self,
        config: dict[str, Any],
        runner: Runner | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        root = config.get("workspace_root", ".")
        self._run: Runner = runner or _default_runner(Path(root))
        self._sleep = sleep
        self._owner_repo: tuple[str, str] | None = None

    @property
    def capabilities(self) -> list[Capability]:
        return [
            {"name": "draft_prs", "supported": True},
            {"name": "ready_toggle", "supported": True},
            {"name": "review_threads", "supported": True},
            {"name": "bot_review_status", "supported": False},
            {"name": "squash_merge", "supported": True},
            {"name": "delete_branch", "supported": True},
            {"name": "ci_rollup", "supported": True},
            {"name": "default_reviewers", "supported": False},
        ]

    # ─── helpers ──────────────────────────────────────────────────────────

    def _ok(self, args: list[str], what: str) -> str:
        result = self._run(args)
        if result.returncode != 0:
            raise ForgeError(f"{what} failed: {(result.stderr or '').strip()}")
        return result.stdout or ""

    # like _ok but for IDEMPOTENT reads: a transient gh/GraphQL 5xx survives a bounded
    # retry. Retries on ANY non-zero return (a permanent error fails identically at
    # bounded cost, and this needs no allowlist for novel 5xx phrasings).
    _READ_BACKOFFS = (0.5, 1.0)

    def _ok_read(self, args: list[str], what: str) -> str:
        result = self._run(args)
        for backoff in self._READ_BACKOFFS:
            if result.returncode == 0:
                return result.stdout or ""
            self._sleep(backoff)
            result = self._run(args)
        if result.returncode != 0:
            raise ForgeError(f"{what} failed: {(result.stderr or '').strip()}")
        return result.stdout or ""

    @staticmethod
    def _number_from_url(url: str) -> int:
        tail = url.rstrip("/").rsplit("/", 1)[-1]
        try:
            return int(tail)
        except ValueError:
            raise ForgeError(f"cannot parse PR number from URL {url!r}") from None

    def _pr_from_json(self, item: dict[str, Any]) -> PullRequest:
        url = str(item.get("url") or "")
        number = int(item.get("number") or self._number_from_url(url))
        return {
            "id": str(number),
            "url": url,
            "number": number,
            "draft": bool(item.get("isDraft", False)),
            "base": str(item.get("baseRefName") or ""),
            "head": str(item.get("headRefName") or ""),
            "state": str(item.get("state") or "OPEN"),
        }

    # ─── PR mechanics ─────────────────────────────────────────────────────

    def detect_pr(self, branch: str) -> PullRequest | None:
        raw = self._ok_read(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "open",
                "--json",
                "number,url,isDraft,baseRefName,headRefName,state",
                "--limit",
                "1",
            ],
            "gh pr list",
        )
        try:
            items = json.loads(raw or "[]")
        except json.JSONDecodeError:
            return None
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return self._pr_from_json(items[0])
        return None

    def pr_info(self, pr_id: str) -> PullRequest | None:
        # PR-number -> PR reverse lookup. Reads ANY state (no --state filter), so
        # `revise` can detect a MERGED PR. Returns None on empty/unparseable JSON;
        # an absent PR makes `gh pr view` exit non-zero, which `_ok_read` surfaces
        # as a ForgeError (the verb's "no PR" path), NOT a silent None.
        raw = self._ok_read(
            [
                "gh",
                "pr",
                "view",
                pr_id,
                "--json",
                "number,url,isDraft,baseRefName,headRefName,state",
            ],
            "gh pr view",
        )
        try:
            item = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return None
        if isinstance(item, dict) and item:
            return self._pr_from_json(item)
        return None

    def open_pr(self, base: str, head: str, title: str, body: str, draft: bool) -> PullRequest:
        args = [
            "gh",
            "pr",
            "create",
            "--base",
            base,
            "--head",
            head,
            "--title",
            title,
            "--body",
            body or title,
        ]
        if draft:
            args.append("--draft")
        out = self._ok(args, "gh pr create")
        url = next((ln.strip() for ln in reversed(out.splitlines()) if ln.strip()), "")
        if not url:
            existing = self.detect_pr(head)
            if existing is None:
                raise ForgeError("gh pr create returned no URL and none is resolvable")
            return existing
        number = self._number_from_url(url)
        return {
            "id": str(number),
            "url": url,
            "number": number,
            "draft": draft,
            "base": base,
            "head": head,
            "state": "OPEN",
        }

    def ci_rollup(self, pr_id: str) -> CIStatus:
        raw = self._ok(
            ["gh", "pr", "view", pr_id, "--json", "statusCheckRollup"],
            "gh pr view",
        )
        try:
            rollup = (json.loads(raw or "{}") or {}).get("statusCheckRollup") or []
        except json.JSONDecodeError:
            rollup = []
        return _classify_rollup(rollup)

    def mark_ready(self, pr_id: str) -> None:
        self._ok(["gh", "pr", "ready", pr_id], "gh pr ready")

    def merge(self, pr_id: str, squash: bool = True) -> None:
        args = ["gh", "pr", "merge", pr_id]
        if squash:
            args.append("--squash")
        self._ok(args, "gh pr merge")

    def delete_branch(self, branch: str) -> None:
        self._ok(["git", "push", "origin", "--delete", branch], "git push --delete")

    def set_default_reviewers(self, pr_id: str) -> None:
        # GitHub has no default-reviewers REST surface for a solo repo; CODEOWNERS
        # covers review assignment. The first supported=false capability in a live
        # adapter, so create_pr degrades cleanly.
        raise NotSupported("github adapter does not set default reviewers")

    # ─── review threads (GraphQL) ─────────────────────────────────────────

    # GraphQL needs explicit owner/repo (the {owner}/{repo} REST placeholder
    # expansion does not apply to `gh api graphql`). Resolve once, cache.
    def _resolve_owner_repo(self) -> tuple[str, str]:
        if self._owner_repo is None:
            raw = self._ok_read(["gh", "repo", "view", "--json", "nameWithOwner"], "gh repo view")
            try:
                owner, repo = str((json.loads(raw or "{}") or {}).get("nameWithOwner") or "").split(
                    "/", 1
                )
            except ValueError:
                raise ForgeError(f"cannot parse owner/repo from {raw!r}") from None
            self._owner_repo = (owner, repo)
        return self._owner_repo

    def review_threads(self, pr_id: str) -> list[ReviewThread]:
        """Unresolved review threads on the PR, normalized.

        Resolved threads (`isResolved == true`) are dropped so a fixed thread does
        not re-surface on the post-fix re-fetch (mirrors the bitbucket adapter)."""
        owner, repo = self._resolve_owner_repo()
        # single page; >100 unresolved threads on one PR is out of scope.
        raw = self._ok_read(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={_THREADS_QUERY}",
                "-f",
                f"owner={owner}",
                "-f",
                f"repo={repo}",
                "-F",
                f"number={pr_id}",
            ],
            "gh api graphql reviewThreads",
        )
        try:
            data = json.loads(raw or "{}") or {}
        except json.JSONDecodeError as exc:
            raise ForgeError(f"gh api graphql reviewThreads: bad JSON: {exc}") from exc
        nodes = (
            (((data.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get(
                "reviewThreads"
            )
            or {}
        ).get("nodes") or []
        threads: list[ReviewThread] = []
        for node in nodes:
            if not isinstance(node, dict) or node.get("isResolved"):
                continue
            comment = next(iter((node.get("comments") or {}).get("nodes") or []), {}) or {}
            body = str(comment.get("body") or "")
            author = str(((comment.get("author") or {}) or {}).get("login") or "")
            state = ((comment.get("pullRequestReview") or {}) or {}).get("state")
            threads.append(
                {
                    "id": str(node.get("id") or ""),
                    "file": node.get("path"),
                    "line": node.get("line"),
                    "severity": _severity_from_state(state),
                    "title": _title_from_body(body),
                    "body": body,
                    "resolved": False,
                    "author": author,
                    "parent_id": None,
                }
            )
        return threads

    def bot_review_present(self, pr_id: str) -> bool:
        # No review bot is wired on the GitHub self-target (flow's own PRs get no CodeRabbit
        # review; hot beads gate on the stage-merge §2 subagent instead), so there is no
        # async-review completion signal to wait for. Unsupported -> forge_cli degrades to
        # {"supported": false} and review_loop skips the wait.
        raise NotSupported("bot_review_status not implemented for github")

    def post_reply(self, pr_id: str, thread_id: str, body: str) -> None:
        # plain _ok (NOT retried): a reply is not idempotent, a double-apply would
        # double-comment.
        self._ok(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={_REPLY_MUTATION}",
                "-F",
                f"pullRequestReviewThreadId={thread_id}",
                "-f",
                f"body={body}",
            ],
            "gh api graphql addPullRequestReviewThreadReply",
        )

    def resolve_thread(self, pr_id: str, thread_id: str) -> bool:
        """Resolve a thread; return the host's post-mutation `isResolved` as truth.

        Via `_ok_read` (retry is safe: resolving an already-resolved thread is an
        idempotent no-op success, so a transient 401 survives the bounded retry)."""
        raw = self._ok_read(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={_RESOLVE_MUTATION}",
                "-F",
                f"threadId={thread_id}",
            ],
            "gh api graphql resolveReviewThread",
        )
        try:
            data = json.loads(raw or "{}") or {}
        except json.JSONDecodeError as exc:
            raise ForgeError(f"gh api graphql resolveReviewThread: bad JSON: {exc}") from exc
        thread = ((data.get("data") or {}).get("resolveReviewThread") or {}).get("thread") or {}
        return bool(thread.get("isResolved"))


def _severity_from_state(state: str | None) -> THREAD_SEVERITY:
    # host-fact mapping (the kx17.1/kx17.4 seam): a CHANGES_REQUESTED review is the
    # blocking signal -> major; COMMENTED / missing / null review -> minor.
    return "major" if state == "CHANGES_REQUESTED" else "minor"


def _title_from_body(body: str) -> str:
    # GitHub threads have no title field; use the first non-empty line, truncated.
    line = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    return line[:80]


# Non-terminal verdicts a legacy StatusContext (no `status` field) can carry; these
# read as still-running, NOT failed (else a pending check trips a premature fix cycle).
_NONTERMINAL_VERDICTS = frozenset(
    {"", "PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}
)

# Terminal verdicts that are not failures the change caused: a superseded duplicate
# concurrent run (CANCELLED/STALE) or a deliberate non-run (NEUTRAL/SKIPPED). These
# read as pending (re-poll), not failed (else a transient CANCELLED trips a phantom
# fix cycle on a PR whose real checks all end SUCCESS).
_SUPERSEDED_VERDICTS = frozenset({"CANCELLED", "STALE", "NEUTRAL", "SKIPPED"})


def _classify_rollup(rollup: list) -> CIStatus:
    """green iff non-empty and every check is completed-SUCCESS (matches
    evolve_reap.rollup_is_green); pending if any check is still running (CheckRun
    status != COMPLETED, or a StatusContext with a non-terminal state); a superseded
    terminal verdict (_SUPERSEDED_VERDICTS) also reads as pending, not failed; failed
    only when a check reaches a terminal non-SUCCESS verdict outside those sets."""
    checks: list[CICheck] = []
    any_pending = False
    any_failed = False
    for e in rollup:
        if not isinstance(e, dict):
            any_failed = True
            continue
        status = e.get("status")
        verdict = (e.get("conclusion") or e.get("state") or "").upper()
        checks.append(
            {
                "name": str(e.get("name") or e.get("context") or "check"),
                "status": str(status or ""),
                "conclusion": verdict,
                "url": e.get("detailsUrl") or e.get("targetUrl"),
            }
        )
        if status and status != "COMPLETED":
            any_pending = True
        elif verdict == "SUCCESS":
            continue
        elif verdict in _NONTERMINAL_VERDICTS or verdict in _SUPERSEDED_VERDICTS:
            any_pending = True
        else:
            any_failed = True

    if not rollup:
        status_lit: CI_STATUS = "pending"
        detail = "no checks registered yet"
    elif any_failed:
        status_lit = "failed"
        detail = f"{len(checks)} checks, at least one not green"
    elif any_pending:
        status_lit = "pending"
        detail = f"{len(checks)} checks, some still running"
    else:
        status_lit = "green"
        detail = f"{len(checks)} checks, all green"
    return {"status": status_lit, "checks": checks, "detail": detail}
