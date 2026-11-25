# Security policy

compactlab reads **sensitive local data**: agent transcripts (which can contain code, secrets pasted into chats,
file paths and tool output) and harness configuration files it may modify.

## What compactlab does to protect it

- Lite mode (default) makes no network calls and no model calls. The one exception you can turn on is
  `compactlab pricing auto on`: background runs (`serve`, `recalibrate`) then download the public price files
  (models.dev, else LiteLLM) when the logs show a model with no price row (retried daily, or after an hour for a newly
  seen model, up to 5 times) and once a week otherwise. The request is a plain GET of the whole file with a generic
  User-Agent; no model names, paths or usage leave the machine.
- The scan cache (`~/.compactlab/cache`, 0600 files in a 0700 directory) stores lean sessions: token counts and
  times, whether a user message corrected the agent, a hash of each tool call (its tool name and a hash, no path or
  command) and the length of each compaction summary. No prompt text, tool arguments, output snippets or summaries;
  the one exception is a log whose last user message has no reply yet, whose text waits in the cache until it does.
  It also keeps the last few analysis results (numbers, model names and session ids). Rows of deleted logs are
  dropped (and zeroed in the file) after each scan, and older cache versions are deleted on upgrade (version 4, which
  held prompt text, included). Disable it with `--no-cache`; wipe every version with `compactlab cache clear`.
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
