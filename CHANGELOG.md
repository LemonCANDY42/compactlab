# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Changed
- **Clearer terminal and report wording** (from a review while making the promo video):
  - one verdict line first in `demo` / `simulate` (terminal and plain output) and `top`:
    `→ Opus 5.5 main: set 625k  ▼ −22% cost  ▲ +3.0 min/day wall-clock`;
  - the action "set" is now "change" (zh 建议改): it reads as advice, not as already applied;
  - time is labelled "wall-clock" (zh 总耗时) everywhere, the net change (added compaction wait minus faster
    turns), so it no longer shares a word with the "time" profile; cost is 成本 in Chinese, not the profile name;
  - keep-default reasons in plain words: `not worth it at $250/h: saves $3.7/day but adds 1.1 min/day of
    wall-clock (= $4.6)`;
  - `top` drops the "◴ – · next –" and "service down" placeholders until something has calibrated or a service
    was set up ("service not running" then), draws the default `┊`, current `╎` and profile `┃` marks differently,
    labels the selected threshold on the axis, keys the `░` saved hatch, and shows "keep default" instead of a
    hatched bar for models that keep it;
  - panel subtitles and `top` show the prompt-cache TTL the replay used for Claude Code (`cache 1 h`).

### Fixed
- **Replay model and presentation fixes** (from an independent review of the simulator, selection and report):
  - sessions are grouped by harness, model and role, so one model name under two harnesses is two groups; a session
    that switched models is still replayed with every turn priced at the model that served it (and its logged cost
    likewise);
  - `--max-threshold` below the grid start is an error (`InfeasibleCap`) instead of recommending the last grid point
    above the cap (`50k` used to select `350k`);
  - re-selecting a profile (`--profile`, `--value-of-time`, the service) keeps deep mode's measured recall
    guardrail: the lowered pick and the `deep_recall_below_floor` flag no longer revert to the lite choice;
  - a time model on the prior also uses the prior's re-discovery step; one 600-second sample no longer moves it;
  - re-discovery calibration only uses compactions with a full 20-turn window before and after and no other
    compaction inside (a boundary at turn 10 sliced backwards: constant-repeat work gave rho 0.377, now 0);
  - the report's per-session replay runs on every turn and thins the drawn line afterwards (it stepped through 160
    growth bins and counted 160 compactions against the engine's 266); the session picker lists the group's own
    harness only;
  - the terminal `c` copy and the report's Copy button use the harness's recommender, so a DeepSeek Harness
    threshold copies a `modelPolicies` entry instead of Claude's `modelSettings` JSON.
- **Config-writing and service safety fixes** (from an independent review of `apply` and `serve`):
  - a running service re-reads and validates the consent file before every auto-apply, so `consent clear` or a
    passed `expires` stops writes without a restart (the reason shows under `last_apply`);
  - backups under `~/.compactlab/backups` are created 0700 / 0600 instead of the process umask;
  - `consent set` stores the settings file as an absolute path, and a stored relative path is refused;
  - Codex `config.toml` edits are string-aware (quoted keys, signed values, `key = ...` lines inside multi-line
    strings) and parsed before writing; invalid TOML or a non-integer value is refused;
  - `max_change_pct` compares with the value in force, including an inherited top-level `autoCompactWindow`, and
    `0` is a real limit;
  - auto-apply picks only grid points within `--max-step`, so rounding can no longer exceed it (960k → 850k);
  - an error in the auto-apply phase (e.g. a malformed settings file) backs off and shows in status instead of
    killing the scheduler thread.
- **Input parsing, pricing, deep-mode and lens-mod fixes** (from an independent review):
  - a recognized Claude Code or Codex record with a non-numeric, negative or non-finite token count (e.g.
    `"input_tokens":"oops"`) is skipped and counted in `parse_errors` instead of aborting the whole scan;
  - refreshed or overridden prices re-scale the carried 1-hour cache-write and long-context tiers to the new base
    prices, so a refreshed Opus input no longer keeps the old 1-hour write price;
  - `pricing refresh` drops rows with negative, NaN/infinite or non-numeric rates or invalid windows (and fails
    if none are left), and replaces the cached file atomically, so a bad fetch cannot clobber good prices;
  - deep mode: `--budget` now also stops the run as soon as the measured spend after a call exceeds the cap (it
    used to check only the estimate before a call), and the Claude Code executor passes `max_tokens` as
    `CLAUDE_CODE_MAX_OUTPUT_TOKENS`. Codex has no per-call output limit, so there one call can still overshoot;
  - deep mode probes no longer put the expected file path in the question (an empty summary scored 100%): every
    probe asks the same open question, answered once and scored against each file;
  - the Claude Code lens mod re-reads the lens file after every turn, so a recalibration shows up in a running
    session even for the same model.
- **Cache TTL and write price from the logs.** The replay assumed a 5-minute prompt cache priced at the 5-minute
  write tier, but Claude Code on a subscription caches the main session for 1 hour, billed at 2× the input price.
  The Claude Code parser now keeps each request's 1-hour share (`usage.cache_creation.ephemeral_1h_input_tokens`,
  `Turn.cache_write_1h`); a group whose written tokens are mostly 1-hour is replayed with a 1-hour TTL and the
  1-hour write price (new `cache_write_1h` price rows for Claude models). Groups record `cache_ttl` and
  `cache_ttl_source`, shown in the CLI notes and the report; the actual cost prices each write at its tier.
  `--cache-ttl` still overrides. On the maintainer's history the balanced pick moved from 550k (−33%) to 625k
  (−22%) for Opus 5.5 and from 575k (−24%) to 650k (−15%) for Sonnet 5.5. Parser version 4 re-parses cached files.

### Added
- **`share --verify` and schema `compactlab.share/2`.** A record can carry a per-group `verification` block with
  what `verify` found (predicted vs logged cost and compactions per 1k turns, interval, whether the setting was in
  effect, sample sizes). The checker accepts `/1` and `/2`; `aggregate_shares.py` adds the median prediction error.
- **`compactlab verify`: check a change against the logs.** Splits sessions at the last `apply` (manifest time; `--id`
  for another) and compares, per model and role, logged cost and compactions per 1k turns before vs after, with an
  80% bootstrap interval over sessions, against a replay of the before sessions at the old and new threshold (no data
  from after) and a replay of the after sessions (shows a changed workload). Reports whether compactions land near
  the new threshold (a setting overridden elsewhere shows as "far"), says "too early" below 3 sessions / 2 active days
  per side, and drops sessions that span the change. Cost only; wall-clock is not verified.
- **`compactlab share`: an anonymous, opt-in record of the estimated savings.** Writes `compactlab-share.json`
  with, per harness / model / role, the thresholds compared and the estimated change per day (each profile vs the
  harness default and vs your current setting, and your current setting vs the default), plus trust markers
  (history length, time-model source, replay cost over logged cost). Figures are rounded; no session ids, paths,
  timestamps, curves or text; unknown model names become `other`; demo data is refused. `share --check FILE`
  validates a record against an allow-list schema (`compactlab.share/1`). New "Share my estimated savings" issue
  form, a workflow that validates and labels submitted records, and `scripts/aggregate_shares.py` for medians per
  group. Nothing is sent by the tool. See `docs/SHARING.md`.
- **DeepSeek Harness (`dsh`) adapter, experimental.** Reads `$DSH_HOME/sessions` (default `~/.dsh/sessions`): the
  newest `session[.vN].jsonl[.zstd]` per session, zstd via the standard library on Python 3.14+ or the `zstd`
  command (a torn last frame is dropped), disjoint usage counts, `compaction/*` events (failed ones dropped),
  subagents and seeded forks. Default rule: the lower of 80% of the window and window − 256k output reserve − 65,536
  (727,040 on a 1,048,576 window). `recommend --harness dsh` prints a compaction-basic `modelPolicies` entry
  (`thresholdRatio`, `headroomTokens: 0`, explicit `maxTokens`); `apply` does not write dsh config. DeepSeek
  V4 Flash / Pro pricing rows (off-peak list price). `--dsh-root`; `doctor` reports the zstd decoder. The scan cache
  re-parses changed compressed files instead of resuming them (`Adapter.resumable`, `Adapter.read_records`).
- docs/ROADMAP.md.

### Changed
- Install instructions use the GitHub source (`uv tool install git+https://github.com/LemonCANDY42/compactlab`,
  `pipx install git+…`, or `uvx --from git+… compactlab demo`); compactlab is not on PyPI. A test keeps
  PyPI-style install commands out of the docs. The terminal SVGs show the command they ran.
- **Terminal views.** A stdlib-only renderer (`compactlab.term`: truecolor / 256 / 16 colours, NO_COLOR,
  `--plain`, `--color`, `--unit`, CJK-aware widths 30..200) gives `demo`, `simulate`, `recommend`, `doctor` and `serve`
  a fastfetch-style header and per-model panels (threshold gauge, was/now bars, cost sparkline, profile chips,
  recommendations with keep-default reasons). Piped output keeps the full plain tables.
- **`compactlab top`.** A live curses view (tree, braille cost/time chart, meters; keys for profile, unit, filter,
  copy, recalibrate, help; mouse; one static frame off a TTY). `serve` writes an endpoint file for it.
- `docs/assets/demo.svg` and the new `docs/assets/top.svg` are rendered from the real CLI output.
- `compactlab serve` is quiet: one in-place progress line only on a terminal (cut to its width at a space, so it
  never wraps into extra rows), nothing otherwise; the log says `recalibrating (reason)` and `recalibrated in Ns:
  N files, M cached, K parsed, X groups; next calibration: <time>`, and the next calibration at start.
- **One percent base.** Overview cards, the chart's Total rows and the sidebar badges all divide the summed change by
  the summed default of the models marked "set" (they disagreed: −21% vs −24% for the same −$185.3/day).
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
