"""Bitbucket forge adapter over the `brinta-ai` CLI.

Same host as `forge_bitbucket.py` (Bitbucket Cloud), different transport: every
REST call goes through `brinta-ai bitbucket <METHOD> <path> [body]`, the
authenticated proxy the Brinta marketplace plugins already depend on. One
credential store (`~/.config/brinta/git-credentials.json`, written by
`brinta-ai setup`) then serves flow and the Brinta tooling alike, and `bkt`
is not needed on the machine.

The adapter subclasses `BitbucketAdapter` and swaps `_api`: the parent builds
`2.0/...` paths for `bkt api`, while the proxy roots at the v2 API already, so
the prefix is stripped here. The two `bkt pr checks` text-parsing ops
(`ci_rollup`, `bot_review_present`) are reimplemented over the PR commit-status
endpoint instead, which carries the same pipeline and CodeRabbit entries as
structured JSON.

Proxy contract (brinta-ai >= 0.2.31, `bitbucket` is Tier 2 in
brinta-ai/docs/cli-contract.md):
- stdout is the raw response body; a bodyless success prints `(HTTP <n>, no body)`.
- non-2xx exits 1 with the body on stdout and one stderr line.
"""

from __future__ import annotations

import json
from typing import Any, override

from forge import CICheck, CIStatus, ForgeError
from forge_bitbucket import BitbucketAdapter

_TERMINAL = ("SUCCESSFUL", "FAILED", "STOPPED", "ERROR")


class BrintaAdapter(BitbucketAdapter):
    backend = "brinta"

    # ─── transport ────────────────────────────────────────────────────────

    @override
    def _api(
        self,
        path: str,
        what: str,
        *,
        method: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        # Parent methods build `2.0/...` paths for `bkt api`; the proxy already
        # roots at the v2 API, so strip the prefix. Full URLs pass through
        # unchanged on both transports.
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

    # ─── commit-status ops (replace the bkt `pr checks` text parsing) ─────

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

    @override
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

    @override
    def bot_review_present(self, pr_id: str) -> bool:
        # Same completion signal as the bkt adapter: CodeRabbit's commit status
        # reaching a terminal state (any, incl. FAILED - CR has stopped either way).
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
