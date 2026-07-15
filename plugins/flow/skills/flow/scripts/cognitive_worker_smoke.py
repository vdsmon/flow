"""Prepare and verify real parent-to-worker capsule smoke evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import shlex
import sys
from pathlib import Path
from typing import Any

import cognitive_workers
from _atomicio import atomic_write_text

SCHEMA = "flow.cognitive-worker-smoke/v1"


def _write_json(path: Path, value: object, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")
    path.chmod(mode)


def prepare(
    *,
    direction: str,
    root: Path,
    source_root: Path,
    facade: Path,
    route: dict[str, str],
) -> dict[str, Any]:
    """Create one fresh challenge-bound read-only cross-harness fixture."""
    if direction not in {"codex-parent", "claude-parent"}:
        raise ValueError("direction must be codex-parent or claude-parent")
    parent = "codex" if direction == "codex-parent" else "claude_code"
    expected_worker = "claude_code" if parent == "codex" else "codex"
    if route.get("harness") != expected_worker:
        raise ValueError("smoke worker harness must differ from its real parent harness")
    smoke_root = root.expanduser().resolve()
    smoke_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    smoke_root.chmod(0o700)
    for name in (direction, "tmp", "artifacts", "capsules"):
        (smoke_root / name).mkdir(mode=0o700)
    nonce = secrets.token_bytes(32)
    challenge_digest = hashlib.sha256(nonce).hexdigest()
    nonce_path = smoke_root / "challenge.bin"
    nonce_path.write_bytes(nonce)
    nonce_path.chmod(0o400)

    source = source_root.expanduser().resolve()
    source_receipt = cognitive_workers.git_receipt(source)
    candidate_plan = {
        "digest": hashlib.sha256(
            f"cross-harness:{direction}:{challenge_digest}".encode()
        ).hexdigest(),
        "challenge_digest": challenge_digest,
    }
    input_value = {
        "schema": "flow.cognitive-worker-smoke-input/v1",
        "challenge_digest": challenge_digest,
        "candidate_plan": candidate_plan,
    }
    input_path = smoke_root / "input.json"
    _write_json(input_path, input_value, 0o400)
    input_digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
    order = cognitive_workers.WorkOrder(
        logical_invocation_id=f"smoke:{direction}:{challenge_digest}",
        generation=1,
        profile="plan_assessor",
        source_root=str(source),
        source_sha=source_receipt["head"],
        route=route,
        route_snapshot_digest=hashlib.sha256(
            json.dumps(route, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        input_bundle=str(input_path),
        input_digest=input_digest,
        facts={
            "ticket": {"key": "FLOW-SMOKE", "title": "Cross-harness challenge"},
            "base_sha": source_receipt["head"],
            "route_digest": hashlib.sha256(
                json.dumps(route, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "candidate_plan": candidate_plan,
            "planner_receipt": {"digest": challenge_digest},
            "assessment_rubric": (
                "Return approve only when the exact challenge digest appears in the candidate."
            ),
        },
        challenge_digest=challenge_digest,
    )
    order_path = smoke_root / "work-order.json"
    _write_json(order_path, order.to_mapping(), 0o400)
    facade_path = facade.expanduser().resolve()
    if not facade_path.is_absolute():
        raise ValueError("smoke facade must be absolute")
    harness_value = "codex" if parent == "codex" else "claude-code"
    command_parts = [
        f"FLOW_HARNESS={harness_value}",
        str(facade_path),
        "cognitive-worker",
        "run",
        "--work-order",
        str(order_path),
        "--artifact-root",
        str(smoke_root / "artifacts"),
        "--capsule-root",
        str(smoke_root / "capsules"),
    ]
    command = " ".join(shlex.quote(part) for part in command_parts)
    body = {
        "schema": SCHEMA,
        "direction": direction,
        "parent_harness": parent,
        "worker_harness": expected_worker,
        "challenge_digest": challenge_digest,
        "nonce_path": str(nonce_path),
        "work_order": str(order_path),
        "input_path": str(input_path),
        "artifact_root": str(smoke_root / "artifacts"),
        "capsule_root": str(smoke_root / "capsules"),
        "facade_command": command,
        "source_receipt_before": source_receipt,
        "outer_evidence": str(smoke_root / "outer-evidence.json"),
        "source_root": str(source),
    }
    manifest = {**body, "digest": cognitive_workers._digest(body)}
    manifest_path = smoke_root / "manifest.json"
    _write_json(manifest_path, manifest, 0o400)
    return {**manifest, "manifest_path": str(manifest_path)}


def transcript_invocations(path: Path, parent_harness: str) -> list[dict[str, Any]]:
    """Return the shell commands the real parent actually executed, from its own transcript.

    The parent's transcript is the only outer evidence the orchestrator cannot author. A
    hand-written summary proves nothing: an owner could claim a parent ran the facade while
    invoking it directly. Codex emits ``command_execution`` items; Claude Code emits Bash
    ``tool_use`` blocks paired with a ``tool_result``.
    """
    invocations: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if parent_harness == "codex":
            item = event.get("item")
            # Codex reports each command twice: once in progress, once terminal.
            if (
                isinstance(item, dict)
                and item.get("type") == "command_execution"
                and item.get("exit_code") is not None
            ):
                invocations.append(
                    {
                        "command": item.get("command"),
                        "exit_code": item.get("exit_code"),
                        "output": item.get("aggregated_output"),
                    }
                )
            continue
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Bash":
                command = (block.get("input") or {}).get("command")
                pending[str(block.get("id"))] = {"command": command}
            elif block.get("type") == "tool_result":
                record = pending.pop(str(block.get("tool_use_id")), None)
                if record is None:
                    continue
                body = block.get("content")
                text = body if isinstance(body, str) else json.dumps(body)
                record["output"] = text
                # Claude Code does not surface a shell exit code here; the nested worker
                # receipts are what prove success, so leave it unclaimed.
                record["exit_code"] = None
                invocations.append(record)
    return invocations


_WRAPPER_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_WRAPPER_C_FLAGS = {"-c", "-lc", "-ic", "-lic", "-cl"}


def _facade_argv(command: object) -> list[str]:
    """Return the argv a recorded command line would execute, unwrapping one login shell.

    Codex records ``/bin/zsh -lc '<approved command>'``; Claude Code records the approved command
    as the Bash argv itself. A wrapper counts only when the shell carries exactly one ``-c`` body
    and nothing else, so the body is the whole command rather than one clause among several.
    """
    try:
        argv = shlex.split(str(command or ""))
    except ValueError:
        return []
    if len(argv) == 3 and Path(argv[0]).name in _WRAPPER_SHELLS and argv[1] in _WRAPPER_C_FLAGS:
        try:
            argv = shlex.split(argv[2])
        except ValueError:
            return []
    return argv


def _verify_real_parent(
    manifest: dict[str, Any], outer: dict[str, Any], outcome_digest: object
) -> list[str]:
    """Check the parent's own transcript for the facade invocation and the nested outcome digest.

    An invocation is attributed to the facade only when its argv, after unwrapping a single login
    shell, is token-for-token the facade command. No other command may appear, the matched
    invocation must not have failed, and the nested outcome digest must appear in the output the
    parent recorded for it.

    This rejects a shell line that merely contains the facade text next to other clauses, such as
    one that echoes the command and cats a pre-existing outcome.json. It does not make the
    transcript unforgeable: the transcript is an unsigned file, so an owner who hand-authors the
    whole JSONL can still fabricate an item whose argv is exact and whose output carries the digest.
    """
    errors: list[str] = []
    stdout_path = Path(str(outer.get("stdout_path", "")))
    if not stdout_path.is_file():
        return ["outer evidence has no parent transcript to verify"]
    try:
        invocations = transcript_invocations(stdout_path, str(manifest.get("parent_harness")))
    except OSError as exc:
        return [f"parent transcript is unreadable: {exc}"]
    expected_argv = _facade_argv(manifest.get("facade_command"))
    if not expected_argv:
        return ["the smoke manifest carries no parseable facade command"]
    matched = [item for item in invocations if _facade_argv(item.get("command")) == expected_argv]
    if not matched:
        errors.append("the parent transcript never executed the exact absolute facade command")
    if len(invocations) > len(matched):
        errors.append("the parent executed a command outside its single allowed invocation")
    if any(item.get("exit_code") not in (0, None) for item in matched):
        errors.append("the parent's own transcript records a failed facade invocation")
    digest = str(outcome_digest or "")
    if not digest:
        errors.append("no nested outcome digest is available to bind to the parent transcript")
    elif not any(digest in str(item.get("output") or "") for item in matched):
        errors.append("the parent's own transcript never carried the nested outcome digest")
    return errors


def _verify_imported_change(
    manifest: dict[str, Any],
    order: cognitive_workers.WorkOrder,
    outcome: dict[str, Any],
    after: dict[str, Any],
    before_digest: object,
) -> list[str]:
    """Attest that a capsule_writer's authoritative worktree holds exactly its imported patch.

    A read_only or disposable worker must leave the authoritative worktree byte-identical, but an
    importing writer changes it by design: the change is the import. It is valid only when the
    nested change receipt is an applied import within its allowed mutation paths and the staged
    diff against the baseline is byte-identical to the receipt's ``authoritative_diff_digest``. A
    worktree left unchanged imported nothing and is not a proof.
    """
    errors: list[str] = []
    change = outcome.get("receipts", {}).get("change", {})
    if change.get("import_result") != "applied":
        errors.append("capsule_writer change receipt is not an applied import")
    touched = set(change.get("touched_paths") or [])
    allowed = set(change.get("allowed_paths") or [])
    if not touched <= allowed:
        errors.append("capsule_writer imported outside its allowed mutation paths")
    if after["digest"] == before_digest:
        errors.append("capsule_writer left the authoritative worktree unchanged")
    try:
        staged = cognitive_workers._git_bytes(
            Path(str(manifest["source_root"])),
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            "-M",
            "--cached",
            order.source_sha,
        )
    except cognitive_workers.WorkerFailure:
        errors.append("authoritative staged diff is unavailable")
    else:
        if hashlib.sha256(staged).hexdigest() != change.get("authoritative_diff_digest"):
            errors.append("authoritative worktree does not match the imported change receipt")
    return errors


def verify_manifest(manifest: dict[str, Any]) -> dict[str, Any]:  # noqa: C901
    """Verify outer parent authority and the complete nested receipt chain."""
    errors: list[str] = []
    if manifest.get("schema") != SCHEMA:
        errors.append("unsupported smoke manifest schema")
    manifest_body = {key: value for key, value in manifest.items() if key != "digest"}
    if manifest.get("digest") != cognitive_workers._digest(manifest_body):
        errors.append("smoke manifest digest is invalid")
    outer_path = Path(str(manifest.get("outer_evidence", "")))
    try:
        outer = json.loads(outer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        outer = {}
        errors.append("outer parent evidence is missing or invalid")
    expected_executable = "codex" if manifest.get("parent_harness") == "codex" else "claude"
    executable = Path(str(outer.get("executable", ""))).name
    if executable != expected_executable:
        errors.append("outer evidence does not prove the real parent executable")
    if not outer.get("version"):
        errors.append("outer evidence has no parent CLI version")
    if outer.get("exit_code") != 0:
        errors.append("outer parent command did not exit successfully")
    if outer.get("facade_command") != manifest.get("facade_command"):
        errors.append("outer evidence does not bind the exact absolute facade command")
    for field in ("stdout_path", "stderr_path"):
        path = Path(str(outer.get(field, "")))
        if not path.is_file():
            errors.append(f"outer evidence is missing {field}")

    outcome: dict[str, Any] = {}
    try:
        order = cognitive_workers.WorkOrder.from_mapping(
            json.loads(Path(str(manifest["work_order"])).read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError, cognitive_workers.WorkerFailure, KeyError) as exc:
        errors.append(f"work order is invalid: {exc}")
        order = None
    if order is not None and order.challenge_digest != manifest.get("challenge_digest"):
        errors.append("work order challenge does not match the manifest")
    authority = (
        cognitive_workers.ROLE_CATALOG[order.profile].authority
        if order is not None
        else "read_only"
    )
    if order is not None:
        outcome_path = (
            Path(str(manifest["artifact_root"]))
            / "invocations"
            / hashlib.sha256(order.logical_invocation_id.encode()).hexdigest()
            / "outcome.json"
        )
        try:
            outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            errors.append("nested worker outcome is missing")
        if outcome.get("status") != "succeeded":
            errors.append("nested worker did not succeed")
        route = outcome.get("receipts", {}).get("route", {})
        if route.get("activation") != "active" or route.get("effective") != order.route:
            errors.append("nested route receipt is not exact and active")
        process = outcome.get("receipts", {}).get("process", {})
        terminal = all(
            process.get(key) is True
            for key in ("child_reaped", "process_group_absent", "stdout_eof", "stderr_eof")
        )
        if not terminal:
            errors.append("nested physical attempt lacks terminal acknowledgement")
        disposal = outcome.get("receipts", {}).get("disposal", {})
        if disposal.get("absent") is not True or disposal.get("quarantined") is not False:
            errors.append("nested capsule was not safely disposed")
        if outcome:
            body = {key: value for key, value in outcome.items() if key != "digest"}
            if outcome.get("digest") != cognitive_workers._digest(body):
                errors.append("nested outcome digest is invalid")
            else:
                # A leased capsule_writer order binds run()'s owner check to its run and lease.
                # Replaying with the order's own run/lease reaches the idempotent durable-outcome
                # path (no re-import, no re-invoke) rather than a false owner-mismatch.
                writer = authority == "capsule_writer"
                try:
                    replay = cognitive_workers.CognitiveWorkers(
                        artifact_root=Path(str(manifest["artifact_root"])),
                        capsule_root=Path(str(manifest["capsule_root"])),
                    ).run(
                        order,
                        cognitive_workers.OwnerProof(
                            owner_id="smoke-verifier",
                            harness=str(manifest["parent_harness"]),
                            run_id=order.run_id if writer else None,
                            lease_fence=order.lease_fence if writer else None,
                        ),
                    )
                    if replay.to_mapping().get("digest") != outcome.get("digest"):
                        errors.append("logical replay returned a different durable outcome")
                except (KeyError, cognitive_workers.WorkerFailure) as exc:
                    errors.append(f"logical replay failed: {exc}")
    errors.extend(_verify_real_parent(manifest, outer, outcome.get("digest")))
    try:
        after = cognitive_workers.git_receipt(Path(str(manifest["source_root"])))
    except (OSError, KeyError, cognitive_workers.WorkerFailure):
        errors.append("authoritative post-Git receipt is unavailable")
    else:
        before_digest = manifest.get("source_receipt_before", {}).get("digest")
        if authority == "capsule_writer" and order is not None:
            errors.extend(_verify_imported_change(manifest, order, outcome, after, before_digest))
        elif after["digest"] != before_digest:
            errors.append("authoritative repository changed during the smoke")
    return {
        "schema": "flow.cognitive-worker-smoke-verification/v1",
        "verified": not errors,
        "errors": errors,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare or verify cross-harness worker smoke evidence."
    )
    sub = parser.add_subparsers(dest="operation", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument(
        "--direction", choices=["codex-parent", "claude-parent"], required=True
    )
    prepare_parser.add_argument("--root", required=True)
    prepare_parser.add_argument("--source-root", required=True)
    prepare_parser.add_argument("--facade", required=True)
    prepare_parser.add_argument("--harness", choices=["codex", "claude_code"], required=True)
    prepare_parser.add_argument("--model", required=True)
    prepare_parser.add_argument("--effort", required=True)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--manifest", required=True)
    return parser


def cli_main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.operation == "prepare":
            result = prepare(
                direction=args.direction,
                root=Path(args.root),
                source_root=Path(args.source_root),
                facade=Path(args.facade),
                route={"harness": args.harness, "model": args.model, "effort": args.effort},
            )
        else:
            value = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
            result = verify_manifest(value)
    except (OSError, ValueError, json.JSONDecodeError, cognitive_workers.WorkerFailure) as exc:
        sys.stderr.write(f"cognitive-worker-smoke: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0 if result.get("verified", True) else 2


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["cli_main", "prepare", "verify_manifest"]
