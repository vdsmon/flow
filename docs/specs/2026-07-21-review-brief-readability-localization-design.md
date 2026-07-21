# Review Brief readability and localization

Status: approved for implementation on 2026-07-21.

## Motivation

The Review Brief is useful, but the current renderer makes dense reviews harder than
the Forge: the desktop document is narrow, code is small, the navigation rail scrolls
away and cannot collapse, code evidence shows only the head snapshot, and long system-map
labels overlap. Its fixed English interface also clashes with briefs authored in
Portuguese.

The fix belongs in the existing deterministic renderer and stylesheet. It must not add
fields to the authored content model, external assets, or a JavaScript framework.

## Design

### Layout and navigation

- Raise the desktop shell ceiling to 1640px and let content use the
  available width instead of stopping at 930px.
- Keep the navigation rail at the viewport edge while the document scrolls.
- Make the rail collapsible with native HTML disclosure behavior and CSS. The expanded
  state shows the current navigation; the collapsed state leaves a narrow reopen control.
- Keep the rail absent on mobile, where document width remains viewport-bound.

### Focused diff evidence

- Resolve the PR base branch from Forge metadata and derive the merge base locally.
- For each authored evidence range, parse the file's unified diff and retain the hunks
  that intersect that range.
- Render additions with a green background and `+`, deletions with a red background and
  `-`, and context lines neutrally. Show the relevant old or new line number.
- If the evidence range did not change, preserve the current commit-pinned source excerpt
  as neutral context. The Forge remains the source of truth for the complete diff.
- Increase code typography and allocate most of each evidence card's width to code.

### System map

- Increase node width and height, wrap labels into multiple SVG `tspan` rows, and size
  column/row spacing from the new node dimensions.
- Cap pathological labels with an ellipsis after three lines. The full
  authored label remains available to assistive technology.
- Preserve horizontal scrolling for maps that genuinely exceed the viewport.

### Language inference

- Infer `pt-BR` or English from the dominant natural-language prose in the authored
  content. Score prose tokens and Portuguese diacritics; ignore code paths and source
  excerpts. Portuguese must lead the English score by at least two points; ties and
  weaker signals use English.
- Select all renderer-owned copy from one internal locale catalog: document language,
  navigation, headings, badges, scenario labels, links, notes, facts, and footer.
- Leave authored prose unchanged. No locale field or schema-version change is introduced.

## Verification

- Unit tests cover language inference, translated renderer copy, merge-base diff parsing,
  added/deleted/context rendering, unchanged-range fallback, and wrapped map labels.
- Playwright covers a wide desktop viewport and a mobile viewport, page overflow,
  readable code font size, rail collapse/expand and sticky position, map label bounds,
  diff colors/markers, console health, and serious accessibility violations.
- The in-app Browser had no available browser instance; the user explicitly approved
  Flow's existing Playwright suite as the visual-validation fallback.

## Out of scope

- Translating authored prose.
- Adding automatic translation or a language-detection dependency.
- Replacing the Review Brief design system or adding a frontend framework.
- Reproducing every Forge diff feature or displaying the entire PR diff.

## Rendering correction

Visual review of the real Perfin brief exposed four defects in the first implementation:

- The diff row class `context` collided with the narrative `.context` layout rule, adding
  large vertical margins between unchanged lines. Render unchanged rows with a dedicated
  `unchanged` class instead.
- A row's background stopped at the visible code viewport while its text continued into
  horizontal overflow. Place all rows in one max-content-width wrapper and stretch every
  row across that shared width so added and deleted colors paint the complete line.
- The old `highlight_lines` metadata added an unexplained light-green gutter on top of the
  real diff semantics. Remove that field from validation, the provider schema, authoring
  instructions, fixtures, and rendering. No compatibility layer is required.
- The expanded navigation toggle was visually louder than the navigation itself. Use an
  icon-only control in both states while retaining an accessible action label.

Regression coverage must verify consecutive 26px diff rows without narrative margins,
full-width added/deleted paint after horizontal scrolling, absence of the decisive gutter,
and icon-only collapse/expand behavior.

## Typography rebalance

Visual review of the corrected real Perfin brief showed that the document still required
browser zoom for comfortable reading, while the display title consumed too much of the
first viewport. Apply one explicit scale across the whole brief:

- Cap the desktop title at 48px and use approximately 36px on mobile.
- Set lead/outcome prose to 20px and main narrative prose to 16–18px.
- Keep card bodies, scenarios, checks, and lists at 16px or larger.
- Keep metadata and secondary labels at 13–14px or larger.
- Render code at 15px with a comfortable line height.
- Render sidebar navigation at 13px.
- Render system-map labels at 15px and map kinds at 12px.

Remove the footer from the renderer, locale catalog, and stylesheet rather than hiding
it. It does not help review decisions and competes with the document's closing content.

The approved real-content preview is
`/private/tmp/perfin-typography-preview-v2.html`. Regression coverage must assert the
important computed sizes at desktop and mobile widths, the absence of a footer, and no
new page-level overflow.
