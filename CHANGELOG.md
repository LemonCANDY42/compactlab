# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Changed
- **Profiles replace tiers.** Three profiles, always computed side by side: `cost` ($10/h of agent time),
  `balanced` (default, $100/h) and `time` ($400/h). `--profile` on simulate / report / recommend / apply / retune
  picks the one to lead with or write; `--value-of-time` overrides its price of time and re-selects from a saved
  result without replaying. `standard` / `headroom`, `--tier`, `--headroom`, `--headroom-cap` and `--view` are gone
  (`--max-threshold` replaces `--headroom-cap`). Result schema 4.
- Why: with $10/h the old `headroom` pick on real data compacted several times more often than the default and
  added many hours of compaction wait per month.
- Guardrails: `balanced` never below 25% of the effective max window, `time` never below 35%; both keep added
  compaction wait under 30 min/day vs the default (`--wait-cap`); `cost` warns instead. Recall floor kept.
- Low-confidence latency fits (fewer than 20 timed compactions, thin turn bins) no longer drive the choice: the
  group uses the pooled harness fit or a pessimistic prior, and says which.
- "keep default" also when a profile does not beat the default on its own score.
- Comparisons now include minutes/day, added compaction wait/day and compactions/day, against default and current.
- Report: profile switch, three profiles side by side with flags, copy panel per profile. CLI/report state that
  costs are list-price estimates and recall is a lite-mode proxy.
- Synthetic demo latencies are closer to real logs (compaction ≈ 90 s + 0.4 s per 1k tokens).

### Added
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
