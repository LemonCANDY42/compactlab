# Engine API

`compactlab engine …` is the interface the compactlab menu bar app (and any other tool) uses. Each command prints
JSON on stdout and nothing else; logs and tracebacks go to stderr. Nothing here needs the network or a port.

```bash
compactlab engine hello                   # versions, contract version, where the logs and configs are
compactlab engine sessions                # the active sessions now
compactlab engine watch                   # the same as JSON Lines while running; commands on stdin
compactlab engine result [--profile time] # the last calibration, summarized
compactlab engine calibrate --reason app  # run one lite calibration (like `recalibrate`), then print its summary
compactlab engine plan                    # what `apply` would change for the calibrated pick, with its hash
compactlab engine apply --expect-plan H   # write exactly that plan (or one change of it with --only)
compactlab engine revert [--id ID]        # undo an apply from its manifest
compactlab engine history                 # the latest applies and what each key holds now
compactlab engine verify [--id ID]        # an apply's predicted effect against the logs since
compactlab engine consent show|set|clear  # the auto-apply consent (Claude Code)
```

## Contract

Every object has `engine_api` (the contract version, now `1`) and `kind`. Within one version fields and kinds are only
added, never removed, renamed or retyped. A consumer must ignore fields and kinds it does not know, and should refuse
to write anything when `engine_api` differs from the one it was built for (reading can go on). Times are Unix seconds
(floats), token counts are integers, and a missing value is `null`.

`tests/contract/` holds an example of every kind. The Python tests check every command's output against them (a lost
or retyped field fails as a breaking change), and the app's tests decode the same files. After adding a field,
regenerate them with `COMPACTLAB_UPDATE_CONTRACT=1 pytest tests/test_engine.py`.

Exit codes: 0 success; 1 an `error` object was printed; 2 a bad option (`bad_args`, or argparse's usage text on
stderr for an unknown command).

## `hello`

Run it first. `version` is the compactlab version, `commands` the engine commands this version has,
`live_harnesses` the harnesses `sessions` and `watch` read. `consent` is the stored auto-apply consent (`null` when
there is none or it has lapsed); a consumer turns on writing only when `commands` has `plan` and `apply` (0.2 on). `harnesses` lists every adapter with its log roots and
config file (`exists` says whether each is there). `result` is where `calibrate` keeps its result, `service` the
calibrator's bookkeeping: `last_calibrated`, `last_attempt`, `failures` (in a row) and `last_error`.

## `sessions`

Sessions with a logged record in the last `--active-hours` (default 6), newest first. Per session:

| Field | Meaning |
| --- | --- |
| `harness`, `id`, `role`, `parent_id` | `role` is `main` or `subagent`; a subagent's `parent_id` is its main session |
| `project` | last part of the logged working directory; `null` with `--no-project-names` |
| `model`, `platform` | model of the latest request; where it ran, when the log says (Codex `model_provider`) |
| `ctx` | context size of the latest request (input + cache read + cache write); `null` right after a compaction |
| `compacted_at` | when the session compacted after its latest request, else `null` |
| `window`, `window_source` | the context window and where it came from (the log, the price data, a Claude Code rule) |
| `threshold`, `threshold_source`, `threshold_note` | the auto-compact point in force: `configured` (capped at what the harness honours) or `default`, with the setting's location or the harness rule |
| `last_request_at`, `last_activity_at` | time of the latest request; time of the newest record (the file's mtime only when no record is dated: copying or syncing a log moves its mtime) |
| `cache_ttl_s`, `cache_expires_at` | prompt-cache tier of the latest request that wrote cache (3600 or 300; Claude Code only) and when it goes cold without another request; `null` when the log does not say |

Top level: `at`, `active_s`, `unreadable` (files that could not be read this time) and `idle_compaction`
(Claude Code's setting: `enabled` is `null` when not set, which means on; `min_tokens` is the smallest context it
compacts on return).

How it reads: the same parsers as the analysis, but only the last 256 KiB of each recently written file, plus the
records a parser needs from the top of the file (Codex's `session_meta`). It reads further back (up to 32 MiB) when
that holds no complete request, or when the newest Claude Code record is more than 10 minutes after the latest
request found. Claude Code sometimes appends copies of earlier messages with their original times (before a
`/compact`, for example); the full parse drops them as already seen, so the latest request here is the newest by
time, and a deeper read finds it when the copies fill the tail. On 85 real files (the 83 active ones and a 109 MB log
ending in 20 MB of copies) the latest request's time, context and model, the ids, role, parent, compaction state and
working directory matched a full parse in every case.

Known gaps: while Claude Code is still writing such copies and nothing newer has followed them (about the minute a
`/compact` takes), or when the copies exceed 32 MiB, the latest request shown can be an older one. A log written to
again after two quiet days (a resumed session) can take up to 10 minutes to show up.

Finding files is incremental: a full walk every 10 minutes; in between, only directories whose mtime moved are listed
again and only files written in the last two days (or `--active-hours`, if longer) are stat'ed. A file whose size and
mtime are unchanged is not read again.

## `watch`

A resident process. stdout: one `hello` line, then a `sessions` line at start and whenever the sessions change
(`at` alone changing does not count). stdin, one command per line:

| Command | Effect |
| --- | --- |
| `refresh` | files changed (e.g. an FSEvents callback): rescan, print only if something changed |
| `snapshot` | rescan and print even if nothing changed |
| `ping` | prints `{"engine_api": 1, "kind": "pong"}` |
| `quit` | exit 0 |

End of input, or a closed stdout, also exits 0, so the process never outlives its parent. Scans are at least `--min-interval` seconds
apart (default 5; requests in between merge into one). After `--interval` quiet seconds (default 60; 0 = never) it
scans anyway, so sessions that went quiet drop out. An unknown command prints a `bad_command` error, and a failed
scan a `scan_failed` error; neither stops the process.

Measured on a heavy history (3,200 log files, 83 active sessions): peak memory footprint 32 MB, of which about
28 MB is the interpreter and the CLI's imports; about 1.5% of one core while sessions are being written and it gets a
`refresh` every 2 s, and nothing between scans.

## `result` and `calibrate`

`result` reads the calibrator's `<state-dir>/service/result.json` (or `--result`); `--profile` re-selects without a
replay. `calibrate` runs one lite calibration with the same options and auto-apply rules as `recalibrate`, writes
the same files, and prints the same object. It first takes the state dir's lock (`<state-dir>/service/lock`), which
every calibration and config write shares: the CLI, `serve`, a launchd/systemd schedule and the app. While another
process holds it, `calibrate` runs nothing and prints `busy` at once (exit 1); the message names the job, its pid and
when it started. Retry later; a busy run is not a failure in `service`. The lock is released when its holder exits,
crashes included. Fields:

The result keeps the latest run of every harness. A calibration replaces the harnesses it scanned (all of them
unless `--harness` limits it, a harness with no logs in the window included, so its rows disappear) and keeps the
others as they were; a harness whose logs could not be read keeps its rows. Kept groups keep their picks, and
`--profile` re-selects each harness under the options of the run it comes from. `lens.json` follows the same rule.

- `calibration`: `at`, `took_s`, `reason`, `every_s`; `mode`, `data_through`, `demo`, `profile` (the selected one).
  These describe the latest run.
- `harnesses`: per harness with groups, the run they come from: `mode`, `data_through` (its newest turn analysed)
  and `calibration` (`null` for a run that was not a calibration, such as `simulate`).
- `groups`, one per harness, model and role: `selected`, `default_threshold`, `current_threshold` (configured or
  `null`), `current_note`, `effective_max`, `max_window`, `window_source`, `platform`, `days`, `active_days`,
  `n_sessions`, `n_turns`, `no_action_needed`, `superseded_by`, and `profiles.<cost|balanced|time>` with
  `threshold`, `action` (`set`, `keep current`, `keep default`, `no action needed`), `flags`, and `vs_default` /
  `vs_current` impact (USD and minutes per day and per active day, compactions per day, cost and time change as a
  share; `null` when there is nothing to compare).
- `idle_compaction`: the verdict on Claude Code's idle compaction; `null` when no group has the idle gaps it acts
  on.
- `global_keys`: the single decision for a harness with one key for every model (Codex), per profile.
- `model_notes`: models priced from another row, unset models, window and default changes, superseded models,
  recent price changes.
- `service` (as in `hello`) and `auto_apply` (what `calibrate --auto-apply` decided, else `null`).

The result never contains transcript text, paths or project names.

## Errors

`{"engine_api": 1, "kind": "error", "error": {"code": "...", "message": "..."}}`

| Code | When |
| --- | --- |
| `no_result` | `result` before any calibration |
| `bad_result` | the result file is unreadable or from an older compactlab |
| `calibration_failed` | `calibrate` failed (no sessions, a malformed config, …); the message says why |
| `busy` | `calibrate`: another process holds the state dir's lock (a calibration, `apply --write`, `retune` or `revert`); nothing ran |
| `bad_args` | an invalid option value |
| `plan_changed` | `apply`: the plan is no longer the one with `--expect-plan`'s hash (a calibration moved the pick, the file changed); nothing written |
| `refused` | `plan` / `apply` / `revert` / `consent`: a config file that cannot be edited safely, nothing to change, no settings file |
| `no_apply` | `revert` / `verify`: no apply manifest (or not the one `--id` names) |
| `no_sessions` | `verify`: no sessions in the window |
| `bad_command`, `scan_failed` | `watch` only; it keeps running |
| `internal` | an unexpected exception (traceback on stderr); `watch` prints it too when it cannot start |

## Changing settings: `plan`, `apply`, `revert`, `history`, `verify`, `consent`

These go through the same code as `compactlab apply` / `revert` / `verify` (backups, atomic writes, manifests; see
[SAFETY.md](SAFETY.md)). The targets are each harness's config file (`--claude-settings`, `--codex-config`, or the
default the adapter finds), every main-session model of the result, and the usual bounds (100k–1M). The person who
clicks Apply on a reviewed plan is the authorization; nothing here writes without `--expect-plan`.

- `plan [--profile P] [--only HARNESS:MODEL ...]`: `profile`, `plan_hash`, `changes` (per change: `harness`, `model`,
  `file`, `key`, `old` (`null` when unset), `new`, `plan_hash` (of that change alone) and `impact`: `against`
  (`current` or `default`), `usd_per_day`, `min_per_day`, `cost_pct`, `time_pct` (negative: cheaper / faster; the
  share is `null` for Codex's one key), `days`, `sessions`), `skipped` (reasons), `diff` (unified) and `notes`. Only
  picks whose action is `set` become changes.
- `apply --expect-plan HASH [--profile P] [--only ...]`: takes the state dir's lock, plans again and writes only if the
  hash is unchanged (`plan_changed` otherwise), then records the write as auto-apply does (`service/applied.json`,
  `service/journal.jsonl`), so auto-apply's manual-edit check and cooldown count it. Prints `applied`: `id` (the
  manifest) and `changes`. To apply one change of a plan, pass its `--only harness:model` and its own `plan_hash`.
- `revert [--id ID]` (default: the latest apply): under the lock, restores the file from the backup when nobody has
  touched it since, else only the keys still holding what compactlab wrote. Prints `reverted`: `id`, `log` and
  `complete` (`false` when a key edited since was left alone).
- `history [--limit 10]`: `applies`, newest first: `id`, `at`, `by` (`app`, `consent` for auto-apply, or the CLI's
  preauth), and per key `old`, `new`, `now` (what the file holds now) and `status`: `in_effect`, `reverted`, or
  `changed` (someone edited it since: drift).
- `verify [--id ID] [--since-days N]`: `verification`: `id`, `applied_at`, and per model `verdict` (`consistent`,
  `less`, `more`, `untouched`, `already`, `unclear`, `too_early`, `no_data`), `summary`, `in_effect`, `cost_pct`,
  `cost_pct_predicted`, `sessions_before`, `sessions_after`. It reads the logs (with the scan cache); the app runs it
  at most daily and passes a window around the change.
- `consent show|set|clear [--models M ...] [--profile P]`: `consent` (as in `hello`). `set` stores a consent for
  Claude Code's settings file; `--models` defaults to `*`, every model, including ones that appear later.
  `calibrate --auto-apply` then applies within the auto-apply rules (7 days and 500 turns of data, at most 100k per
  step, a 7-day cooldown, never over a value someone else set).

The CLI keeps its own form: `compactlab apply --json` prints the plan with `plan_hash`, and
`compactlab apply --write --expect-plan <plan_hash> --json` writes only if the plan is still that one (exit 2, or 75
for `busy`).

## Privacy

`sessions` and `watch` print project names (the last part of the working directory) to their caller only; they
write no file. Everything else follows [SECURITY.md](../SECURITY.md): no transcript text in any output, no network.
