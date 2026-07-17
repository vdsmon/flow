# ruff: noqa: E501
"""Render and control the attended planning review surface.

HTML and Markdown consume the same canonical plan envelope and feedback ledger.
Lavish is an interface enhancement; failure never changes gate readiness or hides
review state.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

from _atomicio import atomic_write_text
from planning_attempt import FeedbackEntry, PlanEnvelope


class ReviewError(RuntimeError):
    """The review surface cannot accept the requested transition."""


def _items(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _html_list(values: Iterable[object], *, empty: str = "None declared") -> str:
    rendered = list(values)
    if not rendered:
        return f'<p class="muted">{html.escape(empty)}</p>'
    return "<ul>" + "".join(f"<li>{html.escape(_text(item))}</li>" for item in rendered) + "</ul>"


def _markdown_list(values: Iterable[object], *, empty: str = "None declared") -> str:
    rendered = list(values)
    return "\n".join(f"- {_text(item)}" for item in rendered) if rendered else f"- {empty}"


def _scenario_html(scenarios: list[object]) -> str:
    cards: list[str] = []
    for scenario in scenarios:
        if isinstance(scenario, dict):
            before = html.escape(_text(scenario.get("before", "")))
            after = html.escape(_text(scenario.get("after", "")))
        else:
            before, after = html.escape(_text(scenario)), ""
        cards.append(
            '<article class="scenario"><div><span>Before</span><p>'
            + before
            + '</p></div><div class="arrow">→</div><div><span>After</span><p>'
            + after
            + "</p></div></article>"
        )
    return "".join(cards) or '<p class="muted">No scenario comparison declared.</p>'


def _architecture_html(values: list[object]) -> str:
    if not values:
        return '<p class="muted">No system relationships declared.</p>'
    nodes = []
    for index, item in enumerate(values):
        if index:
            nodes.append('<span class="connector" aria-hidden="true">→</span>')
        nodes.append(f'<span class="node">{html.escape(_text(item))}</span>')
    return (
        '<div class="architecture" aria-label="Plan execution sequence">'
        + "".join(nodes)
        + "</div>"
    )


def _feedback_html(feedback: Iterable[FeedbackEntry]) -> str:
    cards = []
    for item in feedback:
        reason = (
            f'<p class="reason">Reason: {html.escape(item.rejection_reason or "")}</p>'
            if item.disposition == "rejected"
            else ""
        )
        cards.append(
            '<article class="feedback"><header><code>'
            + html.escape(item.id)
            + '</code><span class="pill">'
            + html.escape(item.disposition)
            + "</span></header><blockquote>"
            + html.escape(item.verbatim)
            + "</blockquote><p><strong>Anchors</strong> "
            + html.escape(", ".join(item.anchors) or "none")
            + "</p><p><strong>Owner synthesis</strong> "
            + html.escape(item.owner_synthesis or "none")
            + "</p>"
            + reason
            + "</article>"
        )
    return "".join(cards) or '<p class="muted">No feedback has been recorded.</p>'


def render_html(
    envelope: PlanEnvelope,
    *,
    feedback: Iterable[FeedbackEntry],
    route: Mapping[str, object],
    assessment: Mapping[str, object],
    degradation: str | None,
) -> str:
    """Return a self-contained polished Lavish review companion."""
    plan = envelope.plan
    degradation_html = (
        '<div class="notice">Lavish: degraded mid-loop - ' + html.escape(degradation) + "</div>"
        if degradation
        else ""
    )
    route_text = " / ".join(
        str(route.get(key, "unknown")) for key in ("harness", "model", "effort")
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Plan review · {html.escape(envelope.attempt_id)} · v{envelope.version}</title>
<style>
:root{{--ink:#171511;--muted:#6f695f;--paper:#fbfaf7;--card:#fff;--line:#ded8ce;--accent:#125b4c;--warm:#f3ead8;--shadow:0 18px 55px rgba(50,42,30,.10)}}
*{{box-sizing:border-box;min-width:0}}
html,body{{max-width:100%;overflow-x:hidden}}
body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.6 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{width:min(1120px,calc(100% - 32px));margin:32px auto 80px}}
header.hero{{padding:clamp(28px,5vw,64px);border:1px solid var(--line);border-radius:28px;background:linear-gradient(135deg,#fff 0%,var(--warm) 100%);box-shadow:var(--shadow)}}
.eyebrow{{color:var(--accent);font-size:.78rem;font-weight:800;letter-spacing:.12em;text-transform:uppercase}}
h1{{font:700 clamp(2.2rem,6vw,5.2rem)/.98 ui-serif,Georgia,serif;max-width:13ch;margin:.22em 0}}
h2{{font:700 clamp(1.45rem,3vw,2.15rem)/1.15 ui-serif,Georgia,serif;margin:0 0 18px}}
p,li,blockquote,code{{overflow-wrap:anywhere}}
img,svg,video,canvas{{display:block;max-width:100%}}
table{{width:100%;table-layout:fixed}}
.lede{{font-size:clamp(1.05rem,2vw,1.3rem);max-width:70ch}}
.meta{{display:flex;flex-wrap:wrap;gap:10px;margin-top:24px}}
.pill{{display:inline-flex;padding:5px 10px;border:1px solid var(--line);border-radius:999px;background:#fff;font-size:.8rem;font-weight:700}}
.grid{{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:18px;margin-top:18px}}
.card{{grid-column:span 6;padding:clamp(22px,3vw,36px);border:1px solid var(--line);border-radius:22px;background:var(--card);box-shadow:0 8px 24px rgba(50,42,30,.05)}}
.wide{{grid-column:1/-1}}
.scenario{{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);gap:18px;align-items:center;padding:20px 0;border-top:1px solid var(--line)}}
.scenario:first-child{{border-top:0}}
.scenario span{{font-size:.72rem;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:var(--accent)}}
.arrow{{font-size:1.7rem;color:var(--accent)}}
.architecture{{display:flex;flex-wrap:wrap;align-items:center;gap:10px}}
.node{{flex:1 1 180px;padding:14px;border:1px solid var(--line);border-radius:12px;background:var(--warm);font-weight:700;text-align:center}}
.connector{{color:var(--accent);font-size:1.35rem;font-weight:800}}
.route{{font:700 clamp(1rem,2.6vw,1.45rem)/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;padding:18px;border-radius:14px;background:#17231f;color:#d7f1e7;white-space:pre-wrap;overflow-wrap:anywhere}}
.feedback{{padding:18px;border:1px solid var(--line);border-radius:16px;margin-top:12px}}
.feedback header{{display:flex;justify-content:space-between;gap:12px;align-items:center}}
blockquote{{margin:16px 0;padding-left:16px;border-left:3px solid var(--accent)}}
.notice{{margin:18px 0;padding:14px 18px;border-radius:14px;background:#fff2cf;border:1px solid #d6a941}}
.muted{{color:var(--muted)}}
form{{display:grid;gap:12px}}
textarea{{width:100%;min-height:120px;padding:14px;border:1px solid var(--line);border-radius:12px;font:inherit;resize:vertical}}
button{{justify-self:start;padding:11px 18px;border:0;border-radius:999px;background:var(--accent);color:#fff;font-weight:800;cursor:pointer}}
@media(max-width:760px){{.card{{grid-column:1/-1}}.scenario{{grid-template-columns:1fr}}.arrow{{transform:rotate(90deg);justify-self:start}}}}
</style>
</head>
<body>
<main>
{degradation_html}
<header class="hero">
  <div class="eyebrow">Complete plan · version {envelope.version}</div>
  <h1>Why this change exists</h1>
  <p class="lede">{html.escape(_text(plan.get("motivation", "No motivation declared.")))}</p>
  <div class="meta"><span class="pill">{html.escape(envelope.status)}</span><span class="pill">{html.escape(_text(plan.get("lane")))} lane</span><span class="pill">Base {html.escape(envelope.base_sha[:12])}</span><span class="pill">Plan {html.escape(envelope.digest[:12])}</span></div>
</header>
<section class="grid">
  <article class="card wide"><h2>Goal</h2><p class="lede">{html.escape(_text(plan.get("goal")))}</p></article>
  <article class="card wide"><h2>Before and after</h2>{_scenario_html(_items(plan.get("scenarios")))}</article>
  <article class="card wide"><h2>Execution route</h2><div class="route">{html.escape(route_text)}</div><p class="muted">Authored by {html.escape(_text(envelope.author))}. The owner cockpit remains the only writable human surface.</p></article>
  <article class="card"><h2>System relationships</h2>{_architecture_html(_items(plan.get("architecture")))}</article>
  <article class="card"><h2>Decisions</h2>{_html_list(_items(plan.get("decisions")))}</article>
  <article class="card"><h2>Acceptance outcomes</h2>{_html_list(_items(plan.get("acceptance_outcomes")))}</article>
  <article class="card"><h2>Delivery steps</h2>{_html_list(_items(plan.get("steps")))}</article>
  <article class="card"><h2>Files</h2>{_html_list(_items(plan.get("files")))}</article>
  <article class="card"><h2>Context inspected</h2>{_html_list(_items(plan.get("context_paths")))}</article>
  <article class="card"><h2>Verification</h2>{_html_list(_items(plan.get("verification")))}<p><strong>E2E recipe</strong><br>{html.escape(_text(plan.get("e2e_recipe")))}</p></article>
  <article class="card"><h2>Compatibility</h2>{_html_list(_items(plan.get("compatibility")))}<p><strong>Rollout</strong><br>{html.escape(_text(plan.get("rollout")))}</p></article>
  <article class="card"><h2>Risks</h2>{_html_list(_items(plan.get("risks")))}</article>
  <article class="card"><h2>Owner assessment</h2><p>{html.escape(_text(dict(assessment)))}</p></article>
  <article class="card wide"><h2>Feedback ledger</h2>{_feedback_html(feedback)}</article>
  <article class="card wide">
    <h2>Request a revision</h2>
    <p class="muted">Your words are relayed verbatim with their visual anchor. Use Lavish's built-in send/end control when review is complete.</p>
    <form id="feedback-form" data-lavish-action="queue-prompt">
      <label for="feedback">Feedback</label>
      <textarea id="feedback" name="feedback" placeholder="Describe the concern and the section it applies to."></textarea>
      <button type="submit">Queue feedback</button>
    </form>
  </article>
</section>
</main>
<script>
document.getElementById('feedback-form').addEventListener('submit', function(event){{
  event.preventDefault();
  const value = document.getElementById('feedback').value.trim();
  if (value && window.lavish && window.lavish.queuePrompt) {{
    window.lavish.queuePrompt(value);
    document.getElementById('feedback').value = '';
  }}
}});
</script>
</body>
</html>
"""


def render_markdown(
    envelope: PlanEnvelope,
    *,
    feedback: Iterable[FeedbackEntry],
    route: Mapping[str, object],
    assessment: Mapping[str, object],
    degradation: str | None,
) -> str:
    """Return the same review evidence as deterministic Markdown."""
    plan = envelope.plan
    scenarios = []
    for item in _items(plan.get("scenarios")):
        if isinstance(item, dict):
            scenarios.append(
                f"Before: {_text(item.get('before'))} → After: {_text(item.get('after'))}"
            )
        else:
            scenarios.append(_text(item))
    ledger = []
    for item in feedback:
        line = f"{item.id} [{item.disposition}] {item.verbatim}"
        if item.anchors:
            line += f" (anchors: {', '.join(item.anchors)})"
        if item.owner_synthesis:
            line += f"; owner synthesis: {item.owner_synthesis}"
        if item.rejection_reason:
            line += f"; reason: {item.rejection_reason}"
        ledger.append(line)
    route_text = " / ".join(
        str(route.get(key, "unknown")) for key in ("harness", "model", "effort")
    )
    prefix = f"Lavish: skipped - {degradation}\n\n" if degradation else ""
    return f"""{prefix}# Complete plan v{envelope.version}

Plan digest: `{envelope.digest}`

Base: `{envelope.base_sha}`

Route: `{route_text}`

## Why this change exists

{_text(plan.get("motivation", "No motivation declared."))}

## Goal

{_text(plan.get("goal"))}

Lane: `{_text(plan.get("lane"))}`

## Before and after

{_markdown_list(scenarios)}

## System relationships

{_markdown_list(_items(plan.get("architecture")))}

## Decisions

{_markdown_list(_items(plan.get("decisions")))}

## Acceptance outcomes

{_markdown_list(_items(plan.get("acceptance_outcomes")))}

## Delivery steps

{_markdown_list(_items(plan.get("steps")))}

## Files and context

{_markdown_list(_items(plan.get("files")) + _items(plan.get("context_paths")))}

## Verification

{_markdown_list(_items(plan.get("verification")))}

E2E recipe: {_text(plan.get("e2e_recipe"))}

## Compatibility and rollout

{_markdown_list(_items(plan.get("compatibility")))}

Rollout: {_text(plan.get("rollout"))}

## Risks

{_markdown_list(_items(plan.get("risks")))}

## Owner assessment

`{_text(dict(assessment))}`

## Feedback ledger

{_markdown_list(ledger, empty="No feedback has been recorded")}

Review feedback must be drained and frozen before the owner requests native approval.
"""


def write_review(path: Path, content: str) -> None:
    atomic_write_text(path.expanduser().resolve(), content)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render a canonical planning review artifact.")
    parser.add_argument("--envelope", required=True)
    parser.add_argument("--feedback", required=True)
    parser.add_argument("--route", required=True)
    parser.add_argument("--assessment", required=True)
    parser.add_argument("--format", choices=["html", "markdown"], required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--degradation")
    return parser


def cli_main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        envelope = PlanEnvelope.from_mapping(json.loads(Path(args.envelope).read_text()))
        feedback_data = json.loads(Path(args.feedback).read_text())
        if not isinstance(feedback_data, list) or any(
            not isinstance(item, dict) for item in feedback_data
        ):
            raise ReviewError("feedback input must be a list of objects")
        feedback = [
            FeedbackEntry.create(
                feedback_id=item["id"],
                verbatim=item["verbatim"],
                anchors=item.get("anchors", []),
                owner_synthesis=item.get("owner_synthesis", ""),
                disposition=item.get("disposition", "pending"),
                rejection_reason=item.get("rejection_reason"),
            )
            for item in feedback_data
        ]
        route = json.loads(Path(args.route).read_text())
        assessment = json.loads(Path(args.assessment).read_text())
        if not isinstance(route, dict) or not isinstance(assessment, dict):
            raise ReviewError("route and assessment inputs must be JSON objects")
        content = (
            render_html(
                envelope,
                feedback=feedback,
                route=route,
                assessment=assessment,
                degradation=args.degradation,
            )
            if args.format == "html"
            else render_markdown(
                envelope,
                feedback=feedback,
                route=route,
                assessment=assessment,
                degradation=args.degradation,
            )
        )
        write_review(Path(args.output), content)
    except (OSError, ValueError, ReviewError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"plan-review: {exc}\n")
        return 2
    sys.stdout.write(str(Path(args.output).expanduser().resolve()) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "ReviewError",
    "render_html",
    "render_markdown",
    "write_review",
]
