"""Flag parked PRs carrying a NEW unresolved human review as `/flow revise` candidates.

The final enrichment of `/flow queue`'s status path (epic flow-kx17.5). The queue
already surfaces `parked` keys — open PRs awaiting the maintainer's review+merge.
This asks one more question per parked PR: does it carry an unresolved Major+ review
thread? On a day-job delivery the original review_loop terminal was "CI green AND
zero unresolved Major+", so a genuine new human CHANGES_REQUESTED (→ `major`) is
exactly what makes a parked PR newly actionable → `/flow revise <pr#>`.

PR resolution uses the EXACT slugged head ref the queue already gathered, NOT a
reconstructed `feature/<key>` (which would not match the real
`feature/<key>-<slug>` branch and silently flag nothing). The caller passes
`--pr-refs` = every open-PR head ref; each parked key joins to its ref via
`key_from_ref`.

Surfaces NATIVE Major+ only — no `revise_config` / `apply_floor`. The plain-comment
severity floor is a revise-TIME knob (what the fix loop chases); applying it here
would bump leftover bot minors to major and produce false "human review" flags.

CLI:
  queue_reviews.py --workspace-root <dir> --keys <comma-keys> --pr-refs <comma-refs>

Always exits 0 with a valid JSON array on stdout (best-effort: per-key forge errors
are swallowed so the status path never breaks).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import forge
from _evolve_common import key_from_ref

# minor/nit are intentionally excluded: a leftover unresolved bot minor must never
# produce a false human-review flag (the plain-comment floor is a revise-time knob).
_MAJOR_PLUS = {"major", "critical"}


def flag_parked_reviews(keys: list[str], pr_refs: list[str], adapter: Any) -> list[dict]:
    """For each parked key with a matching open-PR ref, count unresolved Major+ threads.

    Returns a result dict only for keys whose `unresolved_major > 0`. Best-effort:
    a forge error (incl. `NotSupported`) on one key is swallowed — that key is not
    flagged, the others still process.
    """
    ref_by_key: dict[str, str] = {}
    for ref in pr_refs:
        key = key_from_ref(ref)
        if key and key not in ref_by_key:
            ref_by_key[key] = ref

    results: list[dict] = []
    for key in keys:
        ref = ref_by_key.get(key)
        if ref is None:
            continue
        try:
            pr = adapter.detect_pr(ref)
            if pr is None:
                continue
            threads = adapter.review_threads(pr["id"])
        except forge.ForgeError:
            continue

        flagged = [t for t in threads if t.get("severity") in _MAJOR_PLUS and not t.get("resolved")]
        if not flagged:
            continue
        results.append(
            {
                "key": key,
                "pr_id": pr["id"],
                "pr_url": pr.get("url"),
                "unresolved_major": len(flagged),
                "threads": [
                    {"id": t.get("id"), "severity": t.get("severity"), "title": t.get("title")}
                    for t in flagged
                ],
            }
        )
    return results


def cli_main(argv: list[str], forge_factory: Any = None) -> int:
    parser = argparse.ArgumentParser(
        description="Flag parked PRs with unresolved Major+ human reviews."
    )
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--keys", default="", help="comma-separated parked keys")
    parser.add_argument("--pr-refs", default="", help="comma-separated open-PR head refs")
    args = parser.parse_args(argv)

    keys = [k.strip() for k in args.keys.split(",") if k.strip()]
    pr_refs = [r.strip() for r in args.pr_refs.split(",") if r.strip()]

    try:
        config = forge.read_forge_config(Path(args.workspace_root))
    except forge.ForgeConfigError:
        config = None
    if config is None or not keys:
        print(json.dumps([]))
        return 0

    factory = forge_factory or forge.make_forge
    try:
        adapter = factory(config)
    except Exception:
        print(json.dumps([]))
        return 0

    results = flag_parked_reviews(keys, pr_refs, adapter)
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
