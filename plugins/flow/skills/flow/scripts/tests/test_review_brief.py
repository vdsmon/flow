from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import review_brief as rb
import state

SHA_A = "a" * 40
SHA_B = "b" * 40


class FakeForge:
    backend = "github"

    def __init__(self, sha: str = SHA_A):
        self.sha = sha
        self.capabilities = []
        self.source_calls: list[tuple[str, str, str, int, int]] = []

    def pr_info(self, pr_id: str):
        return {
            "id": pr_id,
            "number": int(pr_id),
            "url": f"https://github.com/acme/flow/pull/{pr_id}",
            "draft": True,
            "base": "main",
            "head": "feat/review-brief",
            "head_sha": self.sha,
            "state": "OPEN",
        }

    def source_url(self, pr_id: str, sha: str, path: str, start_line: int, end_line: int):
        self.source_calls.append((pr_id, sha, path, start_line, end_line))
        return f"https://github.com/acme/flow/blob/{sha}/{path}#L{start_line}-L{end_line}"


class GitRunner:
    def __init__(
        self,
        head: str = SHA_A,
        remote_head: str | None = None,
        *,
        diffs: dict[str, str] | None = None,
    ):
        self.head = head
        self.remote_head = remote_head or head
        self.diffs = diffs or {}
        self.calls: list[list[str]] = []
        self.files = {
            "src/scope.py": (
                "def resolve_scope(cwd):\n"
                "    # Keep <script>alert('no')</script> inert.\n"
                '    return {"root": cwd, "attempts": 3}\n'
                "\n"
                "def cleanup(scope):\n"
                "    return scope\n"
            )
        }

    def __call__(self, args: list[str]):
        self.calls.append(args)
        if args == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, self.head + "\n", "")
        if args[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(args, 0, SHA_B + "\n", "")
        if args[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(args, 0, self.diffs.get(args[-1], ""), "")
        if args[:4] == ["git", "ls-remote", "--exit-code", "origin"]:
            return subprocess.CompletedProcess(args, 0, f"{self.remote_head}\t{args[4]}\n", "")
        if args[:2] == ["git", "show"]:
            path = args[2].split(":", 1)[1]
            value = self.files.get(path)
            if value is not None:
                return subprocess.CompletedProcess(args, 0, value, "")
        return subprocess.CompletedProcess(args, 1, "", f"unexpected command: {args}")


def _content(*, mode: str = "full") -> dict:
    return {
        "schema_version": 1,
        "mode": mode,
        "title": "Cleanup that cannot escape its workspace",
        "outcome": "The invoking workspace is now the hard boundary for cleanup.",
        "risk": "high",
        "change_shape": "Cross-cutting safety change",
        "motivation": {
            "observed_problem": "Cleanup could derive scope from unrelated maintainer state.",
            "why_it_matters": "A destructive candidate set could include another repository.",
        },
        "scenarios": [
            {
                "name": "An operator starts from workspace A",
                "before_label": "scope could drift",
                "after_label": "scope is derived once",
                "before_steps": ["Invoke in A", "Read global state", "Select worktree in B"],
                "after_steps": ["Invoke in A", "Resolve A", "Reject worktree in B"],
            }
        ],
        "system_map": {
            "caption": "One boundary now feeds discovery and deletion.",
            "nodes": [
                {"id": "workspace", "label": "Workspace", "kind": "Boundary", "changed": True},
                {"id": "discover", "label": "Discovery", "kind": "Filter", "changed": True},
                {"id": "reap", "label": "Reap", "kind": "Action", "changed": False},
            ],
            "edges": [
                {"from": "workspace", "to": "discover"},
                {"from": "discover", "to": "reap"},
            ],
        },
        "decisions": [
            {"title": "Resolve once", "body": "Pass the boundary into downstream mechanics."}
        ],
        "invariants": [
            {"title": "Scope is stable", "body": "Discovery and action share one identity."}
        ],
        "code_evidence": [
            {
                "claim": "Derive scope at the command boundary",
                "explanation": "Downstream cleanup receives an already-resolved workspace.",
                "path": "src/scope.py",
                "start_line": 1,
                "end_line": 3,
            }
        ],
        "verification": [
            {
                "claim": "Scope scenarios pass",
                "evidence": "12 targeted tests passed.",
                "status": "passed",
            }
        ],
        "limitations": ["The Forge remains the source of truth for the full diff."],
        "reviewer_prompts": ["Does every destructive path receive the same scope?"],
    }


def _write_content(tmp_path: Path, value: dict) -> Path:
    path = tmp_path / "brief-input.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _request(tmp_path: Path, content_path: Path, *, open_browser: bool = False):
    return rb.RenderRequest(
        workspace_root=tmp_path,
        ticket_dir=tmp_path / ".flow" / "runs" / "flow-x",
        pr_id="42",
        content_path=content_path,
        open_browser=open_browser,
    )


def test_render_publishes_self_contained_snapshot_and_receipt(tmp_path):
    content_path = _write_content(tmp_path, _content())
    forge = FakeForge()
    runner = GitRunner()

    receipt = rb.render(_request(tmp_path, content_path), forge=forge, runner=runner)

    artifact_dir = tmp_path / ".flow" / "runs" / "flow-x" / "stages" / "review_brief" / SHA_A
    html_path = artifact_dir / f"review-brief-{SHA_A[:12]}.html"
    assert receipt.snapshot_sha == SHA_A
    assert receipt.mode == "full"
    assert receipt.html_path == str(html_path)
    assert (artifact_dir / "brief.json").is_file()
    assert (artifact_dir / "receipt.json").is_file()
    document = html_path.read_text(encoding="utf-8")
    assert "Content-Security-Policy" in document
    assert "default-src &#x27;none&#x27;" in document
    assert "<script>alert" not in document
    assert "&lt;script&gt;alert" in document
    assert "Relevant components and the direction" in document
    assert 'aria-labelledby="brief-title"' in document
    assert 'tabindex="0" aria-label="Scrollable relevant system map"' in document
    assert f"blob/{SHA_A}/src/scope.py#L1-L3" in document
    assert "estimated" not in document.lower()
    assert forge.source_calls == [("42", SHA_A, "src/scope.py", 1, 3)]


def test_render_uses_focused_unified_diff_for_changed_evidence(tmp_path):
    diff = """diff --git a/src/scope.py b/src/scope.py
index 1111111..2222222 100644
--- a/src/scope.py
+++ b/src/scope.py
@@ -1,3 +1,3 @@
 def resolve_scope(cwd):
-    return {"root": cwd}
+    return {"root": cwd, "attempts": 3}
     return scope
"""
    runner = GitRunner(diffs={"src/scope.py": diff})
    receipt = rb.render(
        _request(tmp_path, _write_content(tmp_path, _content())),
        forge=FakeForge(),
        runner=runner,
    )

    document = Path(receipt.html_path).read_text(encoding="utf-8")
    assert 'class="code-line deleted"' in document
    assert 'class="diff-marker">-' in document
    assert 'class="code-line added"' in document
    assert 'class="diff-marker">+' in document
    assert 'class="code-line unchanged"' in document
    assert 'class="code-lines"' in document
    assert "decisive" not in document
    assert ["git", "merge-base", SHA_A, "refs/remotes/origin/main"] in runner.calls


def test_unchanged_evidence_falls_back_to_commit_pinned_source(tmp_path):
    receipt = rb.render(
        _request(tmp_path, _write_content(tmp_path, _content())),
        forge=FakeForge(),
        runner=GitRunner(),
    )

    document = Path(receipt.html_path).read_text(encoding="utf-8")
    assert 'class="code-line unchanged"' in document
    assert 'class="diff-marker"> ' in document
    assert 'class="code-line added' not in document
    assert 'class="code-line deleted' not in document
    assert "decisive" not in document


def test_highlight_lines_is_not_part_of_the_authoring_contract():
    schema = rb.provider_schema()
    evidence = schema["properties"]["code_evidence"]["items"]
    assert "highlight_lines" not in evidence["properties"]
    assert "highlight_lines" not in evidence["required"]

    content = _content()
    content["code_evidence"][0]["highlight_lines"] = [1]
    with pytest.raises(rb.ValidationError, match="unknown fields: highlight_lines"):
        rb.validate_content(content)


def test_portuguese_authored_prose_localizes_renderer_copy(tmp_path):
    content = _content()
    content.update(
        {
            "title": "Rebaixamento de grau sem perder o controle",
            "outcome": "O modelo mantém a posição e o extrato consistentes para o administrador.",
            "change_shape": "Alteração focada no domínio",
            "motivation": {
                "observed_problem": "A conciliação podia produzir dados inconsistentes.",
                "why_it_matters": "O administrador precisa confiar na posição do fundo.",
            },
            "limitations": ["A revisão completa continua disponível no Forge."],
            "reviewer_prompts": ["As regras do domínio continuam claras?"],
        }
    )
    content["scenarios"] = [
        {
            "name": "Um administrador rebaixa o grau",
            "before_label": "os nomes divergiam",
            "after_label": "o domínio fica consistente",
            "before_steps": ["Enviar o arquivo", "Conciliar os dados"],
            "after_steps": ["Enviar o arquivo", "Confirmar a posição"],
        }
    ]
    content["system_map"]["caption"] = "O modelo alimenta a posição e o extrato."
    content["system_map"]["nodes"] = [
        {"id": "modelo", "label": "Modelo do fundo", "kind": "Domínio", "changed": True}
    ]
    content["system_map"]["edges"] = []
    content["decisions"] = [
        {"title": "Renomear uma vez", "body": "O domínio usa um nome consistente."}
    ]
    content["invariants"] = [
        {"title": "A posição permanece correta", "body": "O extrato continua conciliado."}
    ]
    content["code_evidence"][0].update(
        {
            "claim": "Aplicar a regra no limite",
            "explanation": "O serviço recebe o modelo já validado.",
        }
    )
    content["verification"] = [
        {
            "claim": "Os cenários passam",
            "evidence": "Todos os testes direcionados passaram.",
            "status": "passed",
        }
    ]

    receipt = rb.render(
        _request(tmp_path, _write_content(tmp_path, content)),
        forge=FakeForge(),
        runner=GitRunner(),
    )

    document = Path(receipt.html_path).read_text(encoding="utf-8")
    assert '<html lang="pt-BR">' in document
    assert "Evidências focadas no código" in document
    assert "A fatia relevante do sistema" in document
    assert "Nesta página" in document
    assert "Focused code evidence" not in document


def test_long_system_map_labels_wrap_without_losing_the_full_label(tmp_path):
    content = _content()
    label = (
        "Modelo de subclasse e grau renomeado para o domínio financeiro com "
        "detalhamento adicional que não cabe dentro do cartão"
    )
    content["system_map"]["nodes"][0]["label"] = label

    receipt = rb.render(
        _request(tmp_path, _write_content(tmp_path, content)),
        forge=FakeForge(),
        runner=GitRunner(),
    )

    document = Path(receipt.html_path).read_text(encoding="utf-8")
    assert f"<title>Boundary: {label}</title>" in document
    assert document.count('<tspan x="16"') >= 2
    assert "…</tspan>" in document


def test_render_opens_local_file_but_open_failure_is_nonfatal(tmp_path):
    content_path = _write_content(tmp_path, _content())
    opened: list[str] = []

    def opener(uri: str) -> bool:
        opened.append(uri)
        return False

    receipt = rb.render(
        _request(tmp_path, content_path, open_browser=True),
        forge=FakeForge(),
        runner=GitRunner(),
        opener=opener,
    )

    assert opened[0].startswith("file://")
    assert receipt.opened is False
    assert receipt.warnings == ["browser did not confirm that it opened the review brief"]


def test_auto_mode_uses_compact_for_a_small_linear_change(tmp_path):
    content = _content(mode="auto")
    content["scenarios"] = []
    content["system_map"] = None
    content["decisions"] = []
    content["limitations"] = []
    content["reviewer_prompts"] = []

    receipt = rb.render(
        _request(tmp_path, _write_content(tmp_path, content)),
        forge=FakeForge(),
        runner=GitRunner(),
    )

    assert receipt.mode == "compact"
    document = Path(receipt.html_path).read_text(encoding="utf-8")
    assert 'id="scenarios"' not in document
    assert 'id="map"' not in document
    assert "Focused code evidence" in document


def test_render_refuses_when_local_head_is_not_the_pr_head(tmp_path):
    request = _request(tmp_path, _write_content(tmp_path, _content()))

    with pytest.raises(rb.SnapshotMismatch, match="does not match PR head"):
        rb.render(request, forge=FakeForge(SHA_A), runner=GitRunner(SHA_B))


def test_render_expands_matching_abbreviated_pr_head_from_remote(tmp_path):
    request = _request(tmp_path, _write_content(tmp_path, _content()))
    runner = GitRunner(SHA_A)

    receipt = rb.render(request, forge=FakeForge(SHA_A[:12]), runner=runner)

    assert receipt.snapshot_sha == SHA_A
    assert [
        "git",
        "ls-remote",
        "--exit-code",
        "origin",
        "refs/heads/feat/review-brief",
    ] in runner.calls


def test_render_refuses_abbreviated_pr_head_that_disagrees_with_remote(tmp_path):
    request = _request(tmp_path, _write_content(tmp_path, _content()))

    with pytest.raises(rb.SnapshotMismatch, match="does not match remote branch head"):
        rb.render(
            request,
            forge=FakeForge(SHA_A[:12]),
            runner=GitRunner(SHA_A, remote_head=SHA_B),
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update({"surprise": True}), "unknown fields"),
        (
            lambda value: value["code_evidence"][0].update({"path": "../secret"}),
            "safe repository-relative",
        ),
        (
            lambda value: value["system_map"]["edges"].append({"from": "reap", "to": "workspace"}),
            "acyclic",
        ),
    ],
)
def test_validation_rejects_ambiguous_or_unsafe_content(mutate, message):
    value = _content()
    mutate(value)

    with pytest.raises(rb.ValidationError, match=message):
        rb.validate_content(value)


def test_freshness_is_current_only_when_receipt_local_and_pr_heads_match(tmp_path):
    request = _request(tmp_path, _write_content(tmp_path, _content()))
    rb.render(request, forge=FakeForge(SHA_A), runner=GitRunner(SHA_A))
    freshness_request = rb.FreshnessRequest(request.workspace_root, request.ticket_dir, "42")

    current = rb.freshness(freshness_request, forge=FakeForge(SHA_A), runner=GitRunner(SHA_A))
    local_mutation = rb.freshness(
        freshness_request, forge=FakeForge(SHA_A), runner=GitRunner(SHA_B)
    )
    pushed_mutation = rb.freshness(
        freshness_request, forge=FakeForge(SHA_B), runner=GitRunner(SHA_B)
    )

    assert current.status == "current"
    assert local_mutation.status == "stale"
    assert "does not match PR head" in local_mutation.reason
    assert pushed_mutation.status == "stale"
    assert pushed_mutation.receipt_sha == SHA_A
    assert SHA_B[:12] in pushed_mutation.reason


def test_freshness_reports_missing_before_first_render(tmp_path):
    result = rb.freshness(
        rb.FreshnessRequest(tmp_path, tmp_path / "run", "42"),
        forge=FakeForge(),
        runner=GitRunner(),
    )

    assert result.status == "missing"
    assert result.receipt_sha is None


def test_full_mode_requires_orientation_beyond_code(tmp_path):
    content = _content(mode="full")
    content["scenarios"] = []
    content["system_map"] = None
    content["decisions"] = []

    with pytest.raises(rb.ValidationError, match="orient a cold reviewer"):
        rb.validate_content(content)


# ─── unattended skip authorization (flow-rptq) ─────────────────────────────────

_KEY = "flow-x"


def _seed_frontmatter(tmp_path: Path, *, unattended) -> None:
    tickets = tmp_path / ".flow" / "tickets"
    tickets.mkdir(parents=True, exist_ok=True)
    if unattended is None:
        body = "+++\n+++\n"
    elif isinstance(unattended, bool):
        body = f"+++\nunattended = {'true' if unattended else 'false'}\n+++\n"
    else:
        body = f'+++\nunattended = "{unattended}"\n+++\n'
    (tickets / f"{_KEY}.md").write_text(body, encoding="utf-8")


def _seed_review_brief_skip(
    tmp_path: Path,
    *,
    reason: str = rb.CANONICAL_UNATTENDED_SKIP_REASON,
) -> Path:
    ticket_dir = tmp_path / ".flow" / "runs" / _KEY
    state.init(ticket_dir, _KEY, "jira", ["review_brief"])
    state.begin_stage(ticket_dir, "review_brief", "a" * 40)
    skill_output = {"review_brief_skip": reason}
    state.finish_stage(ticket_dir, "review_brief", "completed", "a" * 40, skill_output=skill_output)
    return ticket_dir


def _freshness_request(tmp_path: Path, ticket_dir: Path) -> rb.FreshnessRequest:
    return rb.FreshnessRequest(workspace_root=tmp_path, ticket_dir=ticket_dir, pr_id="42")


def test_unattended_canonical_skip_is_authorized_as_disabled(tmp_path: Path):
    ticket_dir = _seed_review_brief_skip(tmp_path)
    _seed_frontmatter(tmp_path, unattended=True)

    result = rb.freshness(_freshness_request(tmp_path, ticket_dir))

    assert result.status == "disabled"


def test_attended_canonical_skip_is_blocking_missing(tmp_path: Path):
    ticket_dir = _seed_review_brief_skip(tmp_path)
    _seed_frontmatter(tmp_path, unattended=False)

    result = rb.freshness(
        _freshness_request(tmp_path, ticket_dir), forge=FakeForge(), runner=GitRunner()
    )

    assert result.status == "missing"
    assert "no review brief exists" in result.reason


@pytest.mark.parametrize("unattended", [None, "yes", 1])
def test_absent_or_non_boolean_unattended_fails_closed(tmp_path: Path, unattended):
    ticket_dir = _seed_review_brief_skip(tmp_path)
    _seed_frontmatter(tmp_path, unattended=unattended)

    result = rb.freshness(
        _freshness_request(tmp_path, ticket_dir), forge=FakeForge(), runner=GitRunner()
    )

    assert result.status == "missing"


def test_noncanonical_skip_reason_fails_closed_even_when_unattended(tmp_path: Path):
    ticket_dir = _seed_review_brief_skip(tmp_path, reason="not the canonical reason")
    _seed_frontmatter(tmp_path, unattended=True)

    result = rb.freshness(
        _freshness_request(tmp_path, ticket_dir), forge=FakeForge(), runner=GitRunner()
    )

    assert result.status == "missing"


def test_attended_normal_render_remains_current(tmp_path: Path):
    """An attended run's authored, rendered brief still reports current."""
    _seed_frontmatter(tmp_path, unattended=False)
    ticket_dir = tmp_path / ".flow" / "runs" / _KEY
    state.init(ticket_dir, _KEY, "jira", ["review_brief"])
    state.begin_stage(ticket_dir, "review_brief", "a" * 40)
    state.finish_stage(
        ticket_dir,
        "review_brief",
        "completed",
        "a" * 40,
        skill_output={},
    )
    request = _request(tmp_path, _write_content(tmp_path, _content()))
    rb.render(request, forge=FakeForge(SHA_A), runner=GitRunner(SHA_A))

    result = rb.freshness(
        rb.FreshnessRequest(request.workspace_root, request.ticket_dir, "42"),
        forge=FakeForge(SHA_A),
        runner=GitRunner(SHA_A),
    )

    assert result.status == "current"


def test_freshness_disabled_flag_unaffected_by_skip_authorization(tmp_path: Path):
    # The `enabled=False` shape (workspace mode=off) short-circuits before the skip
    # cross-check even when a completed review_brief skip receipt exists on disk.
    ticket_dir = _seed_review_brief_skip(tmp_path)
    _seed_frontmatter(tmp_path, unattended=False)

    result = rb.freshness(rb.FreshnessRequest(tmp_path, ticket_dir, "42", enabled=False))

    assert result.status == "disabled"
    assert result.reason == "review brief is disabled"
