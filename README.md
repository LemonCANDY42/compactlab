# compactlab

Replay your own agent-session history and find the auto-compact threshold that minimises cost
without forgetting too much. Per model, per role (main vs subagent). Offline by default.

> Status: v0.1, early. Cost curves are replays of *your* history against a price table.
> The recall side is an **assumed proxy**, not a measured curve for any model. Read
> [docs/METHODOLOGY.md](docs/METHODOLOGY.md) before trusting a number.

## Why

Long agent sessions are dominated by cache-read cost on a growing context. Compacting early is
cheaper per turn but costs summary tokens, a cache rewrite and re-discovery of what the summary
dropped. Compacting late is the opposite, and very long contexts degrade recall. The right
threshold depends on your sessions, your model and its prices, so compactlab measures it from them.

## Install

```bash
uv tool install .          # or: pip install .
compactlab demo            # synthetic data -> compactlab-demo/report.html
```

Python 3.11+, no runtime dependencies. macOS is the supported platform in v0.1.

## Use

```bash
compactlab scan                                  # what sessions were found
compactlab simulate --out result.json            # sweep thresholds, print recommendations
compactlab report --result result.json --out report.html
compactlab apply --result result.json --claude-settings ~/.claude/settings.json   # dry-run diff
```

Harnesses: Claude Code (`~/.claude/projects/**/*.jsonl`, including `subagents/`) and Codex
(`~/.codex/sessions/**/*.jsonl`). The adapter interface (`compactlab.adapters.base.Adapter`) is
meant for more; dsh, opencode and pi are not implemented.

### The report

A single self-contained HTML file (no server, no network, no CDN):

- Gear switch: **Eco** / **Balanced** / **Recall** / **Custom** slider, taken from the Pareto frontier.
- Estimated spend vs. a replay of your historical behaviour and vs. never compacting.
- Pareto chart (cost vs assumed recall), cost/penalty/combined curves, where your tokens sit by context size.
- Any session's real context curve overlaid with the same session replayed at the chosen threshold.

### Applying a threshold (explicit, reversible)

`apply` is a dry-run that prints a diff. Writing needs `--write` **and** authorization:
a pre-authorization file ([example](examples/preauth.example.json)) listing allowed files, key
patterns, models and value bounds, or the explicit `--i-authorize` flag with paths and bounds.
Every write makes a backup plus a manifest; `compactlab revert` restores only the keys it changed.
`compactlab retune` re-runs the analysis and applies under the same file, and only if it says
`"allow_retune": true`.

| Harness | Setting written | Notes |
| --- | --- | --- |
| Claude Code | `modelSettings.<model>.autoCompactWindow` in `settings.json` | per model; 100000–1000000 or `"auto"`; needs a recent Claude Code (docs say v2.1.288+) |
| Codex | top-level `model_auto_compact_token_limit` in `config.toml` | global, not per model; no per-role form |

Subagents cannot be set separately in either harness, so the written value is the main-role pick.

### Opt-in AI evaluation

`compactlab eval` checks summaries with ground-truth probes, exact match, and an LLM judge on a
different model. It prints an estimated cost, refuses to run without
`--i-understand-this-calls-an-llm`, and stops at `--budget-usd`. v0.1 ships no network client:
pass `--client package.module:Class` implementing `LLMClient.complete`, or `--client mock`.

## Development

```bash
uv venv && uv pip install -e ".[dev]" && .venv/bin/python -m pytest
python scripts/secrets_scan.py --all     # run before any push
```

Tests use synthetic fixtures only. MIT licensed.
