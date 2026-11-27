# compactlab menu bar app

A macOS menu bar app for compactlab, in English and Simplified Chinese ([design notes](../docs/MENUBAR_APP.zh-CN.md), in Chinese).
It shows how close each active Claude Code and Codex session is to auto-compaction, whether its prompt cache is still
warm, and the last calibration's threshold for every model; recalibrates once a day in the background; and lets you
review a suggested change, apply it, check it against your logs later, and undo it.

It is a thin client: every number comes from the `compactlab` command-line tool through `compactlab engine`
([ENGINE_API.md](../docs/ENGINE_API.md)), and the engine's `apply` is the only thing that writes a harness setting
(backup, manifest, undo). The app parses no logs and uses no network.

<p align="center">
  <img src="../docs/assets/app-panel.png" alt="The menu bar panel: one suggested change, active sessions and the threshold for each model and role (synthetic demo data)" width="380">
  <img src="../docs/assets/app-review.png" alt="The review window: default to 500k for opus-5.5, its saving per day, the data behind it and the setting it writes, with Apply (synthetic demo data)" width="440">
</p>

<sub>Synthetic demo data. Chinese: [panel](../docs/assets/app-panel.zh-CN.png), [review](../docs/assets/app-review.zh-CN.png).
Regenerate with `scripts/screenshots.sh`.</sub>

## Requirements

- macOS 14 or later.
- The engine, 0.1.0 or later (0.2 or later to change settings from the app): `brew install LemonCANDY42/tap/compactlab` or
  `uv tool install compactlab`. The app looks for `compactlab` in Settings → Engine,
  then on PATH, `~/.local/bin`, `/opt/homebrew/bin` and `/usr/local/bin`.

## What it does

| Part | How |
| --- | --- |
| Menu bar icon | A half-circle gauge filled to the fullest session active in the last hour (share of its threshold); amber at 85%; grey with no recent session; `!` when the engine is missing or the session watcher stopped. Optionally the percentage beside it. |
| Active sessions | One resident `compactlab engine watch` (about 30 MB). FSEvents on the log directories (2 s coalescing) sends it `refresh`; nothing polls. Subagents are counted under their main session. A cold cache shows what the next send rewrites and a button that copies `/compact`. |
| Thresholds | `compactlab engine result`, one row per model and role (Codex: its one key), plus Claude Code's idle compaction. The profile menu re-selects without a replay. |
| Calibration | `compactlab engine calibrate` as a child process that exits when done: when the service schedule says it is due (daily; after a failure, 5 minutes doubling), on wake for a missed one, or from the button. Put off in Low Power Mode or on battery below 20%. It shares one lock with the CLI and `compactlab schedule`: while another holds it, nothing runs and the panel says so. Times out after 10 minutes; can be cancelled. |
| Report | `compactlab report --result …` into `~/.compactlab/app/report.html`, opened in the default browser. |
| Suggested changes | `compactlab engine plan` after each calibration: the panel's first row says how many and what they save, and Review… opens a window with each change (old → new, $ and minutes per day against the setting in force, days and sessions of data, the key and file), the exact edit, and Apply (one change or all). Apply runs `engine apply --expect-plan <hash>`: the engine plans again under its lock and writes nothing if the plan moved. "Remind me in a week" mutes the suggestion. |
| Last change | `engine history`: what the newest apply changed and whether each key still holds it (in effect, undone, or changed by someone else since), its verification, and Undo (`engine revert`, which leaves keys edited since alone). |
| Verification | `engine verify` for the newest change once it is a day old, at most daily, until the verdict is final: as predicted, saved less or more, no effect, too early. |
| Notifications | Each posted once: a suggestion the last two calibrations agree on (at least 2% or 2 minutes a day), with Review; a setting auto-apply wrote, with Undo; a setting compactlab wrote that someone else changed; a verification result; three failed calibrations in a row. A dot on the menu bar icon while a suggestion waits (a changed setting stays visible in the panel instead). Each kind can be turned off. |
| Auto-apply | Off by default. On: the engine's consent for Claude Code (`engine consent set`), and each calibration runs with `--auto-apply`: at least 7 days and 500 turns of data, at most 100k per step, at most weekly, never over a value someone else set. Codex is only changed when you click Apply. |
| Settings | General: open at login (`SMAppService`), percentage in the menu bar, project names (shown only in the panel). Changes: auto-apply, notifications. Engine: path, restart, copy diagnostics (no session content or project names). |

If the watcher exits it is restarted; three exits within a minute stop it with a message and a Restart button.
An engine that speaks another contract version is shown read-only with a note: nothing is calibrated or changed. An
engine before 0.2 has no `plan`: the app shows the thresholds and says to use `compactlab apply`. The demo result is
never applied.
A calibration that times out or is cancelled backs off like a failed one (5 minutes, doubling); one whose interval
is off (`serve --every off`) turns the app's schedule off too. Before a scheduled run the app re-reads the service
state, so a calibration by the CLI or `serve` postpones it.

Known gap: a log folder created after the app started (the first Codex session, say) is watched only after a
restart; until then its sessions show up through the engine's own scan every 60 seconds.

## Build and test

```bash
cd app
swift test                       # contract fixtures (tests/contract), presentation, processes against fake engines
swift run compactlab-app         # run from the build directory
scripts/package_app.sh           # build/compactlab.app (translations, icon), signed ad hoc
scripts/make_dmg.sh build/compactlab.app   # build/compactlab-<version>.dmg
python3 scripts/check_strings.py # every interface string has a Chinese translation
scripts/screenshots.sh           # docs/assets/app-*.png from synthetic demo data, both languages
```

`swift test` also runs the app against a real engine (calibrate on the demo logs in a scratch home, apply, undo, the
settings file byte for byte) when `COMPACTLAB_TEST_ENGINE` names one; CI sets it.

Translations: English is the key (`Text("…")`, `L("…")`), `Resources/zh-Hans.lproj/Localizable.strings` the Chinese,
using the project's Chinese terms (README.zh-CN.md, the design doc) and macOS's own for system things. The app follows
the system language; System Settings → General → Language & Region → Applications sets it per app.

`compactlab-app --render-panel out.png [--render-review review.png] [--wait 20]` starts the engine as usual, draws the
panel (and the review window) into PNGs and exits (0 when the engine was usable), printing the diagnostics; add
`-AppleLanguages '(zh-Hans)'` for Chinese. CI uses it as a launch smoke test, with an empty home and with a
calibration of the demo logs, in both languages.

## Signing and notarization

`scripts/package_app.sh --universal` signs with `CODESIGN_IDENTITY` (a Developer ID Application identity) when it is
set, with the hardened runtime and a secure timestamp; `scripts/notarize.sh compactlab.app` notarizes, staples and
zips it, given an App Store Connect API key (`NOTARY_KEY`, `NOTARY_KEY_ID`, `NOTARY_ISSUER`). The app is not
sandboxed: it reads `~/.claude` and `~/.codex` and runs the engine.

`scripts/make_dmg.sh` puts the app in a disk image (signed with the same identity), and `scripts/notarize.sh` takes the
image too. The `App` workflow does all of this on pushes to `main` when these repository secrets exist and uploads the
disk image, the zip and a filled-in Homebrew cask as artifacts; on a published GitHub release it attaches the disk
image and the cask to it. Without the secrets it uploads an ad-hoc build (runs only where it was built).
`scripts/set_signing_secrets.sh DeveloperID.p12 AuthKey_<id>.p8 <issuer id>` stores them: run it yourself, it asks
for the .p12 password, checks both files the way the workflow uses them (the notary key against Apple; `--check`
stops there) and passes each value to `gh secret set` on standard input.

| Secret | Value |
| --- | --- |
| `DEVELOPER_ID_CERT_P12` | the Developer ID Application certificate and key, exported as .p12, base64 |
| `DEVELOPER_ID_CERT_PASSWORD` | the .p12 password |
| `NOTARY_KEY_P8` | an App Store Connect API key (.p8), base64 |
| `NOTARY_KEY_ID`, `NOTARY_ISSUER` | that key's ID and issuer ID |

The bundle identifier is `io.github.lemoncandy42.compactlab` (`BUNDLE_ID` overrides it).

## Homebrew

`packaging/compactlab-app.rb` is the cask (`compactlab-app`, needing the `compactlab` formula of the same tap);
`scripts/cask.sh compactlab-X.Y.Z.dmg` fills in the version and checksum. After a release, commit it to
LemonCANDY42/homebrew-tap as `Casks/compactlab-app.rb`; then `brew install --cask LemonCANDY42/tap/compactlab-app`,
and `brew upgrade` updates it. `zap` removes the app's preferences only: `~/.compactlab` (results, backups, manifests)
belongs to the formula.

## Releasing

The engine and the app share the version (`pyproject.toml`), and the app changes settings only with an engine of its
version or later: a cask that reaches people before the formula would open read-only. So the cask goes last.

1. Before tagging, on the release commit: `scripts/package_app.sh && scripts/release_check.sh --source
   build/compactlab.app` installs this checkout the way the formula does (its `git archive`, a fresh venv) and runs
   review → apply → undo through the app against it; the packaged app must open with settings writable.
2. Tag `vX.Y.Z` and publish the GitHub release; the `Publish` workflow uploads the engine to PyPI, and the `App`
   workflow attaches the notarized disk image and the filled-in cask (it fails without the signing secrets).
3. Point `Formula/compactlab.rb` in LemonCANDY42/homebrew-tap at the new tag's tarball and checksum; the tap's CI
   installs it on macOS and Linux.
4. On a Mac with a clean Homebrew install (`brew uninstall compactlab`, then `brew install LemonCANDY42/tap/compactlab`),
   install the app from the release's disk image and run `scripts/release_check.sh "$(brew --prefix)/bin/compactlab"
   /Applications/compactlab.app` (Gatekeeper must accept it). The `Release check` workflow (dispatch it with the tag)
   does the same on a fresh runner with the app built from the tag.
5. Then commit the release's `compactlab-app.rb` to the tap as `Casks/compactlab-app.rb`.

## Left out on purpose

- Sparkle updates: the app uses no network; Homebrew updates it.
- A widget: it needs an app extension, which a plain Swift package cannot build.
- Python inside the app: the engine is installed on its own (Homebrew, uv), which keeps the app small and signing simple.
- Changing idle compaction from the app: `apply` does not write that setting; the panel shows the verdict.
- A language menu: macOS sets an app's language per app (System Settings → General → Language & Region).
