"""Forge interface: the single source of truth for PR-host (forge) operations.

Library module (no shebang, no PEP 723 inline deps). Imported by other scripts.

The Forge Protocol declares the cross-host contract for PR mechanics: open a PR,
detect an existing one, read the CI rollup, drive review-bot threads, mark a draft
ready, squash-merge, delete the remote branch. Adapters (github / bitbucket)
implement it; they are constructed by `make_forge(config)`, which lazy-imports them
so this module stays stdlib-only.

This mirrors the tracker seam (`tracker.py`): a closed capability enum, normalized
result shapes, a typed exception tree, and a lazy-import factory. The `review_loop`
and `create_pr` stages reach the host ONLY through this seam (via `forge_cli.py`),
so a GitHub and a Bitbucket workspace run the same prose.

Key invariants:

- `FORGE_CAPABILITY_ENUM` is a CLOSED enum. Adapters advertise capabilities only
  from this set.
- Capability-gated methods (`review_threads`, `post_reply`, `resolve_thread`,
  `mark_ready`, `delete_branch`, `set_default_reviewers`) MUST raise `NotSupported`
  when the matching
  capability advertises `supported=false`, so callers can tell "this host cannot
  do X" from "this code path is unfinished".
- `resolve_thread` returns True ONLY when the thread is VERIFIED resolved. The
  Bitbucket adapter re-reads the comment and tests `.resolution != null`; it never
  trusts a top-level `resolved` flag (a hard-won ship-it gotcha).
- The `green` verdict in `ci_rollup` follows `evolve_reap.rollup_is_green`:
  non-empty AND every check completed-SUCCESS.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Protocol, TypedDict, runtime_checkable

# ─── Closed enums ────────────────────────────────────────────────────────────

FORGE_CAPABILITY_ENUM = Literal[
    "draft_prs",  # open_pr honors draft=True
    "ready_toggle",  # mark_ready() flips draft -> ready
    "review_threads",  # review_threads()/post_reply()/resolve_thread() implemented
    "squash_merge",  # merge(squash=True)
    "delete_branch",  # delete_branch()
    "ci_rollup",  # ci_rollup() implemented
    "default_reviewers",  # set_default_reviewers() attaches repo default reviewers on open
]

CI_STATUS = Literal["green", "pending", "failed"]

THREAD_SEVERITY = Literal["critical", "major", "minor", "nit", "unknown"]


# ─── Normalized result shapes ────────────────────────────────────────────────


class PullRequest(TypedDict):
    """A PR, normalized across hosts. `id` is the host handle the other ops take
    (gh: the number as a string; bitbucket: the PR id)."""

    id: str
    url: str
    number: int
    draft: bool
    base: str
    head: str
    state: str  # backend-native ("OPEN"/"open"/"MERGED"/...)


class CICheck(TypedDict):
    name: str
    status: str  # backend-native, e.g. "COMPLETED" / "IN_PROGRESS"
    conclusion: str  # backend-native, e.g. "SUCCESS" / "FAILURE" / ""
    url: str | None


class CIStatus(TypedDict):
    """One-shot CI rollup. The Monitor in the review_loop owns the repeat+sleep;
    this is a single read."""

    status: CI_STATUS
    checks: list[CICheck]
    detail: str  # one-line human trace ("3 checks, all green" / "build failed")


class ReviewThread(TypedDict):
    """A review-bot comment thread, normalized. Both adapters map raw host JSON to
    this shape so the loop prose never sees host-specific fields."""

    id: str
    file: str | None
    line: int | None
    severity: THREAD_SEVERITY
    title: str
    body: str
    resolved: bool
    author: str
    parent_id: str | None


class Capability(TypedDict):
    name: FORGE_CAPABILITY_ENUM
    supported: bool


# ─── Exceptions ──────────────────────────────────────────────────────────────


class ForgeError(Exception):
    """Base for all forge exceptions."""


class NotSupported(ForgeError):
    """Raised by a capability-gated method the adapter does not support.

    Adapters MUST raise this (not bare NotImplementedError) so callers can
    distinguish "this host cannot do X" from "this code path is unfinished".
    """


class ForgeConfigError(ForgeError):
    """Configuration error detected at factory time or validate-workspace.py."""


# ─── Protocol ────────────────────────────────────────────────────────────────


@runtime_checkable
class Forge(Protocol):
    """Cross-host PR interface. Implemented by per-host adapters.

    `detect_pr` / `pr_info` / `open_pr` / `ci_rollup` / `merge` are MANDATORY. The review-thread
    trio (`review_threads`, `post_reply`, `resolve_thread`) plus `mark_ready`,
    `delete_branch`, and `set_default_reviewers` are CAPABILITY-GATED: each MUST
    raise `NotSupported` when its capability advertises `supported=false`.
    """

    backend: str  # "github" | "bitbucket"
    capabilities: list[Capability]

    def detect_pr(self, branch: str) -> PullRequest | None: ...
    def pr_info(self, pr_id: str) -> PullRequest | None: ...  # PR-number reverse lookup, ANY state
    def open_pr(self, base: str, head: str, title: str, body: str, draft: bool) -> PullRequest: ...
    def ci_rollup(self, pr_id: str) -> CIStatus: ...
    def review_threads(self, pr_id: str) -> list[ReviewThread]: ...  # cap-gated
    def post_reply(self, pr_id: str, thread_id: str, body: str) -> None: ...  # cap-gated
    def resolve_thread(self, pr_id: str, thread_id: str) -> bool: ...  # cap-gated
    def mark_ready(self, pr_id: str) -> None: ...  # cap-gated ready_toggle
    def merge(self, pr_id: str, squash: bool = True) -> None: ...
    def delete_branch(self, branch: str) -> None: ...  # cap-gated delete_branch
    def set_default_reviewers(self, pr_id: str) -> None: ...  # cap-gated default_reviewers


# ─── Factory + config ────────────────────────────────────────────────────────

KNOWN_BACKENDS: tuple[str, ...] = ("github", "bitbucket")


def make_forge(config: dict[str, Any]) -> Forge:
    """Construct a Forge adapter from the flattened `[forge]` config.

    `config` MUST contain a `backend` key naming one of `KNOWN_BACKENDS`. Adapters
    are lazy-imported so this module stays stdlib-only.

    Raises:
        ForgeConfigError: if `backend` is missing or not in `KNOWN_BACKENDS`.
    """
    backend = config.get("backend")
    if backend is None:
        raise ForgeConfigError(
            f"forge.backend missing in workspace.toml; expected one of {KNOWN_BACKENDS!r}"
        )
    if backend not in KNOWN_BACKENDS:
        raise ForgeConfigError(
            f"forge.backend={backend!r} not recognized; expected one of {KNOWN_BACKENDS!r}"
        )

    if backend == "github":
        from forge_github import GitHubAdapter

        return GitHubAdapter(config)
    if backend == "bitbucket":
        from forge_bitbucket import BitbucketAdapter

        return BitbucketAdapter(config)

    # Unreachable per the membership check above; kept as a typing safety net.
    raise ForgeConfigError(f"forge.backend={backend!r} not handled by factory")


def read_forge_config(workspace_root: Path) -> dict[str, Any] | None:
    """Read `.flow/workspace.toml` and return the flattened `[forge]` config.

    Unlike `[tracker]`, the `[forge]` block is OPTIONAL: a workspace that keeps
    `create_pr`/`review_loop` at `none` needs no forge. Returns `None` when the
    block is absent so callers can decide whether their stage requires it.

    The per-backend sub-block (`[forge.github]` / `[forge.bitbucket]`) is flattened
    into the top level and `workspace_root` is injected, matching `make_forge`'s
    expectation. Shared by `forge_cli.py` and `create_pr.py`.

    Raises:
        ForgeConfigError: the block is present but `backend` is missing/unknown.
    """
    import _workspace

    try:
        data = _workspace.load_workspace_toml(workspace_root)
    except _workspace.WorkspaceConfigError as exc:
        raise ForgeConfigError(str(exc)) from exc
    forge = data.get("forge")
    if not isinstance(forge, dict):
        return None
    backend = forge.get("backend")
    if backend not in KNOWN_BACKENDS:
        raise ForgeConfigError(f"unknown forge.backend {backend!r}")
    flat: dict[str, Any] = {"backend": backend}
    sub = forge.get(backend)
    if isinstance(sub, dict):
        flat.update(sub)
    flat["workspace_root"] = str(workspace_root)
    return flat


__all__ = [
    "CI_STATUS",
    "FORGE_CAPABILITY_ENUM",
    "KNOWN_BACKENDS",
    "THREAD_SEVERITY",
    "CICheck",
    "CIStatus",
    "Capability",
    "Forge",
    "ForgeConfigError",
    "ForgeError",
    "NotSupported",
    "PullRequest",
    "ReviewThread",
    "make_forge",
    "read_forge_config",
]
