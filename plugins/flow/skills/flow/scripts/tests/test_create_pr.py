from __future__ import annotations

import subprocess
from typing import ClassVar

import pytest

import create_pr as cp
import pr_body
from forge import PR_STATE, ForgeError, NotSupported, PullRequest

Recorder = list[list[str]]

# a realistic `git log -1 --format=%b`: the compose_commit skeleton body (trailer +
# surviving marker) plus appended hard-wrapped prose, distinct from the %s subject.
_RAW_BODY = (
    "ticket: flow-x1yq\n"
    "Closes flow-nr8c\n"
    "files:\n"
    "  - create_pr.py\n"
    "\n"
    "# body — fill in below this line\n"
    "This change builds a real PR\n"
    "body from the commit — cleanly.\n"
)


def _pr(url: str, head: str, *, base: str = "main", draft: bool = False) -> PullRequest:
    return {
        "id": "1",
        "url": url,
        "number": 1,
        "draft": draft,
        "base": base,
        "head": head,
        "state": "OPEN",
    }


def _git_runner(*, branch: str = "feature/flow-aut.7-x", push_rc: int = 0):
    """Fake runner for the git-only calls create_pr still makes directly."""
    calls: Recorder = []

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(args, 0, branch + "\n", "")
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(args, push_rc, "", "remote rejected")
        if args[:2] == ["git", "log"]:
            fmt = next((a for a in args if a.startswith("--format=")), "")
            if fmt == "--format=%b":
                return subprocess.CompletedProcess(args, 0, _RAW_BODY, "")
            return subprocess.CompletedProcess(args, 0, "chore: add coverage\n", "")
        return subprocess.CompletedProcess(args, 1, "", f"unexpected {args}")

    return run, calls


class _FakeForge:
    backend = "github"
    capabilities: ClassVar[list] = []

    def __init__(self, *, existing: str | None = None, created="https://github.com/o/r/pull/42"):
        self._existing = existing
        self._created = created
        self.opened: list[dict] = []
        self.raise_on_open: Exception | None = None
        self.reviewers_set: list[str] = []
        self.raise_on_reviewers: Exception | None = None

    def detect_pr(self, branch: str, state: PR_STATE = "open") -> PullRequest | None:
        return _pr(self._existing, branch) if self._existing else None

    def pr_info(self, pr_id: str) -> PullRequest | None:
        return None

    def open_pr(self, base: str, head: str, title: str, body: str, draft: bool) -> PullRequest:
        if self.raise_on_open:
            raise self.raise_on_open
        self.opened.append(
            {"base": base, "head": head, "draft": draft, "title": title, "body": body}
        )
        return _pr(self._created, head, base=base, draft=draft)

    def ci_rollup(self, pr_id: str):
        raise NotImplementedError

    def review_threads(self, pr_id: str):
        raise NotImplementedError

    def bot_review_present(self, pr_id: str) -> bool:
        raise NotImplementedError

    def post_reply(self, pr_id: str, thread_id: str, body: str) -> None: ...
    def resolve_thread(self, pr_id: str, thread_id: str) -> bool:
        return True

    def mark_ready(self, pr_id: str) -> None: ...
    def merge(self, pr_id: str, squash: bool = True) -> None: ...
    def delete_branch(self, branch: str) -> None: ...

    def set_default_reviewers(self, pr_id: str) -> None:
        if self.raise_on_reviewers:
            raise self.raise_on_reviewers
        self.reviewers_set.append(pr_id)

    def source_url(self, pr_id: str, sha: str, path: str, start_line: int, end_line: int) -> str:
        return f"https://github.com/o/r/blob/{sha}/{path}#L{start_line}-L{end_line}"


def _ran(calls: Recorder, prefix: list[str]) -> bool:
    return any(c[: len(prefix)] == prefix for c in calls)


def test_creates_when_no_existing_pr(tmp_path):
    run, calls = _git_runner()
    fg = _FakeForge(existing=None)
    url = cp.open_or_get_pr(tmp_path, base="main", runner=run, forge=fg)
    assert url == "https://github.com/o/r/pull/42"
    assert len(fg.opened) == 1
    assert _ran(calls, ["git", "push"])


def test_push_uses_explicit_refspec(tmp_path):
    run, calls = _git_runner()
    fg = _FakeForge(existing=None)
    cp.open_or_get_pr(tmp_path, base="main", runner=run, forge=fg)
    assert [
        "git",
        "push",
        "-u",
        "origin",
        "feature/flow-aut.7-x:refs/heads/feature/flow-aut.7-x",
    ] in calls
    assert ["git", "push", "-u", "origin", "feature/flow-aut.7-x"] not in calls


def test_idempotent_reuses_existing_pr(tmp_path):
    run, _ = _git_runner()
    fg = _FakeForge(existing="https://github.com/o/r/pull/7")
    url = cp.open_or_get_pr(tmp_path, base="main", runner=run, forge=fg)
    assert url == "https://github.com/o/r/pull/7"
    assert fg.opened == []  # never double-open


def test_refuses_protected_branch(tmp_path):
    run, _ = _git_runner(branch="main")
    with pytest.raises(cp.RefusedBranch):
        cp.open_or_get_pr(tmp_path, runner=run, forge=_FakeForge())


def test_refuses_detached_head(tmp_path):
    # a detached HEAD rev-parses to the literal "HEAD"; never push refs/heads/HEAD.
    run, calls = _git_runner(branch="HEAD")
    with pytest.raises(cp.RefusedBranch):
        cp.open_or_get_pr(tmp_path, runner=run, forge=_FakeForge())
    assert not _ran(calls, ["git", "push"])


def test_push_failure_is_tool_error(tmp_path):
    run, _ = _git_runner(push_rc=1)
    with pytest.raises(cp.ToolError):
        cp.open_or_get_pr(tmp_path, runner=run, forge=_FakeForge())


def test_forge_error_is_tool_error(tmp_path):
    run, _ = _git_runner()
    fg = _FakeForge()
    fg.raise_on_open = ForgeError("gh pr create failed")
    with pytest.raises(cp.ToolError):
        cp.open_or_get_pr(tmp_path, runner=run, forge=fg)


def test_open_draft_by_default(tmp_path):
    run, _ = _git_runner()
    fg = _FakeForge()
    cp.open_or_get_pr(tmp_path, base="main", runner=run, forge=fg)
    assert fg.opened[0]["draft"] is True


def test_open_ready_when_draft_false(tmp_path):
    run, _ = _git_runner()
    fg = _FakeForge()
    cp.open_or_get_pr(tmp_path, base="main", draft=False, runner=run, forge=fg)
    assert fg.opened[0]["draft"] is False


def test_open_draft_passes_draft(tmp_path):
    run, _ = _git_runner()
    fg = _FakeForge()
    cp.open_or_get_pr(tmp_path, base="main", draft=True, runner=run, forge=fg)
    assert fg.opened[0]["draft"] is True


def test_cli_prints_pr_url_token(tmp_path, monkeypatch, capsys):
    run, _ = _git_runner(branch="feature/flow-x")
    fg = _FakeForge(existing="https://github.com/o/r/pull/5")
    monkeypatch.setattr(cp, "_default_runner", lambda _repo: run)
    monkeypatch.setattr(cp, "_resolve_forge", lambda _ws: fg)
    rc = cp.cli_main(["--workspace-root", str(tmp_path), "--base", "main", "--ticket", "flow-x"])
    assert rc == 0
    assert "PR_URL=https://github.com/o/r/pull/5" in capsys.readouterr().out


def test_cli_missing_forge_block_is_tool_error(tmp_path, monkeypatch):
    run, _ = _git_runner(branch="feature/flow-x")
    monkeypatch.setattr(cp, "_default_runner", lambda _repo: run)
    # no [forge] block in tmp_path -> _resolve_forge raises ToolError -> exit 2
    rc = cp.cli_main(["--workspace-root", str(tmp_path), "--base", "main"])
    assert rc == 2


def test_cli_refused_protected_branch(tmp_path, monkeypatch):
    run, _ = _git_runner(branch="main")
    monkeypatch.setattr(cp, "_default_runner", lambda _repo: run)
    rc = cp.cli_main(["--workspace-root", str(tmp_path), "--base", "main"])
    assert rc == 3


def test_draft_config_default_true_when_no_workspace(tmp_path):
    assert cp._draft_config(tmp_path) is True


def test_draft_config_reads_false(tmp_path):
    (tmp_path / ".flow").mkdir()
    (tmp_path / ".flow" / "workspace.toml").write_text(
        "[create_pr]\ndraft = false\n", encoding="utf-8"
    )
    assert cp._draft_config(tmp_path) is False


def test_draft_config_reads_true(tmp_path):
    (tmp_path / ".flow").mkdir()
    (tmp_path / ".flow" / "workspace.toml").write_text(
        "[create_pr]\ndraft = true\n", encoding="utf-8"
    )
    assert cp._draft_config(tmp_path) is True


def test_base_config_none_when_no_workspace(tmp_path):
    assert cp._base_config(tmp_path) is None


def test_base_config_reads_value(tmp_path):
    (tmp_path / ".flow").mkdir()
    (tmp_path / ".flow" / "workspace.toml").write_text(
        '[create_pr]\nbase = "dev"\n', encoding="utf-8"
    )
    assert cp._base_config(tmp_path) == "dev"


def test_base_config_non_string_is_none(tmp_path):
    (tmp_path / ".flow").mkdir()
    (tmp_path / ".flow" / "workspace.toml").write_text(
        "[create_pr]\nbase = true\n", encoding="utf-8"
    )
    assert cp._base_config(tmp_path) is None


def test_cli_base_from_config(tmp_path, monkeypatch):
    (tmp_path / ".flow").mkdir()
    (tmp_path / ".flow" / "workspace.toml").write_text(
        '[create_pr]\nbase = "dev"\n', encoding="utf-8"
    )
    run, _ = _git_runner(branch="feature/flow-x")
    fg = _FakeForge(existing=None)
    monkeypatch.setattr(cp, "_default_runner", lambda _repo: run)
    monkeypatch.setattr(cp, "_resolve_forge", lambda _ws: fg)
    rc = cp.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    assert fg.opened[0]["base"] == "dev"


def test_cli_explicit_base_beats_config(tmp_path, monkeypatch):
    (tmp_path / ".flow").mkdir()
    (tmp_path / ".flow" / "workspace.toml").write_text(
        '[create_pr]\nbase = "dev"\n', encoding="utf-8"
    )
    run, _ = _git_runner(branch="feature/flow-x")
    fg = _FakeForge(existing=None)
    monkeypatch.setattr(cp, "_default_runner", lambda _repo: run)
    monkeypatch.setattr(cp, "_resolve_forge", lambda _ws: fg)
    rc = cp.cli_main(["--workspace-root", str(tmp_path), "--base", "main"])
    assert rc == 0
    assert fg.opened[0]["base"] == "main"


def test_cli_base_defaults_to_main(tmp_path, monkeypatch):
    run, _ = _git_runner(branch="feature/flow-x")
    fg = _FakeForge(existing=None)
    monkeypatch.setattr(cp, "_default_runner", lambda _repo: run)
    monkeypatch.setattr(cp, "_resolve_forge", lambda _ws: fg)
    rc = cp.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    assert fg.opened[0]["base"] == "main"


def test_built_scrubbed_body_reaches_open_pr(tmp_path):
    # the raw %b (trailer + marker + wrapped prose) is built into a clean body:
    # trailer dropped, marker gone, prose unwrapped, Closes footer kept.
    run, _ = _git_runner()
    fg = _FakeForge(existing=None)
    cp.open_or_get_pr(tmp_path, base="main", runner=run, forge=fg)
    opened = fg.opened[0]
    body = opened["body"]
    assert "ticket:" not in body
    assert "fill in below" not in body
    assert "This change builds a real PR body from the commit, cleanly." in body
    assert "—" not in body  # scrub ran in the chain (em-dash gone)
    assert body.rstrip().endswith("Closes flow-nr8c")
    # title stays the raw commit subject, untouched by the body transform
    assert opened["title"] == "chore: add coverage"


def test_reviewers_set_on_open(tmp_path):
    run, _ = _git_runner()
    fg = _FakeForge(existing=None)
    cp.open_or_get_pr(tmp_path, base="main", runner=run, forge=fg)
    assert fg.reviewers_set == ["1"]  # pr id from _pr()


def test_reviewers_not_set_on_existing_pr(tmp_path):
    run, _ = _git_runner()
    fg = _FakeForge(existing="https://github.com/o/r/pull/7")
    cp.open_or_get_pr(tmp_path, base="main", runner=run, forge=fg)
    assert fg.reviewers_set == []  # early-return on existing PR; set-on-open only


def test_not_supported_reviewers_degrades(tmp_path):
    run, _ = _git_runner()
    fg = _FakeForge(existing=None)
    fg.raise_on_reviewers = NotSupported("github has no default reviewers")
    url = cp.open_or_get_pr(tmp_path, base="main", runner=run, forge=fg)
    assert url == "https://github.com/o/r/pull/42"  # PR still returned
    assert len(fg.opened) == 1


def test_generic_forge_error_reviewers_degrades(tmp_path):
    run, _ = _git_runner()
    fg = _FakeForge(existing=None)
    fg.raise_on_reviewers = ForgeError("reviewer API hiccup")
    url = cp.open_or_get_pr(tmp_path, base="main", runner=run, forge=fg)
    assert url == "https://github.com/o/r/pull/42"  # hiccup never fails an open PR
    assert len(fg.opened) == 1


def test_empty_prose_body_falls_back_to_subject(tmp_path):
    # a %b that is trailer-only (no prose) -> built body is empty -> subject used.
    calls_runner, _ = _git_runner()

    def run(args):
        if args[:2] == ["git", "log"]:
            fmt = next((a for a in args if a.startswith("--format=")), "")
            if fmt == "--format=%b":
                return subprocess.CompletedProcess(args, 0, "ticket: flow-x\n", "")
            return subprocess.CompletedProcess(args, 0, "chore: subj only\n", "")
        return calls_runner(args)

    fg = _FakeForge(existing=None)
    cp.open_or_get_pr(tmp_path, base="main", runner=run, forge=fg)
    assert fg.opened[0]["body"] == "chore: subj only"


# ─── authored body via --body-file ───────────────────────────────────────────


def test_authored_body_file_reaches_open_pr_with_footer(tmp_path):
    # --body-file: the authored prose is used verbatim (scrubbed), and the
    # deterministic Closes footer from the commit trailer is appended. The commit
    # prose is NOT used.
    run, _ = _git_runner()
    fg = _FakeForge(existing=None)
    body_file = tmp_path / "pr_body.md"
    body_file.write_text(
        "**Summary line.**\n\nWhy this change.\n\n## Changes\n- `x.py`: does a thing\n"
    )
    cp.open_or_get_pr(tmp_path, base="main", runner=run, forge=fg, body_file=body_file)
    body = fg.opened[0]["body"]
    assert body.startswith("**Summary line.**")
    assert "## Changes" in body
    assert "builds a real PR" not in body  # commit prose not used
    assert body.rstrip().endswith("Closes flow-nr8c")  # deterministic footer from %b


def test_authored_body_scrub_floor_runs(tmp_path):
    run, _ = _git_runner()
    fg = _FakeForge(existing=None)
    body_file = tmp_path / "pr_body.md"
    body_file.write_text("Summary — with an em dash.\n")
    cp.open_or_get_pr(tmp_path, base="main", runner=run, forge=fg, body_file=body_file)
    assert "—" not in fg.opened[0]["body"]  # scrub floor ran over authored prose


def test_authored_empty_body_falls_back_to_subject(tmp_path):
    run, _ = _git_runner()
    fg = _FakeForge(existing=None)
    body_file = tmp_path / "pr_body.md"
    body_file.write_text("   \n")
    cp.open_or_get_pr(tmp_path, base="main", runner=run, forge=fg, body_file=body_file)
    assert fg.opened[0]["body"] == "chore: add coverage"


def test_missing_body_file_is_tool_error(tmp_path):
    run, _ = _git_runner()
    fg = _FakeForge(existing=None)
    with pytest.raises(cp.ToolError):
        cp.open_or_get_pr(
            tmp_path, base="main", runner=run, forge=fg, body_file=tmp_path / "nope.md"
        )


def test_authored_oversized_body_is_capped(tmp_path):
    # an oversized fenced-<details> evidence body on the authored path is capped by
    # _compose_body so it can never fail open_pr; the <summary> survives the trim.
    run, _ = _git_runner()
    fg = _FakeForge(existing=None)
    huge = "\n".join(f"log line {i}" for i in range(20000))
    body_file = tmp_path / "pr_body.md"
    body_file.write_text(
        "**Summary.**\n\nWhy.\n\n## Evidence\n<details>\n<summary>run: ok</summary>\n\n"
        f"```\n{huge}\n```\n\n</details>\n"
    )
    cp.open_or_get_pr(tmp_path, base="main", runner=run, forge=fg, body_file=body_file)
    body = fg.opened[0]["body"]
    assert len(body) > 0
    assert len(body) <= pr_body._FORGE_BODY_CAP
    assert "<summary>run: ok</summary>" in body


def test_fallback_oversized_body_is_capped(tmp_path):
    # the no-body-file path is capped too. The oversized %b block must be FENCED:
    # build_body unwraps prose but preserves fenced code, so a fenced block is what
    # actually reaches enforce_cap on this path.
    huge = "\n".join(f"log {i}" for i in range(20000))
    raw = "ticket: flow-x\nCloses flow-nr8c\n\nWhy this change.\n\n```\n" + huge + "\n```\n"

    def run(args):
        if args[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(args, 0, "feature/flow-x\n", "")
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["git", "log"]:
            fmt = next((a for a in args if a.startswith("--format=")), "")
            if fmt == "--format=%b":
                return subprocess.CompletedProcess(args, 0, raw, "")
            return subprocess.CompletedProcess(args, 0, "chore: big body\n", "")
        return subprocess.CompletedProcess(args, 1, "", f"unexpected {args}")

    fg = _FakeForge(existing=None)
    cp.open_or_get_pr(tmp_path, base="main", runner=run, forge=fg)
    body = fg.opened[0]["body"]
    assert len(body) > 0
    assert len(body) <= pr_body._FORGE_BODY_CAP
    # tier-1 structured trim, not the hard-truncate backstop: marker present,
    # fence head and tail survive, and the Closes footer is intact.
    assert "lines trimmed" in body
    assert "log 0" in body
    assert "log 19999" in body
    assert "Closes flow-nr8c" in body


# ─── bitbucket flatten: no raw HTML in bitbucket markdown ────────────────────


class _FakeBitbucketForge(_FakeForge):
    backend = "bitbucket"


_EVIDENCE_BODY = (
    "**Summary.**\n\nWhy.\n\n## Evidence\n<details>\n<summary>run: 3 passed (1s)</summary>\n\n"
    "```\nline a\nline b\n```\n\n</details>\n"
)


def test_bitbucket_authored_body_details_flattened(tmp_path):
    # bitbucket renders no raw HTML in markdown, so the authored <details> wrapper
    # is flattened to a plain ### heading + fenced body before open_pr.
    run, _ = _git_runner()
    fg = _FakeBitbucketForge(existing=None)
    body_file = tmp_path / "pr_body.md"
    body_file.write_text(_EVIDENCE_BODY)
    cp.open_or_get_pr(tmp_path, base="main", runner=run, forge=fg, body_file=body_file)
    body = fg.opened[0]["body"]
    assert "<details>" not in body
    assert "</details>" not in body
    assert "<summary>" not in body
    assert "### run: 3 passed (1s)" in body
    assert "```\nline a\nline b\n```" in body  # fenced transcript preserved
    assert body.rstrip().endswith("Closes flow-nr8c")  # footer still appended


def test_github_authored_body_details_untouched(tmp_path):
    run, _ = _git_runner()
    fg = _FakeForge(existing=None)
    body_file = tmp_path / "pr_body.md"
    body_file.write_text(_EVIDENCE_BODY)
    cp.open_or_get_pr(tmp_path, base="main", runner=run, forge=fg, body_file=body_file)
    body = fg.opened[0]["body"]
    assert "<details>" in body
    assert "<summary>run: 3 passed (1s)</summary>" in body


def test_bitbucket_fallback_body_details_flattened(tmp_path):
    # the no-body-file (commit-derived) path flattens too.
    raw = (
        "ticket: flow-x\nCloses flow-nr8c\n\nWhy.\n\n<details>\n<summary>run: ok</summary>\n\n"
        "```\nline a\n```\n\n</details>\n"
    )

    def run(args):
        if args[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(args, 0, "feature/flow-x\n", "")
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["git", "log"]:
            fmt = next((a for a in args if a.startswith("--format=")), "")
            if fmt == "--format=%b":
                return subprocess.CompletedProcess(args, 0, raw, "")
            return subprocess.CompletedProcess(args, 0, "chore: subj\n", "")
        return subprocess.CompletedProcess(args, 1, "", f"unexpected {args}")

    fg = _FakeBitbucketForge(existing=None)
    cp.open_or_get_pr(tmp_path, base="main", runner=run, forge=fg)
    body = fg.opened[0]["body"]
    assert "<details>" not in body
    assert "<summary>" not in body
    assert "### run: ok" in body
    assert "Closes flow-nr8c" in body


def test_cli_passes_body_file(tmp_path, monkeypatch):
    captured: dict = {}

    def fake_open(ws, *, base, draft, body_file=None):
        captured["body_file"] = body_file
        return "https://u/1"

    monkeypatch.setattr(cp, "open_or_get_pr", fake_open)
    bf = tmp_path / "b.md"
    bf.write_text("x")
    rc = cp.cli_main(["--workspace-root", str(tmp_path), "--body-file", str(bf)])
    assert rc == 0
    assert str(captured["body_file"]) == str(bf)
