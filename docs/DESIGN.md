# compactlab design (v1)

Goal: from a user's own agent-session history, estimate which auto-compact
threshold minimises cost plus the cost of forgetting, per model and per role
(main vs subagent), show it, and optionally write it to harness config.

Pipeline (each stage is a pure function over plain data; JSON between stages):

    adapters -> Session[Turn, Compaction] -> calibrate -> simulate(grid) ->
    select(recall floor) -> result.json -> report.html / apply

Decisions
- Offline by default. No network, no LLM calls. The AI-eval layer is a separate
  module that needs an explicit flag, prints a cost estimate, and enforces a
  hard budget.
- Replay uses growth increments (tokens added per turn), so compactions that
  already happened in history do not distort the curve.
- Cost = cache read/write/output at versioned, user-overridable prices, with
  cache TTL/idle effects and long-context tiers.
- Penalty = re-discovery tokens and correction turns after a compaction,
  calibrated from observed compactions when enough exist, else a labelled prior.
- Recall is an *assumed* proxy (context-rot table x summary retention), user
  overridable. It is not a measured Claude 5 curve and is used only as a floor.
- Selection: among thresholds with recall >= floor, the largest one whose
  cost+penalty is within a tolerance of the minimum.
- Apply: dry-run default; writes need a pre-authorization file or explicit flag;
  backup + manifest; revert restores only the keys we changed.
- Targets: Claude Code `modelSettings.<model>.autoCompactWindow` (100k-1M or
  "auto"); Codex top-level `model_auto_compact_token_limit` in config.toml
  (global, not per-model).

Out of scope for v1: dsh, opencli/opencode, pi adapters (interface only),
live telemetry, Windows/Linux path handling beyond defaults.
