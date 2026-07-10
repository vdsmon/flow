"""JiraAdapter: Atlassian Jira Cloud adapter for the Tracker protocol.

Stdlib-only. Transport is `urllib.request.urlopen` by default; tests inject a
fake via the `http` constructor parameter.

Base URL: `https://api.atlassian.com/ex/jira/{cloud_id}` (basic auth tolerated
on this OAuth-style host). Email + API token taken from environment:

- `ATLASSIAN_EMAIL`
- `ATLASSIAN_API_TOKEN`

Adapter raises `TrackerConfigError` at construction if either is missing.

See `inventory.md` (sibling file) for the full call-site map, HTTP error
classification table, ADF policy, board strategy, and epic-link probe.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, cast

from tracker import (
    Attachment,
    Capability,
    Comment,
    Content,
    FieldSpec,
    Link,
    NotSupported,
    ShipState,
    Sprint,
    Ticket,
    TicketRef,
    TicketState,
    TrackerConfigError,
    TrackerError,
    Transition,
    TransitionFailureKind,
    TransitionResult,
)

# ─── Module-level constants ──────────────────────────────────────────────────

ATLASSIAN_OAUTH_HOST = "https://api.atlassian.com"

_BLOCKED_HINTS = ("block", "hold", "wait")
_REVIEW_HINTS = ("review", "qa", "merge", "approval")
_CANCELLED_RESOLUTIONS = ("won't do", "wont do", "cancelled", "canceled", "duplicate", "won't fix")

# Transition-error regexes (see inventory.md HTTP error table).
_RE_WRONG_SOURCE = re.compile(r"(?i)\btransition\b.*\b(not valid|invalid|cannot be applied)\b")
_RE_VALIDATOR = re.compile(r"(?i)\bvalidat(or|ion)\b.*\b(fail|error|reject)\b")

_JIRA_CAPABILITIES: list[Capability] = [
    {"name": "comments_adf", "supported": True},
    {"name": "comments_markdown", "supported": False},
    {"name": "attachments", "supported": True},
    {"name": "watchers", "supported": True},
    {"name": "sprints", "supported": True},
    {"name": "fix_versions", "supported": True},
    {"name": "components", "supported": True},
    {"name": "epic_link", "supported": True},
    {"name": "pr_links", "supported": True},
    {"name": "ci_links", "supported": True},
    {"name": "boards", "supported": True},
    {"name": "custom_fields", "supported": True},
    {"name": "transitions_with_validators", "supported": True},
    {"name": "resolutions", "supported": True},
]


_GET_FIELDS = [
    "summary",
    "description",
    "status",
    "issuetype",
    "priority",
    "assignee",
    "comment",
    "parent",
    "attachment",
    "resolution",
    "labels",
    "issuelinks",
]

# Seam kind vocabulary -> Jira link-type name. blocks/depends_on both map to the Blocks type;
# relates -> Relates. Unknown kinds pass through raw so a workspace can name a custom type.
_LINK_KIND_NAMES: dict[str, str] = {
    "blocks": "Blocks",
    "depends_on": "Blocks",
    "relates": "Relates",
}

# `HttpFn` signature: receives a urllib.request.Request, returns a response object
# exposing `.read()` (bytes) and `.status` (int) and `.headers` (Mapping).
HttpFn = Callable[[urllib.request.Request], Any]


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _basic_auth_header(email: str, token: str) -> str:
    raw = f"{email}:{token}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _retry_after_seconds(value: str | None, default: float) -> float:
    """Parse a Retry-After header value to a delay in seconds.

    RFC 7231 permits either a delay in seconds or an HTTP-date. Try the numeric
    form first, then the date form (delay = max(0, date - now)). Fall back to
    `default` when both fail.
    """
    if value is None:
        return default
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    try:
        then = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return default
    if then is None:
        return default
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    delta = (then - datetime.now(UTC)).total_seconds()
    return max(0.0, delta)


def _parse_jira_error_body(e: urllib.error.HTTPError) -> tuple[dict[str, Any], bytes]:
    try:
        raw_body = e.read()
    except Exception:
        raw_body = b""
    parsed_body: dict[str, Any] = {}
    if raw_body:
        try:
            parsed_body = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            parsed_body = {"errorMessages": [raw_body.decode("utf-8", "replace")]}
    return parsed_body, raw_body


def _adf_paragraph(text: str) -> dict[str, Any]:
    """Wrap plain text as a single-paragraph ADF document."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]},
        ],
    }


def _content_to_adf(content: Content) -> dict[str, Any]:
    """Convert a Content payload to ADF JSON. md coerces to plain (lossy)."""
    fmt = content["fmt"]
    body = content["body"]
    if fmt == "adf":
        try:
            return cast("dict[str, Any]", json.loads(body))
        except json.JSONDecodeError as e:
            raise TrackerError(f"Content.fmt='adf' but body is not valid JSON: {e}") from e
    if fmt in ("plain", "md"):
        return _adf_paragraph(body)
    raise TrackerError(f"unknown Content.fmt={fmt!r}")


def _adf_to_plain(node: dict[str, Any] | None) -> str:
    """Extract text from an ADF doc. Lossy on rich formatting (intentional)."""
    if node is None:
        return ""
    parts: list[str] = []

    def walk(n: dict[str, Any]) -> None:
        if n.get("type") == "text" and isinstance(n.get("text"), str):
            parts.append(n["text"])
        for child in n.get("content", []) or []:
            walk(child)

    walk(node)
    return "".join(parts)


def _classify_transition_error(
    status: int, body: dict[str, Any]
) -> tuple[TransitionFailureKind, str]:
    """Map a 4xx /transitions response body to a TransitionFailureKind + detail."""
    errors = body.get("errors") or {}
    messages = body.get("errorMessages") or []
    joined_msgs = " ; ".join(str(m) for m in messages)

    if status == 403:
        return "permission_denied", joined_msgs or "permission denied"

    if errors:
        # Structured field errors take precedence over message-text matching.
        keys = sorted(errors.keys())
        detail = f"required fields: {keys}; messages: {joined_msgs}"
        return "missing_required_field", detail

    if _RE_WRONG_SOURCE.search(joined_msgs):
        return "wrong_source_state", joined_msgs
    if _RE_VALIDATOR.search(joined_msgs):
        return "validator_failed", joined_msgs

    return "validator_failed", joined_msgs or f"HTTP {status} with no error body"


def _normalize_state(
    native_status: str, category_key: str | None, resolution: str | None
) -> tuple[str, str]:
    """Return (normalized, diagnostic) per inventory.md normalization table."""
    native_lc = native_status.lower()
    cat = (category_key or "").lower()

    if cat == "done":
        if resolution and resolution.lower() in _CANCELLED_RESOLUTIONS:
            return "cancelled", f"category=done + resolution={resolution!r} -> cancelled"
        return "done", f"category=done + resolution={resolution!r} -> done"
    if cat == "new":
        return "open", f"category=new + native={native_status!r} -> open"
    if cat == "indeterminate":
        for hint in _BLOCKED_HINTS:
            if hint in native_lc:
                return (
                    "blocked",
                    f"category=indeterminate + native={native_status!r} "
                    f"matched blocked hint {hint!r}",
                )
        for hint in _REVIEW_HINTS:
            if hint in native_lc:
                return (
                    "in_review",
                    f"category=indeterminate + native={native_status!r} "
                    f"matched review hint {hint!r}",
                )
        return (
            "in_progress",
            f"category=indeterminate + native={native_status!r} -> in_progress (default)",
        )

    # Unknown category: fall through to in_progress as conservative default.
    return "in_progress", f"unknown category={category_key!r} + native={native_status!r}"


# ─── Adapter ─────────────────────────────────────────────────────────────────


class JiraAdapter:
    """Jira Cloud adapter. See module docstring for transport + auth conventions."""

    backend = "jira"
    capabilities = _JIRA_CAPABILITIES

    def __init__(self, config: dict[str, Any], http: HttpFn | None = None) -> None:
        cloud_id = config.get("cloud_id")
        project_key = config.get("project_key")
        if not cloud_id or not project_key:
            raise TrackerConfigError(
                "tracker.jira requires cloud_id and project_key in workspace.toml"
            )
        self.cloud_id: str = cloud_id
        self.project_key: str = project_key
        self.assignee_account_id: str | None = config.get("assignee_account_id")

        email = os.environ.get("ATLASSIAN_EMAIL", "").strip()
        token = os.environ.get("ATLASSIAN_API_TOKEN", "").strip()
        if not email or not token:
            raise TrackerConfigError(
                "JiraAdapter requires ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN env vars"
            )
        self._auth_header = _basic_auth_header(email, token)
        self._http: HttpFn = http if http is not None else urllib.request.urlopen
        # Cached at first set_sprint / list_sprints call.
        self._scrum_board_id: int | None = None

    # ─── core HTTP ────────────────────────────────────────────────────────

    def _url(self, path: str, *, agile: bool = False) -> str:
        base = "agile/1.0" if agile else "api/3"
        return f"{ATLASSIAN_OAUTH_HOST}/ex/jira/{self.cloud_id}/rest/{base}{path}"

    def _build_request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        *,
        agile: bool,
        query: dict[str, Any] | None,
        extra_headers: dict[str, str] | None,
        body_bytes: bytes | None,
    ) -> urllib.request.Request:
        url = self._url(path, agile=agile)
        if query:
            url = url + "?" + urllib.parse.urlencode(query, doseq=True)

        headers = {
            "Authorization": self._auth_header,
            "Accept": "application/json",
        }
        if body_bytes is not None:
            data = body_bytes
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            data = None
        if extra_headers:
            headers.update(extra_headers)

        return urllib.request.Request(url=url, data=data, method=method, headers=headers)

    def _http_error_retry_delay(self, e: urllib.error.HTTPError, attempt: int, path: str) -> float:
        status = e.code
        parsed_body, raw_body = _parse_jira_error_body(e)

        if status == 401:
            raise TrackerConfigError(
                "invalid credentials: check ATLASSIAN_EMAIL/ATLASSIAN_API_TOKEN"
            ) from e
        if status == 404:
            msg = parsed_body.get("errorMessages") or [f"endpoint not found: {path}"]
            raise TrackerError(f"{msg[0]}") from e
        if status == 409:
            raise TrackerError(f"conflict: {parsed_body or raw_body!r}") from e
        if status == 429 and attempt < 3:
            header_val = e.headers.get("Retry-After") if e.headers else None
            return min(_retry_after_seconds(header_val, 1.0), 30.0)
        if 500 <= status < 600 and attempt < 2:
            return 1.0 if attempt == 0 else 3.0
        if status == 429 or 500 <= status < 600:
            # Exhausted 5xx/429 retries raise plain TrackerError (matching URLError
            # exhaustion) so transition() never classifies them as hard 4xx failures
            # and enqueue-on-transient callers hit the transient path.
            raise TrackerError(f"HTTP {status} on {path}: transient retries exhausted") from e
        # Caller-visible 4xx (other than the special-cased ones) -- re-raise as HTTPError
        # so callers expecting transition-style classification can catch + handle.
        raise _JiraHTTPError(status, parsed_body, raw_body, path) from e

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        agile: bool = False,
        query: dict[str, Any] | None = None,
        raw_response: bool = False,
        extra_headers: dict[str, str] | None = None,
        body_bytes: bytes | None = None,
    ) -> Any:
        """Make a Jira REST call. Returns parsed JSON dict, or raw response if raw_response.

        Body precedence: `body_bytes` (used for multipart) > `body` (JSON dict). 5xx + 429
        retry policy applied. Auth always present. Errors classified per inventory.md.
        """
        req = self._build_request(
            method,
            path,
            body,
            agile=agile,
            query=query,
            extra_headers=extra_headers,
            body_bytes=body_bytes,
        )

        last_err: TrackerError | None = None
        for attempt in range(4):
            try:
                resp = self._http(req)
                if raw_response:
                    return resp
                raw = resp.read()
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as e:
                delay = self._http_error_retry_delay(e, attempt, path)
                time.sleep(delay)
                last_err = TrackerError(f"http {e.code} retry (attempt {attempt + 1})")
                continue
            except urllib.error.URLError as e:
                if attempt < 2:
                    time.sleep(1.0 if attempt == 0 else 3.0)
                    last_err = TrackerError(f"network error: {e.reason}")
                    continue
                raise TrackerError(f"network error: {e.reason}") from e

        if last_err is not None:
            raise last_err
        raise TrackerError("request retry exhausted with no captured error")

    # ─── builders ─────────────────────────────────────────────────────────

    def _comment_from_json(self, c: dict[str, Any]) -> Comment:
        body_node = c.get("body") or {}
        return {
            "id": str(c.get("id", "")),
            "author": (c.get("author") or {}).get("displayName", ""),
            "body": {"body": _adf_to_plain(body_node), "fmt": "plain"},
            "created_at": c.get("created", ""),
        }

    def _attachment_from_json(self, a: dict[str, Any]) -> Attachment:
        return {
            "id": str(a.get("id", "")),
            "filename": a.get("filename", ""),
            "size": int(a.get("size", 0)),
            "mime_type": a.get("mimeType", ""),
            "created_at": a.get("created", ""),
            "url": a.get("content"),
        }

    def _ticket_from_json(self, issue: dict[str, Any], links: list[Link] | None = None) -> Ticket:
        f = issue.get("fields", {}) or {}
        comments = (f.get("comment") or {}).get("comments", []) or []
        attachments = f.get("attachment") or []
        parent = f.get("parent") or {}
        priority = (f.get("priority") or {}).get("name", "")
        status = (f.get("status") or {}).get("name", "")
        assignee = f.get("assignee")
        description_node = f.get("description")
        return {
            "key": issue.get("key", ""),
            "summary": f.get("summary", ""),
            "status": status,
            "priority": priority,
            "description": _adf_to_plain(description_node),
            "type": (f.get("issuetype") or {}).get("name", ""),
            "assignee": (assignee or {}).get("accountId") if assignee else None,
            "comments": [self._comment_from_json(c) for c in comments],
            "parent": parent.get("key") if parent else None,
            "attachments": [self._attachment_from_json(a) for a in attachments],
            "links": links if links is not None else [],
            "labels": [str(x) for x in (f.get("labels") or [])],
        }

    def _ticket_ref_from_json(self, issue: dict[str, Any]) -> TicketRef:
        f = issue.get("fields", {}) or {}
        return {
            "key": issue.get("key", ""),
            "summary": f.get("summary", ""),
            "status": (f.get("status") or {}).get("name", ""),
            "priority": (f.get("priority") or {}).get("name", ""),
        }

    def _state_from_issue(self, issue: dict[str, Any]) -> TicketState:
        f = issue.get("fields", {}) or {}
        status = f.get("status") or {}
        category = status.get("statusCategory") or {}
        native_status = status.get("name", "")
        category_key = category.get("key")
        resolution_obj = f.get("resolution") or {}
        resolution = resolution_obj.get("name") if resolution_obj else None
        normalized, diagnostic = _normalize_state(native_status, category_key, resolution)
        return {
            "native_status": native_status,
            "native_status_category": category.get("name") if category_key else None,
            "resolution": resolution,
            "normalized": cast("Any", normalized),
            "adapter_mapping_diagnostic": diagnostic,
        }

    # ─── lifecycle (Protocol) ─────────────────────────────────────────────

    def get(self, key: str) -> Ticket:
        issue = self._request(
            "GET",
            f"/issue/{urllib.parse.quote(key)}",
            query={"fields": ",".join(_GET_FIELDS)},
        )
        links: list[Link] = []
        try:
            remote = self._request("GET", f"/issue/{urllib.parse.quote(key)}/remotelink")
            for rl in remote or []:
                obj = (rl.get("object") or {}) if isinstance(rl, dict) else {}
                url = obj.get("url", "")
                if url:
                    links.append({"kind": "remote", "from_key": key, "to_key": url})
        except TrackerError:
            # Remote link failures are non-fatal for the main fetch.
            pass
        for lnk in issue.get("fields", {}).get("issuelinks") or []:
            kind = (lnk.get("type") or {}).get("name", "relates").lower()
            inward = lnk.get("inwardIssue") or {}
            outward = lnk.get("outwardIssue") or {}
            if inward.get("key"):
                links.append({"kind": kind, "from_key": key, "to_key": inward["key"]})
            if outward.get("key"):
                links.append({"kind": kind, "from_key": outward["key"], "to_key": key})
        return self._ticket_from_json(issue, links=links)

    def list_assigned(self, filter: str = "open") -> list[TicketRef]:
        if filter == "open":
            jql = "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC"
        elif filter == "all":
            jql = "assignee = currentUser() ORDER BY updated DESC"
        else:
            jql = filter  # caller-supplied JQL passthrough
        body = {
            "jql": jql,
            "fields": ["summary", "status", "priority"],
            "maxResults": 50,
        }
        resp = self._request("POST", "/search/jql", body=body)
        return [self._ticket_ref_from_json(i) for i in (resp.get("issues") or [])]

    def list_transitions(self, key: str) -> list[Transition]:
        resp = self._request(
            "GET",
            f"/issue/{urllib.parse.quote(key)}/transitions",
            query={"expand": "transitions.fields"},
        )
        out: list[Transition] = []
        for tr in resp.get("transitions") or []:
            target_status = (tr.get("to") or {}).get("name", "")
            target_cat = ((tr.get("to") or {}).get("statusCategory") or {}).get("key")
            target_normalized, _ = _normalize_state(target_status, target_cat, None)
            req_fields_raw = tr.get("fields") or {}
            req_fields: list[FieldSpec] = []
            for fkey, fspec in req_fields_raw.items():
                if fspec.get("required"):
                    schema = fspec.get("schema") or {}
                    raw_type = schema.get("type", "string")
                    if raw_type in ("string", "user", "date", "datetime", "number"):
                        ftype: str = raw_type
                    elif raw_type == "option":
                        ftype = "enum"
                    else:
                        ftype = "string"
                    allowed = fspec.get("allowedValues") or []
                    enum_values = [str(v.get("value") or v.get("name") or "") for v in allowed]
                    req_fields.append(
                        {
                            "key": fkey,
                            "type": cast("Any", ftype),
                            "enum_values": enum_values or None,
                            "required": True,
                        }
                    )
            out.append(
                {
                    "id": str(tr.get("id", "")),
                    "name": tr.get("name", ""),
                    "to_state": target_status,
                    "to_normalized_state": cast("Any", target_normalized),
                    "required_fields": req_fields,
                    "available": bool(tr.get("isAvailable", True)),
                    "unavailable_reason": None,
                }
            )
        return out

    def create(
        self,
        summary: Content,
        description: Content,
        type: str,
        parent: str | None = None,
        labels: list[str] | None = None,
        assignee: str | None = None,
    ) -> str:
        fields: dict[str, Any] = {
            "project": {"key": self.project_key},
            "issuetype": {"name": type},
            "summary": _adf_to_plain(_content_to_adf(summary))
            if summary["fmt"] != "plain"
            else summary["body"],
            "description": _content_to_adf(description),
        }
        if parent:
            fields["parent"] = {"key": parent}
        if labels:
            fields["labels"] = labels
        if assignee:
            fields["assignee"] = {"accountId": assignee}
        resp = self._request("POST", "/issue", body={"fields": fields})
        return resp.get("key", "")

    def transition(
        self,
        key: str,
        transition_id: str,
        fields: dict[str, Any] | None = None,
    ) -> TransitionResult:
        body: dict[str, Any] = {"transition": {"id": transition_id}}
        if fields:
            body["fields"] = fields
        try:
            self._request("POST", f"/issue/{urllib.parse.quote(key)}/transitions", body=body)
        except _JiraHTTPError as e:
            failure_kind, detail = _classify_transition_error(e.status, e.body)
            return {
                "success": False,
                "failure_kind": failure_kind,
                "failure_detail": detail,
                "new_state": None,
            }
        try:
            new_state: TicketState | None = self.state(key)
        except TrackerError:
            # The transition applied; a failed follow-up read must not surface as failure.
            new_state = None
        return {
            "success": True,
            "failure_kind": None,
            "failure_detail": None,
            "new_state": new_state,
        }

    def comment(self, key: str, body: Content) -> None:
        self._request(
            "POST",
            f"/issue/{urllib.parse.quote(key)}/comment",
            body={"body": _content_to_adf(body)},
        )

    def link(self, from_key: str, to_key: str, kind: str) -> None:
        name = _LINK_KIND_NAMES.get(kind, kind)
        self._request(
            "POST",
            "/issueLink",
            body={
                "type": {"name": name},
                "inwardIssue": {"key": from_key},
                "outwardIssue": {"key": to_key},
            },
        )

    def state(self, key: str) -> TicketState:
        issue = self._request(
            "GET",
            f"/issue/{urllib.parse.quote(key)}",
            query={"fields": "status,resolution"},
        )
        return self._state_from_issue(issue)

    def project_requires_pr(self) -> bool:
        """Conservative default. Requires `workflows.read` scope; many tokens lack it.

        Returns False on auth failure or empty workflow list; the workspace's ship-event observer is
        the authoritative source. This is a hint used by `is_shipped` to decide between "shipped"
        and "indeterminate".
        """
        try:
            resp = self._request(
                "GET",
                "/workflow/search",
                query={"projectKey": self.project_key, "expand": "transitions.rules"},
            )
        except TrackerError:
            return False
        for wf in resp.get("values") or []:
            for tr in wf.get("transitions") or []:
                target = (tr.get("to") or {}).get("statusCategory") or {}
                if target.get("key") != "done":
                    continue
                for rule in (tr.get("rules") or {}).get("validators") or []:
                    rule_type = (rule.get("type") or "").lower()
                    if "pullrequest" in rule_type or "linkedpr" in rule_type:
                        return True
        return False

    def is_shipped(self, key: str) -> ShipState:
        """PURE READ. Never writes under `.flow/`.

        Caller (the workspace's `observe_ship_event`) is responsible for persisting the evidence
        dict when `state == "not_yet_observed"`. Adapter has no knowledge of `.flow/` path, see plan
        section "Shipped predicate / ship-event evidence".
        """
        issue_state = self.state(key)
        if issue_state["normalized"] != "done":
            return {
                "state": "not_shipped",
                "shipped_at": None,
                "evidence": None,
                "source": "none",
            }

        evidence: dict[str, Any] = {
            "tracker": "jira",
            "tracker_status": issue_state["native_status"],
            "resolution": issue_state["resolution"],
        }

        if self.project_requires_pr():
            # Done category but no PR linkage means indeterminate until a ship
            # event with PR evidence is observed.
            evidence["requires_pr"] = True
            return {
                "state": "indeterminate",
                "shipped_at": None,
                "evidence": evidence,
                "source": "live_backend_query",
            }
        return {
            "state": "not_yet_observed",
            "shipped_at": None,
            "evidence": evidence,
            "source": "live_backend_query",
        }

    # ─── capability-gated typed ops ───────────────────────────────────────

    def _resolve_scrum_board(self) -> int:
        if self._scrum_board_id is not None:
            return self._scrum_board_id
        resp = self._request(
            "GET",
            "/board",
            agile=True,
            query={"projectKeyOrId": self.project_key, "type": "scrum"},
        )
        boards = resp.get("values") or []
        if not boards:
            raise NotSupported(f"no scrum board configured for project={self.project_key}")
        self._scrum_board_id = int(boards[0]["id"])
        return self._scrum_board_id

    def set_sprint(self, key: str, sprint_id: str) -> None:
        self._request(
            "POST",
            f"/sprint/{urllib.parse.quote(sprint_id)}/issue",
            agile=True,
            body={"issues": [key]},
        )

    def list_sprints(self, project: str) -> list[Sprint]:
        # Protocol passes `project` for backends where sprint scope is per-project (e.g. beads).
        # Jira sprints belong to boards; we resolve via `self.project_key` cached at __init__.
        del project
        board_id = self._resolve_scrum_board()
        resp = self._request(
            "GET",
            f"/board/{board_id}/sprint",
            agile=True,
            query={"state": "active,future,closed", "maxResults": 50},
        )
        out: list[Sprint] = []
        for s in resp.get("values") or []:
            state_raw = (s.get("state") or "").lower()
            state: Any = state_raw if state_raw in ("active", "closed", "future") else "future"
            out.append(
                {
                    "id": str(s.get("id", "")),
                    "name": s.get("name", ""),
                    "state": state,
                    "start_date": s.get("startDate"),
                    "end_date": s.get("endDate"),
                }
            )
        return out

    def list_issue_types(self) -> list[dict[str, Any]]:
        """Issue types available for the project, each `{name, hierarchyLevel}`.

        Reads the createmeta issuetypes page (takes the project KEY, not a
        numeric id). The verb uses `hierarchyLevel` to find the epic/parent type
        and to offer a type vocab.
        """
        resp = self._request(
            "GET",
            f"/issue/createmeta/{urllib.parse.quote(self.project_key)}/issuetypes",
            query={"maxResults": 50},
        )
        return [
            {
                "name": it.get("name", ""),
                "hierarchyLevel": it.get("hierarchyLevel"),
            }
            for it in resp.get("issueTypes") or []
        ]

    def list_epics(self) -> list[dict[str, Any]]:
        """Active hierarchy-1 issues for parent selection, `[{key, summary}]`.

        Resolves the hierarchy-1 type name via `list_issue_types()` (never hardcodes "Epic", some
        projects name it "Project"). Returns `[]` when no hierarchy-1 type exists.
        """
        epic_type = next(
            (it["name"] for it in self.list_issue_types() if it.get("hierarchyLevel") == 1),
            None,
        )
        if not epic_type:
            return []
        # JQL quotes are backslash-escaped; a type name may legitimately carry an
        # apostrophe (e.g. "Bug's Nest"), which would otherwise break the query.
        esc = epic_type.replace("\\", "\\\\").replace("'", "\\'")
        jql = (
            f"project = {self.project_key} AND issuetype = '{esc}' "
            "AND statusCategory != Done ORDER BY updated DESC"
        )
        resp = self._request(
            "POST",
            "/search/jql",
            body={"jql": jql, "fields": ["summary"], "maxResults": 50},
        )
        out: list[dict[str, Any]] = []
        for issue in resp.get("issues") or []:
            f = issue.get("fields") or {}
            out.append({"key": issue.get("key", ""), "summary": f.get("summary", "")})
        return out

    def get_attachments(self, key: str) -> list[Attachment]:
        issue = self._request(
            "GET",
            f"/issue/{urllib.parse.quote(key)}",
            query={"fields": "attachment"},
        )
        atts = (issue.get("fields") or {}).get("attachment") or []
        return [self._attachment_from_json(a) for a in atts]

    def download_attachment(self, attachment: Attachment) -> bytes:
        """Fetch an attachment's raw bytes from its content URL.

        `attachment["url"]` is Jira's absolute `content` link, so it bypasses
        `_url`/`_request` (which prepend the API base). Jira 302-redirects content
        to a signed media URL; urllib follows the redirect and drops the
        Authorization header on the cross-host hop, which is correct (the target
        carries its own signature).
        """
        url = attachment.get("url")
        if not url:
            raise TrackerError(f"attachment {attachment.get('id')!r} has no content url")
        req = urllib.request.Request(
            url=url,
            method="GET",
            headers={"Authorization": self._auth_header, "Accept": "*/*"},
        )
        try:
            resp = self._http(req)
            return resp.read()
        except urllib.error.HTTPError as exc:
            raise TrackerError(f"attachment download failed ({url}): HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise TrackerError(f"attachment download failed ({url}): {exc.reason}") from exc


# ─── Internal exception (escapes _request to caller, never user-visible) ─────


class _JiraHTTPError(TrackerError):
    """Carries the structured 4xx body so transition() can classify it."""

    def __init__(self, status: int, body: dict[str, Any], raw_body: bytes, path: str) -> None:
        super().__init__(f"HTTP {status} on {path}: {body or raw_body!r}")
        self.status = status
        self.body = body
        self.raw_body = raw_body
        self.path = path
