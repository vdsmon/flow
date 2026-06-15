"""CLI wrapper around the Forge Protocol.

Library + thin CLI. Stdlib-only.

Lets reference-doc prose call forge.<method>() from Bash (the `review_loop` stage
drives it). Each subcommand maps to a Forge Protocol method; output is JSON to
stdout; errors go to stderr with structured exit codes. This is the ONLY forge
surface the prose calls, mirroring `tracker_cli.py`.

Subcommands:
  detect-pr      --branch B                         forge.detect_pr(branch) -> PR|null
  pr-info        --pr ID                            forge.pr_info(id) -> PR|null (ANY state)
  open-pr        --base --head --title --body [--draft]  forge.open_pr(...) -> PR
  ci-rollup      --pr ID                            forge.ci_rollup(id) -> CIStatus (one-shot)
  review-threads --pr ID                            forge.review_threads(id) -> [thread]
  post-reply     --pr ID --thread CID --text "..."  forge.post_reply(...) -> {ok}
  resolve-thread --pr ID --thread CID               forge.resolve_thread(...) -> {resolved}
  mark-ready     --pr ID                            forge.mark_ready(id) -> {ok}
  merge          --pr ID [--squash]                 forge.merge(id, squash) -> {ok}
  delete-branch  --branch B                         forge.delete_branch(branch) -> {ok}

Capability-gated subcommands (review-threads / post-reply / resolve-thread /
mark-ready / delete-branch) degrade on `NotSupported` to `{"supported": false}` with
exit 0, so a host that cannot do X is not an error (mirrors tracker_cli's
download-attachments on beads).

Workspace resolution: reads `.flow/workspace.toml` `[forge]` block via
`forge.read_forge_config`. The block is OPTIONAL; a forge subcommand on a workspace
without `[forge]` is a config error (exit 2).

Exit codes:
  0 = success
  1 = transient/unknown forge error (network / auth / retryable)
  2 = workspace config invalid (no workspace.toml, malformed, no [forge] block)
  3 = invalid CLI args
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from forge import ForgeConfigError, ForgeError, NotSupported, make_forge, read_forge_config


def _emit(obj: Any) -> int:
    sys.stdout.write(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n")
    return 0


# ─── Subcommand dispatch ─────────────────────────────────────────────────────


def _cmd_detect_pr(forge: Any, args: argparse.Namespace) -> int:
    return _emit(forge.detect_pr(args.branch))


def _cmd_pr_info(forge: Any, args: argparse.Namespace) -> int:
    return _emit(forge.pr_info(args.pr))


def _cmd_open_pr(forge: Any, args: argparse.Namespace) -> int:
    return _emit(forge.open_pr(args.base, args.head, args.title, args.body, bool(args.draft)))


def _cmd_ci_rollup(forge: Any, args: argparse.Namespace) -> int:
    return _emit(forge.ci_rollup(args.pr))


def _cmd_review_threads(forge: Any, args: argparse.Namespace) -> int:
    return _emit(forge.review_threads(args.pr))


def _cmd_post_reply(forge: Any, args: argparse.Namespace) -> int:
    forge.post_reply(args.pr, args.thread, args.text)
    return _emit({"ok": True})


def _cmd_resolve_thread(forge: Any, args: argparse.Namespace) -> int:
    return _emit({"resolved": bool(forge.resolve_thread(args.pr, args.thread))})


def _cmd_mark_ready(forge: Any, args: argparse.Namespace) -> int:
    forge.mark_ready(args.pr)
    return _emit({"ok": True})


def _cmd_merge(forge: Any, args: argparse.Namespace) -> int:
    forge.merge(args.pr, squash=bool(args.squash))
    return _emit({"ok": True})


def _cmd_delete_branch(forge: Any, args: argparse.Namespace) -> int:
    forge.delete_branch(args.branch)
    return _emit({"ok": True})


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CLI wrapper around the Forge Protocol.")
    parser.add_argument("--workspace-root", default=".")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("detect-pr", help="forge.detect_pr(branch)")
    p.add_argument("--branch", required=True)

    p = sub.add_parser("pr-info", help="forge.pr_info(pr) — reverse lookup, ANY state")
    p.add_argument("--pr", required=True)

    p = sub.add_parser("open-pr", help="forge.open_pr(base, head, title, body, draft)")
    p.add_argument("--base", default="main")
    p.add_argument("--head", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--body", default="")
    p.add_argument("--draft", action="store_true")

    p = sub.add_parser("ci-rollup", help="forge.ci_rollup(pr) — one-shot")
    p.add_argument("--pr", required=True)

    p = sub.add_parser("review-threads", help="forge.review_threads(pr)")
    p.add_argument("--pr", required=True)

    p = sub.add_parser("post-reply", help="forge.post_reply(pr, thread, body)")
    p.add_argument("--pr", required=True)
    p.add_argument("--thread", required=True)
    p.add_argument("--text", required=True)

    p = sub.add_parser("resolve-thread", help="forge.resolve_thread(pr, thread)")
    p.add_argument("--pr", required=True)
    p.add_argument("--thread", required=True)

    p = sub.add_parser("mark-ready", help="forge.mark_ready(pr)")
    p.add_argument("--pr", required=True)

    p = sub.add_parser("merge", help="forge.merge(pr, squash)")
    p.add_argument("--pr", required=True)
    p.add_argument("--squash", action="store_true")

    p = sub.add_parser("delete-branch", help="forge.delete_branch(branch)")
    p.add_argument("--branch", required=True)

    return parser.parse_args(argv)


_DISPATCH: dict[str, Any] = {
    "detect-pr": _cmd_detect_pr,
    "pr-info": _cmd_pr_info,
    "open-pr": _cmd_open_pr,
    "ci-rollup": _cmd_ci_rollup,
    "review-threads": _cmd_review_threads,
    "post-reply": _cmd_post_reply,
    "resolve-thread": _cmd_resolve_thread,
    "mark-ready": _cmd_mark_ready,
    "merge": _cmd_merge,
    "delete-branch": _cmd_delete_branch,
}


def cli_main(argv: list[str], forge_factory: Any = None) -> int:
    """Dispatch a subcommand. `forge_factory` is injectable for tests
    (default: real `make_forge`)."""
    args = _parse_args(argv)
    workspace_root = Path(args.workspace_root).resolve()
    try:
        config = read_forge_config(workspace_root)
    except ForgeConfigError as exc:
        sys.stderr.write(f"forge-cli: {exc}\n")
        return 2
    if config is None:
        sys.stderr.write("forge-cli: workspace.toml has no [forge] block\n")
        return 2
    factory = forge_factory or make_forge
    try:
        forge = factory(config)
    except Exception as exc:
        sys.stderr.write(f"forge-cli: factory error: {exc}\n")
        return 2
    handler = _DISPATCH.get(args.cmd)
    if handler is None:
        sys.stderr.write(f"forge-cli: unknown subcommand {args.cmd!r}\n")
        return 3
    try:
        return handler(forge, args)
    except NotSupported:
        # Capability-gated op the host cannot do — degrade, not an error.
        return _emit({"supported": False})
    except ForgeError as exc:
        sys.stderr.write(f"forge-cli: forge error: {exc}\n")
        return 1
    except (KeyError, ValueError) as exc:
        sys.stderr.write(f"forge-cli: invalid argument: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["cli_main"]
