# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Changed
- `compactlab serve` is quiet: one in-place progress line only on a terminal (cut to its width at a space, so it
  never wraps into extra rows), nothing otherwise; the log says `recalibrating (reason)` and `recalibrated in Ns:
  N files, M cached, K parsed, X groups; next calibration: <time>`, and the next calibration at start.
- **Before / after on the Overview.** Per model, a grey "was" bar (default) with the "now" bar (profile) overlaid,
  the saving hatched green and additions amber, for cost and wall-clock per day; a total row from summed amounts;
  `absolute | each row = 100%` and sort toggles; screen-reader sentences. The model page shows a compact pair.
  A dumbbell style (was ring → now dot, green or amber line) shares everything; default from nine rows up.
- **Percent first.** Changes lead with their percent of the harness default (cost vs the default's window cost,
  time vs its total wall-clock: the engine's `d_cost_pct` / `d_time_pct`) with the per-day amount secondary, in the
  report (header toggle `% | $`, remembered) and the lens mod (`▼−61% (−$204.8/day)`). lens.json gains `cost_pct` /
  `time_pct`. Totals divide summed amounts by summed defaults; zero references fall back to amounts.
- **Signed deltas everywhere.** Every change against the default or current setting shows arrow and sign
  (`▼ −$429.3/day`, `▲ +5.8 min`) in the report and the lens mod; absolute figures show neither. Chart tooltip rows
  name their basis (window total vs per-day delta), in English and Chinese.
- **Report review fixes.** Overview totals count only groups whose action is "set" (keep-default rows greyed, also in
  sidebar badges); one answer line with a Copy button in the sticky bar replaces the money and time KPIs; ▼ saves /
  ▲ adds instead of minus signs, with a key on the chart; chart labels de-overlapped (halos, side away from the
  curve, panel titles at the left, at least four ticks); current setting shown or "no current setting found";
  sessions filtered to the group; per-role no-change reasons; labelled thresholds, session counts and note glyphs;
  phone captions restored; darker-mode contrast; charts refit with a ResizeObserver.
- **Report layout.** Opens on the model with the largest per-day impact (never one that needs no change). Each
  model page leads with one chart: $/day and min/day vs the default over the threshold axis, session peaks behind,
  the recall-floor zone hatched, marks for default, current and the three profiles, and a draggable cursor with the
  slider under it; then profile cards, KPIs and the table. Sidebar is a tree (harness > family > model, role chips,
  impact badges, search, remembered folds, a collapsed "No change needed" bucket, drawer on phones) with an
  Overview page (per-model bars and a roll-up of recommended settings). Models that never compact get a short
  state and a peak chart instead of $0 cards. Warning triangles only for real guardrail flags. See
  docs/REPORT_UI.md.
- **Profiles replace tiers.** Three profiles, always computed side by side: `cost` ($10/h of agent time),
  `balanced` (default, $250/h) and `time` ($600/h). `--profile` on simulate / report / recommend / apply / retune
  picks the one to lead with or write; `--value-of-time` overrides its price of time and re-selects from a saved
  result without replaying. `standard` / `headroom`, `--tier`, `--headroom`, `--headroom-cap` and `--view` are gone
  (`--max-threshold` replaces `--headroom-cap`). Result schema 4.
- Why: with $10/h the old `headroom` pick on real data compacted several times more often than the default and
  added many hours of compaction wait per month.
- Guardrails: `balanced` never below 40% of the effective max window, `time` never below 50%; both keep added
  compaction wait under 30 min/day vs the default (`--wait-cap`); `cost` warns instead. Recall floor kept.
- Low-confidence latency fits (fewer than 20 timed compactions, thin turn bins) no longer drive the choice: the
  group uses the pooled harness fit or a pessimistic prior, and says which.
- "keep default" also when a profile does not beat the default on its own score.
- Comparisons now include minutes/day, added compaction wait/day and compactions/day, against default and current.
- Report: profile switch, three profiles side by side with flags, copy panel per profile. CLI/report state that
  costs are list-price estimates and recall is a lite-mode proxy.
- Report UI: the threshold slider can be dragged continuously (the control bar is built once and updated in place
  instead of being re-rendered on every input event); sticky control bar; slider marks for the three profiles,
  the default and the current setting (click to snap); hover/tap tooltips on the charts (click selects); focus and
  scroll kept across re-renders; copy button feedback and a file:// fallback; 44px touch targets on narrow or
  touch screens; reduced-motion and focus-ring support; no horizontal scroll at 375 / 768 / 1440 px. Regression
  tests run the report in headless Chrome (skipped when Chrome is absent).
- Report redesign: warm off-white / near-black themes from CSS tokens (system preference plus a manual
  Auto/Light/Dark toggle), one clay accent, hairline borders, a system serif for titles and tabular mono numbers,
  a group sidebar that becomes a top bar on phones, a header with date range, data source and mode, KPI cards,
  calmer charts with direct labels instead of legends, and plain-language callouts for thin data, short histories,
  borrowed time models and guardrail flags (red only when a guardrail is broken). States it is an independent
  open-source tool. Still one offline file; slider, sticky bar, tooltips and a11y behaviour unchanged.
- Report shows less text: inline SVG icons (coin / scale / clock for the profiles, refresh, shield, calendar),
  delta chips with ▲▼, gauges for recall and for the threshold as a share of the max window, ⓘ and ⚠ icon buttons
  with tooltips (also their accessible names) instead of paragraphs and banners, one language at a time
  (browser language by default), and a one-line footer disclaimer. Tests check every icon is named or hidden.
- docs/assets adds report-dark.png, report-mobile.png and report-mobile-dark.png.
- Headless Chrome in tests and `scripts/make_assets.py` runs with `--use-mock-keychain` so it never asks for the
  macOS keychain.
- Synthetic demo latencies are closer to real logs (compaction ≈ 90 s + 0.4 s per 1k tokens).

### Fixed
- compactlab-lens: the band wrapped mid-token in narrow terminals (`┃525` / `k`). Rows are now packed to the band's
  real width (`bodyColumns`), counting wide glyphs as two columns, and break only between whole pieces. The compact
  form starts below 60 columns and the two-row form below about 40. The unfolded card follows the same rules, and
  tests cover 30–120 columns in both languages.

### Added
- **Service mode.** `compactlab serve` is a stdlib-only service that binds loopback only (other binds are refused
  without `--unsafe-bind`) and checks the Host header. It serves the live report, `/api/result`,
  `/api/profile/<name>` and `/api/health`, plus a token-guarded `POST /api/recalibrate`. It recalibrates in lite mode
  every `--every` (s/m/h/d/w or off): incremental scan, single-flight, backoff after failures, lower priority, a
  rotating log, no network.
- `compactlab recalibrate` (one run).
- `compactlab schedule print|install|status|uninstall` for launchd and systemd user units. It writes only into the
  user's unit folder, never loads them, and never overwrites a foreign file.
- `compactlab consent set|show|clear` and `--auto-apply` / `--dry-run`. Auto-apply has minimum history, a low-confidence
  skip, a dead-band and minimum benefit, a 100k max step, a 7-day cooldown, guardrail checks per step and manual-edit
  detection (`notify`, never overwrite). It reuses apply's backup and manifest, adds a change journal, and `revert`
  understands it.
- The report shows calibration age and next run, a recalibrate button when served, and a drift marker. The lens mod
  shows calibration age (amber when stale). See docs/SERVICE.md.
- Larger default picks: `balanced` now prices time at $250/h with a 40% minimum window, `time` at $600/h with 50%
  (were $100/h with 25% and $400/h with 35%). The report shows a guardrail that merely moved the pick as an info
  icon instead of a warning; warnings are reserved for flags that need attention.
- **compactlab-lens**, a Claude Code mod in `mods/compactlab-lens` (with `.claude-plugin/marketplace.json` at the
  repository root). It shows one row above the prompt, expandable, and a pane on desktop: this session's context as a
  dot bar with ticks at the default and the profile threshold, a context-per-turn sparkline (an SVG on desktop),
  $/day and min/day chips vs default and current, compactions so far, a ⚠ when the window in force differs, a
  profile switch that only changes the display, and a button that copies `/autocompact <n>`. It is read only: one
  local file and the engine's own figures, with no network, no model calls and no settings writes. It is an
  independent project, not affiliated with Anthropic. See docs/MOD.md.
- `simulate --lens-out` (default `~/.compactlab/lens.json`): the small per-model summary the mod reads.
- **Per-day impact** for every profile of every group, vs the harness default and vs the current setting: USD,
  minutes (compaction wait and turn-speed gain separately) and compactions, per calendar day, per active day and
  per month. In the report (new section, basis switch), in `recommend` (table after the snippets, `impact` in
  `--json`) and in the result (`comparison.<profile>.vs_default.impact`). Groups record `active_days` and `span`.
- Time dimension: turn-latency and compaction-latency fits from log timestamps, wall-clock per threshold
  (saved by smaller contexts vs added by compactions and re-discovery).
- Baselines: every profile compared with the harness default (`data/harness_defaults.json`, with sources) and with the
  current configured value; "no action needed" / "keep default"; `apply` skips those.
- `compactlab recommend`: ready-to-paste settings per harness, scope (global / project / agent), profile and format
  (json / toml / yaml / env / shell), with merge one-liners, evidence and revert hints; `--copy`, `--check`;
  per-adapter recommenders; config facts with sources and verified-on dates in `data/config_knobs.json`;
  "Copy settings" panel in the report.
- Bilingual output by default: CLI summary and HTML report in Chinese and English (`--lang`, report switch).

### Changed
- Renamed the opt-in mode `hyper` to `deep`.

## [0.1.0] - unreleased

### Added
- Claude Code and Codex adapters with resumable parsers, unknown-event counting and fork/inherited-prefix handling.
- Incremental on-disk scan cache keyed by size, mtime, head hash and byte offset.
- Replay simulator: cache read/write/output costs, long-context tiers, cache TTL/idle effects, summary size,
  post-compaction base context, re-discovery penalty calibrated from observed compactions.
- Two result tiers per model and role: `standard` (optimum) and `headroom` (default).
- Two modes: `lite` (offline, default) and opt-in `deep` (re-summarize at candidate thresholds, probes, LLM judge,
  estimate + `--yes` + hard `--budget`; Claude Code / Codex executors).
- Self-contained HTML report: gears, Pareto frontier, cost curves, token distribution, history-vs-replay overlay,
  lite-vs-deep comparison, sessions table.
- `apply` / `retune` / `revert` with pre-authorization files, backups, manifests and key-level revert for
  Claude Code `modelSettings.<model>.autoCompactWindow` and Codex `model_auto_compact_token_limit`.
- `doctor`, `pricing show|refresh` (models.dev / LiteLLM), `cache info|clear`, `demo`, `--json` output.
- Bundled pricing snapshot with source and date; harness format facts with verification status.
