"""Tracker interface: the single source of truth for ticket lifecycle operations.

Library module (no shebang, no PEP 723 inline deps). Imported by other scripts.

The Tracker Protocol declares the cross-backend contract. Adapters (jira / beads /
future markdown / linear) implement it. Day 1 adapters live in `tracker_jira.py`
and `tracker_beads.py` — they are constructed by `make_tracker(config)`, which
lazy-imports them so this module stays stdlib-only.

Key invariants:

- `CAPABILITY_ENUM` is a CLOSED enum. Unknown capability names = config error at
  validate-workspace.py time. Adapters MUST advertise capabilities only from this set.
- `Transition.id` is the OPAQUE backend transition identifier. Callers MUST pass
  the id to `transition()`, NOT the human-readable `name`. Two transitions can
  share a name pointing to different ids (Jira common pattern).
- `is_shipped` is a PURE READ. Adapters MUST NOT write under `.flow/`. The writer
  is `observe-ship-event.py` invoked by the reflect stage or `/flow sync
  --observe-ship`.
- No `extra: dict` escape on `create()` or any mutator. Generic `edit(fields)`
  is explicitly DROPPED; ticket field mutations go through typed setters
  (`set_summary`, `set_description`, `set_priority`, `set_labels`,
  `set_assignee`). Backend-rich operations (sprints, watchers, fix_versions,
  components, epic_link, board_rank, custom fields, attachments) go through
  dedicated typed methods that raise `NotSupported` when the corresponding
  capability is `supported=false`.
"""

from __future__ import annotations

from typing import (
    Any,
    Literal,
    Protocol,
    TypedDict,
    runtime_checkable,
)

# ─── Closed enums ────────────────────────────────────────────────────────────

CAPABILITY_ENUM = Literal[
    "comments_adf",
    "comments_markdown",
    "attachments",
    "watchers",
    "sprints",
    "fix_versions",
    "components",
    "epic_link",
    "pr_links",
    "ci_links",
    "boards",
    "custom_fields",
    "transitions_with_validators",
    "resolutions",
]

NORMALIZED_STATES = Literal[
    "open",
    "in_progress",
    "blocked",
    "in_review",
    "done",
    "cancelled",
]

ShipStateLiteral = Literal[
    "shipped",
    "not_shipped",
    "indeterminate",
    "not_yet_observed",
]

ShipSource = Literal[
    "frozen_event_file",
    "live_backend_query",
    "none",
]

TransitionFailureKind = Literal[
    "none",
    "permission_denied",
    "wrong_source_state",
    "validator_failed",
    "ambiguous_transition",
    "missing_required_field",
]


# ─── Primitive value types ───────────────────────────────────────────────────


class Content(TypedDict):
    """Caller-declared content. Adapter converts if backend needs another fmt."""

    body: str
    fmt: Literal["md", "adf", "plain"]


class FieldSpec(TypedDict, total=False):
    """Typed field spec for transitions, custom fields, and create/edit payloads.

    `enum_values` is required iff `type == "enum"`. `required` defaults to False
    when omitted.
    """

    key: str
    type: Literal["string", "user", "enum", "date", "datetime", "number", "content"]
    enum_values: list[str] | None
    required: bool


class Comment(TypedDict):
    id: str
    author: str
    body: Content
    created_at: str  # ISO8601 UTC, Z suffix


class Attachment(TypedDict):
    id: str
    filename: str
    size: int
    mime_type: str
    created_at: str
    url: str | None  # adapter-specific download URL when supported


class Link(TypedDict):
    kind: str  # "depends_on" | "blocks" | "relates" | adapter-specific
    from_key: str
    to_key: str


class Sprint(TypedDict):
    id: str
    name: str
    state: Literal["active", "closed", "future"]
    start_date: str | None
    end_date: str | None


# ─── Capability ──────────────────────────────────────────────────────────────


class Capability(TypedDict):
    """Adapter-advertised capability flag, optionally with per-capability schema."""

    name: CAPABILITY_ENUM
    supported: bool
    # Optional per-capability payload schema (e.g., the `sprints` capability may
    # include a payload_schema describing allowed sprint field shape). The
    # validator rejects extra/edit payloads that exercise a capability where
    # supported=false.
    payload_schema: dict[str, Any] | None


# ─── Ticket shape ────────────────────────────────────────────────────────────


class TicketRef(TypedDict):
    key: str  # "FT-1234" | "bd-abc123"
    summary: str
    status: str  # backend-native status string
    priority: str


class Ticket(TicketRef):
    description: str
    type: str  # "Task" | "Story" | "Bug" | ...
    assignee: str | None
    comments: list[Comment]
    parent: str | None
    attachments: list[Attachment]
    links: list[Link]


# ─── State + transitions ─────────────────────────────────────────────────────


class TicketState(TypedDict):
    """Rich state captured alongside normalized state; metrics + dashboards need both."""

    native_status: str  # e.g. "In Progress", "QA", "Ready for Release"
    native_status_category: str | None  # Jira: "To Do" / "In Progress" / "Done"; None for beads
    resolution: str | None  # Jira: "Done" / "Won't Do"; None if unresolved
    normalized: NORMALIZED_STATES
    adapter_mapping_diagnostic: str  # one-line trace of which adapter rule produced normalized


class Transition(TypedDict):
    """One available transition. Callers select by `id`, never by `name`.

    `list_transitions` MAY return multiple entries with the same `name` pointing
    to different `id` values (Jira workflow pattern). Selecting by name is
    therefore ambiguous; the contract is strictly id-keyed.
    """

    id: str
    name: str
    to_state: str
    to_normalized_state: NORMALIZED_STATES
    required_fields: list[FieldSpec]
    available: bool
    unavailable_reason: str | None


class TransitionResult(TypedDict):
    success: bool
    failure_kind: TransitionFailureKind | None
    failure_detail: str | None
    new_state: TicketState | None  # populated on success


# ─── Shipped predicate ───────────────────────────────────────────────────────


class ShipState(TypedDict):
    """Result of `Tracker.is_shipped(key)`.

    PURE READ. The adapter NEVER writes to `.flow/`. When `state == "shipped"`,
    `source` is `frozen_event_file` and `evidence` is the frozen record. When
    `state == "not_yet_observed"`, `source` is `live_backend_query` and
    `evidence` is freshly computed for the workspace's `observe_ship_event(...)`
    function to persist. When `state == "not_shipped"` or `"indeterminate"`,
    `source == "none"` and `evidence` is None.
    """

    state: ShipStateLiteral
    shipped_at: str | None  # ISO8601 UTC; populated iff state=shipped
    evidence: dict[str, Any] | None
    source: ShipSource


# ─── Exceptions ──────────────────────────────────────────────────────────────


class TrackerError(Exception):
    """Base for all tracker exceptions."""


class NotSupported(TrackerError):
    """Raised by capability-gated methods when the adapter does not support them.

    Adapters MUST raise this (not bare NotImplementedError) so callers can
    distinguish "this backend cannot do X" from "this code path is unfinished".
    """


class TrackerConfigError(TrackerError):
    """Configuration error detected at factory time or validate-workspace.py."""


# ─── Protocol ────────────────────────────────────────────────────────────────


@runtime_checkable
class Tracker(Protocol):
    """Cross-backend ticket interface. Implemented by per-backend adapters.

    Lifecycle methods (`get`, `list_assigned`, `list_linked`, `list_transitions`,
    `create`, `edit`, `transition`, `comment`, `link`, `state`,
    `project_requires_pr`, `is_shipped`) are MANDATORY for all backends.

    Typed Jira-rich methods (`set_sprint`, `list_sprints`, `add_watcher`,
    `set_fix_versions`, `set_components`, `set_epic_link`, `board_rank`,
    `set_custom_field`, `get_attachments`, `upload_attachment`) are
    CAPABILITY-GATED. Each MUST raise `NotSupported` when the corresponding
    capability advertises `supported=false`.
    """

    backend: str  # "jira" | "beads"
    capabilities: list[Capability]

    # ─── lifecycle (mandatory) ────────────────────────────────────────────

    def get(self, key: str) -> Ticket: ...
    def list_assigned(self, filter: str = "open") -> list[TicketRef]: ...
    def list_linked(self, key: str) -> list[TicketRef]: ...
    def list_transitions(self, key: str) -> list[Transition]: ...
    def create(
        self,
        summary: Content,
        description: Content,
        type: str,
        parent: str | None = None,
        labels: list[str] | None = None,
        assignee: str | None = None,
    ) -> str: ...
    def set_summary(self, key: str, summary: Content) -> None: ...
    def set_description(self, key: str, description: Content) -> None: ...
    def set_priority(self, key: str, priority: str) -> None: ...
    def set_labels(self, key: str, labels: list[str]) -> None: ...
    def set_assignee(self, key: str, account_id: str | None) -> None: ...
    def transition(
        self,
        key: str,
        transition_id: str,
        fields: dict[str, Any] | None = None,
    ) -> TransitionResult: ...
    def comment(self, key: str, body: Content) -> None: ...
    def link(self, from_key: str, to_key: str, kind: str) -> None: ...
    def state(self, key: str) -> TicketState: ...
    def project_requires_pr(self) -> bool: ...
    def is_shipped(self, key: str) -> ShipState: ...

    # ─── capability-gated typed ops ───────────────────────────────────────

    def set_sprint(self, key: str, sprint_id: str) -> None: ...
    def list_sprints(self, project: str) -> list[Sprint]: ...
    def add_watcher(self, key: str, account_id: str) -> None: ...
    def set_fix_versions(self, key: str, versions: list[str]) -> None: ...
    def set_components(self, key: str, components: list[str]) -> None: ...
    def set_epic_link(self, key: str, epic_key: str) -> None: ...
    def board_rank(self, key: str, after_key: str | None) -> None: ...
    def set_custom_field(
        self,
        key: str,
        field_key: str,
        value: Any,
        schema: FieldSpec,
    ) -> None: ...
    def get_attachments(self, key: str) -> list[Attachment]: ...
    def upload_attachment(self, key: str, path: str) -> str: ...


# ─── Factory ─────────────────────────────────────────────────────────────────

KNOWN_BACKENDS: tuple[str, ...] = ("jira", "beads")


def make_tracker(config: dict[str, Any]) -> Tracker:
    """Construct a Tracker adapter from workspace config.

    `config` is the parsed `[tracker]` block from `.flow/workspace.toml`. It MUST
    contain a `backend` key naming one of `KNOWN_BACKENDS`.

    Adapters are lazy-imported so this module stays stdlib-only and so a missing
    adapter file fails LOUDLY at construction (not silently at module-import time).

    Raises:
        TrackerConfigError: if `backend` is missing or not in `KNOWN_BACKENDS`.
        ImportError: if the chosen backend's adapter module is not yet installed
            (phase 1-2 expected state for both jira and beads).
    """
    backend = config.get("backend")
    if backend is None:
        raise TrackerConfigError(
            f"tracker.backend missing in workspace.toml; expected one of {KNOWN_BACKENDS!r}"
        )
    if backend not in KNOWN_BACKENDS:
        raise TrackerConfigError(
            f"tracker.backend={backend!r} not recognized; expected one of {KNOWN_BACKENDS!r}"
        )

    if backend == "jira":
        # Lazy import; isolates Jira HTTP stack from beads-only workspaces.
        from tracker_jira import JiraAdapter

        return JiraAdapter(config)  # type: ignore[return-value]
    if backend == "beads":
        # Lazy import; isolates subprocess/`bd` requirements from jira-only workspaces.
        from tracker_beads import BeadsAdapter

        return BeadsAdapter(config)  # type: ignore[return-value]

    # Unreachable per the membership check above; kept as a typing safety net.
    raise TrackerConfigError(f"tracker.backend={backend!r} not handled by factory")


__all__ = [
    "CAPABILITY_ENUM",
    "KNOWN_BACKENDS",
    "NORMALIZED_STATES",
    "Attachment",
    "Capability",
    "Comment",
    "Content",
    "FieldSpec",
    "Link",
    "NotSupported",
    "ShipSource",
    "ShipState",
    "ShipStateLiteral",
    "Sprint",
    "Ticket",
    "TicketRef",
    "TicketState",
    "Tracker",
    "TrackerConfigError",
    "TrackerError",
    "Transition",
    "TransitionFailureKind",
    "TransitionResult",
    "make_tracker",
]
