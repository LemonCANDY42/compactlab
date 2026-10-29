# Methodology and limits

## Measured vs assumed

| Piece | Source | Status |
| --- | --- | --- |
| Context growth per turn, idle gaps, outputs | your transcripts | measured |
| Prices | `data/pricing.json` (models.dev snapshot, dated) → `pricing refresh` → `--pricing` | public list prices; provenance shown per row |
| Summary size, base context | median observed, else 12k / 35k | measured if available, else prior |
| Re-discovery penalty | repeated reads/searches and correction phrases in the 20 turns after a compaction, minus the rate in the 20 turns before | calibrated with ≥5 observed compactions, else prior (ρ = 0.04, 6k tokens, 0.3 correction turns) |
| Recall (lite) | context-rot table × summary-retention decay | **assumed**; placeholders you can replace |
| Recall (deep) | probes answered from re-generated summaries, judged by a different model | measured on samples, with variance |

compactlab does not contain measured recall curves for Claude 5 or any other model.

## Replay

A session becomes growth increments `g_t` (tokens added per turn), outputs `o_t` and idle gaps `Δt_t`.
Historical compactions are removed (growth across a boundary is set to 0). For threshold `T`, with context `c`:

```
c ← c + g_t
turn cost  = c_prev · (Δt_t > TTL or just compacted ? p_write : p_read) + g_t · p_write + o_t · p_out
if c > T:  cost += c · p_read + S · p_out          # summarizer call
           c ← B + S                                 # next turn rewrites the prefix
penalty:   next turn adds ρ·(c_before − B − S) + F tokens; plus k correction turns at (B+S)·p_read + ō·p_out
```

Prices are chosen per request by size (long-context tiers). Penalty is reported as the difference between a run
with and without it.

Recall proxy per turn: `rot(c) · (1 − (1 − r)·exp(−turns_since_compaction / H))` with retention `r = 0.9`,
`H = 15`, and `rot` interpolated from `(0, 1.0) (100k, .98) (200k, .95) (400k, .90) (600k, .84) (1M, .75)`.

## Time

compactlab fits wall-clock behaviour from your own timestamps, per model and role (`sim/timing.py`):

- **Turn latency vs context**: latency = response timestamp − last user/tool-result timestamp before it (≤ 600 s).
  Samples are binned by 50k tokens of context; bin medians (≥ 5 samples) are made monotone non-decreasing and
  interpolated. Confidence: `ok` with ≥ 200 samples in ≥ 3 bins, `low` otherwise, `prior` with no usable
  timestamps.
- **Compaction latency vs size**: compaction timestamp − last input before it (≤ 900 s), fitted as `a + b·pre_tokens`
  (a, b ≥ 0). Confidence: `ok` with ≥ 20 samples, `low` with 5–19, `prior` below that.
- **Thin data never drives a pick.** A fit that is not `ok` is not used. The group borrows the pooled fit of all
  sessions of the same harness when that one is `ok` (`source: pooled`); otherwise it uses a prior (`source: prior`):
  turn latency 8 s + 4 s per 100k, compaction latency 30 s + 0.3 s per 1k tokens (about 2 minutes at 300k). The
  compaction prior is deliberately pessimistic, so missing data biases toward fewer compactions, not more. The
  result records `turn_source` / `compact_source` and the own sample counts, and the CLI and report print them.
- **Re-discovery time**: (re-discovered tokens ÷ median tool-result size + correction turns) × median turn latency.

Per threshold the replay sums turn time at the simulated context, compaction time and re-discovery time. Versus the
default this splits into *time saved by smaller contexts* and *time added by compactions and re-discovery*.
Time enters the score as `time_hours × value_of_time`. Each profile sets its own value of time (below);
`--value-of-time` overrides the selected profile's, and `--value-of-time 0` gives a cost-only ranking.

Limits: for Claude Code the response timestamp is the first content block, so long generations are undercounted;
timestamps include tool and network time; latency depends on load. Treat time as a secondary signal.

## Baselines

Every result is compared with:

- **default**: the harness's own compaction point if you change nothing (`data/harness_defaults.json`). Claude Code:
  about 967K for models with a native 1M window, 200K for Sonnet/Opus 4.6 without extended context, else the
  context limit (docs: code.claude.com model-config, "Default auto-compact thresholds"). Codex: 90% of the context
  window, and a configured `model_auto_compact_token_limit` is clamped to that (openai/codex `ModelInfo`).
  Codex's window comes from `model_context_window` in the logs when present.
- **current**: the value in your config, read-only (`modelSettings.<model>.autoCompactWindow`, then top-level
  `autoCompactWindow`, in `~/.claude/settings.json`; top-level `model_auto_compact_token_limit` in
  `~/.codex/config.toml`). Project-level settings, env vars and CLI flags can override these and are not read.

If the default never compacts in a group's sessions, the result is **no action needed**. A profile whose threshold
is at or above the default, or whose score is not lower than the default's at the same value of time, is **keep
default**. `apply` skips both. Comparisons are reported in total and per day (`days` = span of the group's
timestamps, at least 1): Δcost, Δwall-clock (hours and minutes/day), added compaction wait (minutes/day),
compactions/day and Δrecall, against the default and against the current setting.

## Profiles and guardrails

Score per threshold: `list-price replay cost + re-discovery penalty + value_of_time × wall-clock hours`.

| Profile | value of time | minimum threshold | added compaction wait vs default |
| --- | --- | --- | --- |
| `cost` | $10/h | none (flagged below 25% of the effective max window) | flagged above 30 min/day |
| `balanced` (default) | $250/h | 40% of the effective max window | ≤ 30 min/day (`--wait-cap`) |
| `time` | $600/h | 50% of the effective max window | ≤ 30 min/day |

Selection per profile: restrict the grid to `[minimum, min(effective max, --max-threshold)]`; for `balanced` and
`time`, keep only thresholds whose added compaction + re-discovery wait vs the default is within the cap (if none
is, the cap is dropped and flagged); then, among thresholds with recall ≥ floor (default 0.90), take the largest
whose score is within 2% of the lowest (the rule earlier versions called **standard**, now applied per profile).
Flags record when a guardrail moved the pick (`guardrail_applied`, with the unconstrained threshold), when cost goes
below a quarter of the window, when wait exceeds the cap, and when the recall floor could not be met.

Why these numbers: with a value of time of $10/h, real histories favored windows near the low end of the grid and
several times the default's compactions, at minutes of wait each. Pricing an hour of the developer's attention at
$250 (balanced) or $600 (time) moves the optimum to the hundreds of thousands of tokens. The minimum fractions and
the 30 min/day cap are judgement calls that keep any profile from producing an extreme setting; they are options,
not facts about any model.

Selection only needs the stored grid arrays, so `report`, `recommend` and `apply` accept `--profile` and
`--value-of-time` on a saved `result.json` and re-select without replaying. The report's Pareto frontier and score
curve are drawn at the selected profile's value of time.

## Per-day impact

For each profile and each reference point (harness default, current setting), with `Δ` = profile minus reference
over the whole replayed history (`sim/select.py: impact`):

| figure | formula |
| --- | --- |
| money | `Δ(list-price cost + re-discovery penalty)` |
| wall-clock (net) | `Δtime = wait − turn gain` |
| compaction wait | `Δ(compaction + re-discovery seconds)`, positive = more waiting |
| turn gain | `reference turn seconds − profile turn seconds`, positive = faster turns |
| compactions | the profile's compaction count (and the reference's) |

Each is divided three ways: **per day** by `days = max(last turn − first turn, 1 day)` in the group; **per active
day** by `active_days`, the number of local calendar dates with at least one turn, capped at `days` (a 10.2-day span
can touch 11 dates); **per month** = per day × 30. Negative money or minutes means a saving. Results saved before
`active_days` existed are re-selected on load; their per-active-day figures stay empty until `simulate` is re-run.

## Deep mode

For each model/role group, up to N sessions (largest peak first) are cut where the replay would compact at each
candidate threshold (the three profiles' thresholds). The executor model summarizes a reconstructed excerpt of
the turns before the cut; ground-truth probes (files accessed before the cut) are answered from the summary;
a different judge model votes `repeats` times. Measured recall is reported next to the assumed value and flagged
when they differ by more than 0.1. If measured recall at a profile's threshold falls below the floor, that profile is
lowered to the largest smaller sampled threshold that meets it (or the smallest sampled one) and flagged
`deep_recall_below_floor`.

## Known limits

- Replay cost is a list-price estimate, not your invoice: it ignores plan pricing, subscriptions and discounts. Compare thresholds with
  each other, not with billing.
- Growth is assumed independent of the threshold.
- Cost-only optima tend to sit low; the value of time and the guardrails push the pick up. These are judgement
  calls, made explicit so you can change them.
- Recall in lite mode is a proxy (context-rot table × summary retention), not a measurement; only deep mode measures.
- Deep mode summarizes compactlab's excerpt of a session (tool calls, user text, short output snippets), not the
  full transcript, so it is a proxy for the harness's own summarizer. Probes cover file recall only in v0.1.
- Fact-check heuristics are regexes with false positives and negatives.
- Codex fork/replay handling follows public parsers and is marked `assumed` where unverified; see
  `data/harness_formats.json`.
- The report was checked by running its script against a DOM stub and by headless rendering of synthetic data.
