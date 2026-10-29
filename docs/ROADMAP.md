# Roadmap

What comes next, in order. Each step ships on its own; dates are not promised. Star counts and config facts are
from 2026-10-29 and change quickly.

## Where things stand

- Harnesses: Claude Code and Codex (parse, replay, recommend, apply, revert).
- Lite mode is offline and default; deep mode exists but has not been run against real history at scale.
- Recall in lite mode is an assumed proxy, and the savings are counterfactual estimates at list prices.
- Installs from GitHub only; no PyPI package, no tagged release.

## v0.1.0 — release what exists

- Tag v0.1.0 with release notes; publish to PyPI so the short install command works without the GitHub URL
  (and lift the docs test that forbids PyPI-style install commands at the same time).
- Social preview image in the repository settings.
- Merge or close the open Dependabot PRs.

## v0.2 — check the estimates against reality

The tool's numbers are only as good as the replay. Before adding breadth, measure it.

- `compactlab verify`: after a threshold change, compare the cost and wait the replay predicted with what the logs
  show for the days since (same model, same role, matched by active day). Report the error, not just the saving.
- Run deep mode on a sample of real sessions to replace the lite recall proxy with measured recall, and publish
  the method and the result, whichever way it goes.

## v0.3 — DeepSeek Harness (`dsh`) — experimental adapter landed

[deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) (MIT, ~265k stars). Feasible with
no new dependency.

- Logs: `~/.dsh/sessions/--<cwd>--/<session>/session[.vN].jsonl.zstd` (highest `vN` per session; plain `.jsonl`
  when run with `compression: 'none'`). The file is a chain of independent zstd frames. Python 3.14+ reads it
  with the standard library (`compression.zstd`); older Pythons fall back to the `zstd` CLI or skip with a message.
- Rows: a `session` header (`origin: 'subagent'`, `parentSession`, `isSeeded`), then events `{type, seq, time,
  data}`. Usage comes from `assistant/message` → `data.usage` with disjoint `inputTokens` / `cacheReadTokens` /
  `cacheWriteTokens` / `outputTokens`; compactions from `compaction/start|summary|end`. Seeded forks skip the
  inherited prefix up to `session/end-seed`, as Codex forks do today.
- Knob: the `@deepseek-ai/dsh-compaction-basic` plugin config. It triggers at
  `min(W × thresholdRatio, W − maxOutput − headroomTokens)` (defaults 0.8 and 65,536) and supports per-model
  `modelPolicies`. `recommend` emits a per-model policy with `thresholdRatio` set and `headroomTokens` low enough
  not to override it.
- Pricing: DeepSeek bills peak hours at 2× off-peak. The bundled rows use off-peak list prices. Peak pricing scales
  every rate by the same factor, so it moves the dollar figures but hardly the chosen threshold; pricing each request
  by its timestamp is still to do.
- Done: the adapter (plain and zstd logs, newest generation only, subagents, seeded forks, failed compactions),
  the default rule, DeepSeek pricing rows, `doctor` decoder check, and a `recommend` snippet. It parses dsh's own
  snapshot logs with no unknown events.
- Open, to settle against a real log: whether `reasoningTokens` is inside `outputTokens`, whether every row carries
  `time` (snapshots strip it; the adapter falls back to stream chunk times), and where a user's plugin config lives
  (until then `apply` does not write it). Marked experimental until someone runs it on real history.

## v0.4 — ratio-knob harnesses

Most other harnesses set the threshold as a fraction of the window, not a token count. One shared step turns the
recommended token threshold into the harness's unit (and says when a model's window makes the fraction coarse).

| Harness | Stars | Knob (default) | Logs | Order |
| --- | --- | --- | --- | --- |
| OpenCode | ~223k | `compaction.reserved` tokens in `opencode.json` (10,000) | `~/.local/share/opencode` (format to confirm) | 1 |
| Gemini CLI | ~109k | `model.compressionThreshold` (0.5) | `~/.gemini/tmp/<hash>/chats/` | 2 |
| Hermes Agent | ~258k | `compression.threshold` (0.5), per-model `model_thresholds` | to confirm | 3 |
| Qwen Code | ~29k | `context.autoCompactThreshold` (0.85) | `~/.qwen` (to confirm) | 4 |
| Kimi CLI | ~11k | `loop_control.compaction_trigger_ratio` (0.85) and `reserved_context_size` | `~/.kimi/sessions` (to confirm) | 5 |
| Goose | ~56k | `GOOSE_AUTO_COMPACT_THRESHOLD` (0.8) / `GOOSE_AUTO_COMPACT_TOKEN_LIMIT` | sqlite (to confirm) | 6 |

Not planned for now: Copilot CLI (no documented threshold knob), Cline and Aider (no auto-compaction knob found),
Cursor (closed logs). Each adapter follows [ADDING_A_HARNESS.md](ADDING_A_HARNESS.md): one `Adapter`, fixtures,
facts with sources in `data/harness_formats.json` and `data/config_knobs.json`, and "supported" only after a run on
real logs.

## Later

- Per-project and per-task-type thresholds where the harness allows it.
- A lens view for harnesses with a plugin surface (DSH plugins, OpenCode).
- Shared, anonymized calibration data (opt-in) so new users without much history get a better prior.
