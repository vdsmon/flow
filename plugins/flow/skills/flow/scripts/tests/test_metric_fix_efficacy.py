"""Tests for metric.py fix-efficacy: per closed MACHINERY-fix bead falsification.

Mirrors friction_recurrence.analyze()'s read + distinctive-anchor selection but
joins per BEAD (`ticket`) instead of per anchor class. Fixtures follow
test_friction_recurrence.py's shape; every anchor relied on appears in >=2
entries (the DF_LO=2 floor) or it silently drops and the bead reads
unmeasurable. Anchors avoid digits: friction_recurrence.anchors()'s snake regex
is letter-only, so a token like "case1" collapses to "case" with the digit
dropped -- confusing for a test fixture, so plain words are used instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import metric


def _seed_workspace(root: Path, namespace: str = "demo", *, initialized: bool = True) -> None:
    flow = root / ".flow"
    (flow / namespace).mkdir(parents=True, exist_ok=True)
    if initialized:
        (flow / ".initialized").write_text("", encoding="utf-8")
    (flow / "workspace.toml").write_text(
        f'[tracker]\nbackend = "beads"\n\n[memory]\nnamespace = "{namespace}"\n',
        encoding="utf-8",
    )


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e, sort_keys=True) for e in entries]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _friction(
    *,
    id_: str,
    ts: str,
    stage: str = "implement",
    type_: str = "RETRY",
    ticket: str = "T-1",
    run_id: str = "run-1",
    body: str = "",
) -> dict:
    return {
        "id": id_,
        "ts": ts,
        "run_id": run_id,
        "ticket": ticket,
        "stage": stage,
        "type": type_,
        "severity": "major",
        "body": body,
    }


def _machinery(*, id_: str, ts: str, ticket: str, body: str) -> dict:
    return {"id": id_, "ts": ts, "ticket": ticket, "type": "LEARNED", "body": body}


def _knowledge_path(root: Path, namespace: str = "demo") -> Path:
    return root / ".flow" / namespace / "knowledge.jsonl"


def _friction_path(root: Path, namespace: str = "demo") -> Path:
    return root / ".flow" / namespace / "friction.jsonl"


def _compute(root: Path, namespace: str = "demo") -> dict:
    return metric.compute_fix_efficacy(root, namespace)


# --- per-bead join: verdicts -------------------------------------------------


def test_recurred_bead(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    fix_ts = "2026-06-01T00:00:00.000Z"
    _write_jsonl(
        _knowledge_path(tmp_path),
        [
            _machinery(
                id_="k-1",
                ts=fix_ts,
                ticket="T-recur",
                body="MACHINERY: sig_recur_anchor patched. Fix (commit aaaaaaa).",
            )
        ],
    )
    _write_jsonl(
        _friction_path(tmp_path),
        [_friction(id_="f-1", ts="2026-06-02T00:00:00.000Z", body="sig_recur_anchor fired again")],
    )

    result = _compute(tmp_path)

    assert len(result["beads"]) == 1
    bead = result["beads"][0]
    assert bead["ticket"] == "T-recur"
    assert bead["verdict"] == "recurred"
    assert bead["measurable"] is True
    assert bead["post_fix_count"] == 1
    assert bead["claimed_anchors"] == ["sig_recur_anchor"]
    assert result["totals"] == {
        "fix_beads": 1,
        "recurred": 1,
        "clean": 0,
        "unmeasurable": 0,
        "recurrence_rate": 1.0,
    }


def test_clean_measurable_bead(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    fix_ts = "2026-06-01T00:00:00.000Z"
    _write_jsonl(
        _knowledge_path(tmp_path),
        [
            _machinery(
                id_="k-2",
                ts=fix_ts,
                ticket="T-clean",
                body="MACHINERY: sig_clean_anchor patched. Fix (commit bbbbbbb).",
            )
        ],
    )
    _write_jsonl(
        _friction_path(tmp_path),
        [
            _friction(
                id_="f-2",
                ts="2026-05-01T00:00:00.000Z",
                body="sig_clean_anchor seen before the fix",
            )
        ],
    )

    result = _compute(tmp_path)

    bead = result["beads"][0]
    assert bead["ticket"] == "T-clean"
    assert bead["verdict"] == "clean"
    assert bead["measurable"] is True
    assert bead["claimed_anchors"] == ["sig_clean_anchor"]
    assert bead["post_fix_count"] == 0
    assert result["totals"]["unmeasurable"] == 0
    assert result["totals"]["clean"] == 1


def test_multi_entry_bead_fix_ts_is_min_claimed_is_union(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_jsonl(
        _knowledge_path(tmp_path),
        [
            _machinery(
                id_="k-3a",
                ts="2026-06-01T00:00:00.000Z",
                ticket="T-multi",
                body="MACHINERY: multi_anchor_alpha issue fixed.",
            ),
            _machinery(
                id_="k-3b",
                ts="2026-06-03T00:00:00.000Z",
                ticket="T-multi",
                body="MACHINERY: multi_anchor_beta issue fixed too.",
            ),
        ],
    )
    _write_jsonl(
        _friction_path(tmp_path),
        [
            _friction(
                id_="f-3a",
                ts="2026-05-30T00:00:00.000Z",
                body="multi_anchor_alpha seen earlier too",
            ),
            _friction(
                id_="f-3b",
                ts="2026-06-02T00:00:00.000Z",
                body="multi_anchor_beta trouble between the two fixes",
            ),
        ],
    )

    result = _compute(tmp_path)

    assert len(result["beads"]) == 1
    bead = result["beads"][0]
    assert bead["ticket"] == "T-multi"
    # fix_ts is the MIN across the bead's entries, so the earlier fix anchors
    # the join even though multi_anchor_beta's own fix landed later.
    assert bead["fix_ts"] == "2026-06-01T00:00:00.000Z"
    assert bead["claimed_anchors"] == ["multi_anchor_alpha", "multi_anchor_beta"]
    assert bead["verdict"] == "recurred"
    assert bead["post_fix_count"] == 1
    assert [r["id"] for r in bead["recurrences"]] == ["f-3b"]


def test_unmeasurable_no_distinctive_anchor(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_jsonl(
        _knowledge_path(tmp_path),
        [
            _machinery(
                id_="k-4",
                ts="2026-06-01T00:00:00.000Z",
                ticket="T-rare",
                body="MACHINERY: rare_lonely_anchor patched alone.",
            )
        ],
    )
    # no friction entry shares rare_lonely_anchor -> df=1, drops below the floor.

    result = _compute(tmp_path)

    bead = result["beads"][0]
    assert bead["ticket"] == "T-rare"
    assert bead["claimed_anchors"] == []
    assert bead["measurable"] is False
    assert bead["verdict"] == "clean"
    assert result["totals"]["unmeasurable"] == 1
    assert result["totals"]["clean"] == 1
    assert result["totals"]["recurred"] == 0


def test_strict_boundary_ts_equal_fix_ts_not_counted(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    fix_ts = "2026-06-01T00:00:00.000Z"
    _write_jsonl(
        _knowledge_path(tmp_path),
        [
            _machinery(
                id_="k-5",
                ts=fix_ts,
                ticket="T-boundary",
                body="MACHINERY: boundary_anchor patched.",
            )
        ],
    )
    _write_jsonl(
        _friction_path(tmp_path),
        [_friction(id_="f-5", ts=fix_ts, body="boundary_anchor at the exact fix instant")],
    )

    result = _compute(tmp_path)

    bead = result["beads"][0]
    assert bead["measurable"] is True
    assert bead["post_fix_count"] == 0
    assert bead["verdict"] == "clean"
    assert bead["recurrences"] == []


def test_before_fix_friction_not_counted(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    fix_ts = "2026-06-05T00:00:00.000Z"
    _write_jsonl(
        _knowledge_path(tmp_path),
        [
            _machinery(
                id_="k-6",
                ts=fix_ts,
                ticket="T-before",
                body="MACHINERY: before_fix_anchor patched.",
            )
        ],
    )
    _write_jsonl(
        _friction_path(tmp_path),
        [
            _friction(
                id_="f-6", ts="2026-06-01T00:00:00.000Z", body="before_fix_anchor happened earlier"
            )
        ],
    )

    result = _compute(tmp_path)

    bead = result["beads"][0]
    assert bead["post_fix_count"] == 0
    assert bead["verdict"] == "clean"
    assert bead["recurrences"] == []


def test_fix_sha_inline_evidence(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_jsonl(
        _knowledge_path(tmp_path),
        [
            _machinery(
                id_="k-7",
                ts="2026-06-01T00:00:00.000Z",
                ticket="T-sha",
                body="MACHINERY: sha_evidence_anchor patched. Fix (commit abc1234).",
            )
        ],
    )
    _write_jsonl(
        _friction_path(tmp_path),
        [
            _friction(
                id_="f-7",
                ts="2026-05-01T00:00:00.000Z",
                body="sha_evidence_anchor also mentioned before",
            )
        ],
    )

    result = _compute(tmp_path)

    assert result["beads"][0]["fix_shas"] == ["abc1234"]


def test_empty_corpus_zero_beads_and_rate(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)

    result = _compute(tmp_path)

    assert result["beads"] == []
    assert result["totals"] == {
        "fix_beads": 0,
        "recurred": 0,
        "clean": 0,
        "unmeasurable": 0,
        "recurrence_rate": 0,
    }


def test_bead_sort_order(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_jsonl(
        _knowledge_path(tmp_path),
        [
            _machinery(
                id_="k-hi",
                ts="2026-06-01T00:00:00.000Z",
                ticket="Z-hi",
                body="MACHINERY: hi_pfc_anchor broke twice.",
            ),
            _machinery(
                id_="k-lo",
                ts="2026-06-01T00:00:00.000Z",
                ticket="A-lo",
                body="MACHINERY: lo_pfc_anchor broke once.",
            ),
            _machinery(
                id_="k-clean-a",
                ts="2026-06-01T00:00:00.000Z",
                ticket="A-clean",
                body="MACHINERY: clean_a_anchor fixed cleanly.",
            ),
            _machinery(
                id_="k-clean-b",
                ts="2026-06-01T00:00:00.000Z",
                ticket="B-clean",
                body="MACHINERY: clean_b_anchor fixed cleanly too.",
            ),
        ],
    )
    _write_jsonl(
        _friction_path(tmp_path),
        [
            _friction(
                id_="f-hi-1", ts="2026-06-02T00:00:00.000Z", body="hi_pfc_anchor fired again"
            ),
            _friction(
                id_="f-hi-2", ts="2026-06-03T00:00:00.000Z", body="hi_pfc_anchor fired a third time"
            ),
            _friction(
                id_="f-lo-1", ts="2026-06-02T00:00:00.000Z", body="lo_pfc_anchor fired again"
            ),
            _friction(
                id_="f-clean-a",
                ts="2026-05-01T00:00:00.000Z",
                body="clean_a_anchor mentioned pre-fix",
            ),
            _friction(
                id_="f-clean-b",
                ts="2026-05-01T00:00:00.000Z",
                body="clean_b_anchor mentioned pre-fix",
            ),
        ],
    )

    result = _compute(tmp_path)

    # recurred beads first (higher post_fix_count first), then clean beads
    # tie-broken by ticket ascending.
    assert [b["ticket"] for b in result["beads"]] == ["Z-hi", "A-lo", "A-clean", "B-clean"]


# --- CLI -----------------------------------------------------------------------


def test_cli_table_render_default(tmp_path: Path, capsys) -> None:
    _seed_workspace(tmp_path)
    _write_jsonl(
        _knowledge_path(tmp_path),
        [
            _machinery(
                id_="k-cli",
                ts="2026-06-01T00:00:00.000Z",
                ticket="T-cli",
                body="MACHINERY: cli_anchor_token patched.",
            )
        ],
    )
    _write_jsonl(
        _friction_path(tmp_path),
        [
            _friction(
                id_="f-cli", ts="2026-06-02T00:00:00.000Z", body="cli_anchor_token fired again"
            )
        ],
    )

    rc = metric.cli_main(["fix-efficacy", "--namespace", "demo", "--workspace-root", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "recurred" in out
    assert "T-cli" in out
    import pytest

    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cli_json_output(tmp_path: Path, capsys) -> None:
    _seed_workspace(tmp_path)
    _write_jsonl(
        _knowledge_path(tmp_path),
        [
            _machinery(
                id_="k-cli2",
                ts="2026-06-01T00:00:00.000Z",
                ticket="T-cli2",
                body="MACHINERY: cli_json_anchor patched.",
            )
        ],
    )
    _write_jsonl(
        _friction_path(tmp_path),
        [
            _friction(
                id_="f-cli2", ts="2026-06-02T00:00:00.000Z", body="cli_json_anchor fired again"
            )
        ],
    )

    rc = metric.cli_main(
        ["fix-efficacy", "--namespace", "demo", "--workspace-root", str(tmp_path), "--json"]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "beads" in payload
    assert "totals" in payload
    assert payload["resolved_workspace_root"] == str(tmp_path.resolve())
    assert payload["totals"]["fix_beads"] == 1


def test_cli_namespace_autoresolve(tmp_path: Path, capsys) -> None:
    _seed_workspace(tmp_path)

    rc = metric.cli_main(["fix-efficacy", "--workspace-root", str(tmp_path)])

    assert rc == 0
    assert "namespace" not in capsys.readouterr().err


def test_cli_no_flow_dir(tmp_path: Path, capsys) -> None:
    _seed_workspace(tmp_path, initialized=False)

    rc = metric.cli_main(["fix-efficacy", "--namespace", "demo", "--workspace-root", str(tmp_path)])

    assert rc == 1
    assert "no .flow" in capsys.readouterr().err


def test_forwarder_from_recall(tmp_path: Path, capsys) -> None:
    import recall

    _seed_workspace(tmp_path)

    rc = recall.cli_main(
        ["--metric", "fix-efficacy", "--namespace", "demo", "--workspace-root", str(tmp_path)]
    )

    assert rc == 0
    assert "fix-efficacy" in capsys.readouterr().out
