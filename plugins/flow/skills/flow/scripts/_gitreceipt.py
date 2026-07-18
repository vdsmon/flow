"""Capture and validate the canonical read-only Git receipt."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

SCHEMA = "flow.git-receipt/v1"
_RUNTIME_POINTERS = frozenset({"skill-root", "memory-root", "layout-version"})
_HARNESS_CONFIG = (".claude/settings.json", ".claude/settings.local.json")
_UNTRACKED_DIGEST_MAX_FILE_BYTES = 8 * 1024 * 1024
_STREAM_HASH_CHUNK_BYTES = 1024 * 1024


class GitReceiptError(RuntimeError):
    """A Git receipt cannot be captured or validated."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _json_stable(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_stable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_stable(item) for item in value]
    return value


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stream_file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(_STREAM_HASH_CHUNK_BYTES):
                digest.update(chunk)
    except OSError as exc:
        raise GitReceiptError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _git_bytes(root: Path, *args: str, allow_returncodes: tuple[int, ...] = (0,)) -> bytes:
    environment = dict(os.environ)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, env=environment, capture_output=True, check=False
        )
    except OSError as exc:
        raise GitReceiptError(f"git {' '.join(args)} could not run: {exc}") from exc
    if result.returncode not in allow_returncodes:
        detail = result.stderr.decode(errors="replace").strip()
        raise GitReceiptError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _encoded_path(raw: bytes) -> dict[str, str]:
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        decoded = ""
    if decoded and not any(char in decoded for char in ("\n", "\r", "\x00")):
        return {"path": decoded, "path_encoding": "utf8"}
    return {"path": base64.b64encode(raw).decode("ascii"), "path_encoding": "base64"}


def _runtime_surface(root: Path) -> list[list[Any]]:
    """Digest executable and pointer files under the ignored runtime directory."""
    runtime = root / ".flow" / "runtime"
    entries: list[list[Any]] = []
    for path in sorted(runtime.rglob("*")) if runtime.is_dir() else []:
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            if not (info.st_mode & 0o111 or path.name in _RUNTIME_POINTERS):
                continue
            entries.append([path.relative_to(root).as_posix(), info.st_mode, _file_digest(path)])
        except FileNotFoundError:
            continue
    return entries


def _harness_surface(root: Path) -> list[list[Any]]:
    """Digest ignored harness settings that can declare executable hooks."""
    entries: list[list[Any]] = []
    for relative in _HARNESS_CONFIG:
        path = root / relative
        if not path.is_file():
            continue
        entries.append([relative, path.lstat().st_mode, _file_digest(path)])
    return entries


def _untracked_content(root: Path) -> list[list[Any]]:
    """Digest non-ignored untracked paths without buffering large regular files."""
    raw = _git_bytes(root, "ls-files", "--others", "--exclude-standard", "-z")
    entries: list[list[Any]] = []
    for name in sorted(item for item in raw.split(b"\0") if item):
        path = root / os.fsdecode(name)
        encoded = _encoded_path(name)
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                content = hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
            elif stat.S_ISREG(info.st_mode):
                if info.st_size <= _UNTRACKED_DIGEST_MAX_FILE_BYTES:
                    content = _file_digest(path)
                else:
                    content = _stream_file_digest(path)
            else:
                content = f"size:{info.st_size}"
        except OSError:
            continue
        entries.append(
            [encoded["path"], encoded["path_encoding"], info.st_mode, info.st_size, content]
        )
    return entries


def _capture(root: Path) -> dict[str, Any]:
    resolved = root.resolve()
    git_dir_raw = _git_bytes(resolved, "rev-parse", "--absolute-git-dir").strip()
    common_raw = _git_bytes(resolved, "rev-parse", "--git-common-dir").strip()
    head = _git_bytes(resolved, "rev-parse", "HEAD").strip().decode()
    branch = (
        _git_bytes(resolved, "symbolic-ref", "-q", "HEAD", allow_returncodes=(0, 1))
        .strip()
        .decode(errors="replace")
    )
    status_bytes = _git_bytes(resolved, "status", "--porcelain=v2", "-z", "--untracked-files=all")
    index_bytes = _git_bytes(resolved, "ls-files", "--stage", "-z")
    index_flags = _git_bytes(resolved, "ls-files", "-v", "-z")
    worktree_diff = _git_bytes(
        resolved, "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv"
    )
    submodules = _git_bytes(resolved, "submodule", "status", "--recursive")
    git_dir = Path(os.fsdecode(git_dir_raw))
    if not git_dir.is_absolute():
        git_dir = resolved / git_dir
    hooks = sorted(
        (entry.name, entry.stat().st_mode, _file_digest(entry))
        for entry in (git_dir / "hooks").glob("*")
        if entry.is_file() and not entry.name.endswith(".sample")
    )
    metadata: dict[str, Any] = {}
    for relative in ("HEAD", "config", "packed-refs"):
        path = git_dir / relative
        if path.is_file():
            data = path.read_bytes()
            metadata[relative] = {"length": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        else:
            metadata[relative] = None
    flow_refs = _git_bytes(resolved, "for-each-ref", "refs/flow/")
    body = _json_stable(
        {
            "schema": SCHEMA,
            "root": str(resolved),
            "head": head,
            "head_ref": branch or None,
            "git_dir": str(git_dir.resolve()),
            "common_dir": os.fsdecode(common_raw),
            "status": {
                "length": len(status_bytes),
                "sha256": hashlib.sha256(status_bytes).hexdigest(),
            },
            "index": {
                "length": len(index_bytes),
                "sha256": hashlib.sha256(index_bytes).hexdigest(),
            },
            "index_flags": {
                "length": len(index_flags),
                "sha256": hashlib.sha256(index_flags).hexdigest(),
            },
            "worktree_diff": {
                "length": len(worktree_diff),
                "sha256": hashlib.sha256(worktree_diff).hexdigest(),
            },
            "submodules": {
                "length": len(submodules),
                "sha256": hashlib.sha256(submodules).hexdigest(),
            },
            "flow_refs": {
                "length": len(flow_refs),
                "sha256": hashlib.sha256(flow_refs).hexdigest(),
            },
            "metadata": metadata,
            "hooks": hooks,
            "runtime_surface": _runtime_surface(resolved),
            "harness_surface": _harness_surface(resolved),
            "untracked_content": _untracked_content(resolved),
        }
    )
    assert isinstance(body, dict)
    return {**body, "digest": _digest(body)}


def capture(root: Path) -> dict[str, Any]:
    """Capture the canonical ``flow.git-receipt/v1`` for a repository root."""
    try:
        return _capture(root)
    except GitReceiptError:
        raise
    except OSError as exc:
        raise GitReceiptError(f"git receipt failed: {exc}") from exc


_COMPARISON_FIELDS = (
    "root",
    "head",
    "head_ref",
    "git_dir",
    "common_dir",
    "status",
    "index",
    "index_flags",
    "worktree_diff",
    "submodules",
    "flow_refs",
    "metadata",
    "hooks",
    "runtime_surface",
    "harness_surface",
    "untracked_content",
)


def validate(value: object) -> dict[str, Any]:
    """Validate a complete canonical receipt and its content digest."""
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise GitReceiptError(f"git receipt must use {SCHEMA}")
    receipt = cast(dict[str, Any], value)
    if set(receipt) != set(_COMPARISON_FIELDS) | {"schema", "digest"}:
        raise GitReceiptError("git receipt fields are incomplete")
    body = {key: receipt[key] for key in receipt if key != "digest"}
    if receipt.get("digest") != _digest(body):
        raise GitReceiptError("git receipt digest is invalid")
    return receipt


def changed_fields(before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[str, ...]:
    """Return canonical top-level receipt fields that differ."""
    left = validate(dict(before))
    right = validate(dict(after))
    return tuple(field for field in _COMPARISON_FIELDS if left[field] != right[field])


__all__ = ["SCHEMA", "GitReceiptError", "capture", "changed_fields", "validate"]
