# Methodology and limits

## Measured vs assumed

| Piece | Source | Status |
| --- | --- | --- |
| Context growth per turn, idle gaps, outputs | your transcripts | measured |
| Prices | `data/pricing.json` (models.dev snapshot, dated) → `pricing refresh` → `--pricing` | public list prices; provenance shown per row |
| Summary size, base context | median observed, else 12k / 35k | measured if available, else prior |
| Re-discovery penalty | repeated reads/searches and correction phrases in the 20 turns after a compaction, minus the rate in the 20 turns before | calibrated with ≥5 observed compactions, else prior (ρ = 0.04, 6k tokens, 0.3 correction turns) |
| Recall (lite) | context-rot table × summary-retention decay | **assumed**; placeholders you can replace |
| Recall (hyper) | probes answered from re-generated summaries, judged by a different model | measured on samples, with variance |

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

## Selection and tiers

- **standard**: among thresholds with recall ≥ floor (default 0.90), the largest whose combined cost + penalty is
  within 2% of the minimum.
- **headroom** (default): the largest threshold whose combined score is within `--headroom` (default 12%) of the
  feasible minimum, recall ≥ floor, rounded to the grid step, ≤ `--headroom-cap` and the model window, ≥ standard.
  The report shows its extra cost and how many fewer compactions it implies.
- Gears in the report: Eco (cheapest Pareto point meeting the floor), Standard, Headroom, Recall (highest recall on
  the frontier), Custom.

## Hyper mode

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
- Hyper mode summarizes compactlab's excerpt of a session (tool calls, user text, short output snippets), not the
  full transcript, so it is a proxy for the harness's own summarizer. Probes cover file recall only in v0.1.
- Fact-check heuristics are regexes with false positives and negatives.
- Codex fork/replay handling follows public parsers and is marked `assumed` where unverified; see
  `data/harness_formats.json`.
- The report was checked by running its script against a DOM stub and by headless rendering of synthetic data.
