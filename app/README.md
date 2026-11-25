# compactlab menu bar app

A read-only macOS menu bar app for compactlab (Phase 1 of [the design](../docs/MENUBAR_APP.zh-CN.md)). It shows how
close each active Claude Code and Codex session is to auto-compaction, whether its prompt cache is still warm, and
the last calibration's threshold for every model, and recalibrates once a day in the background.

It is a thin client: every number comes from the `compactlab` command-line tool through `compactlab engine`
([ENGINE_API.md](../docs/ENGINE_API.md)). The app parses no logs, writes no settings and uses no network.

## Requirements

- macOS 14 or later.
- The engine, 0.1.0 or later: `brew install LemonCANDY42/tap/compactlab` or
  `uv tool install git+https://github.com/LemonCANDY42/compactlab`. The app looks for `compactlab` in Settings → Engine,
  then on PATH, `~/.local/bin`, `/opt/homebrew/bin` and `/usr/local/bin`.

## What it does

| Part | How |
| --- | --- |
| Menu bar icon | A half-circle gauge filled to the fullest session active in the last hour (share of its threshold); amber at 85%; grey with no recent session; `!` when the engine is missing or the session watcher stopped. Optionally the percentage beside it. |
| Active sessions | One resident `compactlab engine watch` (about 30 MB). FSEvents on the log directories (2 s coalescing) sends it `refresh`; nothing polls. Subagents are counted under their main session. A cold cache shows what the next send rewrites and a button that copies `/compact`. |
| Thresholds | `compactlab engine result`, one row per model and role (Codex: its one key), plus Claude Code's idle compaction. The profile menu re-selects without a replay. |
| Calibration | `compactlab engine calibrate` as a child process that exits when done: when the service schedule says it is due (daily; after a failure, 5 minutes doubling), on wake for a missed one, or from the button. Put off in Low Power Mode or on battery below 20%. It shares one lock with the CLI and `compactlab schedule`: while another holds it, nothing runs and the panel says so. Times out after 10 minutes; can be cancelled. |
| Report | `compactlab report --result …` into `~/.compactlab/app/report.html`, opened in the default browser. |
| Settings | Open at login (`SMAppService`), percentage in the menu bar, project names (shown only in the panel), engine path, restart, copy diagnostics (no session content or project names). |

If the watcher exits it is restarted; three exits within a minute stop it with a message and a Restart button.
An engine that speaks another contract version is shown read-only with a note.

## Build and test

```bash
cd app
swift test                       # contract fixtures (tests/contract), presentation, processes against fake engines
swift run compactlab-app         # run from the build directory
scripts/package_app.sh           # build/compactlab.app, signed ad hoc
```

`compactlab-app --render-panel out.png [--wait 20]` starts the engine as usual, draws the panel into a PNG and exits
(0 when the engine was usable), printing the diagnostics. CI uses it as a launch smoke test, with an empty home and
with the demo logs and result.

## Signing and notarization

`scripts/package_app.sh --universal` signs with `CODESIGN_IDENTITY` (a Developer ID Application identity) when it is
set, with the hardened runtime and a secure timestamp; `scripts/notarize.sh compactlab.app` notarizes, staples and
zips it, given an App Store Connect API key (`NOTARY_KEY`, `NOTARY_KEY_ID`, `NOTARY_ISSUER`). The app is not
sandboxed: it reads `~/.claude` and `~/.codex` and runs the engine.

The `App` workflow does this on pushes to `main` when these repository secrets exist, and uploads the zip as an
artifact; otherwise it uploads an ad-hoc build:

| Secret | Value |
| --- | --- |
| `DEVELOPER_ID_CERT_P12` | the Developer ID Application certificate and key, exported as .p12, base64 |
| `DEVELOPER_ID_CERT_PASSWORD` | the .p12 password |
| `NOTARY_KEY_P8` | an App Store Connect API key (.p8), base64 |
| `NOTARY_KEY_ID`, `NOTARY_ISSUER` | that key's ID and issuer ID |

The bundle identifier is `io.github.lemoncandy42.compactlab` (`BUNDLE_ID` overrides it).

## Not yet

Phase 2: the review page, apply and revert, verification cards, notifications, drift detection. Phase 3: DMG,
Homebrew cask, Sparkle updates, Chinese localization, an app icon.
