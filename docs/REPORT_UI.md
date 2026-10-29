# Report UI

How the HTML report (`src/compactlab/report/template.html`) is laid out, and why. It is one self-contained file:
inline CSS, JS and an SVG icon sprite, no network. Text is kept short; icons carry the meaning and every icon is
either named or hidden from assistive tech, with longer explanations in `(i)` tooltips.

Screenshots (synthetic showcase data from `scripts/make_assets.py`, never real history):
`docs/assets/report.png` (1440, light), `report-dark.png`, `report-overview.png`, `report-noop.png`,
`report-mobile.png` and `report-mobile-dark.png` (390).

## Landing state

- The report opens on the group (harness, model, role) with the largest absolute per-day money impact for the
  summary profile (`balanced` unless changed on the Overview), ties broken by turns. A group with
  `no_action_needed` is never the landing page. If every group needs no change, the Overview opens instead.
- The URL hash keeps the view: `#overview` or `#g=<harness>|<model>|<role>`; a link opens that view directly.

## Three views

| View | When | Shows |
| --- | --- | --- |
| Overview | sidebar "Overview" | summary KPIs, per-model bar chart, roll-up table of recommended settings, copy panel |
| Group | a group that compacts | sticky bar, main chart, profile cards, KPIs, comparison table, copy, details |
| No change | `no_action_needed` | one line ("never reaches the threshold"), peak chips, session-peak chart vs the default |

`body[data-view]` switches sections on and off (`.vg` group only, `.vo` overview only, `.vn` no-change only,
`.vgo` group and overview). Hidden sections are not rendered for a no-change group, so it never shows $0 cards.

## Group page: information hierarchy

1. Header: harness and family (eyebrow), display name and role (title; the full model id is the tooltip), one
   meta row (history range, source, mode, sample size, calibration).
2. Sticky bar: profile switch (cost / balanced / time / custom, ★ = recommended), the threshold under the cursor
   with live $/day and min/day chips, status icons.
3. Main chart, with the slider directly under its plot.
4. Per-day impact: three profile cards (vs default and vs current; per day / per active day / per month).
5. KPIs for the cursor position.
6. Comparison table, copy panel, then details (trade-off, time breakdown, history, lite vs deep, sessions).

Status icons: a warning triangle only for the selected profile's guardrail flags (red when a guardrail is broken,
amber for a guardrail warning such as below a quarter of the window). A guardrail that only moved the pick and all
data-confidence notes (thin data, prior timing, short history) are info icons. A no-change group has no bar and no
status icons.

## Main chart spec

- x: auto-compact threshold over the simulated grid (for example 100k to the effective max window), shared by two
  stacked panels.
- Top panel: money per day against the harness default (0 line = default). Bottom panel: wall-clock minutes per
  day against the default. Both are computed from the stored per-threshold totals with the same arithmetic as
  `compactlab.sim.select.impact`; negative saves.
- Background: histogram of where this group's sessions peak (bins of two grid steps), faint, across both panels,
  labelled "session peaks"; the hover tooltip gives the count for the bin.
- Recall floor: thresholds whose recall proxy is below the floor are a hatched zone, labelled `recall < floor`.
- Marks: default (dashed), current setting (dotted), the three profiles (thin lines with coin, scale and clock
  icons on top). The same marks are clickable buttons under the slider.
- Cursor: a vertical line with a threshold pill, dots on both curves and direct value labels (▼ saves, ▲ adds).
  Drag anywhere on the plot (pointer capture; vertical touch still scrolls the page) or use the slider, which is
  aligned to the plot's x range and is the keyboard control. Hover shows a crosshair and a tooltip.
- No legends: every series and mark is labelled in place. The chart is drawn at its box width (rebuilt on resize)
  so text stays at its real size on a phone.
- The chart is built once per group and language; dragging only moves the cursor (`moveCursor`), so the node under
  the pointer is never replaced.

## Sidebar tree rules

- Hierarchy: harness > model family > model. Family comes from the id (`claude-opus-*` → Opus, `gpt-*` → GPT,
  otherwise the first id segment).
- Display names drop date suffixes (`claude-haiku-4-5-20251001` → Haiku 4.5); the full id is the tooltip. If a
  dated and an undated id would share a name, the date stays visible.
- One row per model; its roles (main / subagent) are small chips on the row, not separate entries. Clicking the
  name opens the role with the larger impact.
- Every node has a badge: estimated money per day vs the default (summary profile) and the number of sessions.
  Nodes are sorted by summed absolute impact, then turns.
- Models whose every role needs no change go to a collapsed "No change needed" bucket at the bottom, each with a
  one-line reason (`peak 506k < 967k`).
- Folds are remembered in `localStorage` (`compactlab.fold`); without a stored choice only the path to the
  selected model is open. Search filters by name, id, family and harness and opens every match.
- "Overview" sits at the top with the total badge.
- Below 900 px the tree is a drawer under the top bar; the drawer button shows the current selection.

## Overview

- Profile switch for the summary profile (also used for the sidebar badges and sorting).
- KPIs: total money per day and minutes per day across models, models to change, groups needing no change.
- Horizontal bars per model: money saved per day, stacked main / subagent (adds in amber, to the left of zero),
  minutes per day as the direct label. Rows open the model.
- Roll-up table: every group with its profile threshold, the default, the action and the per-day numbers.

## Tests

`tests/test_report_dom.py` runs the page in headless Chrome and checks the landing rule, label normalisation,
section order, slider and chart drag (same nodes, synced cursor), profile and mark clicks, warning-icon rules,
accessible names, one language at a time, theme, sticky bar, the tree (folds, search), the Overview and the
no-change view; plus no horizontal scroll at 375, 768 and 1440 px.
