"""File a self-work (machinery) bead into flow's OWN beads DB.

Used by the reflect sling-bead path and `/flow evolve`. Two guarantees:

- Gated on maintainer mode. Outside it the bead is NOT filed (exit 4), so a normal
  user run never requires a flow checkout and machinery friction stays dormant.
- Always targets flow's beads (the resolved maintainer repo root), never the run's
  tracker — which may be Jira. A machinery finding is about the harness, not the
  user's project, so it must land in flow's backlog regardless of the run.

Stdlib-only. `bd` is invoked with cwd = the flow repo so it resolves that repo's
.beads DB.

Identity / convergence (two-layer seam). `--dedup-key <s>` feeds two dedup nets:

1. Exact: reduced to a deterministic `evid:<fingerprint>` label (casefold + collapse
   non-alphanumerics, then sha256[:12]), so wording/format variance can't change it.
   Before creating, flow's beads are checked for that label in ANY status (open or
   closed); if one exists the create is skipped (exit 5).
2. File-anchored fuzzy: for a `<file>::<symptom>` key, the file component (canonicalized
   to its basename) is fingerprinted into an `evidfile:` anchor. On an exact miss, beads
   carrying that anchor are listed and the new summary is token-compared (Jaccard over a
   stemmed, stopword-filtered token set) against each candidate's title; a score over
   THRESHOLD also dedups (exit 5). This catches re-discoveries of the same same-file
   defect phrased differently — where the whole-key exact hash would mint a fresh slug.

Anchor the key on the finding's primary file path (prose convention) so the same defect
maps to the same fingerprint across runs — that is what stops the audit refiling open
work AND re-proposing findings already closed or rejected, so the loop converges instead
of churning.

CLI:
  flow_beads_create.py --workspace-root <dir> --summary <s> --description <d>
      [--type task] [--labels a,b] [--parent KEY] [--dedup-key SLUG]

Exit codes:
  0 = filed (prints the new bead key)
  2 = bd error (stderr propagated)
  4 = not a maintainer setup (dormant; nothing filed)
  5 = duplicate: a bead with this --dedup-key already exists (prints its key)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from _runner import Runner
from _runner import default_runner as _default_runner
from maintainer import resolve_maintainer_repo


class NotMaintainer(Exception):
    """Raised when the run is not in maintainer mode. Exit 4."""


class BeadCreateError(Exception):
    """Raised when `bd create` fails or returns no id. Exit 2."""


class DuplicateBead(Exception):
    """Raised when a bead with the given --dedup-key already exists. Exit 5."""

    def __init__(self, existing_key: str, dedup_key: str) -> None:
        super().__init__(f"bead for evid:{dedup_key} already exists: {existing_key}")
        self.existing_key = existing_key
        self.dedup_key = dedup_key


# every stored status, so dedup also catches closed/rejected findings (not just open)
_ALL_STATUSES = "open,in_progress,blocked,deferred,closed"

# function words only — NOT tuned to any one finding pair (symptom words stay)
_STOPWORDS = frozenset({"a", "an", "the", "to", "of", "that", "and", "or", "for", "in", "on"})

# 0.45 is calibrated on the single real pair available (flow-mst vs flow-9jk, ~0.61);
# a distinct same-file finding scores well below it. Tunable as more real pairs appear.
THRESHOLD = 0.45


def fingerprint(raw: str) -> str:
    """Deterministic 12-hex fingerprint of a dedup key.

    Casefold + collapse every non-alphanumeric run to a single space before
    hashing, so wording/punctuation/separator variance ("scripts/mise.toml: TY
    skips hooks" vs "scripts-mise-toml-ty-skips-hooks") yields the SAME key. That
    plus the prose convention of anchoring the key on the finding's primary file
    path is what keeps the audit's identity stable across runs instead of minting
    a fresh slug each time.
    """
    norm = re.sub(r"[^a-z0-9]+", " ", raw.casefold()).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]


def _basename(p: str) -> str:
    """Strip directory prefix so path-shape variance can't split the file anchor."""
    return p.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _symptom_tokens(text: str) -> frozenset[str]:
    """Token set for fuzzy comparison: casefold, alnum-split, drop function words,
    strip a single trailing 's' from tokens longer than 3 chars (light stemming)."""
    raw = re.sub(r"[^a-z0-9]+", " ", text.casefold()).split()
    out = set()
    for tok in raw:
        if tok in _STOPWORDS:
            continue
        if len(tok) > 3 and tok.endswith("s"):
            tok = tok[:-1]
        out.add(tok)
    return frozenset(out)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _find_by_label(repo: Path, evid_label: str, run: Runner) -> str | None:
    """Return the key of an existing bead carrying evid_label, or None."""
    result = run(["bd", "list", "-l", evid_label, "--status", _ALL_STATUSES, "--json"], repo)
    if result.returncode != 0:
        raise BeadCreateError(f"bd list (dedup check) failed: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    items = (
        payload
        if isinstance(payload, list)
        else payload.get("issues", [])
        if isinstance(payload, dict)
        else []
    )
    for item in items:
        if isinstance(item, dict) and item.get("id"):
            return str(item["id"])
    return None


def _find_fuzzy_duplicate(
    repo: Path, evidfile_label: str, new_summary: str, run: Runner
) -> str | None:
    """Return the key of a same-file candidate whose title is fuzzily equal to
    new_summary (Jaccard >= THRESHOLD), or None."""
    result = run(["bd", "list", "-l", evidfile_label, "--status", _ALL_STATUSES, "--json"], repo)
    if result.returncode != 0:
        raise BeadCreateError(f"bd list (fuzzy dedup check) failed: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    items = (
        payload
        if isinstance(payload, list)
        else payload.get("issues", [])
        if isinstance(payload, dict)
        else []
    )
    new_tokens = _symptom_tokens(new_summary)
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        # bd list --json carries `title` (the one-liner), not `summary`
        if _jaccard(new_tokens, _symptom_tokens(item.get("title", ""))) >= THRESHOLD:
            return str(item["id"])
    return None


def create_bead(
    workspace_root: Path,
    summary: str,
    description: str,
    *,
    type: str = "task",
    labels: list[str] | None = None,
    parent: str | None = None,
    dedup_key: str | None = None,
    runner: Runner | None = None,
) -> str:
    """File a bead into flow's beads and return the new key.

    Raises NotMaintainer outside maintainer mode (caller decides whether that is
    fine — for the reflect dormant path it is). Raises DuplicateBead when
    dedup_key matches an existing bead. Raises BeadCreateError on bd failure.
    """
    repo = resolve_maintainer_repo(workspace_root)
    if repo is None:
        raise NotMaintainer(
            "not a flow maintainer setup (no [maintainer] marker); machinery bead not filed"
        )
    run = runner or _default_runner()
    labels = list(labels or [])
    if dedup_key:
        evid_label = f"evid:{fingerprint(dedup_key)}"
        existing = _find_by_label(repo, evid_label, run)
        if existing is not None:
            raise DuplicateBead(existing, dedup_key)
        labels.append(evid_label)
        file_part, sep, _symptom = dedup_key.partition("::")
        if sep:
            evidfile_label = f"evidfile:{fingerprint(_basename(file_part))}"
            fuzzy = _find_fuzzy_duplicate(repo, evidfile_label, summary, run)
            if fuzzy is not None:
                raise DuplicateBead(fuzzy, dedup_key)
            labels.append(evidfile_label)
    args = ["bd", "create", summary, "--type", type, "--description", description]
    if labels:
        args += ["--labels", ",".join(labels)]
    if parent:
        args += ["--parent", parent]
    args.append("--json")
    result = run(args, repo)
    if result.returncode != 0:
        raise BeadCreateError(f"bd create failed: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BeadCreateError(
            f"bd create did not return JSON: {exc}; raw={result.stdout!r}"
        ) from exc
    key = str(payload.get("id", "")) if isinstance(payload, dict) else ""
    if not key:
        # never re-run create on a parse miss: a second bd create mints a duplicate
        raise BeadCreateError(f"bd create returned no top-level id; raw={payload!r}")
    return key


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="File a self-work bead into flow's beads.")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--type", default="task")
    parser.add_argument("--labels", default="")
    parser.add_argument("--parent", default=None)
    parser.add_argument("--dedup-key", default=None)
    args = parser.parse_args(argv)

    labels = [s for s in (p.strip() for p in args.labels.split(",")) if s]
    try:
        key = create_bead(
            Path(args.workspace_root),
            args.summary,
            args.description,
            type=args.type,
            labels=labels,
            parent=args.parent,
            dedup_key=args.dedup_key,
        )
    except NotMaintainer as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except DuplicateBead as exc:
        print(exc.existing_key)
        print(str(exc), file=sys.stderr)
        return 5
    except BeadCreateError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(key)
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
