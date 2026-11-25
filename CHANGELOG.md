# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Changed
- **`lens.json` and the service's `result.json` keep the latest run of every harness.** A run replaces the harnesses
  it scanned (every one unless `--harness` limits it, including one with no logs in the window) and keeps the
  others' groups, sessions, Codex single-key decision, idle-compaction verdict and model notes from the file, so
  `simulate --harness dsh` no longer leaves a lens with dsh alone. A harness whose logs could not be read keeps its
  entries. Kept groups keep their picks, and re-selection (`--profile`) and the threshold cap use the options of
  the run each harness comes from (another run's cap may not even fit their grid); the top-level fields (`options`,
  `mode`, `calibration`, ...) are the latest run's. Both files and `engine result` gain `harnesses`: per harness, the
  mode, newest turn and calibration its groups come from (and, in `result.json`, its options). Writes are atomic and serialized by a lock
  file beside each file. `simulate --out` and `demo` still write their own run alone. The lens mod reads Claude Code
  groups only (OpenCode can run the same models) and that harness's calibration age.

### Fixed
- **The scan cache kept the text of a turn still in progress.** When a log ended with a user message, a tool call or
  its output that no turn had yet, the row's resume state stored them verbatim (Claude Code and Codex). It now keeps
  only what the analysis uses: whether the message corrects the agent, the tool's name and a hash of its key, and the
  output's token count. Resuming gives the same session as a full parse. The cache version is now 6: the first scan
  deletes version 5's files, which could hold such text.

### Added
- **One calibration or config write at a time, across processes.** `recalibrate`, `engine calibrate`, the runs of
  `serve`, `apply --write`, `retune` and `revert` take `<state-dir>/service/lock` (`flock`, without waiting), so the
  menu bar app, the CLI and a launchd/systemd schedule can start jobs whenever they like. Whoever gets the lock runs;
  the others do nothing and say which job holds it (pid, start time): `engine calibrate` prints the `busy` error (no
  longer reserved), the CLI exits 75 (`EX_TEMPFAIL`), and a scheduled run of `serve` is logged and retried a minute
  later without counting as a failure. The operating system drops the lock when its holder exits or crashes, so there
  is no stale lock. The calibrator re-reads its state under the lock: a calibration another process just made counts
  as the scheduled one, and failures elsewhere back off here too. A dry-run `apply` does not take the lock.
- **`apply --expect-plan <hash>`.** Every plan prints a hash (`plan <hash>`, `plan_hash` with `--json`) over each
  change's harness, file, key, current value and new value, without timestamps. With `--expect-plan`, `apply` makes
  the plan again under the lock and refuses to write (exit 2, no backup or manifest) unless it is the reviewed one.
  With `--json`, refusals are now JSON on stdout too: `{"error": {"code": "plan_changed" | "busy" | "refused",
  "message": ...}}`, next to the new plan for `plan_changed`.
- **`compactlab engine`, the JSON interface for the menu bar app** ([docs/ENGINE_API.md](docs/ENGINE_API.md)).
  `hello`, `sessions`, `watch`, `result` and `calibrate` print versioned JSON (`engine_api: 1`) on stdout, errors
  included; examples of every message in `tests/contract/` are checked against the real output. `watch` is a resident
  JSON Lines process driven by `refresh` / `snapshot` / `ping` / `quit` on stdin that lists the active sessions:
  context of the latest request, model, threshold in force and where it comes from, window, and when the prompt
  cache goes cold. It reads only the end of recently written logs with the analysis's own parsers, reading further
  back when Claude Code has appended copies of earlier messages (on 85 real files, including a 109 MB log ending in
  20 MB of copies, every value matched a full parse), and finds files incrementally; on a 3,200-file history it peaks
  at 32 MB and uses about 1.5% of a core while sessions are being written.
- Codex subagents whose session names their parent only in `source.subagent.thread_spawn` now get that parent.
- **OpenCode adapter** (1.x storage). Reads `~/.local/share/opencode/opencode.db` (`$XDG_DATA_HOME`,
  `--opencode-root`) read-only with SQLite's normal locking, so a running OpenCode is never blocked or written to.
  One turn per `step-finish` part, reasoning counted as output, subagents from `session.parent_id`, compactions from
  the `compaction` part and its `summary: true` reply (whose own call is not a turn). Checked on a real 1.19.5
  database: no unknown events, and the parsed token sums equal OpenCode's own per-session totals. The default rule
  is window − min(max output, 32,000); `recommend` prints a per-model `provider.<id>.models.<model>.limit` (OpenCode
  1.x has no threshold key, and `compaction.reserved` only applies to models whose limit sets `input`) and gives the
  2.x `compaction.buffer` share in a note. `doctor` counts sessions in the 2.x storage, which is not read yet.
  OpenCode's provider ID is the session's platform for pricing (`anthropic/…` is Anthropic's own API, not
  OpenRouter).
- **Gemini CLI adapter, experimental** (built from the 0.46/0.70 source; no real log checked yet). Reads
  `~/.gemini/tmp/<project>/chats/*.jsonl` and subagent chats (`$GEMINI_CLI_HOME`, `--gemini-root`), and the older
  single `session-*.json` files. Messages re-written by id are merged, tool-result messages are not prompts, and a
  `<state_snapshot>` summary marks a compression. Default: half the model's token limit (about 524K); `recommend`
  prints `model.compressionThreshold`.
- **Ratio knobs**: `recommend/units.py` turns a token threshold into a fraction of the window or a share kept free,
  rounding so the trigger never lands above the pick and noting when the window makes a step coarse or when one
  global fraction lands elsewhere for another model. dsh's `thresholdRatio` uses it.
- `simulate` reads the configured threshold for OpenCode (`--opencode-config`) and Gemini CLI (`--gemini-settings`);
  `verify --applied-at` accepts OpenCode (`--model provider/model`) and Gemini CLI changes. `apply` does not write
  either harness's config yet.
- **Codex gets one decision for its one limit.** `model_auto_compact_token_limit` covers every model, but `recommend`
  printed the pick of the model with the most turns (175k from `gpt-6.1-sol` history), which would also apply to the
  GPT-6.2 models now in use and leave them worse off. The value is now chosen across the Codex models in use, each
  scored at the lower of the value and its own clamp, with the usual tolerance rules; every Codex group, `recommend`,
  `apply` and auto-apply follow that one decision. A model that a newer version of its family took over is left out
  and listed (`gpt-6.1-sol` → `gpt-6.2-sol`).
- **Keep the current setting when it is within the tolerance.** A profile's pick that would not beat your current
  setting's score by more than the 2% selection tolerance is now **keep current** instead of a change: the pick is
  the largest threshold within 2% of the best, so it could even score slightly worse than a setting already nearer
  the best. `recommend`, `apply` and auto-apply leave such a setting alone, the idle-compaction verdict counts it as
  in force, and the terminal views, report and `share` say why.
- **Prices, windows and defaults that change over time.** Auto refresh also runs weekly for known models, and every
  price or window a refresh moves is logged (`pricing-changes.json`; recent ones for your models appear in `simulate`
  and `pricing auto status`). A refresh older than the bundled snapshot no longer overrides the bundle's newer rows.
  Each group keeps the price it was replayed with, so loading a saved result after a change says what moved. The
  window is the most common among a group's latest sessions (Codex can change a model's window; a clean switch is
  reported), a Claude Code context beyond the assumed window means the window grew, and threshold compactions landing
  more than 10% from the assumed default are flagged (the harness changed its default, or a setting compactlab cannot
  read is in force). A model no public file lists stops triggering refreshes after 5 tries.
- **New models and other platforms are priced from the right row.** Model IDs are reduced to the vendor's ID and the
  platform they point at (Bedrock, Vertex, OpenRouter, Azure; Codex sessions record `model_provider`), and a lookup
  says how it matched: the platform's own row, the vendor's list price, a borrowed row from the closest model of the
  same family, or the fallback. Before, `claude-fable-5-2` picked up Fable 5's prices by substring (cache reads 5×
  too high) and every GPT-6 model in Codex got the $3/$15 fallback. `pricing refresh` now keeps every vendor's own
  models (not resellers' re-spelled copies) and each platform's prices under `<platform>:<id>`; `pricing show` gains
  `--platform` and a match column. The bundled snapshot adds Fable 5.2 and the GPT-6 family. Codex parser version 6
  re-reads Codex logs once.
- **`compactlab pricing auto on|off|status`: opt-in refresh for new models.** When on, `serve` and scheduled runs
  download models.dev (LiteLLM if that fails) when the logs show a model with no price row of its own, at most once a
  day or once an hour for a model the last attempt did not cover. Off by default; `simulate` and the other commands
  stay offline and say which models need it.
- **`simulate` lists models priced from someone else's row, and Claude Code models with no window of their own** while
  others have one and whose sessions reach it, with the closest configured model's value as a stand-in when the new
  model has under 3 active days of data.
- **Third-party models in Claude Code** (DeepSeek, Kimi, GLM behind `ANTHROPIC_BASE_URL`) are replayed with the 200K
  window Claude Code assumes for an ID it does not recognize, or `CLAUDE_CODE_MAX_CONTEXT_TOKENS` from settings.json's
  `env` block or the environment. A Claude model missing from the default rules compacts at about 967K when its window
  is 1M. `recommend`, `apply` and auto-apply key `modelSettings` by the canonical Claude name, which also covers
  Bedrock, Vertex and dated IDs (a preauthorization or consent listing either spelling matches), and the current
  setting is read the same way; a `[1m]` variant keeps its own key (`claude-opus-4-6[1m]`), read and written apart
  from the base model's. A Claude Code context
  beyond 200K for an unrecognized ID widens the window to what the logs show (the env var is probably set in a shell
  compactlab does not see).
- **Claude Code's idle compaction is modelled and gets its own advice.** `idleCompaction` (on by default) summarizes a
  long conversation left idle on the one-hour cache while the cache is still warm, about 55 minutes in, and applies
  the summary on return. The adapter tells these compactions apart (an `auto` boundary an hour or more after the last
  turn that took under 5 s, or `precomputed: true`); they no longer count as the threshold, as compaction wait or as
  the "history" threshold. The replay charges the summary (a cached read and its output), returns you to the smaller
  context, adds the re-discovery after it, and charges summaries for sessions never resumed at the observed rate.
  Whether a gap fires depends on the app and machine, not the context size (124 of 335 eligible gaps in the
  maintainer's logs), so each gap keeps its logged outcome and the rest fire at the group's rate. Groups on the
  one-hour cache are replayed with the setting flipped too; `simulate` prints a keep-on / turn-off verdict over all of
  them (it is one global setting) and `recommend` adds an `idleCompaction` snippet when flipping wins by more than 2%,
  even when no window changes. Main sessions only; the flipped sweep is saved in the result, so re-selecting a saved
  result (`--profile`, `--value-of-time`) updates the verdict too. `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS` is read from
  settings.json's `env` block or the environment; `verify` uses the same setting.
- **`verify` counts the compaction requests the logs leave out.** Each compaction's summary request (a cached read of
  the context and the summary output) is not a logged turn; observed cost now adds an estimate, as the replay does.
  Threshold, idle and hand-typed compactions are counted apart, the "setting in effect" check uses threshold
  compactions only, and a role that never reached the threshold gets `untouched` instead of "saved less than
  predicted".

### Fixed
- `demo` scanned the real OpenCode, Gemini CLI and dsh logs too (only Claude Code and Codex were pointed at the
  synthetic data), so a demo report could show your own models. It now reads only the synthetic logs.
- Service logging set up twice in one process logged every line twice and kept writing to the first stderr.
- **Codex: most tool calls were missed.** Current Codex runs commands as `custom_tool_call` (`exec`), which the
  adapter ignored (24,107 calls in 30 days of the maintainer's logs); tool outputs as lists of content blocks are now
  counted by their text, not their JSON (an image's base64 no longer counts as context). Codex writes a response's
  `token_count` after that response's tool has run, so turn latency now runs from the input before the response's
  first output item to its last one (it had been the tool's run time, or zero; an aborted response no longer stretches
  the next turn), and a tool output that arrives before its turn is recorded on that turn instead of the previous
  one. Parser version 5 re-reads Codex logs.
- **Re-discovery time could explode.** It divided the re-discovered tokens by the median of all tool results; for a
  Codex group whose logged results were ~4 tokens that gave 134 hours of re-discovery in a 39-hour history (+4,826
  min/day). The step is now in the same units as the tokens: the mean re-discovery result with an observed penalty,
  the prior's 2,000 tokens with the prior.
- **"No action needed" now means the default threshold is never reached.** Idle compactions and the hand-typed
  `/compact` the replay keeps no longer count; `verify`'s predicted threshold compactions leave them out too.
- **"Change" for a negligible gain.** A profile now keeps the default unless it beats it by more than the 2% selection
  tolerance (by the selection rule the default is as good a choice); the reason says "net gain under 2%".

### Changed
- **Scan cache is SQLite, one row per log** (`~/.compactlab/cache/scan-v4.sqlite3`), so a background recalibration
  no longer loads and rewrites one big JSON file. A scan reads and writes only the rows of the files it scans, one
  row per transaction (two runs can share the cache; a write that cannot get the lock in 10 s is skipped), streams
  each log line by line, and rebuilds an unchanged file from its session without reading the resume-only state. Same
  rules as before: size, mtime_ns, SHA-1 of the first 4 KiB, resume offset, parser version, a torn last line is
  never consumed, 0600 files in a 0700 directory. New: rows of deleted logs are pruned after each scan in one
  transaction and zeroed in the files (`secure_delete`, then a checkpoint; a prune that cannot delete, or cannot
  erase yet because another run is reading, says so); a corrupt database is rebuilt, and a row of the wrong shape is
  parsed again instead of failing the calibration; the first scan deletes older cache versions (`scan-v1.json` to
  `scan-v3.json`) and never a newer one's; `cache clear` deletes every version; `cache info` shows rows, rows of
  deleted logs, saved results and older or newer versions; `doctor` reports the cache actually in use (it looked for
  `scan-v1.json`). For adapter authors: a parser's `finish()` must now depend only on its session, and an adapter
  that overrides `load()` overrides `fingerprint()` too (see docs/ADDING_A_HARNESS.md).
- **A calibration with nothing new reuses the last result.** A lite run with the cache keys its result by every log
  it would read (path, parser version, size, mtime_ns, first 4 KiB; OpenCode: the database files and the sessions in
  the window) and by every other input (effective prices, options and the harness settings they read, configured
  thresholds, compactlab's own files, local time rules). When both keys match it returns the saved result without
  decoding a session; the output is byte for byte a full run's, the calibrator still stamps a fresh time and runs
  auto-apply. Zero newly parsed files is not enough on its own (another command may have refreshed the scan cache),
  so the key is the files themselves, taken before the scan. `--no-cache` and deep mode never reuse.
  Measured end to end (`recalibrate`, through publishing result.json and lens.json) on one machine's 30 days of logs
  (3,237 files found, 2,820 in the window; APFS clone, scratch HOME, `/usr/bin/time -l`, CPU time and macOS peak
  memory footprint; the machine was also building Swift, load 8-95, so wall time is given only where load was low):

  | run | before (JSON cache) | SQLite rows | + result reuse |
  | --- | --- | --- | --- |
  | cold (empty cache) | 115 s CPU, 2.10 GB | 85 s, 0.82 GB | 85 s, 0.78 GB |
  | warm (nothing changed) | 20-23 s, 1.17-1.52 GB (15 s wall) | 19-23 s, 0.75-0.84 GB | 0.5-1.3 s, 56 MB (0.9-3.2 s wall) |
  | incremental (5 logs, 12.8 MB appended) | 31-56 s, 1.17-1.51 GB | 13-23 s, 0.75-0.84 GB | 17-25 s, 0.78-0.84 GB |

  `engine calibrate` with nothing changed: 0.4-0.8 s wall at load 14, 59 MB. Building the key costs a full run
  0.6-1.9 s. A run with appended logs still analyses every session, so its peak stays at ~0.8 GB: the sessions
  themselves (mostly the text of user messages), which the next entry removes.
- **The scan cache and the lite analysis keep no transcript text** (cache version 5; the first run deletes version
  4's file, which held it). The lite analysis reads text in three places only: whether a user message corrects the
  agent, which tool calls repeat and how long a compaction summary is. Sessions are now kept lean, on disk and in
  memory: a correction flag, tool keys reduced to the tool name and a 96-bit hash (no path or command), summary
  lengths, and no prompts, output snippets or summaries. Only a log's still-unanswered last message waits in the
  cache until its reply. Results are byte for byte the same; deep mode, which reads text, parses the logs itself.
  The correction check runs the regex only on short windows around the words any match must contain: the same
  answer on all 33,288 prompts of the test history, 5x faster. For adapter authors: a resumed parser must not read
  the text of turns or compactions it made before (use `Compaction.set_summary` / `summary_len`).
  Measured end to end (`recalibrate` through publishing result.json and lens.json; same APFS clone of 30 days of
  logs: 3,243 files, 182,160 turns; scratch HOME; `/usr/bin/time -l`; interleaved runs; load 20-32 from Swift builds
  elsewhere, so CPU time):

  | run | before (result reuse, text in cache) | lean |
  | --- | --- | --- |
  | incremental (5 logs, 12.9 MB appended) | 22.9-25.5 s CPU, 0.80-0.85 GB | 19.0-21.6 s, **189-218 MB** |
  | `engine calibrate`, incremental | 14.8 s, 0.82 GB | 14.7 s, 201 MB |
  | 5 logs rewritten | 23.3-25.8 s, 0.75-0.84 GB | 18.8-20.1 s, 193-209 MB |
  | warm (nothing changed) | 1.35-1.63 s, 56-57 MB | 1.13-1.37 s, 56-59 MB |
  | cold (empty cache) | 168 s (104-148 s in earlier pairs), 0.78-0.88 GB | 132 s, 0.29 GB |

  Cold CPU is within the load noise: the cold scan step alone, three alternating pairs on the same 600 files, was
  18.5-23.4 s before and 21.1-23.9 s lean (mean +5%). The lean sessions of that history hold 109 MB instead of 475
  MB, and the cache file is 99 MB instead of 314 MB. result.json and lens.json are identical to before.
- **Clearer terminal and report wording** (from a review while making the promo video):
  - one verdict line first in `demo` / `simulate` (terminal and plain output) and `top`:
    `→ Opus 5.5 main: set 625k  ▼ −22% cost  ▲ +3.0 min/day wall-clock`;
  - the action "set" is now "change" (zh 建议改): it reads as advice, not as already applied;
  - time is labelled "wall-clock" (zh 总耗时) everywhere, the net change (added compaction wait minus faster
    turns), so it no longer shares a word with the "time" profile; cost is 成本 in Chinese, not the profile name;
  - keep-default reasons in plain words: `not worth it at $250/h: saves $3.7/day but adds 1.1 min/day of
    wall-clock (= $4.6)`;
  - `top` drops the "◴ – · next –" and "service down" placeholders until something has calibrated or a service
    was set up ("service not running" then), draws the default `┊`, current `╎` and profile `┃` marks differently,
    labels the selected threshold on the axis, keys the `░` saved hatch, and shows "keep default" instead of a
    hatched bar for models that keep it;
  - panel subtitles and `top` show the prompt-cache TTL the replay used for Claude Code (`cache 1 h`).

### Fixed
- **History copied into a resumed or forked Claude Code log is no longer counted twice.** The new log repeats the
  parent's messages with their original timestamps; the same API call was priced and counted once per copy (in one
  period a third of the turns and a third of the compactions). The earliest session keeps them; later copies drop
  them and report `skipped_inherited`.
- **The replay keeps your hand-typed `/compact`.** It used to drop every recorded compaction, so the "default"
  baseline assumed you never compact by hand and the estimated saving was credited to the threshold. Now a manual
  `/compact` is replayed at the same place while the replayed context is still at least 70% of the size it was typed
  at (`MANUAL_KEEP`). On a session with 31 manual and 6 automatic compactions the default baseline went from $1,276 to
  $932 against $915 actually logged, and the balanced pick's saving from −31% to −6.5%. The report's single-session
  chart does the same.
- **Claude Code re-writes old compaction records later in a log.** One long session showed 64 compactions, 37 real.
  They are now read once (by record id), and the re-written summary line is skipped with them.
- **Hand-typed `/compact` no longer counts as the threshold.** Claude Code logs whether a compaction was `auto` or
  `manual`; compactlab ignored it, so `verify`, `share --verify` and the "historical" replay counted manual ones in
  the observed compactions and the median size at compaction. A user who types `/compact` often saw "already in
  effect" for a threshold that never was (real logs: 2.9 → 0.5 auto compactions per 1k turns before the change).
  The adapter now records `trigger`; unknown (Codex, dsh) still counts as auto; `observed_manual_compactions` is
  reported separately. The scan cache version is 3, so the first run after upgrading re-reads the logs.
- **Replay model and presentation fixes** (from an independent review of the simulator, selection and report):
  - sessions are grouped by harness, model and role, so one model name under two harnesses is two groups; a session
    that switched models is still replayed with every turn priced at the model that served it (and its logged cost
    likewise);
  - `--max-threshold` below the grid start is an error (`InfeasibleCap`) instead of recommending the last grid point
    above the cap (`50k` used to select `350k`);
  - re-selecting a profile (`--profile`, `--value-of-time`, the service) keeps deep mode's measured recall
    guardrail: the lowered pick and the `deep_recall_below_floor` flag no longer revert to the lite choice;
  - a time model on the prior also uses the prior's re-discovery step; one 600-second sample no longer moves it;
  - re-discovery calibration only uses compactions with a full 20-turn window before and after and no other
    compaction inside (a boundary at turn 10 sliced backwards: constant-repeat work gave rho 0.377, now 0);
  - the report's per-session replay runs on every turn and thins the drawn line afterwards (it stepped through 160
    growth bins and counted 160 compactions against the engine's 266); the session picker lists the group's own
    harness only;
  - the terminal `c` copy and the report's Copy button use the harness's recommender, so a DeepSeek Harness
    threshold copies a `modelPolicies` entry instead of Claude's `modelSettings` JSON.
- **Config-writing and service safety fixes** (from an independent review of `apply` and `serve`):
  - a running service re-reads and validates the consent file before every auto-apply, so `consent clear` or a
    passed `expires` stops writes without a restart (the reason shows under `last_apply`);
  - backups under `~/.compactlab/backups` are created 0700 / 0600 instead of the process umask;
  - `consent set` stores the settings file as an absolute path, and a stored relative path is refused;
  - Codex `config.toml` edits are string-aware (quoted keys, signed values, `key = ...` lines inside multi-line
    strings) and parsed before writing; invalid TOML or a non-integer value is refused;
  - `max_change_pct` compares with the value in force, including an inherited top-level `autoCompactWindow`, and
    `0` is a real limit;
  - auto-apply picks only grid points within `--max-step`, so rounding can no longer exceed it (960k → 850k);
  - an error in the auto-apply phase (e.g. a malformed settings file) backs off and shows in status instead of
    killing the scheduler thread.
- **Input parsing, pricing, deep-mode and lens-mod fixes** (from an independent review):
  - a recognized Claude Code or Codex record with a non-numeric, negative or non-finite token count (e.g.
    `"input_tokens":"oops"`) is skipped and counted in `parse_errors` instead of aborting the whole scan;
  - refreshed or overridden prices re-scale the carried 1-hour cache-write and long-context tiers to the new base
    prices, so a refreshed Opus input no longer keeps the old 1-hour write price;
  - `pricing refresh` drops rows with negative, NaN/infinite or non-numeric rates or invalid windows (and fails
    if none are left), and replaces the cached file atomically, so a bad fetch cannot clobber good prices;
  - deep mode: `--budget` now also stops the run as soon as the measured spend after a call exceeds the cap (it
    used to check only the estimate before a call), and the Claude Code executor passes `max_tokens` as
    `CLAUDE_CODE_MAX_OUTPUT_TOKENS`. Codex has no per-call output limit, so there one call can still overshoot;
  - deep mode probes no longer put the expected file path in the question (an empty summary scored 100%): every
    probe asks the same open question, answered once and scored against each file;
  - the Claude Code lens mod re-reads the lens file after every turn, so a recalibration shows up in a running
    session even for the same model.
- **Cache TTL and write price from the logs.** The replay assumed a 5-minute prompt cache priced at the 5-minute
  write tier, but Claude Code on a subscription caches the main session for 1 hour, billed at 2× the input price.
  The Claude Code parser now keeps each request's 1-hour share (`usage.cache_creation.ephemeral_1h_input_tokens`,
  `Turn.cache_write_1h`); a group whose written tokens are mostly 1-hour is replayed with a 1-hour TTL and the
  1-hour write price (new `cache_write_1h` price rows for Claude models). Groups record `cache_ttl` and
  `cache_ttl_source`, shown in the CLI notes and the report; the actual cost prices each write at its tier.
  `--cache-ttl` still overrides. On the maintainer's history the balanced pick moved from 550k (−33%) to 625k
  (−22%) for Opus 5.5 and from 575k (−24%) to 650k (−15%) for Sonnet 5.5. Parser version 4 re-parses cached files.

### Added
- **`verify` for a change made by hand.** `compactlab verify --applied-at WHEN --model M --new 625k [--old N]`
  builds the manifest from what you give (Codex: no `--model`), since `apply` was not used. The output notes that the
  time and old value are yours, and a new `already` verdict says when the sessions before that time already compacted
  near the new threshold (the settings file's modification time is not when a setting took effect).
- **`share --verify` and schema `compactlab.share/2`.** A record can carry a per-group `verification` block with
  what `verify` found (predicted vs logged cost and compactions per 1k turns, interval, whether the setting was in
  effect, sample sizes). The checker accepts `/1` and `/2`; `aggregate_shares.py` adds the median prediction error.
- **`compactlab verify`: check a change against the logs.** Splits sessions at the last `apply` (manifest time; `--id`
  for another) and compares, per model and role, logged cost and compactions per 1k turns before vs after, with an
  80% bootstrap interval over sessions, against a replay of the before sessions at the old and new threshold (no data
  from after) and a replay of the after sessions (shows a changed workload). Reports whether compactions land near
  the new threshold (a setting overridden elsewhere shows as "far"), says "too early" below 3 sessions / 2 active days
  per side, and drops sessions that span the change. Cost only; wall-clock is not verified.
- **`compactlab share`: an anonymous, opt-in record of the estimated savings.** Writes `compactlab-share.json`
  with, per harness / model / role, the thresholds compared and the estimated change per day (each profile vs the
  harness default and vs your current setting, and your current setting vs the default), plus trust markers
  (history length, time-model source, replay cost over logged cost). Figures are rounded; no session ids, paths,
  timestamps, curves or text; unknown model names become `other`; demo data is refused. `share --check FILE`
  validates a record against an allow-list schema (`compactlab.share/1`). New "Share my estimated savings" issue
  form, a workflow that validates and labels submitted records, and `scripts/aggregate_shares.py` for medians per
  group. Nothing is sent by the tool. See `docs/SHARING.md`.
- **DeepSeek Harness (`dsh`) adapter, experimental.** Reads `$DSH_HOME/sessions` (default `~/.dsh/sessions`): the
  newest `session[.vN].jsonl[.zstd]` per session, zstd via the standard library on Python 3.14+ or the `zstd`
  command (a torn last frame is dropped), disjoint usage counts, `compaction/*` events (failed ones dropped),
  subagents and seeded forks. Default rule: the lower of 80% of the window and window − 256k output reserve − 65,536
  (727,040 on a 1,048,576 window). `recommend --harness dsh` prints a compaction-basic `modelPolicies` entry
  (`thresholdRatio`, `headroomTokens: 0`, explicit `maxTokens`); `apply` does not write dsh config. DeepSeek
  V4 Flash / Pro pricing rows (off-peak list price). `--dsh-root`; `doctor` reports the zstd decoder. The scan cache
  re-parses changed compressed files instead of resuming them (`Adapter.resumable`, `Adapter.read_records`).
- docs/ROADMAP.md.

### Changed
- Install instructions use the GitHub source (`uv tool install git+https://github.com/LemonCANDY42/compactlab`,
  `pipx install git+…`, or `uvx --from git+… compactlab demo`); compactlab is not on PyPI. A test keeps
  PyPI-style install commands out of the docs. The terminal SVGs show the command they ran.
- **Terminal views.** A stdlib-only renderer (`compactlab.term`: truecolor / 256 / 16 colours, NO_COLOR,
  `--plain`, `--color`, `--unit`, CJK-aware widths 30..200) gives `demo`, `simulate`, `recommend`, `doctor` and `serve`
  a fastfetch-style header and per-model panels (threshold gauge, was/now bars, cost sparkline, profile chips,
  recommendations with keep-default reasons). Piped output keeps the full plain tables.
- **`compactlab top`.** A live curses view (tree, braille cost/time chart, meters; keys for profile, unit, filter,
  copy, recalibrate, help; mouse; one static frame off a TTY). `serve` writes an endpoint file for it.
- `docs/assets/demo.svg` and the new `docs/assets/top.svg` are rendered from the real CLI output.
- `compactlab serve` is quiet: one in-place progress line only on a terminal (cut to its width at a space, so it
  never wraps into extra rows), nothing otherwise; the log says `recalibrating (reason)` and `recalibrated in Ns:
  N files, M cached, K parsed, X groups; next calibration: <time>`, and the next calibration at start.
- **One percent base.** Overview cards, the chart's Total rows and the sidebar badges all divide the summed change by
  the summed default of the models marked "set" (they disagreed: −21% vs −24% for the same −$185.3/day).
- **Before / after on the Overview.** Per model, a grey "was" bar (default) with the "now" bar (profile) overlaid,
  the saving hatched green and additions amber, for cost and wall-clock per day; a total row from summed amounts;
  `absolute | each row = 100%` and sort toggles; screen-reader sentences. The model page shows a compact pair.
  A dumbbell style (was ring → now dot, green or amber line) shares everything; default from nine rows up.
- **Percent first.** Changes lead with their percent of the harness default (cost vs the default's window cost,
  time vs its total wall-clock: the engine's `d_cost_pct` / `d_time_pct`) with the per-day amount secondary, in the
  report (header toggle `% | $`, remembered) and the lens mod (`▼−61% (−$204.8/day)`). lens.json gains `cost_pct` /
  `time_pct`. Totals divide summed amounts by summed defaults; zero references fall back to amounts.
- **Signed deltas everywhere.** Every change against the default or current setting shows arrow and sign
  (`▼ −$429.3/day`, `▲ +5.8 min`) in the report and the lens mod; absolute figures show neither. Chart tooltip rows
  name their basis (window total vs per-day delta), in English and Chinese.
- **Report review fixes.** Overview totals count only groups whose action is "set" (keep-default rows greyed, also in
  sidebar badges); one answer line with a Copy button in the sticky bar replaces the money and time KPIs; ▼ saves /
  ▲ adds instead of minus signs, with a key on the chart; chart labels de-overlapped (halos, side away from the
  curve, panel titles at the left, at least four ticks); current setting shown or "no current setting found";
  sessions filtered to the group; per-role no-change reasons; labelled thresholds, session counts and note glyphs;
  phone captions restored; darker-mode contrast; charts refit with a ResizeObserver.
- **Report layout.** Opens on the model with the largest per-day impact (never one that needs no change). Each
  model page leads with one chart: $/day and min/day vs the default over the threshold axis, session peaks behind,
  the recall-floor zone hatched, marks for default, current and the three profiles, and a draggable cursor with the
  slider under it; then profile cards, KPIs and the table. Sidebar is a tree (harness > family > model, role chips,
  impact badges, search, remembered folds, a collapsed "No change needed" bucket, drawer on phones) with an
  Overview page (per-model bars and a roll-up of recommended settings). Models that never compact get a short
  state and a peak chart instead of $0 cards. Warning triangles only for real guardrail flags. See
  docs/REPORT_UI.md.
- **Profiles replace tiers.** Three profiles, always computed side by side: `cost` ($10/h of agent time),
  `balanced` (default, $250/h) and `time` ($600/h). `--profile` on simulate / report / recommend / apply / retune
  picks the one to lead with or write; `--value-of-time` overrides its price of time and re-selects from a saved
  result without replaying. `standard` / `headroom`, `--tier`, `--headroom`, `--headroom-cap` and `--view` are gone
  (`--max-threshold` replaces `--headroom-cap`). Result schema 4.
- Why: with $10/h the old `headroom` pick on real data compacted several times more often than the default and
  added many hours of compaction wait per month.
- Guardrails: `balanced` never below 40% of the effective max window, `time` never below 50%; both keep added
  compaction wait under 30 min/day vs the default (`--wait-cap`); `cost` warns instead. Recall floor kept.
- Low-confidence latency fits (fewer than 20 timed compactions, thin turn bins) no longer drive the choice: the
  group uses the pooled harness fit or a pessimistic prior, and says which.
- "keep default" also when a profile does not beat the default on its own score.
- Comparisons now include minutes/day, added compaction wait/day and compactions/day, against default and current.
- Report: profile switch, three profiles side by side with flags, copy panel per profile. CLI/report state that
  costs are list-price estimates and recall is a lite-mode proxy.
- Report UI: the threshold slider can be dragged continuously (the control bar is built once and updated in place
  instead of being re-rendered on every input event); sticky control bar; slider marks for the three profiles,
  the default and the current setting (click to snap); hover/tap tooltips on the charts (click selects); focus and
  scroll kept across re-renders; copy button feedback and a file:// fallback; 44px touch targets on narrow or
  touch screens; reduced-motion and focus-ring support; no horizontal scroll at 375 / 768 / 1440 px. Regression
  tests run the report in headless Chrome (skipped when Chrome is absent).
- Report redesign: warm off-white / near-black themes from CSS tokens (system preference plus a manual
  Auto/Light/Dark toggle), one clay accent, hairline borders, a system serif for titles and tabular mono numbers,
  a group sidebar that becomes a top bar on phones, a header with date range, data source and mode, KPI cards,
  calmer charts with direct labels instead of legends, and plain-language callouts for thin data, short histories,
  borrowed time models and guardrail flags (red only when a guardrail is broken). States it is an independent
  open-source tool. Still one offline file; slider, sticky bar, tooltips and a11y behaviour unchanged.
- Report shows less text: inline SVG icons (coin / scale / clock for the profiles, refresh, shield, calendar),
  delta chips with ▲▼, gauges for recall and for the threshold as a share of the max window, ⓘ and ⚠ icon buttons
  with tooltips (also their accessible names) instead of paragraphs and banners, one language at a time
  (browser language by default), and a one-line footer disclaimer. Tests check every icon is named or hidden.
- docs/assets adds report-dark.png, report-mobile.png and report-mobile-dark.png.
- Headless Chrome in tests and `scripts/make_assets.py` runs with `--use-mock-keychain` so it never asks for the
  macOS keychain.
- Synthetic demo latencies are closer to real logs (compaction ≈ 90 s + 0.4 s per 1k tokens).

### Fixed
- compactlab-lens: the band wrapped mid-token in narrow terminals (`┃525` / `k`). Rows are now packed to the band's
  real width (`bodyColumns`), counting wide glyphs as two columns, and break only between whole pieces. The compact
  form starts below 60 columns and the two-row form below about 40. The unfolded card follows the same rules, and
  tests cover 30–120 columns in both languages.

### Added
- **Service mode.** `compactlab serve` is a stdlib-only service that binds loopback only (other binds are refused
  without `--unsafe-bind`) and checks the Host header. It serves the live report, `/api/result`,
  `/api/profile/<name>` and `/api/health`, plus a token-guarded `POST /api/recalibrate`. It recalibrates in lite mode
  every `--every` (s/m/h/d/w or off): incremental scan, single-flight, backoff after failures, lower priority, a
  rotating log, no network.
- `compactlab recalibrate` (one run).
- `compactlab schedule print|install|status|uninstall` for launchd and systemd user units. It writes only into the
  user's unit folder, never loads them, and never overwrites a foreign file.
- `compactlab consent set|show|clear` and `--auto-apply` / `--dry-run`. Auto-apply has minimum history, a low-confidence
  skip, a dead-band and minimum benefit, a 100k max step, a 7-day cooldown, guardrail checks per step and manual-edit
  detection (`notify`, never overwrite). It reuses apply's backup and manifest, adds a change journal, and `revert`
  understands it.
- The report shows calibration age and next run, a recalibrate button when served, and a drift marker. The lens mod
  shows calibration age (amber when stale). See docs/SERVICE.md.
- Larger default picks: `balanced` now prices time at $250/h with a 40% minimum window, `time` at $600/h with 50%
  (were $100/h with 25% and $400/h with 35%). The report shows a guardrail that merely moved the pick as an info
  icon instead of a warning; warnings are reserved for flags that need attention.
- **compactlab-lens**, a Claude Code mod in `mods/compactlab-lens` (with `.claude-plugin/marketplace.json` at the
  repository root). It shows one row above the prompt, expandable, and a pane on desktop: this session's context as a
  dot bar with ticks at the default and the profile threshold, a context-per-turn sparkline (an SVG on desktop),
  $/day and min/day chips vs default and current, compactions so far, a ⚠ when the window in force differs, a
  profile switch that only changes the display, and a button that copies `/autocompact <n>`. It is read only: one
  local file and the engine's own figures, with no network, no model calls and no settings writes. It is an
  independent project, not affiliated with Anthropic. See docs/MOD.md.
- `simulate --lens-out` (default `~/.compactlab/lens.json`): the small per-model summary the mod reads.
- **Per-day impact** for every profile of every group, vs the harness default and vs the current setting: USD,
  minutes (compaction wait and turn-speed gain separately) and compactions, per calendar day, per active day and
  per month. In the report (new section, basis switch), in `recommend` (table after the snippets, `impact` in
  `--json`) and in the result (`comparison.<profile>.vs_default.impact`). Groups record `active_days` and `span`.
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
