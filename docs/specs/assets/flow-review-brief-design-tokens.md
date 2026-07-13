# Flow review brief design tokens

Source: approved editorial review-brief concept, 2026-07-13.

## Palette

| Role | Value |
|---|---|
| Paper | `#fbfaf6` |
| Ink | `#18221d` |
| Muted text | `#66716b` |
| Rule | `#dcded6` |
| Moss | `#274f3f` |
| Moss soft | `#e8f0ea` |
| Rust | `#9d4736` |
| Rust soft | `#f7e9e4` |
| Gold | `#b58a42` |
| Dark map | `#202b25` |

## Typography

- Editorial headings and narrative emphasis: `Georgia, "Times New Roman", serif`.
- UI chrome and labels: system sans stack.
- Code: system monospace stack.
- Headline: 34–58px responsive, 1.02 line height, restrained negative tracking.
- Narrative deck: 18px, 1.58 line height.
- Utility labels: 10–12px with deliberate weight and tracking.

## Geometry and rhythm

- Outer document: 24px radius in the concept; the generated file may use the viewport
  as its outer surface while preserving the inner geometry.
- Cards and maps: 11–17px radii, one-pixel neutral borders, restrained elevation.
- Desktop: 190px sticky rail plus a narrative column no wider than 890px.
- Main-column padding: approximately 52px horizontal and vertical.
- Section rhythm: 44–48px between major sections, 12–20px within groups.
- Mobile breakpoint: 760px; hide the rail, stack scenarios/code, use 24px gutters.

## Component families

- Snapshot bar with Flow mark, PR identity, exact SHA, and neutral snapshot state.
- Editorial hero with one contextual label, one title, and one narrative deck.
- Motivation observation paired with factual change metadata.
- Symmetric before/after scenarios with numbered causal steps.
- Dark relevant-system map with gold emphasis on changed nodes.
- Guarantee cards whose text states invariants rather than vanity metrics.
- Focused evidence blocks pairing a claim with exact code and a Forge link.

## Constraints

- The document stays calm, editorial, and evidence-led; it is not a dashboard.
- Metrics appear only when they prove a claim.
- No generic icon row, bento grid, decorative chart, glow, or filler badge.
- The HTML remains complete without JavaScript or network access.
- Dark mode preserves semantic moss/rust/gold roles and contrast.
- Print output retains the narrative order and readable code wrapping.
