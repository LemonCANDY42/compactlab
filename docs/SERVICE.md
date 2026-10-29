# Running compactlab as a service

`compactlab serve` keeps a calibration current in the background. On a schedule it rescans your session logs (only
files that changed since the last scan; the first scan is the only full one) and reruns the lite analysis. It serves
the live report and a small JSON API on localhost. If you opt in, it also nudges your settings toward the chosen
profile, under the rules below.

```bash
compactlab serve                      # 127.0.0.1, a free port, recalibrate daily
compactlab serve --every 6h --port 8765
compactlab recalibrate                # one run, what a scheduler calls
compactlab schedule print             # show the launchd / systemd files; nothing is written
compactlab schedule install --yes     # write them into your LaunchAgents / systemd user folder (you load them)
compactlab schedule status | uninstall --yes
```

`--every` takes `s`, `m`, `h`, `d` or `w` (at least 60 s), or `off` for manual runs only. After a failure the next try
waits 5 minutes, doubling each time, never longer than the interval. A result is stale once it is older than twice
the interval: the report and the compactlab-lens mod show its age with a clock icon, amber when stale.

## API

| Route | What |
| --- | --- |
| `GET /` | the live report, with "calibrated … ago · next in …" and a recalibrate button |
| `GET /api/health` | status: running, last / next calibration, failures, last auto-apply, what was applied |
| `GET /api/result` | the last result (the same JSON as `simulate --out`) |
| `GET /api/profile/<cost\|balanced\|time>` | the groups re-selected for one profile |
| `POST /api/recalibrate` | start a run now (`Authorization: Bearer <token>`); 202 started, 409 already running |

## Auto-apply (opt-in)

Off unless you both store a consent and pass `--auto-apply`:

```bash
compactlab consent set --file ~/.claude/settings.json --models claude-opus-5-5 claude-sonnet-5-5 --profile balanced
compactlab serve --auto-apply             # or: compactlab recalibrate --auto-apply --dry-run
```

The consent names one settings file, the models whose key may change, and the profile; `--expires` is optional. Only
that harness's auto-compact key (`modelSettings.<model>.autoCompactWindow`, or Codex's
`model_auto_compact_token_limit`) is touched, through the same code as `compactlab apply`. That code keeps a backup,
writes atomically and records a manifest; `compactlab revert` undoes it, key by key. Every change is also appended to
`~/.compactlab/service/journal.jsonl`.

A model's setting changes only when all of these hold:

- at least `--min-days` (7) of history and `--min-turns` (500) turns, and a time model fitted from your own data;
- the profile carries no flag that makes it unsafe (recall floor not met, wait cap unreachable, ...);
- the target is more than one grid step plus a dead-band (25k) away from the current value;
- the move saves at least 2% of the current list-price cost per day, or 2 minutes per day;
- the last change compactlab made there is older than `--cooldown` (7d);
- the value in the file is still the one compactlab last wrote. If someone edited it, the run logs a `notify` line and
  leaves it alone.

Each run moves at most `--max-step` (100k) toward the target, on the grid. A step stays inside the profile's
guardrails (minimum window, wait cap, recall floor). The one exception is a setting that starts outside them: a step
may then still be outside, as long as it is strictly closer than where it started. `--dry-run` prints the decision and
the diff and writes nothing.

## Threat model

- **Reachability.** The service binds loopback only. Any other address is refused unless you pass `--unsafe-bind`.
  Every request must carry a loopback `Host` header, which blocks DNS-rebinding pages.
- **The one write route.** `POST /api/recalibrate` needs the token in `~/.compactlab/service/token` (mode 0600, made
  once). The served page embeds the token so its button works. Another web origin cannot read that page, but any local
  process that can reach the port can. The token keeps browsers out, not local programs running as you.
- **What it does on recalibrate.** It only recomputes. It never writes settings unless auto-apply is on with a stored
  consent, and even then only within the rules above.
- **Reads:** your session logs (the same as `simulate`), the bundled and refreshed price tables, your current
  auto-compact settings (read only, for comparison), and the consent file.
- **Writes:** only under `~/.compactlab/`:
  - `service/` holds the result, state, token, log (rotating, 1 MB × 3), journal and applied values;
  - `lens.json` and the scan cache;
  - backups and manifests, written only when auto-apply changes something.

  With auto-apply, it also changes the consented key in the consented file.
- **Never:** network access, telemetry, deep mode (which calls models and stays a manual, budgeted command), or
  running `launchctl` / `systemctl`. `schedule install` writes the unit files and prints the command to load them;
  you run it. It refuses to overwrite a unit file it did not generate.
- **Footprint:** one analysis at a time (single-flight). It runs at lower priority (`--nice 10`; the generated units
  also set background / idle I/O). It keeps only the last result in memory.
