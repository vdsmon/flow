"""Tests for metric.py fix-efficacy: per closed MACHINERY-fix bead falsification.

Mirrors friction_recurrence.analyze()'s read + distinctive-anchor selection but
joins per BEAD (`ticket`) on a (stage, type, anchor) tuple grounded in PRE-fix
friction. Every fixture that expects a recurrence seeds a pre-fix friction entry
sharing the fixed anchor at some stage+type, then a post-fix entry carrying the
same triple; a claimed anchor with no pre-fix occurrence reads unmeasurable.
Fixtures follow test_friction_recurrence.py's shape; every anchor relied on
appears in >=2 entries counted across friction AND machinery (the DF_LO=2
floor) or it silently drops and the bead reads unmeasurable. Anchors avoid
digits: friction_recurrence.anchors()'s snake regex is letter-only, so a token
like "case1" collapses to "case" with the digit dropped -- confusing for a test
fixture, so plain words are used instead.
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


# --- per-bead tuple join: verdicts -------------------------------------------


def test_recurred_bead(tmp_path: Path) -> None:
    """Pre-fix grounding + a post-fix entry with the same (stage, type, anchor)."""
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
        [
            _friction(
                id_="f-0",
                ts="2026-05-20T00:00:00.000Z",
                body="sig_recur_anchor seen before the fix",
            ),
            _friction(
                id_="f-1", ts="2026-06-02T00:00:00.000Z", body="sig_recur_anchor fired again"
            ),
        ],
    )

    result = _compute(tmp_path)

    assert len(result["beads"]) == 1
    bead = result["beads"][0]
    assert bead["ticket"] == "T-recur"
    assert bead["verdict"] == "recurred"
    assert bead["measurable"] is True
    assert bead["post_fix_count"] == 1
    assert bead["claimed_anchors"] == ["sig_recur_anchor"]
    assert bead["claimed_tuples"] == [["implement", "RETRY", "sig_recur_anchor"]]
    assert bead["unmeasurable_reason"] is None
    assert result["totals"] == {
        "fix_beads": 1,
        "recurred": 1,
        "clean": 0,
        "unmeasurable": 0,
        "recurrence_rate": 1.0,
    }


def test_clean_on_stage_mismatch(tmp_path: Path) -> None:
    """The headline de-noise case: the post-fix hit shares the anchor but at a different stage, so
    its tuple is not in claimed_tuples -> clean."""
    _seed_workspace(tmp_path)
    fix_ts = "2026-06-01T00:00:00.000Z"
    _write_jsonl(
        _knowledge_path(tmp_path),
        [
            _machinery(
                id_="k-sm",
                ts=fix_ts,
                ticket="T-stagemiss",
                body="MACHINERY: stage_miss_anchor patched.",
            )
        ],
    )
    _write_jsonl(
        _friction_path(tmp_path),
        [
            _friction(
                id_="f-sm-pre",
                ts="2026-05-20T00:00:00.000Z",
                stage="implement",
                body="stage_miss_anchor before the fix",
            ),
            _friction(
                id_="f-sm-post",
                ts="2026-06-02T00:00:00.000Z",
                stage="commit",
                body="stage_miss_anchor fired again elsewhere",
            ),
        ],
    )

    result = _compute(tmp_path)

    bead = result["beads"][0]
    assert bead["ticket"] == "T-stagemiss"
    assert bead["measurable"] is True
    assert bead["claimed_anchors"] == ["stage_miss_anchor"]
    assert bead["claimed_tuples"] == [["implement", "RETRY", "stage_miss_anchor"]]
    assert bead["verdict"] == "clean"
    assert bead["post_fix_count"] == 0


def test_clean_on_type_mismatch(tmp_path: Path) -> None:
    """Same stage, different type -> the tuple does not join -> clean."""
    _seed_workspace(tmp_path)
    fix_ts = "2026-06-01T00:00:00.000Z"
    _write_jsonl(
        _knowledge_path(tmp_path),
        [
            _machinery(
                id_="k-tm",
                ts=fix_ts,
                ticket="T-typemiss",
                body="MACHINERY: type_miss_anchor patched.",
            )
        ],
    )
    _write_jsonl(
        _friction_path(tmp_path),
        [
            _friction(
                id_="f-tm-pre",
                ts="2026-05-20T00:00:00.000Z",
                type_="RETRY",
                body="type_miss_anchor before the fix",
            ),
            _friction(
                id_="f-tm-post",
                ts="2026-06-02T00:00:00.000Z",
                type_="MISSING_TOOL",
                body="type_miss_anchor fired again as a different type",
            ),
        ],
    )

    result = _compute(tmp_path)

    bead = result["beads"][0]
    assert bead["measurable"] is True
    assert bead["claimed_tuples"] == [["implement", "RETRY", "type_miss_anchor"]]
    assert bead["verdict"] == "clean"
    assert bead["post_fix_count"] == 0


def test_unmeasurable_no_pre_fix_occurrence(tmp_path: Path) -> None:
    """Claimed anchor is distinctive but never seen in pre-fix friction: there was no class to
    recur, so the bead is unmeasurable, not recurred."""
    _seed_workspace(tmp_path)
    fix_ts = "2026-06-01T00:00:00.000Z"
    _write_jsonl(
        _knowledge_path(tmp_path),
        [
            _machinery(
                id_="k-np",
                ts=fix_ts,
                ticket="T-nopre",
                body="MACHINERY: nopre_anchor patched.",
            )
        ],
    )
    _write_jsonl(
        _friction_path(tmp_path),
        [_friction(id_="f-np", ts="2026-06-02T00:00:00.000Z", body="nopre_anchor fired again")],
    )

    result = _compute(tmp_path)

    bead = result["beads"][0]
    assert bead["ticket"] == "T-nopre"
    assert bead["claimed_anchors"] == ["nopre_anchor"]
    assert bead["claimed_tuples"] == []
    assert bead["measurable"] is False
    assert bead["unmeasurable_reason"] == "no-pre-fix-occurrence"
    assert bead["verdict"] == "clean"
    assert result["totals"]["unmeasurable"] == 1
    assert result["totals"]["recurred"] == 0
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
                id_="f-3b-pre",
                ts="2026-05-29T00:00:00.000Z",
                body="multi_anchor_beta grounded before either fix",
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
    # fix_ts is the MIN across the bead's entries, so the earlier fix anchors the join even though
    # multi_anchor_beta's own fix landed later.
    assert bead["fix_ts"] == "2026-06-01T00:00:00.000Z"
    assert bead["claimed_anchors"] == ["multi_anchor_alpha", "multi_anchor_beta"]
    assert bead["verdict"] == "recurred"
    assert bead["post_fix_count"] == 1
    assert [r["id"] for r in bead["recurrences"]] == ["f-3b"]


def test_multi_anchor_post_fix_counts_only_tuple_matches(tmp_path: Path) -> None:
    """One claimed anchor's post-fix hit tuple-matches, the other's does not (wrong stage):
    post_fix_count counts only the matching entry."""
    _seed_workspace(tmp_path)
    fix_ts = "2026-06-01T00:00:00.000Z"
    _write_jsonl(
        _knowledge_path(tmp_path),
        [
            _machinery(
                id_="k-mm",
                ts=fix_ts,
                ticket="T-multimatch",
                body="MACHINERY: match_anchor_one and match_anchor_two patched.",
            )
        ],
    )
    _write_jsonl(
        _friction_path(tmp_path),
        [
            _friction(
                id_="f-pre-one",
                ts="2026-05-20T00:00:00.000Z",
                stage="implement",
                body="match_anchor_one grounded before fix",
            ),
            _friction(
                id_="f-pre-two",
                ts="2026-05-20T00:00:00.000Z",
                stage="implement",
                body="match_anchor_two grounded before fix",
            ),
            _friction(
                id_="f-post-one",
                ts="2026-06-02T00:00:00.000Z",
                stage="implement",
                body="match_anchor_one fired again",
            ),
            _friction(
                id_="f-post-two",
                ts="2026-06-03T00:00:00.000Z",
                stage="commit",
                body="match_anchor_two fired again elsewhere",
            ),
        ],
    )

    result = _compute(tmp_path)

    bead = result["beads"][0]
    assert bead["claimed_anchors"] == ["match_anchor_one", "match_anchor_two"]
    assert bead["verdict"] == "recurred"
    assert bead["post_fix_count"] == 1
    assert [r["id"] for r in bead["recurrences"]] == ["f-post-one"]


def test_missing_stage_type_default_empty_string_match(tmp_path: Path) -> None:
    """Friction with no stage/type keys tuple-matches on ("", "", anchor)."""
    _seed_workspace(tmp_path)
    fix_ts = "2026-06-01T00:00:00.000Z"
    _write_jsonl(
        _knowledge_path(tmp_path),
        [
            _machinery(
                id_="k-ns",
                ts=fix_ts,
                ticket="T-nostage",
                body="MACHINERY: nostage_anchor patched.",
            )
        ],
    )
    _write_jsonl(
        _friction_path(tmp_path),
        [
            {
                "id": "f-ns-pre",
                "ts": "2026-05-20T00:00:00.000Z",
                "run_id": "run-1",
                "ticket": "T-x",
                "body": "nostage_anchor grounded before fix",
            },
            {
                "id": "f-ns-post",
                "ts": "2026-06-02T00:00:00.000Z",
                "run_id": "run-1",
                "ticket": "T-x",
                "body": "nostage_anchor fired again",
            },
        ],
    )

    result = _compute(tmp_path)

    bead = result["beads"][0]
    assert bead["claimed_tuples"] == [["", "", "nostage_anchor"]]
    assert bead["verdict"] == "recurred"
    assert bead["post_fix_count"] == 1


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
    assert bead["unmeasurable_reason"] is None
    assert bead["claimed_anchors"] == ["sig_clean_anchor"]
    assert bead["claimed_tuples"] == [["implement", "RETRY", "sig_clean_anchor"]]
    assert bead["post_fix_count"] == 0
    assert result["totals"]["unmeasurable"] == 0
    assert result["totals"]["clean"] == 1


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
    assert bead["claimed_tuples"] == []
    assert bead["measurable"] is False
    assert bead["unmeasurable_reason"] == "no-distinctive-anchor"
    assert bead["verdict"] == "clean"
    assert result["totals"]["unmeasurable"] == 1
    assert result["totals"]["clean"] == 1
    assert result["totals"]["recurred"] == 0


def test_unmeasurable_no_fix_ts(tmp_path: Path) -> None:
    """A claimed anchor with a distinctive df but no usable fix timestamp cannot
    forward-join: unmeasurable with the no-fix-ts reason, not no-distinctive."""
    _seed_workspace(tmp_path)
    _write_jsonl(
        _knowledge_path(tmp_path),
        [
            _machinery(
                id_="k-nf",
                ts="",
                ticket="T-nofts",
                body="MACHINERY: no_fts_anchor patched but the fix carries no ts.",
            )
        ],
    )
    _write_jsonl(
        _friction_path(tmp_path),
        [_friction(id_="f-nf", ts="2026-05-01T00:00:00.000Z", body="no_fts_anchor also seen once")],
    )

    result = _compute(tmp_path)

    bead = result["beads"][0]
    assert bead["claimed_anchors"] == ["no_fts_anchor"]
    assert bead["fix_ts"] is None
    assert bead["measurable"] is False
    assert bead["unmeasurable_reason"] == "no-fix-ts"
    assert bead["verdict"] == "clean"
    assert result["totals"]["unmeasurable"] == 1


def test_strict_boundary_ts_equal_fix_ts_not_counted(tmp_path: Path) -> None:
    """ts == fix_ts grounds the class but never counts as a post-fix recurrence."""
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
    assert bead["claimed_tuples"] == [["implement", "RETRY", "boundary_anchor"]]
    assert bead["post_fix_count"] == 0
    assert bead["verdict"] == "clean"
    assert bead["recurrences"] == []


def test_ts_equal_fix_ts_grounds_later_recurrence(tmp_path: Path) -> None:
    """A boundary (ts == fix_ts) entry grounds the class so a strictly-later entry with the same
    triple recurs; the boundary entry itself is not counted."""
    _seed_workspace(tmp_path)
    fix_ts = "2026-06-01T00:00:00.000Z"
    _write_jsonl(
        _knowledge_path(tmp_path),
        [
            _machinery(
                id_="k-gb",
                ts=fix_ts,
                ticket="T-groundboundary",
                body="MACHINERY: ground_boundary_anchor patched.",
            )
        ],
    )
    _write_jsonl(
        _friction_path(tmp_path),
        [
            _friction(id_="f-ground", ts=fix_ts, body="ground_boundary_anchor at the fix instant"),
            _friction(
                id_="f-post",
                ts="2026-06-02T00:00:00.000Z",
                body="ground_boundary_anchor fired again",
            ),
        ],
    )

    result = _compute(tmp_path)

    bead = result["beads"][0]
    assert bead["measurable"] is True
    assert bead["verdict"] == "recurred"
    assert bead["post_fix_count"] == 1
    assert [r["id"] for r in bead["recurrences"]] == ["f-post"]


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
                id_="f-hi-pre", ts="2026-05-01T00:00:00.000Z", body="hi_pfc_anchor grounded pre-fix"
            ),
            _friction(
                id_="f-hi-1", ts="2026-06-02T00:00:00.000Z", body="hi_pfc_anchor fired again"
            ),
            _friction(
                id_="f-hi-2", ts="2026-06-03T00:00:00.000Z", body="hi_pfc_anchor fired a third time"
            ),
            _friction(
                id_="f-lo-pre", ts="2026-05-01T00:00:00.000Z", body="lo_pfc_anchor grounded pre-fix"
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
                id_="f-cli-pre",
                ts="2026-05-01T00:00:00.000Z",
                body="cli_anchor_token grounded pre-fix",
            ),
            _friction(
                id_="f-cli", ts="2026-06-02T00:00:00.000Z", body="cli_anchor_token fired again"
            ),
        ],
    )

    rc = metric.cli_main(["fix-efficacy", "--namespace", "demo", "--workspace-root", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "recurred" in out
    assert "T-cli" in out
    assert "claimed_tuples" in out
    assert "cli_anchor_token" in out
    import pytest

    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cli_render_shows_unmeasurable_reason(tmp_path: Path, capsys) -> None:
    _seed_workspace(tmp_path)
    _write_jsonl(
        _knowledge_path(tmp_path),
        [
            _machinery(
                id_="k-ur",
                ts="2026-06-01T00:00:00.000Z",
                ticket="T-unmeas",
                body="MACHINERY: unmeas_render_anchor patched alone.",
            )
        ],
    )

    rc = metric.cli_main(["fix-efficacy", "--namespace", "demo", "--workspace-root", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "no-distinctive-anchor" in out


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
                id_="f-cli2-pre",
                ts="2026-05-01T00:00:00.000Z",
                body="cli_json_anchor grounded pre-fix",
            ),
            _friction(
                id_="f-cli2", ts="2026-06-02T00:00:00.000Z", body="cli_json_anchor fired again"
            ),
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
    assert "claimed_tuples" in payload["beads"][0]


def test_json_claimed_tuples_present_and_sorted(tmp_path: Path, capsys) -> None:
    """The bead's claimed_tuples surface as JSON-friendly, ascending-sorted triples."""
    _seed_workspace(tmp_path)
    fix_ts = "2026-06-01T00:00:00.000Z"
    _write_jsonl(
        _knowledge_path(tmp_path),
        [
            _machinery(
                id_="k-st",
                ts=fix_ts,
                ticket="T-sorted",
                body="MACHINERY: zeta_sort_anchor and alpha_sort_anchor patched.",
            )
        ],
    )
    _write_jsonl(
        _friction_path(tmp_path),
        [
            _friction(
                id_="f-zeta",
                ts="2026-05-20T00:00:00.000Z",
                stage="implement",
                body="zeta_sort_anchor grounded pre-fix",
            ),
            _friction(
                id_="f-alpha",
                ts="2026-05-20T00:00:00.000Z",
                stage="commit",
                body="alpha_sort_anchor grounded pre-fix",
            ),
        ],
    )

    rc = metric.cli_main(
        ["fix-efficacy", "--namespace", "demo", "--workspace-root", str(tmp_path), "--json"]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    tuples = payload["beads"][0]["claimed_tuples"]
    assert tuples == [
        ["commit", "RETRY", "alpha_sort_anchor"],
        ["implement", "RETRY", "zeta_sort_anchor"],
    ]
    assert tuples == sorted(tuples)
    assert all(len(t) == 3 for t in tuples)


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
