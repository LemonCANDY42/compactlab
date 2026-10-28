# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Added
- Time dimension: turn-latency and compaction-latency fits from log timestamps, wall-clock per threshold
  (saved by smaller contexts vs added by compactions and re-discovery), `--value-of-time`, cost+time and cost views.
- Baselines: every tier compared with the harness default (`data/harness_defaults.json`, with sources) and with the
  current configured value; "no action needed" / "keep default"; `apply` skips those.
- `compactlab recommend`: ready-to-paste settings per harness, scope (global / project / agent), tier and format
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
