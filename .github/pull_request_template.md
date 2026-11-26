## What and why

## How it was checked

## Checklist
- [ ] `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` and `uv run pytest -q` pass
- [ ] App changes: `swift test` in `app/` and `python3 app/scripts/check_strings.py` pass
- [ ] Tests and screenshots use synthetic data only (no real transcripts, paths or usernames)
- [ ] `python scripts/secrets_scan.py --all` is clean
- [ ] Docs updated if behaviour or formats changed (both READMEs together; data files with source + date)
- [ ] CHANGELOG.md entry under "Unreleased"
