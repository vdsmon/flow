"""Tests for friction_recurrence.py.

Seeds a real workspace matching test_metric_friction.py's pattern
(`.flow/workspace.toml` + `.flow/<namespace>/{friction,knowledge}.jsonl`
[+ ship-events]).
"""

from __future__ import annotations

import json
from pathlib import Path

import friction_recurrence as fr


def _seed_workspace(root: Path, namespace: str = "demo") -> None:
    flow = root / ".flow"
    (flow / namespace).mkdir(parents=True, exist_ok=True)
    (flow / "workspace.toml").write_text(
        f'[tracker]\nbackend = "jira"\n\n[memory]\nnamespace = "{namespace}"\n',
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


def _machinery(*, id_: str, ts: str, ticket: str = "T-fix", body: str) -> dict:
    return {"id": id_, "ts": ts, "ticket": ticket, "type": "LEARNED", "body": body}


# --- anchors() ---------------------------------------------------------------


def test_anchors_file_vs_snake_dedup_and_special_token():
    text = "saw create_pr.py fail; create_pr also referenced; @{U} tripped"
    result = fr.anchors(text)
    assert result == {"create_pr.py", "@{u}"}


def test_anchors_ignores_flags_and_kebab():
    text = "ran create_pr.py with --workspace-root set; push-state guard tripped"
    result = fr.anchors(text)
    assert result == {"create_pr.py"}


def test_anchors_casing_normalized():
    assert fr.anchors("Version_Remerge.PY") == {"version_remerge.py"}


# --- document_frequencies / distinctive_anchors -------------------------------


def test_df_band_boundaries():
    anchor_sets = (
        [{"a"}]  # df=1, dropped (too rare)
        + [{"b"} for _ in range(2)]  # df=2, kept (lower bound)
        + [{"d"} for _ in range(15)]  # df=15, kept (upper bound)
        + [{"c"} for _ in range(16)]  # df=16, dropped (too common)
    )
    df = fr.document_frequencies(anchor_sets)
    assert df == {"a": 1, "b": 2, "d": 15, "c": 16}
    assert fr.distinctive_anchors(df) == {"b", "d"}


def test_exempt_anchor_skips_df_ceiling_not_floor():
    df = {"hot": 20, "noise": 20, "rare": 1}
    assert fr.distinctive_anchors(df, {"hot", "rare"}) == {"hot"}


# --- keystone contrast: signature unifies what structural fragments ----------


def test_signature_unifies_structural_fragments(tmp_path: Path):
    _seed_workspace(tmp_path)
    fix_ts = "2026-06-01T00:00:00.000Z"
    machinery = [
        _machinery(
            id_="fix-1",
            ts=fix_ts,
            ticket="T-fix",
            body="MACHINERY: signature_bug was patched. Fix (commit abc1234).",
        )
    ]
    friction = [
        _friction(
            id_="f-1",
            ts="2026-06-02T00:00:00.000Z",
            stage="merge",
            type_="RECONCILE",
            body="signature_bug showed up again during merge",
        ),
        _friction(
            id_="f-2",
            ts="2026-06-03T00:00:00.000Z",
            stage="implement",
            type_="DRIFT",
            body="signature_bug reappeared while implementing",
        ),
    ]
    _write_jsonl(tmp_path / ".flow" / "demo" / "friction.jsonl", friction)
    _write_jsonl(tmp_path / ".flow" / "demo" / "knowledge.jsonl", machinery)

    payload = fr.analyze(tmp_path, "demo")

    sig = [c for c in payload["signature_classes"] if c["anchor"] == "signature_bug"]
    assert len(sig) == 1
    assert sig[0]["post_fix_count"] == 2
    assert sig[0]["stages"] == ["implement", "merge"]
    assert sig[0]["types"] == ["DRIFT", "RECONCILE"]

    struct = [c for c in payload["structural_classes"] if c["anchor"] == "signature_bug"]
    assert len(struct) == 2
    assert {c["post_fix_count"] for c in struct} == {1}
    assert {(c["stage"], c["type"]) for c in struct} == {
        ("merge", "RECONCILE"),
        ("implement", "DRIFT"),
    }


# --- no-post-fix negatives -----------------------------------------------------


def test_no_post_fix_dropped_entirely(tmp_path: Path):
    _seed_workspace(tmp_path)
    fix_ts = "2026-06-01T00:00:00.000Z"
    machinery = [
        _machinery(
            id_="fix-2",
            ts=fix_ts,
            ticket="T-fix2",
            body="MACHINERY: stale_flag cleaned up. Fix (commit bbbbbbb).",
        )
    ]
    friction = [
        _friction(
            id_="f-early",
            ts="2026-05-01T00:00:00.000Z",
            stage="merge",
            type_="RECONCILE",
            body="stale_flag lingering before the fix",
        )
    ]
    _write_jsonl(tmp_path / ".flow" / "demo" / "friction.jsonl", friction)
    _write_jsonl(tmp_path / ".flow" / "demo" / "knowledge.jsonl", machinery)

    payload = fr.analyze(tmp_path, "demo")
    assert not any(c["anchor"] == "stale_flag" for c in payload["signature_classes"])
    assert not any(c["anchor"] == "stale_flag" for c in payload["structural_classes"])


def test_structural_drops_dead_bucket_but_signature_keeps_alive_one(tmp_path: Path):
    _seed_workspace(tmp_path)
    fix_ts = "2026-06-01T00:00:00.000Z"
    machinery = [
        _machinery(
            id_="fix-3",
            ts=fix_ts,
            ticket="T-fix3",
            body="MACHINERY: mixed_case timing issue resolved. Fix (commit ccccccc).",
        )
    ]
    friction = [
        _friction(
            id_="f-dead",
            ts="2026-05-01T00:00:00.000Z",
            stage="merge",
            type_="RECONCILE",
            body="mixed_case glitch before the fix",
        ),
        _friction(
            id_="f-alive",
            ts="2026-06-02T00:00:00.000Z",
            stage="implement",
            type_="DRIFT",
            body="mixed_case glitch again after the fix",
        ),
    ]
    _write_jsonl(tmp_path / ".flow" / "demo" / "friction.jsonl", friction)
    _write_jsonl(tmp_path / ".flow" / "demo" / "knowledge.jsonl", machinery)

    payload = fr.analyze(tmp_path, "demo")

    sig = [c for c in payload["signature_classes"] if c["anchor"] == "mixed_case"]
    assert len(sig) == 1
    assert sig[0]["class_size"] == 2
    assert sig[0]["post_fix_count"] == 1

    struct = [c for c in payload["structural_classes"] if c["anchor"] == "mixed_case"]
    assert len(struct) == 1
    assert struct[0]["stage"] == "implement"
    assert struct[0]["type"] == "DRIFT"
    assert struct[0]["post_fix_count"] == 1


def test_machinery_anchor_survives_df_ceiling_end_to_end(tmp_path: Path):
    _seed_workspace(tmp_path)
    machinery = [
        _machinery(
            id_="fix-hot",
            ts="2026-06-01T00:00:00.000Z",
            ticket="T-hot",
            body="MACHINERY: hot_class patched. Fix (commit ddddddd).",
        )
    ]
    friction = [
        _friction(
            id_=f"f-hot-{i}",
            ts=f"2026-06-{i + 2:02d}T00:00:00.000Z",
            body="hot_class fired again; also noise_token present",
        )
        for i in range(20)
    ]
    _write_jsonl(tmp_path / ".flow" / "demo" / "friction.jsonl", friction)
    _write_jsonl(tmp_path / ".flow" / "demo" / "knowledge.jsonl", machinery)

    payload = fr.analyze(tmp_path, "demo")
    sig = [c for c in payload["signature_classes"] if c["anchor"] == "hot_class"]
    assert len(sig) == 1
    assert sig[0]["post_fix_count"] == 20
    # same DF, no MACHINERY claim: the ceiling still drops it
    assert not any(c["anchor"] == "noise_token" for c in payload["signature_classes"])


# --- malformed-field hardening ---------------------------------------------------


def test_null_body_line_tolerated(tmp_path: Path):
    _seed_workspace(tmp_path)
    machinery = [
        _machinery(
            id_="fix-ok",
            ts="2026-06-01T00:00:00.000Z",
            ticket="T-ok",
            body="MACHINERY: guarded_token patched. Fix (commit eeeeeee).",
        ),
        {"id": "k-null", "ts": "2026-06-01T00:00:00.000Z", "ticket": "T-null", "body": None},
    ]
    friction = [
        _friction(
            id_="f-ok",
            ts="2026-06-02T00:00:00.000Z",
            body="guarded_token fired again",
        ),
        {"id": "f-null", "ts": "2026-06-02T00:00:00.000Z", "body": None},
    ]
    _write_jsonl(tmp_path / ".flow" / "demo" / "friction.jsonl", friction)
    _write_jsonl(tmp_path / ".flow" / "demo" / "knowledge.jsonl", machinery)

    payload = fr.analyze(tmp_path, "demo")
    sig = [c for c in payload["signature_classes"] if c["anchor"] == "guarded_token"]
    assert len(sig) == 1
    assert sig[0]["post_fix_count"] == 1


def test_ts_less_fix_cannot_anchor_forward_join(tmp_path: Path):
    _seed_workspace(tmp_path)
    machinery = [
        {"id": "fix-nots", "ticket": "T-nots", "body": "MACHINERY: orphan_token patched."},
    ]
    friction = [
        _friction(id_="f-1", ts="2026-06-02T00:00:00.000Z", body="orphan_token fired"),
        _friction(id_="f-2", ts="2026-06-03T00:00:00.000Z", body="orphan_token fired again"),
    ]
    _write_jsonl(tmp_path / ".flow" / "demo" / "friction.jsonl", friction)
    _write_jsonl(tmp_path / ".flow" / "demo" / "knowledge.jsonl", machinery)

    payload = fr.analyze(tmp_path, "demo")
    assert not any(c["anchor"] == "orphan_token" for c in payload["signature_classes"])
    assert not any(c["anchor"] == "orphan_token" for c in payload["structural_classes"])


# --- fix_sha: both evidence branches -------------------------------------------


def test_fix_sha_inline_branch():
    entry = {
        "id": "m1",
        "ts": "2026-06-01T00:00:00.000Z",
        "ticket": "T-1",
        "type": "LEARNED",
        "body": "MACHINERY: patched. Fix (commit abc1234).",
    }
    assert fr.fix_sha(entry, Path("."), "demo") == "abc1234"


def test_fix_sha_ship_event_fallback(tmp_path: Path):
    _seed_workspace(tmp_path)
    ship_dir = tmp_path / ".flow" / "demo" / "ship-events"
    ship_dir.mkdir(parents=True)
    (ship_dir / "T-2.json").write_text(
        json.dumps({"evidence": {"commit_sha": "deadbee123"}}), encoding="utf-8"
    )
    entry = {
        "id": "m2",
        "ts": "2026-06-01T00:00:00.000Z",
        "ticket": "T-2",
        "type": "LEARNED",
        "body": "MACHINERY: patched with no inline sha.",
    }
    assert fr.fix_sha(entry, tmp_path, "demo") == "deadbee123"


def test_fix_sha_no_evidence_at_all(tmp_path: Path):
    _seed_workspace(tmp_path)
    entry = {
        "id": "m3",
        "ts": "2026-06-01T00:00:00.000Z",
        "ticket": "T-none",
        "type": "LEARNED",
        "body": "MACHINERY: patched, evidence lost.",
    }
    assert fr.fix_sha(entry, tmp_path, "demo") is None


# --- determinism ----------------------------------------------------------------


def test_deterministic_ordering(tmp_path: Path):
    _seed_workspace(tmp_path)
    machinery = [
        _machinery(
            id_="m-bbb",
            ts="2026-06-01T00:00:00.000Z",
            ticket="T-b",
            body="MACHINERY: bbb_token issue. Fix (commit b000001).",
        ),
        _machinery(
            id_="m-aaa",
            ts="2026-06-01T00:00:00.000Z",
            ticket="T-a",
            body="MACHINERY: aaa_token issue. Fix (commit a000001).",
        ),
    ]
    friction = [
        _friction(
            id_="f-b2",
            ts="2026-06-03T00:00:00.000Z",
            stage="merge",
            type_="DRIFT",
            body="bbb_token trouble again",
        ),
        _friction(
            id_="f-b1",
            ts="2026-06-02T00:00:00.000Z",
            stage="merge",
            type_="DRIFT",
            body="bbb_token trouble",
        ),
        _friction(
            id_="f-a1",
            ts="2026-06-02T00:00:00.000Z",
            stage="implement",
            type_="RETRY",
            body="aaa_token trouble",
        ),
    ]
    _write_jsonl(tmp_path / ".flow" / "demo" / "friction.jsonl", friction)
    _write_jsonl(tmp_path / ".flow" / "demo" / "knowledge.jsonl", machinery)

    payload = fr.analyze(tmp_path, "demo")

    sig = payload["signature_classes"]
    assert [c["anchor"] for c in sig] == ["bbb_token", "aaa_token"]
    assert sig[0]["post_fix_count"] == 2
    assert [r["id"] for r in sig[0]["recurrences"]] == ["f-b1", "f-b2"]
    assert sig[1]["recurrences"][0]["id"] == "f-a1"

    struct = payload["structural_classes"]
    assert [c["anchor"] for c in struct] == ["bbb_token", "aaa_token"]


# --- committed-fixture invariant ------------------------------------------------


def test_committed_fixture_invariant(tmp_path: Path):
    corpus_path = Path(__file__).parent / "friction_recurrence_corpus.json"
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    namespace = "demo"
    _seed_workspace(tmp_path, namespace)
    _write_jsonl(tmp_path / ".flow" / namespace / "friction.jsonl", corpus["friction"])
    _write_jsonl(tmp_path / ".flow" / namespace / "knowledge.jsonl", corpus["knowledge"])
    ship_dir = tmp_path / ".flow" / namespace / "ship-events"
    ship_dir.mkdir(parents=True, exist_ok=True)
    for ticket, payload in corpus.get("ship_events", {}).items():
        (ship_dir / f"{ticket}.json").write_text(json.dumps(payload), encoding="utf-8")

    result = fr.analyze(tmp_path, namespace)
    sig = result["signature_classes"]
    struct = result["structural_classes"]

    assert len(sig) >= 1
    assert any(any(f["fix_sha"] for f in c["fixes"]) for c in sig)

    struct_max_by_anchor: dict[str, int] = {}
    for c in struct:
        struct_max_by_anchor[c["anchor"]] = max(
            struct_max_by_anchor.get(c["anchor"], 0), c["post_fix_count"]
        )
    assert any(c["post_fix_count"] >= struct_max_by_anchor.get(c["anchor"], 0) for c in sig)


# --- CLI smoke ------------------------------------------------------------------


def test_cli_happy_prints_json(tmp_path: Path, capsys):
    _seed_workspace(tmp_path)
    rc = fr.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"signature_classes": [], "structural_classes": []}


def test_cli_no_workspace_toml(tmp_path: Path, capsys):
    rc = fr.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 4
    assert "workspace.toml" in capsys.readouterr().err
