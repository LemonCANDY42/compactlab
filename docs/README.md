# compactlab documentation

Start with the [README](../README.md) ([简体中文](../README.zh-CN.md)) for what compactlab does and how to install it.

## Using it

| Doc | What it covers |
| --- | --- |
| [GUIDE.md](GUIDE.md) | Profiles, reading the output, per-day impact, every setting key `recommend` emits, apply and `verify`, idle compaction, lite and deep mode, per-harness notes, limitations |
| [SAFETY.md](SAFETY.md) | What `apply` reads and writes, the pre-authorization file, backups, `--expect-plan`, `revert` |
| [SERVICE.md](SERVICE.md) | `compactlab serve`, schedules, the localhost API, opt-in auto-apply and its threat model |
| [TUI.md](TUI.md) | Terminal views and `compactlab top`, with key bindings |
| [MOD.md](MOD.md) | compactlab-lens, the in-session Claude Code mod |
| [SHARING.md](SHARING.md) | `compactlab share`: the anonymous savings record and how to post it |
| [../app/README.md](../app/README.md) | The macOS menu bar app: what it shows, building, signing, releasing |

## How it works and how far to trust it

| Doc | What it covers |
| --- | --- |
| [WHY_THRESHOLDS.md](WHY_THRESHOLDS.md) | Why the auto-compact threshold matters: the three effects that pull it in different directions |
| [METHODOLOGY.md](METHODOLOGY.md) | The replay, pricing, time model, baselines, profiles and guardrails, deep mode; what is measured and what is assumed |
| [RESULTS.md](RESULTS.md) | One checked case on real logs, with the control that makes the replay trustworthy |

## Building on it

| Doc | What it covers |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Package layout and the data flow through `result.json` |
| [ENGINE_API.md](ENGINE_API.md) | `compactlab engine`: the versioned JSON contract the app and other tools use |
| [ADDING_A_HARNESS.md](ADDING_A_HARNESS.md) | Writing an adapter for another agent harness |
| [REPORT_UI.md](REPORT_UI.md) | Layout and design choices of the HTML report |
| [ROADMAP.md](ROADMAP.md) | What comes next, in order |
| [MENUBAR_APP.zh-CN.md](MENUBAR_APP.zh-CN.md) | Design and evaluation notes behind the menu bar app (Chinese) |

Contributing rules (synthetic data only, the secrets scan, no new runtime dependencies) are in
[CONTRIBUTING.md](../CONTRIBUTING.md); security reports go through [SECURITY.md](../SECURITY.md). Every image in
[assets/](assets) is made from synthetic data by `scripts/make_assets.py` (report and terminal) or
`app/scripts/screenshots.sh` (menu bar app).
