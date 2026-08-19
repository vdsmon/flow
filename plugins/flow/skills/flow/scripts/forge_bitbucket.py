"""Bitbucket forge adapter, transported through the `brinta-ai bitbucket` proxy.

Implements the `Forge` Protocol for Bitbucket Cloud workspaces. This is the home of the
logic that used to live in the external ship-it bundle: PR open/detect, CI rollup,
and the CodeRabbit review-thread fetch + resolve. The hard-won endpoint facts are
ported verbatim-in-spirit (see `resolve_thread`).

Every REST call goes through `brinta-ai bitbucket <METHOD> <path> [body]`, the
authenticated proxy the Brinta marketplace plugins already depend on. One credential
store (`~/.config/brinta/git-credentials.json`, written by `brinta-ai setup`) then
serves flow and the Brinta tooling alike. `_base()` builds `2.0/...` paths; `_api`
strips that prefix before shelling out, since the proxy is already rooted at the v2
API. `ci_rollup` and `bot_review_present` read the PR commit-status endpoint as
structured JSON rather than parsing CLI text output.

Config requires `workspace` + `repo_slug` (the Bitbucket API path needs both).

Resolve gotchas (learned the hard way in ship-it, do NOT re-derive):
- `POST .../comments/<CID>/resolve` is the resolve endpoint; the `links.resolve` rel
  is often absent but the endpoint still works, never gate on the rel.
- Success returns a `comment_resolution` object with NO top-level `resolved:true`.
  Judge success by re-fetching the comment and testing `.resolution != null`.
- Only top-level inline comments (`parent == null`) can be resolved; replies cannot.

Proxy contract (brinta-ai >= 0.2.31, `bitbucket` is Tier 2 in
brinta-ai/docs/cli-contract.md):
- stdout is the raw response body; a bodyless success prints `(HTTP <n>, no body)`.
- non-2xx exits 1 with the body on stdout and one stderr line.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner
from forge import (
    PR_STATE,
    THREAD_SEVERITY,
    CICheck,
    CIStatus,
    ForgeConfigError,
    ForgeError,
    PullRequest,
    ReviewThread,
)

_TERMINAL = ("SUCCESSFUL", "FAILED", "STOPPED", "ERROR")


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
        # `_base()` builds `2.0/...` paths; the proxy already roots at the v2 API,
        # so strip the prefix. Full URLs pass through unchanged.
        proxy_path = path.removeprefix("2.0/")
        args = ["brinta-ai", "bitbucket", method or "GET", proxy_path]
        if payload is not None:
            args.append(json.dumps(payload))
        result = self._run(args)
        text = (result.stdout or "").strip()
        if result.returncode != 0:
            detail = (result.stderr or "").strip() or text
            raise ForgeError(f"{what} failed: {detail}")
        if not text or text.startswith("(HTTP"):
            return None  # bodyless success (204-style writes)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ForgeError(f"{what}: bad JSON: {exc}") from exc

    @staticmethod
    def _pr_from_api(item: dict[str, Any]) -> PullRequest:
        links = item.get("links") or {}
        html = (links.get("html") or {}).get("href") or ""
        src = ((item.get("source") or {}).get("branch") or {}).get("name") or ""
        head_sha = ((item.get("source") or {}).get("commit") or {}).get("hash")
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
            "title": str(item.get("title") or ""),
            "updated_at": str(item.get("updated_on") or ""),
            "head_sha": str(head_sha) if head_sha else None,
        }

    # ─── PR mechanics ─────────────────────────────────────────────────────

    def detect_pr(self, branch: str, state: PR_STATE = "open") -> PullRequest | None:
        # Server-side source-branch filter (`q=`): without it this walked EVERY PR in the
        # repo client-side, and a branch with no merged PR meant exhausting all pages
        # (3100+ PRs, ~2 min per probe witnessed 2026-08-03 when the finalize sweep made
        # the merged-state probe routine).
        # State lives INSIDE `q`, never as the `state=` URL param alongside it: Bitbucket
        # ignores the param when `q` is present, and the live control that proved it
        # returned an OPEN PR from a state=MERGED&q=<branch> probe, which finalize would
        # have taken as merged proof. The client-side state and branch checks below stay:
        # `q` is belt-and-braces, not trusted alone.
        # Still follow `next` like _fetch_all_comments: even filtered, a same-named
        # recreated branch can yield multiple PRs, and a miss here breaks create_pr's
        # resume idempotency.
        wanted_state = state.upper()
        query = quote(f'source.branch.name = "{branch}" AND state = "{wanted_state}"', safe="")
        page = 1
        while True:
            data = self._api(
                f"{self._base()}/pullrequests?q={query}&pagelen=50&page={page}",
                "pr list",
            )
            data = data or {}
            for item in data.get("values") or []:
                src = ((item.get("source") or {}).get("branch") or {}).get("name")
                if src == branch and str(item.get("state") or "").upper() == wanted_state:
                    return self._pr_from_api(item)
            if "next" not in data:
                return None
            page += 1

    def list_authored(self, state: PR_STATE = "open") -> list[PullRequest]:
        me = self._api("2.0/user", "whoami") or {}
        author_uuid = str(me.get("uuid") or "")
        if not author_uuid:
            raise ForgeError("whoami returned no uuid")
        prs: list[PullRequest] = []
        page = 1
        while True:
            data = self._api(
                f"{self._base()}/pullrequests?state={state.upper()}&pagelen=50&page={page}",
                "pr list authored",
            )
            data = data or {}
            prs.extend(
                self._pr_from_api(item)
                for item in data.get("values") or []
                if ((item.get("author") or {}).get("uuid")) == author_uuid
            )
            if "next" not in data:
                break
            page += 1
        return sorted(
            prs,
            key=lambda pr: (str(pr.get("updated_at") or ""), pr["number"]),
            reverse=True,
        )

    def pr_info(self, pr_id: str) -> PullRequest | None:
        # PR-id -> PR reverse lookup. Reads ANY state (no state filter), so `revise`
        # can detect a MERGED PR. `_api` returns None on an empty ("null") body and
        # raises on a non-zero proxy exit (an absent PR), so None means empty, not
        # error (matches the github adapter's shape).
        data = self._api(f"{self._base()}/pullrequests/{pr_id}", "pr view")
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
            f"{self._base()}/pullrequests", "pr create", method="POST", payload=payload
        )
        return self._pr_from_api(data or {})

    def _statuses(self, pr_id: str, what: str) -> list[dict[str, Any]]:
        data = self._api(
            f"{self._base()}/pullrequests/{pr_id}/statuses?pagelen=100",
            what,
        )
        return list((data or {}).get("values") or [])

    @staticmethod
    def _status_ident(entry: dict[str, Any]) -> str:
        raw = f"{entry.get('name') or ''}{entry.get('key') or ''}"
        return raw.lower().replace(" ", "")

    def ci_rollup(self, pr_id: str) -> CIStatus:
        entry = next(
            (
                s
                for s in self._statuses(pr_id, "pr statuses")
                if "pipeline" in self._status_ident(s)
            ),
            None,
        )
        state = str((entry or {}).get("state") or "").upper()
        checks: list[CICheck] = (
            [
                {
                    "name": "Pipeline",
                    "status": state,
                    "conclusion": state,
                    "url": (entry or {}).get("url"),
                }
            ]
            if state
            else []
        )
        if state == "SUCCESSFUL":
            return {"status": "green", "checks": checks, "detail": "pipeline successful"}
        if state in ("FAILED", "STOPPED", "ERROR"):
            return {"status": "failed", "checks": checks, "detail": f"pipeline {state.lower()}"}
        detail = "pipeline in progress" if state else "no pipeline entry yet"
        return {"status": "pending", "checks": checks, "detail": detail}

    def mark_ready(self, pr_id: str) -> None:
        self._api(
            f"{self._base()}/pullrequests/{pr_id}",
            "pr ready",
            method="PUT",
            payload={"draft": False},
        )

    def update_pr_body(self, pr_id: str, body: str) -> None:
        self._api(
            f"{self._base()}/pullrequests/{pr_id}",
            "pr body",
            method="PUT",
            payload={"description": body},
        )

    def merge(self, pr_id: str, squash: bool = True) -> None:
        payload = {"merge_strategy": "squash"} if squash else {}
        self._api(
            f"{self._base()}/pullrequests/{pr_id}/merge",
            "pr merge",
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
        me = self._api("2.0/user", "whoami")
        my_account_id = (me or {}).get("account_id")
        data = self._api(f"{self._base()}/default-reviewers", "default reviewers")
        reviewers = [
            {"uuid": v["uuid"]}
            for v in ((data or {}).get("values") or [])
            if v.get("uuid") and v.get("account_id") != my_account_id
        ]
        self._api(
            f"{self._base()}/pullrequests/{pr_id}",
            "set reviewers",
            method="PUT",
            payload={"reviewers": reviewers},
        )

    def source_url(self, pr_id: str, sha: str, path: str, start_line: int, end_line: int) -> str:
        """Return a commit-pinned Bitbucket Cloud source URL."""
        del pr_id  # The source view is commit-addressed rather than PR-addressed.
        encoded = quote(path, safe="/")
        anchor = (
            f"#lines-{start_line}" if start_line == end_line else f"#lines-{start_line}:{end_line}"
        )
        return f"https://bitbucket.org/{self._workspace}/{self._repo}/src/{sha}/{encoded}{anchor}"

    # ─── review threads (CodeRabbit) ──────────────────────────────────────

    def _fetch_all_comments(self, pr_id: str) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._api(
                f"{self._base()}/pullrequests/{pr_id}/comments?page={page}&pagelen=100",
                "pr comments",
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

        CR registers a commit status (the same source `ci_rollup` reads for the
        pipeline) that goes INPROGRESS -> SUCCESSFUL independent of the finding
        count. That is the reliable completion signal: on a CLEAN review CR posts
        only a Walkthrough and NO `Actionable comments posted: N` comment, so a
        comment-marker gate would never fire and would burn the full wait on every
        clean PR (verified on brinta-data-platform: zero-finding PRs carry a
        `CodeRabbit` status of `SUCCESSFUL` but no count comment). Comment markers
        are also unreliable as a START vs DONE signal, the Walkthrough is posted at
        review start (flow-arva).

        Absent entry (CR not registered yet) or INPROGRESS -> not done; any
        terminal state (incl. FAILED) means CR has stopped, so waiting longer
        will not surface more threads."""
        entry = next(
            (
                s
                for s in self._statuses(pr_id, "pr statuses")
                if "coderabbit" in self._status_ident(s)
            ),
            None,
        )
        state = str((entry or {}).get("state") or "").upper()
        return state in _TERMINAL

    def post_reply(self, pr_id: str, thread_id: str, body: str) -> None:
        self._api(
            f"{self._base()}/pullrequests/{pr_id}/comments",
            "pr comment",
            method="POST",
            payload={"content": {"raw": body}, "parent": {"id": int(thread_id)}},
        )

    def resolve_thread(self, pr_id: str, thread_id: str) -> bool:
        """Resolve a top-level inline comment thread, then VERIFY by re-reading it.

        Success is `.resolution != null` on the re-fetched comment, NOT a top-level
        `resolved` flag (which the resolve response does not carry)."""
        self._api(
            f"{self._base()}/pullrequests/{pr_id}/comments/{thread_id}/resolve",
            "resolve",
            method="POST",
        )
        check = self._api(
            f"{self._base()}/pullrequests/{pr_id}/comments/{thread_id}",
            "resolve verify",
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
