# Contributing

Thanks for helping. compactlab reads sensitive local logs, so the rules below are about privacy as much as code.

## Ground rules

- **Synthetic data only.** Never commit real transcripts, settings, reports built from real history, or paths
  containing a username. Build fixtures with `compactlab.synth` or hand-written minimal JSONL in tests.
- Run `python scripts/secrets_scan.py --all` before every push (pre-commit runs it too).
- No new runtime dependencies without discussion. The HTML report stays a single offline file.
- Network access belongs only in `pricing/refresh.py` and `pricing/auto.py` (explicit or opt-in) and deep-mode
  executors (explicit opt-in).

## Setup

```bash
uv sync --extra dev
uv run pre-commit install
uv run pytest -q
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

The menu bar app (macOS 14+, Swift) has its own tests; see [app/README.md](app/README.md):

```bash
cd app && swift test
python3 app/scripts/check_strings.py   # every interface string has a Chinese translation
```

Where things live: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Adding a harness adapter

See [docs/ADDING_A_HARNESS.md](docs/ADDING_A_HARNESS.md). In short: one `Adapter` + `Parser` subclass in
`src/compactlab/adapters/`, register it, add synthetic fixtures and tests, and record every format fact you rely on in
`src/compactlab/data/harness_formats.json` with a source link and a verification status.

## Pricing updates

Edit `src/compactlab/data/pricing.json` with the source and retrieval date in the commit message, or improve the
parsers in `pricing/refresh.py`. Do not hand-type prices without a public source.

## Docs and screenshots

- `README.md` and `README.zh-CN.md` say the same things in the same order; change both together. Detail belongs in
  `docs/` (indexed in [docs/README.md](docs/README.md)), not in the READMEs.
- Chinese text reuses the terms already in `README.zh-CN.md` and the app's `Localizable.strings` (方案, 阈值, 压缩,
  找回, 校准, 审阅, 撤销, ...) and macOS's own wording for system things.
- Every image in `docs/assets/` comes from synthetic data: `python scripts/make_assets.py` (report, terminal, social
  preview) and `app/scripts/screenshots.sh` (menu bar app).
- Numbers in the docs about real use come from [docs/RESULTS.md](docs/RESULTS.md); do not add claims it does not
  support.

## Commits and releases

Small, focused commits. Add a line under **Unreleased** in `CHANGELOG.md`. Versions follow semantic versioning;
until 1.0, minor versions may change the `result.json` schema (the `schema` field is bumped when they do). Release
steps are in [app/README.md § Releasing](app/README.md#releasing).
