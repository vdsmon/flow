"""JiraAdapter coverage tests.

Strategy:

- Pure helpers (`_content_to_adf`, `_normalize_state`, `_classify_transition_error`,
  `_adf_to_plain`) are unit-tested directly — no HTTP, no auth.
- Adapter methods are tested via a `FakeHttp` callable that returns canned
  `urlopen`-shaped responses. The adapter's `http` constructor parameter is the
  injection point.

No live Jira hits. No `ATLASSIAN_*` env vars expected at test time (we set
them via monkeypatch).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from email.message import Message
from email.utils import format_datetime
from io import BytesIO
from typing import Any, cast

import pytest

import tracker as t
import tracker_jira as tj

# ─── Fake HTTP plumbing ─────────────────────────────────────────────────────


class _Response:
    """Minimal urlopen-shaped response."""

    def __init__(self, body: dict[str, Any] | list[Any] | None, status: int = 200) -> None:
        self.status = status
        if body is None:
            self._payload = b""
        else:
            self._payload = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._payload


def _http_error(
    url: str,
    status: int,
    body: dict[str, Any] | bytes | None,
    *,
    retry_after: str | None = None,
) -> urllib.error.HTTPError:
    if isinstance(body, dict):
        fp = BytesIO(json.dumps(body).encode("utf-8"))
    elif isinstance(body, bytes):
        fp = BytesIO(body)
    else:
        fp = BytesIO(b"")
    headers: Message = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(url, status, "err", headers, fp)  # type: ignore[arg-type]


class _FakeHttp:
    """Sequenced fake HTTP. Each entry is (predicate, response_or_exception)."""

    def __init__(self, responses: Iterable[Any]) -> None:
        self._iter = iter(responses)
        self.calls: list[urllib.request.Request] = []

    def __call__(self, req: urllib.request.Request) -> _Response:
        self.calls.append(req)
        try:
            entry = next(self._iter)
        except StopIteration as e:
            raise AssertionError(f"unexpected extra request: {req.method} {req.full_url}") from e
        if isinstance(entry, BaseException):
            raise entry
        return entry  # type: ignore[return-value]


def _body_dict(req: urllib.request.Request) -> dict[str, Any]:
    """Return the JSON body sent on `req`, or `{}` if none."""
    if req.data is None:
        return {}
    return cast("dict[str, Any]", json.loads(cast("bytes", req.data)))


def _make_adapter(
    monkeypatch: pytest.MonkeyPatch, http: tj.HttpFn, **config_overrides: Any
) -> tj.JiraAdapter:
    monkeypatch.setenv("ATLASSIAN_EMAIL", "you@example.com")
    monkeypatch.setenv("ATLASSIAN_API_TOKEN", "tok")
    cfg: dict[str, Any] = {
        "backend": "jira",
        "cloud_id": "cloud-xyz",
        "project_key": "FT",
        **config_overrides,
    }
    return tj.JiraAdapter(cfg, http=http)


# ─── Construction ───────────────────────────────────────────────────────────


def test_construction_rejects_missing_cloud_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLASSIAN_EMAIL", "you@example.com")
    monkeypatch.setenv("ATLASSIAN_API_TOKEN", "tok")
    with pytest.raises(t.TrackerConfigError, match="cloud_id"):
        tj.JiraAdapter({"backend": "jira", "project_key": "FT"})


def test_construction_rejects_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLASSIAN_EMAIL", "you@example.com")
    monkeypatch.delenv("ATLASSIAN_API_TOKEN", raising=False)
    with pytest.raises(t.TrackerConfigError, match="ATLASSIAN_API_TOKEN"):
        tj.JiraAdapter({"backend": "jira", "cloud_id": "c", "project_key": "FT"})


def test_capabilities_cover_closed_enum(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _make_adapter(monkeypatch, _FakeHttp([]))
    enum_names = {
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
    }
    advertised = {c["name"] for c in adapter.capabilities}
    assert advertised == enum_names


# ─── Content / ADF helpers ──────────────────────────────────────────────────


def test_content_to_adf_accepts_adf_json() -> None:
    body = json.dumps({"type": "doc", "version": 1, "content": []})
    result = tj._content_to_adf({"body": body, "fmt": "adf"})
    assert result["type"] == "doc"


def test_content_to_adf_rejects_malformed_adf() -> None:
    with pytest.raises(t.TrackerError, match="not valid JSON"):
        tj._content_to_adf({"body": "{not json", "fmt": "adf"})


def test_content_to_adf_wraps_plain_as_paragraph() -> None:
    result = tj._content_to_adf({"body": "hi", "fmt": "plain"})
    assert result["content"][0]["content"][0]["text"] == "hi"


def test_content_to_adf_rejects_markdown() -> None:
    with pytest.raises(t.NotSupported, match="markdown"):
        tj._content_to_adf({"body": "# heading", "fmt": "md"})


def test_adf_to_plain_extracts_nested_text() -> None:
    node = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "hello "},
                    {"type": "text", "text": "world"},
                ],
            }
        ],
    }
    assert tj._adf_to_plain(node) == "hello world"


# ─── State normalization mapping ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("native", "category", "resolution", "expected"),
    [
        ("To Do", "new", None, "open"),
        ("Open", "new", None, "open"),
        ("In Progress", "indeterminate", None, "in_progress"),
        ("Blocked", "indeterminate", None, "blocked"),
        ("On Hold", "indeterminate", None, "blocked"),
        ("In Review", "indeterminate", None, "in_review"),
        ("QA", "indeterminate", None, "in_review"),
        ("Ready for Merge", "indeterminate", None, "in_review"),
        ("Done", "done", "Done", "done"),
        ("Done", "done", "Won't Do", "cancelled"),
        ("Done", "done", "Duplicate", "cancelled"),
        ("Done", "done", "Cancelled", "cancelled"),
    ],
)
def test_normalize_state_mapping(
    native: str, category: str, resolution: str | None, expected: str
) -> None:
    normalized, diagnostic = tj._normalize_state(native, category, resolution)
    assert normalized == expected
    assert native in diagnostic or "category" in diagnostic


# ─── Transition error classification ────────────────────────────────────────


def test_classify_transition_403() -> None:
    kind, _ = tj._classify_transition_error(403, {"errorMessages": ["You lack permission"]})
    assert kind == "permission_denied"


def test_classify_transition_missing_required_fields() -> None:
    kind, detail = tj._classify_transition_error(
        400, {"errors": {"resolution": "required", "fixVersions": "required"}}
    )
    assert kind == "missing_required_field"
    assert "fixVersions" in detail


def test_classify_transition_wrong_source_state() -> None:
    kind, _ = tj._classify_transition_error(
        400, {"errorMessages": ["Transition is not valid from current status"]}
    )
    assert kind == "wrong_source_state"


def test_classify_transition_validator_failed() -> None:
    kind, _ = tj._classify_transition_error(
        400, {"errorMessages": ["Validator failed: PR must be linked"]}
    )
    assert kind == "validator_failed"


def test_classify_transition_default_catch_all() -> None:
    kind, detail = tj._classify_transition_error(400, {"errorMessages": ["something else"]})
    assert kind == "validator_failed"
    assert "something else" in detail


# ─── Retry-After parsing ────────────────────────────────────────────────────


def test_retry_after_seconds_numeric() -> None:
    assert tj._retry_after_seconds("5", 1.0) == 5.0


def test_retry_after_seconds_http_date_in_future() -> None:
    # RFC 7231 permits an HTTP-date; float() would raise ValueError here.
    future = datetime.now(UTC) + timedelta(seconds=120)
    header = format_datetime(future, usegmt=True)
    delay = tj._retry_after_seconds(header, 1.0)
    assert 60.0 <= delay <= 120.0


def test_retry_after_seconds_http_date_in_past_clamps_to_zero() -> None:
    past = datetime.now(UTC) - timedelta(seconds=120)
    header = format_datetime(past, usegmt=True)
    assert tj._retry_after_seconds(header, 1.0) == 0.0


def test_retry_after_seconds_garbage_falls_back_to_default() -> None:
    assert tj._retry_after_seconds("not-a-date-or-number", 1.0) == 1.0


def test_retry_after_seconds_none_falls_back_to_default() -> None:
    assert tj._retry_after_seconds(None, 1.0) == 1.0


# ─── Adapter HTTP integration (fake transport) ──────────────────────────────


def _issue_payload(
    key: str = "FT-1", native_status: str = "Open", category_key: str = "new"
) -> dict[str, Any]:
    return {
        "key": key,
        "fields": {
            "summary": "sample",
            "description": {"type": "doc", "content": []},
            "status": {
                "name": native_status,
                "statusCategory": {"key": category_key, "name": "To Do"},
            },
            "issuetype": {"name": "Task"},
            "priority": {"name": "Medium"},
            "assignee": None,
            "comment": {"comments": []},
            "parent": None,
            "attachment": [],
            "labels": [],
            "resolution": None,
            "issuelinks": [],
        },
    }


def test_get_issue_returns_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _FakeHttp(
        [
            _Response(_issue_payload()),  # /issue/FT-1
            _Response([]),  # /issue/FT-1/remotelink
        ]
    )
    adapter = _make_adapter(monkeypatch, http)
    ticket = adapter.get("FT-1")
    assert ticket["key"] == "FT-1"
    assert ticket["summary"] == "sample"
    assert ticket["type"] == "Task"
    assert len(http.calls) == 2


def test_get_issue_populates_links_from_issuelinks(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _issue_payload()
    payload["fields"]["issuelinks"] = [
        {
            "type": {"name": "Blocks"},
            "outwardIssue": {"key": "FT-2"},
        },
        {
            "type": {"name": "Relates"},
            "inwardIssue": {"key": "FT-3"},
        },
    ]
    http = _FakeHttp(
        [
            _Response(payload),  # /issue/FT-1
            _Response([]),  # /issue/FT-1/remotelink
        ]
    )
    adapter = _make_adapter(monkeypatch, http)
    ticket = adapter.get("FT-1")
    links = ticket["links"]
    assert {"kind": "blocks", "from_key": "FT-1", "to_key": "FT-2"} in links
    assert {"kind": "relates", "from_key": "FT-3", "to_key": "FT-1"} in links
    # `issuelinks` must be in the requested field set so the payload carries it.
    requested = http.calls[0].full_url
    assert "issuelinks" in requested


def test_get_fields_includes_issuelinks() -> None:
    assert "issuelinks" in tj._GET_FIELDS


def test_list_assigned_open_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _FakeHttp([_Response({"issues": [_issue_payload(key="FT-9")]})])
    adapter = _make_adapter(monkeypatch, http)
    refs = adapter.list_assigned("open")
    assert refs[0]["key"] == "FT-9"
    sent = http.calls[0]
    assert sent.method == "POST"
    body = _body_dict(sent)
    assert "currentUser()" in body["jql"]
    assert "statusCategory != Done" in body["jql"]


def test_list_transitions_marks_required_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _FakeHttp(
        [
            _Response(
                {
                    "transitions": [
                        {
                            "id": "31",
                            "name": "Done",
                            "to": {
                                "name": "Done",
                                "statusCategory": {"key": "done"},
                            },
                            "isAvailable": True,
                            "fields": {
                                "resolution": {
                                    "required": True,
                                    "schema": {"type": "option"},
                                    "allowedValues": [
                                        {"value": "Done"},
                                        {"value": "Won't Do"},
                                    ],
                                }
                            },
                        }
                    ]
                }
            )
        ]
    )
    adapter = _make_adapter(monkeypatch, http)
    trans = adapter.list_transitions("FT-1")
    assert trans[0]["id"] == "31"
    assert trans[0]["to_normalized_state"] == "done"
    required = trans[0]["required_fields"]
    assert required and required[0]["key"] == "resolution"
    assert required[0]["enum_values"] == ["Done", "Won't Do"]


def test_transition_success_returns_new_state(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _FakeHttp(
        [
            _Response(None),  # POST /transitions
            _Response(_issue_payload(native_status="Done", category_key="done")),  # state() call
        ]
    )
    adapter = _make_adapter(monkeypatch, http)
    result = adapter.transition("FT-1", "31")
    assert result["success"] is True
    assert result["new_state"] is not None
    assert result["new_state"]["normalized"] == "done"


def test_transition_success_when_followup_state_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # POST applies the transition; the follow-up state() GET 404s and raises
    # TrackerError. The applied transition must still report success.
    http = _FakeHttp(
        [
            _Response(None),  # POST /transitions
            _http_error("https://x", 404, {"errorMessages": ["Issue does not exist"]}),  # state()
        ]
    )
    adapter = _make_adapter(monkeypatch, http)
    result = adapter.transition("FT-1", "31")
    assert result["success"] is True
    assert result["failure_kind"] is None
    assert result["new_state"] is None


def test_transition_permission_denied_maps_to_failure_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _FakeHttp(
        [
            _http_error(
                "https://example.com/transitions",
                403,
                {"errorMessages": ["No permission"]},
            )
        ]
    )
    adapter = _make_adapter(monkeypatch, http)
    result = adapter.transition("FT-1", "31")
    assert result["success"] is False
    assert result["failure_kind"] == "permission_denied"


def test_transition_missing_required_field_maps_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _FakeHttp(
        [
            _http_error(
                "https://example.com/transitions",
                400,
                {"errors": {"resolution": "resolution is required"}},
            )
        ]
    )
    adapter = _make_adapter(monkeypatch, http)
    result = adapter.transition("FT-1", "31")
    assert result["failure_kind"] == "missing_required_field"


def test_state_returns_resolution_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _issue_payload(native_status="Done", category_key="done")
    payload["fields"]["resolution"] = {"name": "Won't Do"}
    http = _FakeHttp([_Response(payload)])
    adapter = _make_adapter(monkeypatch, http)
    state = adapter.state("FT-7")
    assert state["resolution"] == "Won't Do"
    assert state["normalized"] == "cancelled"


def test_is_shipped_returns_not_shipped_when_not_done(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _FakeHttp(
        [_Response(_issue_payload(native_status="In Progress", category_key="indeterminate"))]
    )
    adapter = _make_adapter(monkeypatch, http)
    ship = adapter.is_shipped("FT-1")
    assert ship["state"] == "not_shipped"
    assert ship["evidence"] is None


def test_is_shipped_not_yet_observed_when_done_no_pr_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _FakeHttp(
        [
            _Response(_issue_payload(native_status="Done", category_key="done")),  # state()
            # project_requires_pr() — empty workflow list => False
            _Response({"values": []}),
        ]
    )
    adapter = _make_adapter(monkeypatch, http)
    ship = adapter.is_shipped("FT-1")
    assert ship["state"] == "not_yet_observed"
    assert ship["evidence"] is not None
    assert ship["evidence"]["tracker"] == "jira"


def test_is_shipped_indeterminate_when_pr_required(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _FakeHttp(
        [
            _Response(_issue_payload(native_status="Done", category_key="done")),
            _Response(
                {
                    "values": [
                        {
                            "transitions": [
                                {
                                    "to": {"statusCategory": {"key": "done"}},
                                    "rules": {
                                        "validators": [{"type": "com.atlassian.LinkedPullRequest"}]
                                    },
                                }
                            ]
                        }
                    ]
                }
            ),
        ]
    )
    adapter = _make_adapter(monkeypatch, http)
    ship = adapter.is_shipped("FT-1")
    assert ship["state"] == "indeterminate"
    assert ship["evidence"]["requires_pr"] is True


# ─── 401 / 404 error mapping ────────────────────────────────────────────────


def test_401_raises_tracker_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _FakeHttp([_http_error("https://x", 401, {"errorMessages": ["bad creds"]})])
    adapter = _make_adapter(monkeypatch, http)
    with pytest.raises(t.TrackerConfigError, match="invalid credentials"):
        adapter.get("FT-1")


def test_404_on_get_raises_tracker_error(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _FakeHttp([_http_error("https://x", 404, {"errorMessages": ["Issue does not exist"]})])
    adapter = _make_adapter(monkeypatch, http)
    with pytest.raises(t.TrackerError, match="Issue does not exist"):
        adapter.get("FT-999")


def test_429_with_http_date_retry_after_retries_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 429 carrying an HTTP-date Retry-After must not crash on float() parsing;
    # the request retries and the second attempt succeeds.
    future = datetime.now(UTC) + timedelta(seconds=2)
    http = _FakeHttp(
        [
            _http_error(
                "https://x",
                429,
                {"errorMessages": ["rate limited"]},
                retry_after=format_datetime(future, usegmt=True),
            ),
            _Response({"key": "FT-1", "fields": {"status": {}, "resolution": None}}),
        ]
    )
    adapter = _make_adapter(monkeypatch, http)
    slept: list[float] = []
    monkeypatch.setattr(tj.time, "sleep", lambda s: slept.append(s))
    state = adapter.state("FT-1")
    assert state["native_status"] == ""
    assert len(http.calls) == 2
    assert slept and slept[0] <= 30.0


# ─── Capability-gated typed methods ─────────────────────────────────────────


def test_set_sprint_calls_agile_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _FakeHttp([_Response(None)])
    adapter = _make_adapter(monkeypatch, http)
    adapter.set_sprint("FT-1", "42")
    sent = http.calls[0]
    assert "/rest/agile/1.0/sprint/42/issue" in sent.full_url


def test_list_sprints_raises_not_supported_when_no_scrum_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _FakeHttp([_Response({"values": []})])
    adapter = _make_adapter(monkeypatch, http)
    with pytest.raises(t.NotSupported, match="no scrum board"):
        adapter.list_sprints("FT")


def test_add_watcher_sends_bare_json_string(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _FakeHttp([_Response(None)])
    adapter = _make_adapter(monkeypatch, http)
    adapter.add_watcher("FT-1", "user-123")
    sent = http.calls[0]
    assert sent.data == b'"user-123"'


def test_set_fix_versions_sends_named_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _FakeHttp([_Response(None)])
    adapter = _make_adapter(monkeypatch, http)
    adapter.set_fix_versions("FT-1", ["v1.0", "v1.1"])
    body = _body_dict(http.calls[0])
    assert body["fields"]["fixVersions"] == [{"name": "v1.0"}, {"name": "v1.1"}]


def test_set_epic_link_uses_parent_for_next_gen(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _FakeHttp(
        [
            _Response({"style": "next-gen"}),  # project detection
            _Response(None),  # put fields
        ]
    )
    adapter = _make_adapter(monkeypatch, http)
    adapter.set_epic_link("FT-2", "FT-1")
    body = _body_dict(http.calls[-1])
    assert body["fields"]["parent"] == {"key": "FT-1"}


def test_set_epic_link_uses_customfield_for_classic(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _FakeHttp(
        [
            _Response({"style": "classic"}),
            _Response(None),
        ]
    )
    adapter = _make_adapter(monkeypatch, http)
    adapter.set_epic_link("FT-2", "FT-1")
    body = _body_dict(http.calls[-1])
    assert body["fields"]["customfield_10014"] == "FT-1"


# ─── Public surface ─────────────────────────────────────────────────────────


def test_jira_adapter_is_structural_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _make_adapter(monkeypatch, _FakeHttp([]))
    assert isinstance(adapter, t.Tracker)
