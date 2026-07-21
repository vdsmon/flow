"""Render and verify a commit-pinned, self-contained HTML review brief.

The authored JSON describes reviewer-facing claims. This module owns everything
mechanical and security-sensitive: strict validation, PR/local-head binding,
commit-pinned source extraction, Forge links, deterministic layout, HTML escaping,
Content Security Policy, atomic publication, browser opening, and freshness receipts.

CLI:
  review_brief.py render --workspace-root DIR --ticket-dir DIR --pr-id ID \
      --content FILE [--open | --no-open]
  review_brief.py freshness --workspace-root DIR --ticket-dir DIR --pr-id ID

Both commands print one JSON object. Runtime dependencies are Python stdlib only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import re
import subprocess
import sys
import webbrowser
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, cast

import ticket_frontmatter
from _atomicio import atomic_write_text
from _runner import CwdRunner as Runner
from _runner import cwd_default_runner
from forge import Forge, ForgeError, PullRequest, make_forge, read_forge_config

RENDERER_VERSION = 1
SCHEMA_VERSION = 1
CANONICAL_UNATTENDED_SKIP_REASON = "unattended run has no live human reviewer"
_ASSET = Path(__file__).resolve().parent / "assets" / "review_brief.css"
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_FULL_SHA_LENGTHS = frozenset({40, 64})
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_RISK = {"low", "medium", "high"}
_MODE = {"auto", "compact", "full"}
_STATUS = {"passed", "pending", "failed"}
_ROOT_FIELDS = {
    "schema_version",
    "mode",
    "title",
    "outcome",
    "risk",
    "change_shape",
    "motivation",
    "scenarios",
    "system_map",
    "decisions",
    "invariants",
    "code_evidence",
    "verification",
    "limitations",
    "reviewer_prompts",
}


class ReviewBriefError(Exception):
    """Base error for renderer failures safe to show at the stage boundary."""


class ValidationError(ReviewBriefError):
    """The authored brief does not satisfy the review-brief schema."""


class SnapshotMismatch(ReviewBriefError):
    """The PR head and local branch no longer name the same snapshot."""


@dataclass(frozen=True)
class RenderRequest:
    workspace_root: Path
    ticket_dir: Path
    pr_id: str
    content_path: Path
    open_browser: bool = True


@dataclass(frozen=True)
class Receipt:
    status: Literal["current"]
    mode: Literal["compact", "full"]
    snapshot_sha: str
    pr_id: str
    pr_url: str
    html_path: str
    content_path: str
    opened: bool
    warnings: list[str]
    renderer_version: int = RENDERER_VERSION


@dataclass(frozen=True)
class FreshnessRequest:
    workspace_root: Path
    ticket_dir: Path
    pr_id: str
    enabled: bool = True


@dataclass(frozen=True)
class Freshness:
    status: Literal["current", "stale", "missing", "disabled"]
    current_sha: str | None
    pr_head_sha: str | None
    receipt_sha: str | None
    html_path: str | None
    reason: str


@dataclass(frozen=True)
class _Snapshot:
    sha: str
    pr_url: str
    pr_head_sha: str


@dataclass(frozen=True)
class _Excerpt:
    claim: str
    explanation: str
    path: str
    start_line: int
    end_line: int
    highlight_lines: frozenset[int]
    source_url: str
    lines: tuple[str, ...]


BrowserOpener = Callable[[str], bool]


class ReviewBriefForge(Protocol):
    """The narrow Forge surface the renderer actually consumes."""

    def pr_info(self, pr_id: str) -> PullRequest | None: ...
    def source_url(
        self, pr_id: str, sha: str, path: str, start_line: int, end_line: int
    ) -> str: ...


def _fail(message: str) -> None:
    raise ValidationError(message)


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{where} must be an object")
    return cast(dict[str, Any], value)


def _list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(f"{where} must be an array")
    return value


def _keys(value: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        _fail(f"{where} has unknown fields: {', '.join(unknown)}")


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{where} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, where: str) -> str | None:
    if value is None:
        return None
    return _text(value, where)


def _integer(value: Any, where: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail(f"{where} must be an integer >= {minimum}")
    return value


def _text_list(value: Any, where: str, *, allow_empty: bool = True) -> list[str]:
    items = _list(value, where)
    if not allow_empty and not items:
        _fail(f"{where} must not be empty")
    return [_text(item, f"{where}[{index}]") for index, item in enumerate(items)]


def _safe_path(value: Any, where: str) -> str:
    raw = _text(value, where).replace("\\", "/")
    path = PurePosixPath(raw)
    if path.is_absolute() or raw.startswith("~") or ".." in path.parts or "." in path.parts:
        _fail(f"{where} must be a safe repository-relative path")
    return str(path)


def _normalize_motivation(value: Any) -> dict[str, str]:
    item = _object(value, "motivation")
    _keys(item, {"observed_problem", "why_it_matters"}, "motivation")
    return {
        "observed_problem": _text(item.get("observed_problem"), "motivation.observed_problem"),
        "why_it_matters": _text(item.get("why_it_matters"), "motivation.why_it_matters"),
    }


def _normalize_scenarios(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(_list(value, "scenarios")):
        where = f"scenarios[{index}]"
        item = _object(raw, where)
        _keys(
            item,
            {"name", "before_label", "after_label", "before_steps", "after_steps"},
            where,
        )
        result.append(
            {
                "name": _text(item.get("name"), f"{where}.name"),
                "before_label": _text(item.get("before_label"), f"{where}.before_label"),
                "after_label": _text(item.get("after_label"), f"{where}.after_label"),
                "before_steps": _text_list(
                    item.get("before_steps"), f"{where}.before_steps", allow_empty=False
                ),
                "after_steps": _text_list(
                    item.get("after_steps"), f"{where}.after_steps", allow_empty=False
                ),
            }
        )
    return result


def _topological_order(node_ids: list[str], edges: list[dict[str, str]]) -> list[str]:
    following: dict[str, list[str]] = defaultdict(list)
    incoming = dict.fromkeys(node_ids, 0)
    for edge in edges:
        following[edge["from"]].append(edge["to"])
        incoming[edge["to"]] += 1
    ready = deque(node_id for node_id in node_ids if incoming[node_id] == 0)
    ordered: list[str] = []
    while ready:
        current = ready.popleft()
        ordered.append(current)
        for next_id in following[current]:
            incoming[next_id] -= 1
            if incoming[next_id] == 0:
                ready.append(next_id)
    if len(ordered) != len(node_ids):
        _fail("system_map.edges must form an acyclic graph")
    return ordered


def _normalize_map(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    item = _object(value, "system_map")
    _keys(item, {"caption", "nodes", "edges"}, "system_map")
    nodes: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, raw in enumerate(_list(item.get("nodes"), "system_map.nodes")):
        where = f"system_map.nodes[{index}]"
        node = _object(raw, where)
        _keys(node, {"id", "label", "kind", "changed"}, where)
        node_id = _text(node.get("id"), f"{where}.id")
        if not _ID_RE.fullmatch(node_id):
            _fail(f"{where}.id must match {_ID_RE.pattern}")
        if node_id in ids:
            _fail(f"{where}.id duplicates {node_id!r}")
        ids.add(node_id)
        changed = node.get("changed", False)
        if not isinstance(changed, bool):
            _fail(f"{where}.changed must be a boolean")
        nodes.append(
            {
                "id": node_id,
                "label": _text(node.get("label"), f"{where}.label"),
                "kind": _text(node.get("kind"), f"{where}.kind"),
                "changed": changed,
            }
        )
    if not nodes:
        _fail("system_map.nodes must not be empty")
    edges: list[dict[str, str]] = []
    seen_edges: set[tuple[str, str]] = set()
    for index, raw in enumerate(_list(item.get("edges"), "system_map.edges")):
        where = f"system_map.edges[{index}]"
        edge = _object(raw, where)
        _keys(edge, {"from", "to"}, where)
        start = _text(edge.get("from"), f"{where}.from")
        end = _text(edge.get("to"), f"{where}.to")
        if start not in ids or end not in ids:
            _fail(f"{where} references an unknown node")
        if start == end:
            _fail(f"{where} must not be a self-loop")
        pair = (start, end)
        if pair in seen_edges:
            _fail(f"{where} duplicates edge {start!r} -> {end!r}")
        seen_edges.add(pair)
        edges.append({"from": start, "to": end})
    _topological_order([node["id"] for node in nodes], edges)
    return {
        "caption": _text(item.get("caption"), "system_map.caption"),
        "nodes": nodes,
        "edges": edges,
    }


def _normalize_claims(value: Any, where: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for index, raw in enumerate(_list(value, where)):
        location = f"{where}[{index}]"
        item = _object(raw, location)
        _keys(item, {"title", "body"}, location)
        result.append(
            {
                "title": _text(item.get("title"), f"{location}.title"),
                "body": _text(item.get("body"), f"{location}.body"),
            }
        )
    return result


def _normalize_evidence(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    allowed = {
        "claim",
        "explanation",
        "path",
        "start_line",
        "end_line",
        "highlight_lines",
    }
    for index, raw in enumerate(_list(value, "code_evidence")):
        where = f"code_evidence[{index}]"
        item = _object(raw, where)
        _keys(item, allowed, where)
        start = _integer(item.get("start_line"), f"{where}.start_line")
        end = _integer(item.get("end_line"), f"{where}.end_line")
        if end < start:
            _fail(f"{where}.end_line must be >= start_line")
        if end - start > 119:
            _fail(f"{where} may include at most 120 lines")
        highlights = [
            _integer(line, f"{where}.highlight_lines[{line_index}]")
            for line_index, line in enumerate(_list(item.get("highlight_lines", []), where))
        ]
        if any(line < start or line > end for line in highlights):
            _fail(f"{where}.highlight_lines must fall inside the excerpt range")
        result.append(
            {
                "claim": _text(item.get("claim"), f"{where}.claim"),
                "explanation": _text(item.get("explanation"), f"{where}.explanation"),
                "path": _safe_path(item.get("path"), f"{where}.path"),
                "start_line": start,
                "end_line": end,
                "highlight_lines": sorted(set(highlights)),
            }
        )
    if not result:
        _fail("code_evidence must contain at least one focused excerpt")
    return result


def _normalize_verification(value: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for index, raw in enumerate(_list(value, "verification")):
        where = f"verification[{index}]"
        item = _object(raw, where)
        _keys(item, {"claim", "evidence", "status"}, where)
        status = _text(item.get("status"), f"{where}.status").lower()
        if status not in _STATUS:
            _fail(f"{where}.status must be one of {sorted(_STATUS)}")
        result.append(
            {
                "claim": _text(item.get("claim"), f"{where}.claim"),
                "evidence": _text(item.get("evidence"), f"{where}.evidence"),
                "status": status,
            }
        )
    if not result:
        _fail("verification must contain at least one evidence-backed claim")
    return result


def validate_content(value: Any) -> dict[str, Any]:
    """Validate authored JSON and return a deterministic normalized shape."""
    root = _object(value, "review brief")
    _keys(root, _ROOT_FIELDS, "review brief")
    version = _integer(root.get("schema_version"), "schema_version")
    if version != SCHEMA_VERSION:
        _fail(f"schema_version must be {SCHEMA_VERSION}")
    mode = _text(root.get("mode", "auto"), "mode").lower()
    if mode not in _MODE:
        _fail(f"mode must be one of {sorted(_MODE)}")
    risk = _text(root.get("risk"), "risk").lower()
    if risk not in _RISK:
        _fail(f"risk must be one of {sorted(_RISK)}")
    normalized: dict[str, Any] = {
        "schema_version": version,
        "mode": mode,
        "title": _text(root.get("title"), "title"),
        "outcome": _text(root.get("outcome"), "outcome"),
        "risk": risk,
        "change_shape": _text(root.get("change_shape"), "change_shape"),
        "motivation": _normalize_motivation(root.get("motivation")),
        "scenarios": _normalize_scenarios(root.get("scenarios", [])),
        "system_map": _normalize_map(root.get("system_map")),
        "decisions": _normalize_claims(root.get("decisions", []), "decisions"),
        "invariants": _normalize_claims(root.get("invariants", []), "invariants"),
        "code_evidence": _normalize_evidence(root.get("code_evidence")),
        "verification": _normalize_verification(root.get("verification")),
        "limitations": _text_list(root.get("limitations", []), "limitations"),
        "reviewer_prompts": _text_list(root.get("reviewer_prompts", []), "reviewer_prompts"),
    }
    if mode == "full" and not (
        normalized["scenarios"] or normalized["system_map"] or normalized["decisions"]
    ):
        _fail("full mode needs a scenario, system map, or decision to orient a cold reviewer")
    return normalized


def provider_schema() -> dict[str, Any]:
    """Return the closed JSON schema used by the routed content author."""

    def obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    text = {"type": "string", "minLength": 1}
    text_array = {"type": "array", "items": text}
    claim = obj({"title": text, "body": text}, ["title", "body"])
    scenario = obj(
        {
            "name": text,
            "before_label": text,
            "after_label": text,
            "before_steps": text_array,
            "after_steps": text_array,
        },
        ["name", "before_label", "after_label", "before_steps", "after_steps"],
    )
    node = obj(
        {"id": text, "label": text, "kind": text, "changed": {"type": "boolean"}},
        ["id", "label", "kind", "changed"],
    )
    edge = obj({"from": text, "to": text}, ["from", "to"])
    system_map = obj(
        {
            "caption": text,
            "nodes": {"type": "array", "items": node},
            "edges": {"type": "array", "items": edge},
        },
        ["caption", "nodes", "edges"],
    )
    evidence = obj(
        {
            "claim": text,
            "explanation": text,
            "path": text,
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
            "highlight_lines": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1},
            },
        },
        ["claim", "explanation", "path", "start_line", "end_line", "highlight_lines"],
    )
    verification = obj(
        {
            "claim": text,
            "evidence": text,
            "status": {"type": "string", "enum": sorted(_STATUS)},
        },
        ["claim", "evidence", "status"],
    )
    return obj(
        {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "mode": {"type": "string", "enum": sorted(_MODE)},
            "title": text,
            "outcome": text,
            "risk": {"type": "string", "enum": sorted(_RISK)},
            "change_shape": text,
            "motivation": obj(
                {"observed_problem": text, "why_it_matters": text},
                ["observed_problem", "why_it_matters"],
            ),
            "scenarios": {"type": "array", "items": scenario},
            "system_map": {"anyOf": [system_map, {"type": "null"}]},
            "decisions": {"type": "array", "items": claim},
            "invariants": {"type": "array", "items": claim},
            "code_evidence": {"type": "array", "items": evidence},
            "verification": {"type": "array", "items": verification},
            "limitations": text_array,
            "reviewer_prompts": text_array,
        },
        sorted(_ROOT_FIELDS),
    )


def _resolved_mode(content: Mapping[str, Any]) -> Literal["compact", "full"]:
    selected = content["mode"]
    if selected == "compact":
        return "compact"
    if selected == "full":
        return "full"
    structural = bool(content["scenarios"] or content["system_map"] or content["decisions"])
    return "full" if structural or len(content["code_evidence"]) > 2 else "compact"


def _ok(result: subprocess.CompletedProcess[str], what: str) -> str:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise ReviewBriefError(f"{what} failed: {detail}")
    return result.stdout or ""


def _resolve_forge(workspace_root: Path) -> Forge:
    config = read_forge_config(workspace_root)
    if config is None:
        raise ReviewBriefError("review brief requires a [forge] block in .flow/workspace.toml")
    return make_forge(config)


def _remote_branch_sha(branch: str, run: Runner) -> str:
    ref = f"refs/heads/{branch}"
    raw = _ok(
        run(["git", "ls-remote", "--exit-code", "origin", ref]),
        "git ls-remote PR head",
    )
    matches: list[str] = []
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == ref:
            matches.append(fields[0])
    if len(matches) != 1:
        raise ReviewBriefError(f"remote PR branch {branch!r} did not resolve to one commit")
    remote = matches[0].lower()
    if not _SHA_RE.fullmatch(remote) or len(remote) not in _FULL_SHA_LENGTHS:
        raise ReviewBriefError(f"remote PR branch resolved to invalid full SHA {remote!r}")
    return remote


def _head_sha(pr: Mapping[str, Any], run: Runner) -> str:
    raw_value = pr.get("head_sha")
    value = (
        raw_value.lower() if isinstance(raw_value, str) and _SHA_RE.fullmatch(raw_value) else None
    )
    if value is not None and len(value) in _FULL_SHA_LENGTHS:
        return value
    branch = pr.get("head")
    if not isinstance(branch, str) or not branch:
        detail = "an abbreviated head_sha" if value is not None else "no usable head_sha"
        raise ReviewBriefError(f"forge PR response has {detail} and no head branch")
    remote = _remote_branch_sha(branch, run)
    if value is not None and not remote.startswith(value):
        raise SnapshotMismatch(
            f"forge-reported PR head {value} does not match remote branch head {remote[:12]}"
        )
    return remote


def _snapshot(pr_id: str, forge: ReviewBriefForge, run: Runner) -> _Snapshot:
    try:
        pr = forge.pr_info(pr_id)
    except ForgeError as exc:
        raise ReviewBriefError(str(exc)) from exc
    if pr is None:
        raise ReviewBriefError(f"PR {pr_id!r} was not found")
    pr_head = _head_sha(pr, run)
    local = _ok(run(["git", "rev-parse", "HEAD"]), "git rev-parse HEAD").strip().lower()
    if not _SHA_RE.fullmatch(local):
        raise ReviewBriefError(f"local HEAD resolved to invalid SHA {local!r}")
    if local != pr_head:
        raise SnapshotMismatch(
            f"local HEAD {local[:12]} does not match PR head {pr_head[:12]}; "
            "push or update the branch before rendering"
        )
    url = pr.get("url")
    if not isinstance(url, str) or not url:
        raise ReviewBriefError("forge PR response is missing its URL")
    return _Snapshot(sha=local, pr_url=url, pr_head_sha=pr_head)


def _read_content(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReviewBriefError(f"cannot read review-brief content {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"review-brief content is not valid JSON: {exc}") from exc
    return validate_content(raw)


def _extract_evidence(
    content: Mapping[str, Any],
    snapshot: _Snapshot,
    pr_id: str,
    forge: ReviewBriefForge,
    run: Runner,
) -> list[_Excerpt]:
    excerpts: list[_Excerpt] = []
    for index, item in enumerate(content["code_evidence"]):
        path = item["path"]
        raw = _ok(run(["git", "show", f"{snapshot.sha}:{path}"]), f"read {path} at snapshot")
        lines = raw.splitlines()
        start = item["start_line"]
        end = item["end_line"]
        if end > len(lines):
            raise ValidationError(
                f"code_evidence[{index}] ends at line {end}, but {path} has {len(lines)} lines "
                f"at {snapshot.sha[:12]}"
            )
        try:
            source_url = forge.source_url(pr_id, snapshot.sha, path, start, end)
        except ForgeError as exc:
            raise ReviewBriefError(str(exc)) from exc
        excerpts.append(
            _Excerpt(
                claim=item["claim"],
                explanation=item["explanation"],
                path=path,
                start_line=start,
                end_line=end,
                highlight_lines=frozenset(item["highlight_lines"]),
                source_url=source_url,
                lines=tuple(lines[start - 1 : end]),
            )
        )
    return excerpts


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _anchor(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "section"


def _render_steps(items: Sequence[str], *, final_mark: str) -> str:
    rows = []
    for index, step in enumerate(items):
        marker = final_mark if index == len(items) - 1 else str(index + 1)
        rows.append(
            f'<div class="step"><span class="num">{_e(marker)}</span><span>{_e(step)}</span></div>'
        )
    return "".join(rows)


def _render_scenarios(items: Sequence[Mapping[str, Any]]) -> str:
    if not items:
        return ""
    blocks = [
        (
            '<div class="scenario-set">'
            f'<p class="scenario-name">{_e(scenario["name"])}</p>'
            '<div class="scenarios">'
            '<article class="scenario before"><div class="scenario-title">'
            f"<strong>Before</strong><span>{_e(scenario['before_label'])}</span></div>"
            f'<div class="steps">{_render_steps(scenario["before_steps"], final_mark="!")}</div>'
            '</article><div class="arrow" aria-hidden="true">→</div>'
            '<article class="scenario after"><div class="scenario-title">'
            f"<strong>After</strong><span>{_e(scenario['after_label'])}</span></div>"
            f'<div class="steps">{_render_steps(scenario["after_steps"], final_mark="✓")}</div>'
            "</article></div></div>"
        )
        for scenario in items
    ]
    return (
        '<section id="scenarios"><div class="section-head"><h2>Before and after</h2>'
        "<span>Follow the behavior, not the file list</span></div>"
        f"{''.join(blocks)}</section>"
    )


def _map_layout(system_map: Mapping[str, Any]) -> tuple[dict[str, tuple[int, int]], int, int]:
    nodes = system_map["nodes"]
    edges = system_map["edges"]
    node_ids = [node["id"] for node in nodes]
    ordered = _topological_order(node_ids, edges)
    predecessors: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        predecessors[edge["to"]].append(edge["from"])
    rank: dict[str, int] = {}
    for node_id in ordered:
        rank[node_id] = max((rank[parent] + 1 for parent in predecessors[node_id]), default=0)
    columns: dict[int, list[str]] = defaultdict(list)
    for node_id in ordered:
        columns[rank[node_id]].append(node_id)
    positions: dict[str, tuple[int, int]] = {}
    max_rows = max(len(column) for column in columns.values())
    for column, ids in columns.items():
        for row, node_id in enumerate(ids):
            positions[node_id] = (22 + column * 174, 18 + row * 92)
    width = max(620, 44 + (max(rank.values()) + 1) * 174)
    height = max(98, 36 + max_rows * 92)
    return positions, width, height


def _render_map(system_map: Mapping[str, Any] | None) -> str:
    if system_map is None:
        return ""
    positions, width, height = _map_layout(system_map)
    arrows: list[str] = []
    for edge in system_map["edges"]:
        start_x, start_y = positions[edge["from"]]
        end_x, end_y = positions[edge["to"]]
        x1, y1 = start_x + 136, start_y + 31
        x2, y2 = end_x, end_y + 31
        mid = (x1 + x2) / 2
        path = f"M{x1},{y1} C{mid},{y1} {mid},{y2} {x2 - 7},{y2}"
        arrows.append(
            f'<path class="map-edge" d="{path}"/><path class="map-arrow" '
            f'd="M{x2 - 7},{y2 - 4} L{x2},{y2} L{x2 - 7},{y2 + 4} Z"/>'
        )
    nodes: list[str] = []
    for node in system_map["nodes"]:
        x, y = positions[node["id"]]
        changed = " changed" if node["changed"] else ""
        nodes.append(
            f'<g class="map-node{changed}" transform="translate({x} {y})">'
            '<rect width="136" height="62" rx="11"/>'
            f'<text class="kind" x="13" y="20">{_e(node["kind"].upper())}</text>'
            f'<text x="13" y="43">{_e(node["label"])}</text></g>'
        )
    return (
        '<section id="map"><div class="section-head"><h2>The relevant system slice</h2>'
        '<span>Unrelated architecture omitted</span></div><div class="system-map" '
        'tabindex="0" aria-label="Scrollable relevant system map">'
        f'<div class="map-note">{_e(system_map["caption"])}</div>'
        f'<div class="map-canvas"><svg viewBox="0 0 {width} {height}" role="img" '
        'aria-label="Relevant components and the direction of their relationships">'
        f"{''.join(arrows)}{''.join(nodes)}</svg></div></div></section>"
    )


def _render_claims(items: Sequence[Mapping[str, str]]) -> str:
    return "".join(
        '<article class="claim">'
        f'<span class="claim-index">{index}</span><b>{_e(item["title"])}</b>'
        f"<p>{_e(item['body'])}</p></article>"
        for index, item in enumerate(items, 1)
    )


def _render_guarantees(items: Sequence[Mapping[str, str]]) -> str:
    if not items:
        return ""
    return (
        '<section id="invariants"><div class="section-head"><h2>What must remain true</h2>'
        "<span>Review these as invariants</span></div>"
        f'<div class="claims">{_render_claims(items)}</div></section>'
    )


def _render_decisions(items: Sequence[Mapping[str, str]]) -> str:
    if not items:
        return ""
    cards = "".join(
        f'<article class="decision panel"><h3>{_e(item["title"])}</h3>'
        f"<p>{_e(item['body'])}</p></article>"
        for item in items
    )
    return (
        '<section id="decisions"><div class="section-head"><h2>Decisions that shape the change</h2>'
        "<span>Intentional constraints and tradeoffs</span></div>"
        f'<div class="decisions">{cards}</div></section>'
    )


_TOKEN_RE = re.compile(
    r'(?P<string>"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')'
    r"|(?P<comment>\#.*$|//.*$)"
    r"|(?P<number>\b\d+(?:\.\d+)?\b)"
    r"|(?P<keyword>\b(?:and|as|assert|async|await|break|case|class|const|continue|def|"
    r"del|do|elif|else|except|export|extends|false|finally|for|from|function|if|import|in|"
    r"interface|is|lambda|let|match|new|none|not|null|or|pass|raise|return|switch|true|try|"
    r"type|var|while|with|yield)\b)",
    re.IGNORECASE,
)


def _highlight_line(line: str) -> str:
    parts: list[str] = []
    cursor = 0
    for match in _TOKEN_RE.finditer(line):
        parts.append(_e(line[cursor : match.start()]))
        kind = match.lastgroup or ""
        parts.append(f'<span class="tok-{kind}">{_e(match.group())}</span>')
        cursor = match.end()
    parts.append(_e(line[cursor:]))
    return "".join(parts)


def _render_evidence(items: Sequence[_Excerpt], pr_url: str) -> str:
    blocks: list[str] = []
    for excerpt in items:
        code_lines: list[str] = []
        for offset, line in enumerate(excerpt.lines):
            number = excerpt.start_line + offset
            decisive = " decisive" if number in excerpt.highlight_lines else ""
            code_lines.append(
                f'<span class="code-line{decisive}"><span class="line-number">{number}</span>'
                f'<span class="code-text">{_highlight_line(line)}</span></span>'
            )
        blocks.append(
            '<article class="code"><div class="code-copy">'
            f'<span class="code-file">{_e(excerpt.path)}:{excerpt.start_line}</span>'
            f"<strong>{_e(excerpt.claim)}</strong><p>{_e(excerpt.explanation)}</p>"
            f'<a href="{_e(excerpt.source_url)}">Open exact lines in Forge ↗</a>'
            f'</div><div class="code-scroll">{"".join(code_lines)}</div></article>'
        )
    return (
        '<section id="evidence"><div class="section-head"><h2>Focused code evidence</h2>'
        f"<span>{len(items)} excerpt{'s' if len(items) != 1 else ''} · "
        f'<a href="{_e(pr_url)}">full diff in Forge</a></span></div>'
        f'<div class="evidence-list">{"".join(blocks)}</div></section>'
    )


def _render_verification(items: Sequence[Mapping[str, str]]) -> str:
    symbols = {"passed": "✓", "pending": "…", "failed": "!"}
    checks = "".join(
        f'<article class="check panel {_e(item["status"])}">'
        f'<span class="check-mark">{symbols[item["status"]]}</span><div>'
        f"<b>{_e(item['claim'])}</b><p>{_e(item['evidence'])}</p></div></article>"
        for item in items
    )
    return (
        '<section id="verification"><div class="section-head"><h2>Verification &amp; risk</h2>'
        "<span>Evidence over assertion</span></div>"
        f'<div class="verification-grid">{checks}</div></section>'
    )


def _render_list_section(section_id: str, heading: str, items: Sequence[str]) -> str:
    if not items:
        return ""
    rows = "".join(f"<li>{_e(item)}</li>" for item in items)
    return (
        f'<section id="{_e(section_id)}"><div class="section-head"><h2>{_e(heading)}</h2>'
        f'</div><ul class="plain-list panel">{rows}</ul></section>'
    )


def _render_prompts(items: Sequence[str]) -> str:
    if not items:
        return ""
    rows = "".join(f'<li class="panel"><span>{_e(item)}</span></li>' for item in items)
    return (
        '<section id="prompts"><div class="section-head"><h2>Questions worth pressure-testing</h2>'
        "<span>A starting point, not a checklist</span></div>"
        f'<ol class="prompt-list">{rows}</ol></section>'
    )


def _navigation(content: Mapping[str, Any]) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = [("why", "Why this changed")]
    optional = [
        ("scenarios", "Before and after", content["scenarios"]),
        ("map", "System map", content["system_map"]),
        ("decisions", "Decisions", content["decisions"]),
        ("invariants", "Invariants", content["invariants"]),
        ("evidence", "Code evidence", content["code_evidence"]),
        ("verification", "Verification & risk", content["verification"]),
        ("limitations", "Limits & unknowns", content["limitations"]),
        ("prompts", "Review prompts", content["reviewer_prompts"]),
    ]
    links.extend((section_id, label) for section_id, label, value in optional if value)
    return links


def _style() -> str:
    try:
        return _ASSET.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReviewBriefError(f"review brief stylesheet is unavailable: {exc}") from exc


def _document(
    content: Mapping[str, Any], snapshot: _Snapshot, pr_id: str, mode: str, excerpts: list[_Excerpt]
) -> str:
    style = _style()
    digest = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
    csp = (
        "default-src 'none'; "
        f"style-src 'sha256-{digest}'; "
        "img-src data:; base-uri 'none'; form-action 'none'"
    )
    rail = "".join(
        f'<a href="#{_e(section_id)}">{_e(label)}</a>' for section_id, label in _navigation(content)
    )
    motivation = content["motivation"]
    risk_class = " risk-high" if content["risk"] == "high" else ""
    sections = [
        _render_scenarios(content["scenarios"]),
        _render_map(content["system_map"]),
        _render_decisions(content["decisions"]),
        _render_guarantees(content["invariants"]),
        _render_evidence(excerpts, snapshot.pr_url),
        _render_verification(content["verification"]),
        _render_list_section("limitations", "Limits & unknowns", content["limitations"]),
        _render_prompts(content["reviewer_prompts"]),
    ]
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="{_e(csp)}">
  <meta name="color-scheme" content="light dark">
  <title>{_e(content["title"])} · Flow review brief</title>
  <style>{style}</style>
</head>
<body>
  <main class="shell" aria-labelledby="brief-title">
    <header class="topbar">
      <div class="brand"><span class="mark" aria-hidden="true">F</span>Flow review brief</div>
      <div class="meta">
        <a class="pill" href="{_e(snapshot.pr_url)}">PR #{_e(pr_id)}</a>
        <span class="pill" title="{_e(snapshot.sha)}">{_e(snapshot.sha[:12])}</span>
        <span class="pill{risk_class}">{_e(content["risk"])} risk</span>
        <span class="pill snapshot">Snapshot</span>
      </div>
    </header>
    <div class="layout">
      <nav class="rail" aria-label="Review brief sections"><div class="rail-inner">
        <div class="rail-label">On this page</div>{rail}
        <a class="forge-link" href="{_e(snapshot.pr_url)}">Open full diff ↗</a>
      </div></nav>
      <div class="content">
        <section id="why">
          <div class="kicker">{_e(content["change_shape"])} · {_e(mode)} brief</div>
          <h1 id="brief-title">{_e(content["title"])}</h1>
          <p class="deck">{_e(content["outcome"])}</p>
        </section>
        <div class="context">
          <div class="observation">
            <p><strong>What was happening.</strong> {_e(motivation["observed_problem"])}</p>
            <p><strong>Why it matters.</strong> {_e(motivation["why_it_matters"])}</p>
          </div>
          <div class="facts">
            <div class="fact"><span>Change shape</span>
              <strong>{_e(content["change_shape"])}</strong>
            </div>
            <div class="fact"><span>Risk</span><strong>{_e(content["risk"])}</strong></div>
            <div class="fact"><span>Snapshot</span><strong>{_e(snapshot.sha[:12])}</strong></div>
          </div>
        </div>
        {"".join(sections)}
        <footer class="footer">
          This brief is a read-only companion to the Forge review, frozen at
          <code>{_e(snapshot.sha)}</code>. If the branch changes, regenerate it before merge.
        </footer>
      </div>
    </div>
  </main>
</body>
</html>
'''


def _receipt_path(ticket_dir: Path, sha: str) -> Path:
    return ticket_dir / "stages" / "review_brief" / sha / "receipt.json"


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def render(
    request: RenderRequest,
    *,
    forge: ReviewBriefForge | None = None,
    runner: Runner | None = None,
    opener: BrowserOpener | None = None,
) -> Receipt:
    """Publish one snapshot-bound brief and its freshness receipt."""
    workspace_root = request.workspace_root.resolve()
    ticket_dir = request.ticket_dir.resolve()
    run = runner or cwd_default_runner(workspace_root)
    fg = forge or _resolve_forge(workspace_root)
    content = _read_content(request.content_path)
    mode = _resolved_mode(content)
    snapshot = _snapshot(request.pr_id, fg, run)
    excerpts = _extract_evidence(content, snapshot, request.pr_id, fg, run)
    artifact_dir = _receipt_path(ticket_dir, snapshot.sha).parent
    html_path = artifact_dir / f"review-brief-{snapshot.sha[:12]}.html"
    content_path = artifact_dir / "brief.json"
    document = _document(content, snapshot, request.pr_id, mode, excerpts)
    atomic_write_text(content_path, _json(content))
    atomic_write_text(html_path, document)
    warnings: list[str] = []
    opened = False
    if request.open_browser:
        open_fn = opener or webbrowser.open
        try:
            opened = bool(open_fn(html_path.as_uri()))
            if not opened:
                warnings.append("browser did not confirm that it opened the review brief")
        except Exception as exc:  # browser integration is convenience, never publication truth
            warnings.append(f"browser open failed: {exc}")
    receipt = Receipt(
        status="current",
        mode=mode,
        snapshot_sha=snapshot.sha,
        pr_id=request.pr_id,
        pr_url=snapshot.pr_url,
        html_path=str(html_path),
        content_path=str(content_path),
        opened=opened,
        warnings=warnings,
    )
    atomic_write_text(_receipt_path(ticket_dir, snapshot.sha), _json(asdict(receipt)))
    return receipt


def _read_receipt(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _latest_receipt(ticket_dir: Path) -> dict[str, Any] | None:
    root = ticket_dir / "stages" / "review_brief"
    candidates = sorted(
        root.glob("*/receipt.json"), key=lambda path: path.stat().st_mtime, reverse=True
    )
    for path in candidates:
        receipt = _read_receipt(path)
        if receipt is not None:
            return receipt
    return None


def _completed_review_brief_record(ticket_dir: Path) -> tuple[str | None, dict[str, Any] | None]:
    """Read the run's ticket key and its completed review_brief stage record, if any."""
    try:
        raw = json.loads((ticket_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(raw, dict):
        return None, None
    ticket = raw.get("ticket")
    stages = raw.get("stages")
    record = stages.get("review_brief") if isinstance(stages, dict) else None
    if not isinstance(ticket, str) or not isinstance(record, dict):
        return None, None
    if record.get("status") != "completed":
        return None, None
    return ticket, record


def _skip_authorization(workspace_root: Path, ticket_dir: Path) -> Freshness | None:
    """Accept the documented unattended skip recorded by the completed stage."""
    ticket, record = _completed_review_brief_record(ticket_dir)
    if ticket is None or record is None:
        return None
    skill_output = record.get("skill_output")
    skill_output = skill_output if isinstance(skill_output, dict) else {}
    canonical = skill_output.get("review_brief_skip") == CANONICAL_UNATTENDED_SKIP_REASON
    fm_path = workspace_root / ".flow" / "tickets" / f"{ticket}.md"
    unattended = ticket_frontmatter.read(fm_path).get("unattended")
    if canonical and unattended is True:
        return Freshness(
            "disabled",
            None,
            None,
            None,
            None,
            "unattended run authorized the canonical review-brief skip",
        )
    return None


def freshness(
    request: FreshnessRequest,
    *,
    forge: ReviewBriefForge | None = None,
    runner: Runner | None = None,
) -> Freshness:
    """Return whether a current receipt exists for both local and PR heads."""
    if not request.enabled:
        return Freshness("disabled", None, None, None, None, "review brief is disabled")
    workspace_root = request.workspace_root.resolve()
    ticket_dir = request.ticket_dir.resolve()
    authorization = _skip_authorization(workspace_root, ticket_dir)
    if authorization is not None:
        return authorization
    run = runner or cwd_default_runner(workspace_root)
    fg = forge or _resolve_forge(workspace_root)
    try:
        pr = fg.pr_info(request.pr_id)
    except ForgeError as exc:
        raise ReviewBriefError(str(exc)) from exc
    if pr is None:
        raise ReviewBriefError(f"PR {request.pr_id!r} was not found")
    pr_head = _head_sha(pr, run)
    local = _ok(run(["git", "rev-parse", "HEAD"]), "git rev-parse HEAD").strip().lower()
    latest = _latest_receipt(ticket_dir)
    receipt_sha = str(latest.get("snapshot_sha")) if latest else None
    html_path = str(latest.get("html_path")) if latest and latest.get("html_path") else None
    if local != pr_head:
        return Freshness(
            "stale",
            local,
            pr_head,
            receipt_sha,
            html_path,
            f"local HEAD {local[:12]} does not match PR head {pr_head[:12]}",
        )
    expected = _read_receipt(_receipt_path(ticket_dir, pr_head))
    if expected is None:
        status: Literal["stale", "missing"] = "stale" if latest else "missing"
        reason = (
            f"latest brief targets {receipt_sha[:12]}, not {pr_head[:12]}"
            if receipt_sha
            else f"no review brief exists for {pr_head[:12]}"
        )
        return Freshness(status, local, pr_head, receipt_sha, html_path, reason)
    expected_sha = expected.get("snapshot_sha")
    expected_pr = str(expected.get("pr_id", ""))
    expected_html = expected.get("html_path")
    if expected_sha != pr_head or expected_pr != request.pr_id:
        return Freshness(
            "stale",
            local,
            pr_head,
            str(expected_sha) if expected_sha else None,
            str(expected_html) if expected_html else None,
            "receipt identity does not match the current PR snapshot",
        )
    if not isinstance(expected_html, str) or not Path(expected_html).is_file():
        return Freshness(
            "missing",
            local,
            pr_head,
            pr_head,
            str(expected_html) if expected_html else None,
            "receipt exists but its HTML artifact is missing",
        )
    return Freshness(
        "current", local, pr_head, pr_head, expected_html, "brief matches local and PR heads"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    render_parser = sub.add_parser("render")
    render_parser.add_argument("--workspace-root", type=Path, required=True)
    render_parser.add_argument("--ticket-dir", type=Path, required=True)
    render_parser.add_argument("--pr-id", required=True)
    render_parser.add_argument("--content", type=Path, required=True)
    render_parser.add_argument(
        "--open", dest="open_browser", action=argparse.BooleanOptionalAction, default=True
    )
    fresh_parser = sub.add_parser("freshness")
    fresh_parser.add_argument("--workspace-root", type=Path, required=True)
    fresh_parser.add_argument("--ticket-dir", type=Path, required=True)
    fresh_parser.add_argument("--pr-id", required=True)
    fresh_parser.add_argument("--disabled", action="store_true")
    return parser


def cli_main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "render":
            result: Receipt | Freshness = render(
                RenderRequest(
                    workspace_root=args.workspace_root,
                    ticket_dir=args.ticket_dir,
                    pr_id=args.pr_id,
                    content_path=args.content,
                    open_browser=args.open_browser,
                )
            )
        else:
            result = freshness(
                FreshnessRequest(
                    workspace_root=args.workspace_root,
                    ticket_dir=args.ticket_dir,
                    pr_id=args.pr_id,
                    enabled=not args.disabled,
                )
            )
    except ReviewBriefError as exc:
        print(f"review-brief: {exc}", file=sys.stderr)
        return 2
    print(_json(asdict(result)), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))
