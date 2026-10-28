# Safety: apply, revert, pre-authorization

## Defaults

- `compactlab apply` is a **dry-run**: it prints the planned changes and a unified diff and writes nothing.
- Writing needs `--write` **and** one of:
  - `--preauth FILE`, a pre-authorization file (below), or
  - `--i-authorize` with explicit `--claude-settings` / `--codex-config`, `--models`, `--min`, `--max`.
- `retune` (re-analyze + apply) works only with a pre-authorization file that sets `"allow_retune": true`.
- The default value written is the **headroom** tier; `--tier standard|eco|recall` picks another.
  Pre-authorization bounds always win: a value outside them is skipped, never clamped silently.

## Pre-authorization file

```json
{
  "version": 1,
  "expires": "2027-01-31",
  "allow_retune": false,
  "targets": [
    {
      "harness": "claude-code",
      "file": "~/.claude/settings.json",
      "keys": ["modelSettings.*.autoCompactWindow"],
      "models": ["claude-opus-5-5", "claude-sonnet-5-5"],
      "min": 150000,
      "max": 600000,
      "max_change_pct": 60
    }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `expires` | optional ISO date after which the file is refused |
| `targets[].file` | the only file this target may modify |
| `targets[].keys` | glob patterns of dotted keys that may change; anything else is untouched |
| `targets[].models` | models whose value may change |
| `min` / `max` | allowed value range (Claude Code's own range 100000–1000000 also applies) |
| `max_change_pct` | optional cap on relative change vs the current value |

Codex target: `"harness": "codex"`, `"file": "~/.codex/config.toml"`, `"keys": ["model_auto_compact_token_limit"]`.
Codex has one global key, so list the single model you want it to follow.

## What a write does

1. Refuses non-strict JSON (comments, trailing commas) instead of guessing.
2. Changes only the allowed key, creating parent objects if needed; keeps other keys and their order.
   If the file's formatting differs from `json.dumps(indent=N)`, the dry-run says it will be normalized.
3. For TOML, edits or inserts the top-level line only, before the first `[table]`; tables are untouched.
4. Writes a backup and a manifest under `~/.compactlab/` (`--state-dir`), then writes atomically.

## Revert

`compactlab revert` (latest manifest, or `--id`):

- If the file is byte-identical to what apply wrote, it restores the backup exactly.
- Otherwise it restores only the keys apply changed, leaving your later edits alone, and skips a key you have
  edited since (use `--force` to revert it anyway). Objects apply created are removed again when empty.
