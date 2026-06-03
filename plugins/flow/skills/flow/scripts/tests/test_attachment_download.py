"""Attachment download: Jira adapter bytes fetch, beads NotSupported, and the
`tracker_cli download-attachments` command (sanitization, size cap, graceful
no-attachment backends)."""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

import tracker as t
import tracker_beads as tb
import tracker_cli
import tracker_jira as tj
from tracker import NotSupported, TrackerError


class _RawResponse:
    """urlopen-shaped response whose body is raw bytes (not JSON)."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeHttp:
    def __init__(self, responses: list[Any]) -> None:
        self._iter = iter(responses)
        self.calls: list[Any] = []

    def __call__(self, req: Any) -> Any:
        self.calls.append(req)
        entry = next(self._iter)
        if isinstance(entry, BaseException):
            raise entry
        return entry


def _att(**over: Any) -> t.Attachment:
    base: t.Attachment = {
        "id": "10001",
        "filename": "shot.png",
        "size": 6,
        "mime_type": "image/png",
        "created_at": "",
        "url": "https://api.atlassian.com/ex/jira/c/rest/api/3/attachment/content/10001",
    }
    base.update(over)  # type: ignore[typeddict-item]
    return base


def _jira(monkeypatch: pytest.MonkeyPatch, http: tj.HttpFn) -> tj.JiraAdapter:
    monkeypatch.setenv("ATLASSIAN_EMAIL", "you@example.com")
    monkeypatch.setenv("ATLASSIAN_API_TOKEN", "tok")
    return tj.JiraAdapter({"backend": "jira", "cloud_id": "c", "project_key": "FT"}, http=http)


# ─── Jira adapter ───────────────────────────────────────────────────────────


def test_jira_download_returns_bytes_with_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _FakeHttp([_RawResponse(b"\x89PNG\r\n")])
    adapter = _jira(monkeypatch, http)
    data = adapter.download_attachment(_att())
    assert data == b"\x89PNG\r\n"
    req = http.calls[0]
    assert req.full_url.endswith("/attachment/content/10001")
    assert req.get_header("Authorization") is not None


def test_jira_download_no_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _jira(monkeypatch, _FakeHttp([]))
    with pytest.raises(TrackerError):
        adapter.download_attachment(_att(url=None))


def test_jira_download_http_error_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    err = urllib.error.HTTPError("u", 404, "nf", Message(), io.BytesIO(b""))
    adapter = _jira(monkeypatch, _FakeHttp([err]))
    with pytest.raises(TrackerError):
        adapter.download_attachment(_att(url="https://x/y"))


# ─── beads adapter ──────────────────────────────────────────────────────────


def test_beads_download_not_supported() -> None:
    adapter = tb.BeadsAdapter.__new__(tb.BeadsAdapter)
    with pytest.raises(NotSupported):
        adapter.download_attachment(_att())


# ─── tracker_cli download-attachments ───────────────────────────────────────


class _FakeTracker:
    def __init__(self, attachments: list[dict[str, Any]], *, supported: bool = True) -> None:
        self._attachments = attachments
        self._supported = supported
        self.downloaded: list[str] = []

    def get_attachments(self, key: str) -> list[dict[str, Any]]:
        del key
        if not self._supported:
            raise NotSupported("no attachments")
        return self._attachments

    def download_attachment(self, att: dict[str, Any]) -> bytes:
        self.downloaded.append(att["id"])
        return b"DATA-" + att["id"].encode()


def _seed_ws(root: Path, backend: str = "jira") -> None:
    flow = root / ".flow"
    flow.mkdir(parents=True, exist_ok=True)
    if backend == "jira":
        body = (
            '[tracker]\nbackend = "jira"\n\n'
            '[tracker.jira]\ncloud_id = "x"\nproject_key = "FT"\n\n'
            '[memory]\nnamespace = "d"\n'
        )
    else:
        body = (
            '[tracker]\nbackend = "beads"\n\n'
            '[tracker.beads]\nprefix = "bd"\n\n'
            '[memory]\nnamespace = "d"\n'
        )
    (flow / "workspace.toml").write_text(body, encoding="utf-8")


def test_cli_download_writes_and_sanitizes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_ws(tmp_path)
    out = tmp_path / "att"
    fake = _FakeTracker(
        [
            {"id": "1", "filename": "a.png", "size": 5, "url": "u1"},
            {"id": "2", "filename": "../evil name.txt", "size": 3, "url": "u2"},
        ]
    )
    rc = tracker_cli.cli_main(
        [
            "--workspace-root",
            str(tmp_path),
            "download-attachments",
            "--key",
            "FT-1",
            "--out",
            str(out),
        ],
        tracker_factory=lambda _cfg: fake,
    )
    assert rc == 0
    names = sorted(p.name for p in out.iterdir())
    assert "a.png" in names
    assert any("evil" in n and "/" not in n and " " not in n for n in names)
    payload = json.loads(capsys.readouterr().out)
    assert payload["supported"] is True
    assert len(payload["downloaded"]) == 2


def test_cli_download_size_cap_skips(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_ws(tmp_path)
    fake = _FakeTracker([{"id": "1", "filename": "big.bin", "size": 999, "url": "u"}])
    rc = tracker_cli.cli_main(
        [
            "--workspace-root",
            str(tmp_path),
            "download-attachments",
            "--key",
            "FT-1",
            "--out",
            str(tmp_path / "o"),
            "--max-bytes",
            "10",
        ],
        tracker_factory=lambda _cfg: fake,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["downloaded"][0]["skipped"] == "exceeds-max-bytes"
    assert fake.downloaded == []  # over-cap file never fetched


def test_cli_download_beads_graceful(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_ws(tmp_path, backend="beads")
    fake = _FakeTracker([], supported=False)
    rc = tracker_cli.cli_main(
        [
            "--workspace-root",
            str(tmp_path),
            "download-attachments",
            "--key",
            "bd-1",
            "--out",
            str(tmp_path / "o"),
        ],
        tracker_factory=lambda _cfg: fake,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["supported"] is False
    assert payload["downloaded"] == []
