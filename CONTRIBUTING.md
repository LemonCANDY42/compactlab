# Contributing

Thanks for helping. compactlab reads sensitive local logs, so the rules below are about privacy as much as code.

## Ground rules

- **Synthetic data only.** Never commit real transcripts, settings, reports built from real history, or paths
  containing a username. Build fixtures with `compactlab.synth` or hand-written minimal JSONL in tests.
- Run `python scripts/secrets_scan.py --all` before every push (pre-commit runs it too).
- No new runtime dependencies without discussion. The HTML report stays a single offline file.
- Network access belongs only in `pricing/refresh.py` (explicit command) and hyper-mode executors (explicit opt-in).

## Setup

```bash
uv venv && uv pip install -e ".[dev]"
pre-commit install
.venv/bin/python -m pytest --cov
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
```

## Adding a harness adapter

See [docs/ADDING_A_HARNESS.md](docs/ADDING_A_HARNESS.md). In short: one `Adapter` + `Parser` subclass in
`src/compactlab/adapters/`, register it, add synthetic fixtures and tests, and record every format fact you rely on in
`src/compactlab/data/harness_formats.json` with a source link and a verification status.

## Pricing updates

Edit `src/compactlab/data/pricing.json` with the source and retrieval date in the commit message, or improve the
parsers in `pricing/refresh.py`. Do not hand-type prices without a public source.

## Commits and releases

Small, focused commits. Add a line under **Unreleased** in `CHANGELOG.md`. Versions follow semantic versioning;
until 1.0, minor versions may change the `result.json` schema (the `schema` field is bumped when they do).
