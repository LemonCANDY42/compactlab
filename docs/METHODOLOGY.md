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
  interpolated. Confidence: `ok` with ≥ 200 samples in ≥ 3 bins, `low` otherwise, `prior` (8 s + 4 s per 100k)
  with no usable timestamps.
- **Compaction latency vs size**: compaction timestamp − last input before it (≤ 900 s), fitted as `a + b·pre_tokens`
  (a, b ≥ 0) with ≥ 5 samples (`ok` with ≥ 20), else the prior 15 s + 0.12 s per 1k tokens.
- **Re-discovery time**: (re-discovered tokens ÷ median tool-result size + correction turns) × median turn latency.

Per threshold the replay sums turn time at the simulated context, compaction time and re-discovery time. Versus the
default this splits into *time saved by smaller contexts* and *time added by compactions and re-discovery*.
Time enters the score as `time_hours × value_of_time` (default $10/h, a modest weight: you are not idle for all of
an agent's run). `--value-of-time 0` or `--view cost` gives the cost-only ranking; both views are in the result.

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

If the default never compacts in a group's sessions, the result is **no action needed**; a tier at or above the
default is **keep default**. `apply` skips both.

## Selection and tiers

- **standard**: among thresholds with recall ≥ floor (default 0.90), the largest whose score (cost + penalty +
  time, or cost + penalty in the cost view) is within 2% of the minimum.
- **headroom** (default): the largest threshold whose combined score is within `--headroom` (default 12%) of the
  feasible minimum, recall ≥ floor, rounded to the grid step, ≤ `--headroom-cap` and the model window, ≥ standard.
  The report shows its extra cost and how many fewer compactions it implies.
- Gears in the report: Eco (cheapest Pareto point meeting the floor), Standard, Headroom, Recall (highest recall on
  the frontier), Custom.

## Deep mode

For each model/role group, up to N sessions (largest peak first) are cut where the replay would compact at each
candidate threshold (standard, headroom, eco, recall). The executor model summarizes a reconstructed excerpt of
the turns before the cut; ground-truth probes (files accessed before the cut) are answered from the summary;
a different judge model votes `repeats` times. Measured recall is reported next to the assumed value and flagged
when they differ by more than 0.1. If measured recall at the headroom threshold falls below the floor, headroom is
lowered to the largest sampled threshold that meets it (or to standard).

## Known limits

- Replay cost is not your invoice: it ignores uncached input, plan pricing and discounts. Compare thresholds with
  each other, not with billing.
- Growth is assumed independent of the threshold.
- Cost-only optima tend to sit low; the recall and headroom rules push the pick up. These rules are judgement
  calls, made explicit so you can change them.
- Deep mode summarizes compactlab's excerpt of a session (tool calls, user text, short output snippets), not the
  full transcript, so it is a proxy for the harness's own summarizer. Probes cover file recall only in v0.1.
- Fact-check heuristics are regexes with false positives and negatives.
- Codex fork/replay handling follows public parsers and is marked `assumed` where unverified; see
  `data/harness_formats.json`.
- The report was checked by running its script against a DOM stub and by headless rendering of synthetic data.
