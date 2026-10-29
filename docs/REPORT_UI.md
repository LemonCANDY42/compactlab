# Report UI

How the HTML report (`src/compactlab/report/template.html`) is laid out, and why. It is one self-contained file:
inline CSS, JS and an SVG icon sprite, no network. Text is kept short; icons carry the meaning and every icon is
either named or hidden from assistive tech, with longer explanations in `(i)` tooltips.

Screenshots (synthetic showcase data from `scripts/make_assets.py`, never real history):
`docs/assets/report.png` (1440, light), `report-dark.png`, `report-overview.png`, `report-noop.png`,
`report-mobile.png` and `report-mobile-dark.png` (390), plus the sidebar at 35 models over three harnesses
(`report-tree.png` folded, `report-tree-search.png` with a search; a UI-only fixture of copied groups). The
showcase has one dominant synthetic model (Opus 5, about $620/day) among small ones, and synthetic "current" settings on Opus 5.5 (525k) and Sonnet 5.5 (625k) so the current-setting path
shows.

## Landing state

- The report opens on the group (harness, model, role) with the largest absolute per-day money impact for the
  summary profile (`balanced` unless changed on the Overview), preferring groups whose action is "set", ties
  broken by turns. A group with
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
2. Sticky bar: profile switch (cost / balanced / time / custom, ★ = recommended) and the price of time
   (`time @ $250/h`); then one answer line for the cursor position, `450k  saves $44.9 + 14.7 min per day vs
   default 967k`, each part coloured by its own sign, followed by `vs current 525k …` or `no current setting
   found · compared with the default`; a Copy button with this model's setting at that threshold (disabled when
   the action is "keep default"); status icons. On a phone the bar scrolls with the page.
3. Main chart, with the slider directly under its plot.
4. Per-day impact: three profile cards with a labelled threshold (vs default and vs current; per day / per
   active day / per month; the active-day basis only when it differs from per day).
5. Two KPIs the cards do not show: compactions per day and recall (proxy).
6. Comparison table, copy panel, then details (trade-off, time breakdown, history, lite vs deep, sessions).

Units: changes lead with a percent by default, with the per-day amount small after it; the header toggle `% | $`
swaps them and is remembered in `localStorage` (`compactlab.unit`). Cost % is the change as a share of the harness
default's window cost (list price) for that group, the engine's `d_cost_pct`; time % is the change as a share of the
default's total wall-clock, `d_time_pct`. A zero reference has no percent and the amount leads instead. Aggregates
(sidebar nodes, Overview totals and per-model bars) divide summed amounts by summed defaults, never average group
percents; the Overview total uses all groups' default cost as the denominator. In % mode the main chart plots both
panels in percent and Overview bars are sized by percent saved; the Overview table sorts by its column headers,
using the lead unit. The definitions sit in the `(i)` next to the toggle.

Sign convention: every delta (against the default or the current setting) carries both an arrow and a sign:
`▼ −$429.3/day`, `▼ −117 min` save (green), `▲ +$1.2/day`, `▲ +5.8 min` add (amber). This holds for the chart's
cursor labels and axis ticks, the tooltip, the answer line, the profile cards (including the wait / turn-time split),
the comparison table, the Overview KPIs, bars and table, the sidebar badges, and the lens mod's band and card.
Absolute figures (a window's total cost, recall, compactions per day) carry neither, so the two kinds never look
alike. Chart tooltip rows name their basis: `Window cost (total)`, `Δ cost / day vs default` (with the change in
total cost in brackets), `Δ time / day vs default`, `Compactions / day`, `Recall (proxy)`; in Chinese `窗口总成本`,
`较默认每日费用`, `较默认每日耗时`, `每日压缩次数`. Dollar figures carry an `est.` tag nearby and the header
carries a one-line "independent tool, list-price estimates" note.

Status icons: a warning triangle only for the selected profile's guardrail flags (red when a guardrail is broken,
amber for a guardrail warning such as below a quarter of the window). A guardrail that only moved the pick and all
data-confidence notes use their own glyphs (clock: prior timing, database: thin data, calendar: short history,
refresh: penalty prior, sliders: grid edge, shield: guardrail moved the pick), each with a tooltip. A no-change group has no bar and no
status icons.

## Main chart spec

- x: auto-compact threshold over the simulated grid (for example 100k to the effective max window), shared by two
  stacked panels.
- Top panel: money per day against the harness default (0 line = default). Bottom panel: wall-clock minutes per
  day against the default. Both are computed from the stored per-threshold totals with the same arithmetic as
  `compactlab.sim.select.impact`; negative saves.
- Background: histogram of where this group's sessions peak (bins of two grid steps), faint, across both panels;
  the key gives the tallest bar's count and the hover tooltip the count for the bin.
- Recall floor: thresholds whose recall proxy is below the floor are a hatched zone, labelled `recall < floor
  (proxy)`; the key says recall is a lite-mode proxy (the zone can cover the default).
- Key above the plot: session peaks, recall zone, default, current (or "no current setting"), ▼ saves / ▲ adds.
- Layout from the top: threshold pill, profile icons (pushed apart when closer than 15 px), panel title at the
  left edge, panel. Each axis keeps at least four ticks.
- Marks: default (dashed), current setting (dotted), the three profiles (thin lines with coin, scale and clock
  icons on top). The same marks are clickable buttons under the slider.
- Cursor: a vertical line with a threshold pill, dots on both curves and direct value labels (▼ saves, ▲ adds)
  with a halo, placed on the side of the dot away from the curve.
  Drag anywhere on the plot (pointer capture; vertical touch still scrolls the page) or use the slider, which is
  aligned to the plot's x range and is the keyboard control. Hover shows a crosshair and a tooltip.
- Series and marks are labelled in place; the key covers the background layers. The chart is drawn at its box
  width and rebuilt when that changes (a ResizeObserver, so a pane that was hidden or narrow at load is caught)
  so text stays at its real size on a phone. Narrow slider tracks show the profile marks as icons only.
- The chart is built once per group and language; dragging only moves the cursor (`moveCursor`), so the node under
  the pointer is never replaced.

## Sidebar tree rules

- Hierarchy: harness > model family > model. Family comes from the id (`claude-opus-*` → Opus, `gpt-*` → GPT,
  otherwise the first id segment).
- Display names drop date suffixes (`claude-haiku-4-5-20251001` → Haiku 4.5); the full id is the tooltip. If a
  dated and an undated id would share a name, the date stays visible.
- One row per model; its roles (main / subagent) are small chips on the row, not separate entries. Clicking the
  name opens the role with the larger impact.
- Every node has a badge: estimated money per day vs the default (summary profile) counting only groups whose
  action is "set", and the number of sessions (flag icon). A model that would only "keep default" shows its figure
  greyed. Nodes are sorted by that impact, then turns.
- Models whose every role needs no change go to a collapsed "No change needed" bucket at the bottom, each with a
  one-line reason per role (`peak main 416k · sub 506k < 967k`), matching the page it opens.
- Folds are remembered in `localStorage` (`compactlab.fold`); without a stored choice only the path to the
  selected model is open. Search filters by name, id, family and harness and opens every match.
- "Overview" sits at the top with the total badge.
- Below 900 px the tree is a drawer under the top bar; the drawer button shows the current selection.

## Overview

- Profile switch for the summary profile (also used for the sidebar badges and sorting).
- KPIs: money per day and minutes per day if the "set" models are applied (keep-default groups are not counted),
  then models to change, models that keep the default and models needing no change (all in models).
- Before / after, per day: two panels, cost (list price) and wall-clock (agent minutes), one row per model and a
  total row on top (summed was vs summed now of the "set" models). Grey bar = was (the harness default's per-day
  amount, all the model's groups); accent bar on top, from the same origin = now (the profile applied to its "set"
  groups); green hatch with a bracket between the two ends = saved; amber past the grey end = added. "Keep default"
  rows show only the grey bar and a chip. The label column reads `▼ −21%  −$127.5/day` (lead per the `% | $`
  toggle) and `was $619 → now $492`; a dotted leader joins short bars to it. Hover: main / subagent, and for time the
  compaction-wait vs turn-time split. A one-line legend sits above the panels; each row is also a sentence in a
  screen-reader list ("Opus 5: was $619.3 per day, now $491.8, saves 21 percent; …").
- Toggles (remembered): `absolute | each row = 100%` (one shared, labelled axis, or every grey bar the same length so
  small models are readable) and `by amount saved | by % saved` for the row order.
- The model page repeats the language in a compact pair next to the answer line: cost and time at the cursor,
  was vs now, with the legend in its tooltip and accessible name.
- Roll-up table: every group with its profile threshold, the default, the action and the per-day numbers
  (greyed unless the action is "set").
- A "keep default" row carries a one-line reason from the same two tests the engine uses
  (`sim/engine.py` `_comparison`): the profile threshold is not below the default, or its score (cost + time x
  price of time) is not below the default's, e.g. `no net gain at $250/h · money ▼ $3.7, time ▲ +1.1 min
  (▲ +$4.6) → ▲ +$0.9 per day`. The same reason shows on the model page's profile card and in the answer line
  when the cursor sits on such a threshold.

## Sessions table

On a model page it lists that group's sessions ("Sessions · Haiku 5.5 · main"); on the Overview, all sessions.

## Tests

`tests/test_report_dom.py` runs the page in headless Chrome and checks the landing rule, label normalisation,
section order, slider and chart drag (same nodes, synced cursor), profile and mark clicks, warning-icon rules,
accessible names, one language at a time, theme, sticky bar, the answer line and Copy state, current-setting
note and mark, the tree (folds, search), the Overview total (set-only) and keep-default rows, the no-change view
(no cards, filtered sessions, per-role reason); plus no horizontal scroll at 375, 768 and 1440 px.
