"""GitHub forge adapter (`gh` CLI).

Implements the `Forge` Protocol for GitHub workspaces. PR mechanics lift the logic
that lived gh-direct in `create_pr.py` (detect/open) plus the CI rollup semantics
from `evolve_reap.rollup_is_green`.

Review-thread ops are capability-gated OFF for now: the maintainer's repo carries no
live CodeRabbit-on-GitHub review threads yet, so there is nothing to drive and
nothing to test against. The GraphQL `reviewThreads` / `resolveReviewThread` path is
valid and can be wired when a real review-bot-on-GitHub PR exists. `merge` /
`mark_ready` / `delete_branch` are implemented regardless (Layer 2 calls them).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner
from forge import (
    Capability,
    CICheck,
    CIStatus,
    ForgeError,
    NotSupported,
    PullRequest,
    ReviewThread,
)


class GitHubAdapter:
    backend = "github"

    def __init__(self, config: dict[str, Any], runner: Runner | None = None) -> None:
        self._config = config
        root = config.get("workspace_root", ".")
        self._run: Runner = runner or _default_runner(Path(root))

    @property
    def capabilities(self) -> list[Capability]:
        return [
            {"name": "draft_prs", "supported": True},
            {"name": "ready_toggle", "supported": True},
            {"name": "review_threads", "supported": False},
            {"name": "squash_merge", "supported": True},
            {"name": "delete_branch", "supported": True},
            {"name": "ci_rollup", "supported": True},
        ]

    # ─── helpers ──────────────────────────────────────────────────────────

    def _ok(self, args: list[str], what: str) -> str:
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
            return 0

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
        raw = self._ok(
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

    # ─── review threads (capability off for now) ──────────────────────────

    def review_threads(self, pr_id: str) -> list[ReviewThread]:
        raise NotSupported("github adapter does not yet drive review-bot threads")

    def post_reply(self, pr_id: str, thread_id: str, body: str) -> None:
        raise NotSupported("github adapter does not yet drive review-bot threads")

    def resolve_thread(self, pr_id: str, thread_id: str) -> bool:
        raise NotSupported("github adapter does not yet drive review-bot threads")


# Non-terminal verdicts a legacy StatusContext (no `status` field) can carry; these
# read as still-running, NOT failed (else a pending check trips a premature fix cycle).
_NONTERMINAL_VERDICTS = frozenset(
    {"", "PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}
)


def _classify_rollup(rollup: list) -> CIStatus:
    """green iff non-empty and every check is completed-SUCCESS (matches
    evolve_reap.rollup_is_green); pending if any check is still running (CheckRun
    status != COMPLETED, or a StatusContext with a non-terminal state); failed only
    when a check reaches a terminal non-SUCCESS verdict."""
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
        elif verdict in _NONTERMINAL_VERDICTS:
            any_pending = True
        else:
            any_failed = True

    if not rollup:
        status_lit: str = "pending"
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
    return {"status": status_lit, "checks": checks, "detail": detail}  # type: ignore[typeddict-item]
