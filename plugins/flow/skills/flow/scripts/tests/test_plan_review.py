from __future__ import annotations

from pathlib import Path

import pytest

import plan_review as pr
import planning_attempt as pa


def _envelope() -> pa.PlanEnvelope:
    return pa.PlanEnvelope.from_mapping(
        {
            "attempt_id": "attempt-1",
            "version": 2,
            "parent_digest": "c" * 64,
            "base_sha": "a" * 40,
            "route_digest": "b" * 64,
            "author": {
                "id": "codex:gpt-5.6-sol",
                "harness": "codex",
                "model": "gpt-5.6-sol",
            },
            "status": "PLAN_READY",
            "plan": {
                "motivation": "Make a confusing codebase easier to review.",
                "goal": "Make the exact delivery plan understandable before approval.",
                "scenarios": [
                    {"before": "Planner is implicit", "after": "Route is visible and exact"}
                ],
                "architecture": ["Human → owner → planner", "Owner → native gate"],
                "decisions": ["One writable cockpit"],
                "acceptance_outcomes": ["The reviewer can explain what will change and why"],
                "steps": ["Plan", "Assess", "Approve"],
                "files": ["a/very/long/path/that/must/wrap/planning.py"],
                "context_paths": ["routing.py"],
                "verification": ["Run the full suite"],
                "e2e_recipe": "Render both review surfaces and compare their evidence.",
                "lane": "full",
                "compatibility": ["Markdown fallback preserves all review evidence"],
                "rollout": "Start behind an explicit route override.",
                "risks": ["CLI drift"],
            },
            "questions": [],
            "incorporated_feedback_ids": ["F-1"],
        }
    )


def _feedback() -> list[pa.FeedbackEntry]:
    return [
        pa.FeedbackEntry.create(
            feedback_id="F-1",
            verbatim="Show the motivation first.",
            anchors=["motivation"],
            owner_synthesis="Lead with before and after.",
            disposition="incorporated",
        )
    ]


def test_html_is_polished_complete_and_has_no_approval_control(tmp_path: Path) -> None:
    html = pr.render_html(
        _envelope(),
        feedback=_feedback(),
        route={"harness": "codex", "model": "gpt-5.6-sol", "effort": "xhigh"},
        assessment={"outcome": "pass", "assessor_id": "claude-owner"},
        degradation=None,
    )
    assert "Make a confusing codebase easier to review." in html
    assert "Planner is implicit" in html
    assert "a/very/long/path" in html
    assert "The reviewer can explain" in html
    assert "Markdown fallback" in html
    assert "Render both review surfaces" in html
    assert "Show the motivation first." in html
    assert "gpt-5.6-sol" in html
    assert "window.lavish.queuePrompt" in html
    assert "data-lavish-action" in html
    assert "overflow-wrap:anywhere" in html.replace(" ", "")
    assert "approve plan" not in html.lower()
    assert 'data-lavish-action="approve"' not in html.lower()
    assert "minute read" not in html.lower()
    assert "min read" not in html.lower()
    assert "Show the motivation first." in html
    assert "Lead with before and after." in html
    out = tmp_path / "review.html"
    pr.write_review(out, html)
    assert out.read_text(encoding="utf-8") == html


def test_markdown_fallback_is_behaviorally_equivalent_and_visible() -> None:
    markdown = pr.render_markdown(
        _envelope(),
        feedback=_feedback(),
        route={"harness": "codex", "model": "gpt-5.6-sol", "effort": "xhigh"},
        assessment={"outcome": "pass", "assessor_id": "claude-owner"},
        degradation="Lavish could not open",
    )
    for value in (
        "Make a confusing codebase easier to review.",
        "Planner is implicit",
        "a/very/long/path",
        "The reviewer can explain",
        "Markdown fallback",
        "Render both review surfaces",
        "Show the motivation first.",
        "gpt-5.6-sol",
    ):
        assert value in markdown
    assert "Lavish: skipped - Lavish could not open" in markdown
    assert "native approval" in markdown.lower()
    assert "minute read" not in markdown.lower()
    assert "min read" not in markdown.lower()
    assert "Show the motivation first." in markdown
    assert "owner synthesis: Lead with before and after." in markdown


def test_review_freeze_drains_final_feedback_and_closes_surface() -> None:
    controller = pr.ReviewController()
    controller.queue_feedback("F-1", "First", ["motivation"], "why")
    drained = controller.freeze(
        final_batch=[{"id": "F-2", "verbatim": "Second", "anchors": [], "owner_synthesis": ""}]
    )
    assert [item.id for item in drained] == ["F-1", "F-2"]
    assert controller.frozen is True
    with pytest.raises(pr.ReviewError, match="frozen"):
        controller.queue_feedback("F-3", "Late", [], "")


def test_review_freeze_is_atomic_when_final_batch_is_invalid() -> None:
    controller = pr.ReviewController()
    controller.queue_feedback("F-1", "First", ["motivation"], "why")
    with pytest.raises((pa.AttemptError, pr.ReviewError)):
        controller.freeze(
            final_batch=[
                {"id": "F-2", "verbatim": "Second", "anchors": [], "owner_synthesis": ""},
                {"id": "F-3", "verbatim": "", "anchors": [], "owner_synthesis": ""},
            ]
        )
    assert controller.frozen is False
    assert [item.id for item in controller.freeze(final_batch=[])] == ["F-1"]
