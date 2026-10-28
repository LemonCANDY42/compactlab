## What and why

## Checklist
- [ ] `ruff check .`, `ruff format --check .`, `mypy`, `pytest` pass
- [ ] Tests use synthetic fixtures only (no real transcripts, paths or usernames)
- [ ] `python scripts/secrets_scan.py --all` is clean
- [ ] Docs updated (README / docs/*.md / data files with source + date) if behaviour or formats changed
- [ ] CHANGELOG.md entry under "Unreleased"
