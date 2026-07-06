"""Bitbucket forge adapter (`bkt` CLI).

Implements the `Forge` Protocol for Bitbucket workspaces. This is the home of the
logic that used to live in the external ship-it bundle: PR open/detect, CI rollup
from `bkt pr checks`, and the CodeRabbit review-thread fetch + resolve. The hard-won
endpoint facts are ported verbatim-in-spirit (see `resolve_thread`).

Config requires `workspace` + `repo_slug` (the Bitbucket API path needs both).

Resolve gotchas (learned the hard way in ship-it, do NOT re-derive):
- `POST .../comments/<CID>/resolve` is the resolve endpoint; the `links.resolve` rel
  is often absent but the endpoint still works, never gate on the rel.
- Success returns a `comment_resolution` object with NO top-level `resolved:true`.
  Judge success by re-fetching the comment and testing `.resolution != null`.
- Only top-level inline comments (`parent == null`) can be resolved; replies cannot.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner
from forge import (
    THREAD_SEVERITY,
    Capability,
    CICheck,
    CIStatus,
    ForgeConfigError,
    ForgeError,
    PullRequest,
    ReviewThread,
)

_CI_STATE_RE = re.compile(r"INPROGRESS|SUCCESSFUL|FAILED|STOPPED|ERROR", re.IGNORECASE)


class BitbucketAdapter:
    backend = "bitbucket"

    def __init__(self, config: dict[str, Any], runner: Runner | None = None) -> None:
        self._workspace = config.get("workspace")
        self._repo = config.get("repo_slug")
        if not self._workspace or not self._repo:
            raise ForgeConfigError(
                "forge.bitbucket requires workspace + repo_slug in workspace.toml"
            )
        root = config.get("workspace_root", ".")
        self._run: Runner = runner or _default_runner(Path(root))

    @property
    def capabilities(self) -> list[Capability]:
        return [
            {"name": "draft_prs", "supported": True},
            {"name": "ready_toggle", "supported": True},
            {"name": "review_threads", "supported": True},
            {"name": "bot_review_status", "supported": True},
            {"name": "squash_merge", "supported": True},
            {"name": "delete_branch", "supported": True},
            {"name": "ci_rollup", "supported": True},
            {"name": "default_reviewers", "supported": True},
        ]

    # ─── helpers ──────────────────────────────────────────────────────────

    def _base(self) -> str:
        return f"2.0/repositories/{self._workspace}/{self._repo}"

    def _run_text(self, args: list[str], what: str) -> str:
        result = self._run(args)
        if result.returncode != 0:
            raise ForgeError(f"{what} failed: {(result.stderr or '').strip()}")
        return result.stdout or ""

    def _api(
        self,
        path: str,
        what: str,
        *,
        method: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        args = ["bkt", "api", path]
        if method:
            args += ["-X", method]
        if payload is not None:
            args += ["-d", json.dumps(payload)]
        args.append("--json")
        raw = self._run_text(args, what)
        try:
            return json.loads(raw or "null")
        except json.JSONDecodeError as exc:
            raise ForgeError(f"{what}: bad JSON: {exc}") from exc

    @staticmethod
    def _pr_from_api(item: dict[str, Any]) -> PullRequest:
        links = item.get("links") or {}
        html = (links.get("html") or {}).get("href") or ""
        src = ((item.get("source") or {}).get("branch") or {}).get("name") or ""
        dest = ((item.get("destination") or {}).get("branch") or {}).get("name") or ""
        pr_id = str(item.get("id") or "")
        return {
            "id": pr_id,
            "url": str(html),
            "number": int(item.get("id") or 0),
            "draft": bool(item.get("draft", False)),
            "base": str(dest),
            "head": str(src),
            "state": str(item.get("state") or "OPEN"),
        }

    # ─── PR mechanics ─────────────────────────────────────────────────────

    def detect_pr(self, branch: str) -> PullRequest | None:
        # follow `next` like _fetch_all_comments: on a busy workspace the run's PR
        # can sit past page 1, and a miss here breaks create_pr's resume idempotency.
        page = 1
        while True:
            data = self._api(
                f"{self._base()}/pullrequests?state=OPEN&pagelen=50&page={page}",
                "bkt pr list",
            )
            data = data or {}
            for item in data.get("values") or []:
                src = ((item.get("source") or {}).get("branch") or {}).get("name")
                if src == branch:
                    return self._pr_from_api(item)
            if "next" not in data:
                return None
            page += 1

    def pr_info(self, pr_id: str) -> PullRequest | None:
        # PR-id -> PR reverse lookup. Reads ANY state (no state filter), so `revise`
        # can detect a MERGED PR. `_api` returns None on an empty ("null") body and
        # raises on a non-zero `bkt` exit (an absent PR), so None means empty, not
        # error (matches the github adapter's shape).
        data = self._api(f"{self._base()}/pullrequests/{pr_id}", "bkt pr view")
        if not isinstance(data, dict) or not data:
            return None
        return self._pr_from_api(data)

    def open_pr(self, base: str, head: str, title: str, body: str, draft: bool) -> PullRequest:
        payload = {
            "title": title,
            "source": {"branch": {"name": head}},
            "destination": {"branch": {"name": base}},
            "description": body or title,
            "draft": draft,
        }
        data = self._api(
            f"{self._base()}/pullrequests", "bkt pr create", method="POST", payload=payload
        )
        return self._pr_from_api(data or {})

    def ci_rollup(self, pr_id: str) -> CIStatus:
        text = self._run_text(["bkt", "pr", "checks", pr_id], "bkt pr checks")
        pipeline_line = next((ln for ln in text.splitlines() if "pipeline" in ln.lower()), "")
        m = _CI_STATE_RE.search(pipeline_line)
        state = m.group(0).upper() if m else ""
        checks: list[CICheck] = (
            [{"name": "Pipeline", "status": state, "conclusion": state, "url": None}]
            if state
            else []
        )
        if state == "SUCCESSFUL":
            return {"status": "green", "checks": checks, "detail": "pipeline successful"}
        if state in ("FAILED", "STOPPED", "ERROR"):
            return {"status": "failed", "checks": checks, "detail": f"pipeline {state.lower()}"}
        # INPROGRESS, or no pipeline line yet
        detail = "pipeline in progress" if state else "no pipeline entry yet"
        return {"status": "pending", "checks": checks, "detail": detail}

    def mark_ready(self, pr_id: str) -> None:
        self._api(
            f"{self._base()}/pullrequests/{pr_id}",
            "bkt pr ready",
            method="PUT",
            payload={"draft": False},
        )

    def merge(self, pr_id: str, squash: bool = True) -> None:
        payload = {"merge_strategy": "squash"} if squash else {}
        self._api(
            f"{self._base()}/pullrequests/{pr_id}/merge",
            "bkt pr merge",
            method="POST",
            payload=payload,
        )

    def delete_branch(self, branch: str) -> None:
        self._run_text(["git", "push", "origin", "--delete", branch], "git push --delete")

    def set_default_reviewers(self, pr_id: str) -> None:
        """Attach the repo's default reviewers (minus the author) to the PR.

        Self-resolves the author (`GET 2.0/user`; the adapter stores only
        workspace+repo), reads the repo `default-reviewers`, drops the author by
        `account_id`, then PUTs `{"reviewers": [{"uuid": ...}, ...]}` onto the PR
        (the Bitbucket reviewer shape ported from ship-it)."""
        me = self._api("2.0/user", "bkt whoami")
        my_account_id = (me or {}).get("account_id")
        data = self._api(f"{self._base()}/default-reviewers", "bkt default-reviewers")
        reviewers = [
            {"uuid": v["uuid"]}
            for v in ((data or {}).get("values") or [])
            if v.get("uuid") and v.get("account_id") != my_account_id
        ]
        self._api(
            f"{self._base()}/pullrequests/{pr_id}",
            "bkt set reviewers",
            method="PUT",
            payload={"reviewers": reviewers},
        )

    # ─── review threads (CodeRabbit) ──────────────────────────────────────

    def _fetch_all_comments(self, pr_id: str) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._api(
                f"{self._base()}/pullrequests/{pr_id}/comments?page={page}&pagelen=100",
                "bkt pr comments",
            )
            data = data or {}
            comments.extend(data.get("values") or [])
            if "next" not in data:
                break
            page += 1
        return comments

    def review_threads(self, pr_id: str) -> list[ReviewThread]:
        """Unresolved actionable CodeRabbit inline findings, normalized.

        Resolved findings (`resolution != null`) are dropped so a fixed thread does
        not re-surface on the post-fix re-fetch (ported from ship-it)."""
        threads: list[ReviewThread] = []
        for c in self._fetch_all_comments(pr_id):
            author = (c.get("user") or {}).get("display_name", "")
            if author.lower() != "coderabbit":
                continue
            if c.get("resolution") is not None:
                continue
            if not _is_actionable_inline(c):
                continue
            inline = c.get("inline") or {}
            raw = (c.get("content") or {}).get("raw", "")
            threads.append(
                {
                    "id": str(c.get("id") or ""),
                    "file": inline.get("path"),
                    "line": inline.get("to") or inline.get("from"),
                    "severity": _classify_severity(raw),
                    "title": _extract_title(raw),
                    "body": raw,
                    "resolved": False,
                    "author": author,
                    "parent_id": str((c.get("parent") or {}).get("id"))
                    if c.get("parent")
                    else None,
                }
            )
        return threads

    def bot_review_present(self, pr_id: str) -> bool:
        """True once CodeRabbit's review CHECK has reached a terminal state.

        CR registers a commit status (a `CodeRabbit` line in `bkt pr checks`,
        the same source `ci_rollup` reads for the pipeline) that goes
        INPROGRESS -> SUCCESSFUL independent of the finding count. That is the
        reliable completion signal: on a CLEAN review CR posts only a Walkthrough
        and NO `Actionable comments posted: N` comment, so a comment-marker gate
        would never fire and would burn the full wait on every clean PR (verified
        on brinta-data-platform: zero-finding PRs carry `CodeRabbit: SUCCESSFUL`
        but no count comment). Comment markers are also unreliable as a START vs
        DONE signal, the Walkthrough is posted at review start (flow-arva).

        Absent line (CR not registered yet) or INPROGRESS -> not done; any
        terminal state (incl. FAILED) means CR has stopped, so waiting longer
        will not surface more threads."""
        text = self._run_text(["bkt", "pr", "checks", pr_id], "bkt pr checks")
        line = next((ln for ln in text.splitlines() if "coderabbit" in ln.lower()), "")
        m = _CI_STATE_RE.search(line)
        state = m.group(0).upper() if m else ""
        return state in ("SUCCESSFUL", "FAILED", "STOPPED", "ERROR")

    def post_reply(self, pr_id: str, thread_id: str, body: str) -> None:
        self._api(
            f"{self._base()}/pullrequests/{pr_id}/comments",
            "bkt pr comment",
            method="POST",
            payload={"content": {"raw": body}, "parent": {"id": int(thread_id)}},
        )

    def resolve_thread(self, pr_id: str, thread_id: str) -> bool:
        """Resolve a top-level inline comment thread, then VERIFY by re-reading it.

        Success is `.resolution != null` on the re-fetched comment, NOT a top-level
        `resolved` flag (which the resolve response does not carry)."""
        self._api(
            f"{self._base()}/pullrequests/{pr_id}/comments/{thread_id}/resolve",
            "bkt resolve",
            method="POST",
        )
        check = self._api(
            f"{self._base()}/pullrequests/{pr_id}/comments/{thread_id}",
            "bkt resolve verify",
        )
        return bool((check or {}).get("resolution") is not None)


# ─── pure CodeRabbit parsing (ported from fetch_coderabbit_comments.py) ──────


def _is_actionable_inline(comment: dict[str, Any]) -> bool:
    if not comment.get("inline"):
        return False
    raw = (comment.get("content") or {}).get("raw", "")
    if "Actionable comments posted" in raw or "Walkthrough" in raw:
        return False
    if "Potential issue" in raw or "suggestion" in raw.lower():
        return True
    # recognize CodeRabbit's current emoji/pipe metadata header (`_…_ | _…_`);
    # the old "Potential issue"/"suggestion" markers miss it.
    return bool(_CR_INLINE_META_RE.search(raw))


_CR_INLINE_META_RE = re.compile(r"_[^_]+_\s*\|\s*_[^_]+_")


def _classify_severity(raw: str) -> THREAD_SEVERITY:
    if "Critical" in raw:
        return "critical"
    if "Major" in raw:
        return "major"
    if "Minor" in raw:
        return "minor"
    return "unknown"


def _extract_title(raw: str) -> str:
    m = re.search(r"\*\*(.+?)\*\*", raw)
    return m.group(1) if m else "(no title)"
