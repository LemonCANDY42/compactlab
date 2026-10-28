# Methodology and limits

## What is measured vs assumed

| Piece | Source | Status |
| --- | --- | --- |
| Context growth per turn, idle gaps, outputs | your transcripts | measured |
| Prices | `data/pricing.json` or `--pricing` | **illustrative defaults, `verified: false`** |
| Summary size, base context | median observed, else 12k / 35k | measured if available, else prior |
| Re-discovery penalty | repeated reads/searches and correction phrases after compactions, minus the same rate before | calibrated when ≥5 compactions are observed, else prior (rho 0.04, 6k tokens, 0.3 correction turns) |
| Recall | context-rot table × summary-retention decay | **assumed**; defaults are placeholders |

compactlab does **not** contain measured recall curves for Claude 5 or any other model. The recall
floor is a guard-rail on an assumed shape; change the table (`SimOptions.rot_table`) to your own
evidence, or leave the floor non-binding.

## Replay

Each session becomes a trace of growth increments (tokens added per turn), so compactions that already
happened do not distort the curve. For a threshold T the replay charges per turn: prefix at cache-read
price (cache-write price after a compaction or when the idle gap exceeds the TTL), new tokens at
cache-write price, output at output price, with long-context price tiers applied by request size. When
context exceeds T it charges a summarizer call (read the context, write S tokens) and resets context
to base + S. The penalty run adds re-discovery tokens to the next turn and correction turns, and is
reported as the difference between two replays.

## Selection

Among thresholds with assumed recall ≥ floor, take the largest whose combined (cost + penalty) is within
`tolerance` (2%) of the minimum; the range is the feasible thresholds within 5%. If nothing meets the
floor the highest-recall candidates are used and flagged. Gears are Pareto-frontier points: Eco =
cheapest meeting the floor, Balanced = the recommendation, Recall = highest assumed recall.

## Known limits

- Replay cost is not your invoice: it ignores uncached input, tool latency, thinking tokens billed
  separately, discounts and plan-based pricing. Compare thresholds against each other, not against billing.
- Cost-optimal thresholds sit near the low end of the grid for most cost models; the recall assumption is
  what pushes the pick up. Prior studies (e.g. a ~220k optimum, 250–450k range) are context, not validation.
- Growth is assumed independent of the threshold; real behaviour after a compaction can differ.
- Fact-check heuristics (`factcheck.py`) are regexes: they flag unsupported "tests pass"-style claims and
  paths/identifiers never seen before the compaction; expect false positives and negatives.
- The AI-evaluation layer is a stub with a mock client and tests; no real model has been run through it.
- Codex: token events are parsed tolerantly from the rollout format as documented/observed; format
  changes can break parsing silently into fewer turns. Codex has no per-model compact setting.
- The HTML report was checked by executing its script against a DOM stub and by generating it from
  synthetic data; it has not been verified visually in all browsers.
