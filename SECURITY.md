# Security policy

compactlab reads **sensitive local data**: agent transcripts (which can contain code, secrets pasted into chats,
file paths and tool output) and harness configuration files it may modify.

## What compactlab does to protect it

- Lite mode (default) makes no network calls and no model calls.
- The scan cache (`~/.compactlab/cache`, mode 0600) stores parsed excerpts: tool commands and 400-character output
  snippets. Disable it with `--no-cache`; wipe it with `compactlab cache clear`.
- Reports and `result.json` contain token counts, model names and session ids, never transcript text or paths.
  `report --redact` hashes session ids.
- `share` writes a local file of rounded aggregates (no ids, paths, timestamps or text) that you post yourself; the tool
  never sends it. Its check is an allow-list, so an unexpected field rejects the record. See docs/SHARING.md.
- `apply` writes only with `--write` plus a pre-authorization file or `--i-authorize`, keeps backups, and can revert.
- Deep mode sends reconstructed excerpts of sampled sessions to the model you choose, through a harness you
  choose. It shows an estimate first, needs `--yes`, and stops at `--budget`.

## Reporting a vulnerability

Please report privately through GitHub's "Report a vulnerability" (Security Advisories) on this repository.
Include a synthetic reproduction; do not send real transcripts. We aim to acknowledge within 7 days.

Supported versions: the latest release.
