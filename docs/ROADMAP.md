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

- **Price the cache the way it was billed — done.** The replay assumed a 5-minute cache and prices writes at the
  5-minute tier. Claude Code on a subscription caches for 1 hour, and 1-hour writes cost 2× the input price instead
  of 1.25×. The logs say which tier each request used (`usage.cache_creation.ephemeral_1h_input_tokens` /
  `ephemeral_5m_input_tokens`; on the maintainer's last 30 sessions 99.99% of written tokens were 1-hour), so the
  TTL and write price now come from the logs per group, with `--cache-ttl` as the override. It mattered: on the maintainer's history the balanced pick moves from 550k (−33%) to 625k (−22%) for Opus 5.5 and
  from 575k (−24%) to 650k (−15%) for Sonnet 5.5 once replayed with the 1-hour cache it actually used.

- **`compactlab verify` — done for cost and compactions.** After a threshold change it compares what the replay
  predicted with what the logs show since, per 1k turns, with an interval and a "setting in effect" check. Still to
  do: wall-clock (needs a turn-latency comparison that survives tool and network time), and carrying predicted and
  observed figures in `compactlab share` records.
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

## Mods and the prompt cache (ideas from Cache Tax and Ribbon)

[Cache Tax](https://github.com/karanb192/cache-tax) keeps the cache warm and guards cold sends;
[Ribbon](https://github.com/robonuggets/ribbon) shows usage windows and cache state above the prompt. Both sit next
to the question compactlab answers, so the lens mod should work with them instead of beside them.

- **Compact on cold.** After the cache expires, the next send rewrites the whole context anyway. Compacting
  then costs about one uncached read of that context plus the summary, and every later turn reads a small context
  instead of the old one, so a cold return is a natural point to compact. The lens (or a hook) can show both prices
  side by side from the same price rows, e.g. "cold: resend rewrites 480k · /compact first", and replay history to
  say how often that would have paid off. When Cache Tax is installed, read its state (`$.cacheTax.status()`,
  proposed in cache-tax PR #36) and offer compaction as one of its refusal's options instead of adding a second guard.
- **Keepwarm in the replay.** Keepwarm pings read the whole context every 50 minutes of a break, so their cost grows
  with context size. A profile that knows keepwarm is on (from Cache Tax's status, or a flag) should pick a lower
  threshold; the report can show both.
- **Share state the other way.** Expose `$.compactlab.status()` from the lens mod (threshold in force, profile pick,
  tokens until compaction, estimated cost of the next compaction) so a band like Ribbon can show one line, e.g.
  "compacts in 170k · pick 625k", without reading compactlab's files.
- **Plan-limit view.** On a subscription the dollars are a yardstick, not the bill. Ribbon already reads the 5-hour
  and 7-day windows; the lens can say what a threshold change means in those terms where the harness exposes them.
- **Look and fit.** Follow Ribbon's lessons in the lens: no background of its own, inherit the app's text color,
  SVG neutrals from CSS variables with a `prefers-color-scheme` palette, a layout that steps down by width
  (one row, stacked, terminal text), cold or attention states open by default.
- **Trust.** Keep the lens read-only (a mod that changed approvals conflicted with auto-approve), publish a
  "what it can reach" block from `claude plugin validate` and a threat model the way Cache Tax does, and offer the
  one-line agent install ("send this link to your agent").

## Later

- Per-project and per-task-type thresholds where the harness allows it.
- A lens view for harnesses with a plugin surface (DSH plugins, OpenCode).
- Shared, anonymized calibration data (opt-in) so new users without much history get a better prior. The record
  and issue form exist (`compactlab share`, [SHARING.md](SHARING.md)); feeding priors from the pool does not yet.
