# Review Brief typography implementation plan

Status: implemented on 2026-07-21
Design: `docs/specs/2026-07-21-review-brief-readability-localization-design.md`

## Outcome

Apply the approved whole-document typography scale to the existing Review Brief
renderer, remove the footer as dead presentation, and lock the result with computed-style
and responsive visual regressions. Do not add a new configuration surface or content
schema field.

## Implementation sequence

1. In `plugins/flow/skills/flow/scripts/review_brief.py`, remove the English and
   Portuguese footer copy and the rendered footer element.
2. In `plugins/flow/skills/flow/scripts/assets/review_brief.css`, replace the scattered
   small type sizes with the approved scale: 48/36px title, 20px lead, 18px narrative,
   16px card and list bodies, 13–14px metadata, 15px code, 13px navigation, and 15/12px
   system-map labels. Delete the unused footer rules.
3. In `plugins/flow/skills/flow/scripts/tests/test_review_brief.py`, assert that rendered
   artifacts contain no footer.
4. In `plugins/flow/skills/flow/scripts/ui-tests/review-brief.spec.mjs`, assert the
   important desktop and mobile computed sizes, footer absence, existing diff geometry,
   and page overflow behavior.
5. Regenerate and inspect the Playwright goldens, then render the real Perfin content and
   verify it at desktop and mobile widths with no console errors.

## Verification

Run the focused Python tests, the Review Brief Playwright suite, the repository's
configured formatting/lint/type checks for touched files, and `git diff --check`. Reopen
the regenerated real-content artifact for final user review.

Completed verification: 25 focused Python tests, five Playwright desktop/mobile/locale/
no-JavaScript/print tests, Ruff, formatting, and the pinned type checker all pass. The
real Perfin brief has no page overflow, console warnings, network requests, map-label
overflow, legacy highlight rows, or footer at desktop and mobile widths.
