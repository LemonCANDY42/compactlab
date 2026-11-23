# Adding a harness adapter

An adapter turns one harness's session logs into normalized `Session` objects. Everything downstream
(simulation, report, apply) is harness-agnostic.

## 1. Write the parser and adapter

Create `src/compactlab/adapters/<name>.py`:

```python
from pathlib import Path
from typing import Any

from ..model import Compaction, Session, Turn
from .base import Adapter, Parser, env_roots, parse_ts, register


class MyParser(Parser):
    version = 1  # bump when parsing output changes (invalidates the scan cache)

    def __init__(self, path: str):
        self.session = Session(id=Path(path).stem, harness="my-harness", role="main", path=path)

    def feed(self, d: dict[str, Any]) -> None:
        # one JSON object per call; tolerate anything; count what you do not understand
        if d.get("type") not in {"usage", "compaction"}:
            self.note_unknown(self.session, str(d.get("type")))
            return
        ...

    def finish(self) -> Session: ...  # return a copy; do not consume state
    def dump(self) -> dict[str, Any]: ...  # JSON-serializable state, for the resumable cache
    @classmethod
    def load(cls, state: dict[str, Any]) -> "MyParser": ...  # state may be just {"path": ...}


@register
class MyAdapter(Adapter):
    name = "my-harness"
    parser_cls = MyParser

    @classmethod
    def default_roots(cls) -> list[Path]:
        return env_roots("MY_HARNESS_HOME", "sessions", ["~/.my-harness/sessions"])
```

Import the module in `adapters/__init__.py`.

Logs that are not plain append-only JSONL (compressed, one file per generation, a document rewritten in place)
override `find_files()` and `read_records()` and set `resumable = False`: the scan cache then reuses unchanged files
and re-parses changed ones from the start instead of resuming at a byte offset. The dsh and Gemini CLI adapters are
the examples.

A harness that keeps every session in one database overrides `load(path, cache, since)` instead: it returns all
sessions in that file, filters by each session's own update time (the file's mtime says little), and opens the
database read-only so a running harness is never disturbed. The OpenCode adapter is the example (SQLite with
`mode=ro`). Extra `doctor` lines (a decoder, a storage version it cannot read) come from `doctor_checks()`.

If the harness reads environment variables for its paths, add them to the `isolated_home` fixture in
`tests/conftest.py` so tests never see real data.

## 2. Emit the normalized fields correctly

| Field | Meaning |
| --- | --- |
| `Turn.ctx` | total input tokens the model saw for this request (uncached + cache read + cache write) |
| `Turn.cache_read`, `cache_write`, `output` | as billed for this request |
| `Turn.ts` | epoch seconds (used for cache-TTL idle effects) |
| `Turn.tool_keys` | `"Tool:arg"` strings; used for re-discovery detection |
| `Compaction.turn_index` | index of the first turn after the boundary |
| `Session.role` | `"main"` or `"subagent"` |

One `Turn` per billed request. If the harness writes duplicates or cumulative counters, dedupe or convert to
per-request values (see the Codex adapter). Skip usage copied from a parent session (`skipped_inherited`).

## 3. Fixtures and tests

Add a synthetic generator to `compactlab/synth.py` or minimal hand-written JSONL in a test. Cover: normal turns,
duplicates, a compaction, a subagent, malformed lines, an unknown event type, and cache resume
(`tests/test_cache_and_formats.py` shows the pattern). Never use real logs.

## 4. Record format facts

Add an entry under `harnesses` in `src/compactlab/data/harness_formats.json` for every fact the parser relies on,
with `status` (`documented`, `public-tool`, `assumed`) and a source link.

## 5. Recommender (optional)

Add `src/compactlab/recommend/<name>.py` with a `Recommender` subclass (`harness`, `formats` per scope, `generate()`),
register it (and import it in `recommenders()`), and add every key it emits to `data/config_knobs.json` with
`status`, `source` and `verified_on`. If the harness has no knob for a scope, return an `Omitted` with the reason
instead of inventing one.

When the knob is not a token count, convert with `recommend/units.py` rather than by hand: `fraction_for()` for a
fraction of the window (rounded so the trigger never lands above the pick, with a note when one step of the last
decimal is coarse on that window), `free_share()` for a share kept free, and `shared_fraction()` when one global
fraction has to serve models with different windows. Add the default rule to `data/harness_defaults.json` and to
`default_threshold()` / `effective_max()` in `sim/baseline.py`, and a reader in `read_current()` if the setting can
be read back from a config file.

## 6. Config target (optional)

If the harness has a compaction-threshold setting, add it to `apply/__init__.py` with tests on temp files only,
and document it in `docs/SAFETY.md`. If it has none, say so in the README harness table.
