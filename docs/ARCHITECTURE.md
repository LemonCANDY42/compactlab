# Architecture

```
adapters/            claude_code.py, codex.py (+ base.Adapter, registry)
model.py             Turn, Compaction, Session (normalized events)
pricing.py + data/   versioned price table, user override, long-context tiers
calibrate.py         re-discovery penalty from observed compactions (else a labelled prior)
simulate.py          trace building, replay(), threshold sweep, grouping by (model, role)
select.py            recommendation, Pareto frontier, gears (eco / balanced / recall)
factcheck.py         offline heuristics on compaction summaries
aieval.py            opt-in LLM probes + judge, cost estimate, hard budget (stub clients)
apply.py             pre-authorization, planning, diff, backup/manifest, key-level revert
report.py            self-contained HTML (inline CSS/JS/SVG)
cli.py               scan | simulate | report | apply | retune | revert | eval | demo
synth.py             synthetic transcripts for tests and `demo`
```

Data flow: adapters → `Session[]` → `analyze()` → `result.json` (schema 1) → `report` / `apply`.
`result.json` is the stable seam: the report and `apply` read nothing else.

## Adding a harness

Subclass `Adapter`, implement `find_files()` and `parse_file()` (tolerate unknown lines), decorate with
`@register`. Emit `Turn.ctx` as total input tokens seen by the model (uncached + cache read + cache write),
one `Turn` per billed call, and `Compaction.turn_index` as the first turn after the boundary.

## Safety properties

- No network anywhere except inside a user-supplied `LLMClient`.
- `apply` never writes without `--write` plus a pre-authorization file or `--i-authorize`; it refuses
  non-strict JSON, rejects values outside bounds/unlisted models/keys, writes atomically, and keeps a
  backup and manifest. Revert restores only keys it changed and skips keys edited since (unless `--force`).
- Tests for apply/revert use temp copies only. The tool reads real transcripts only when a user runs
  `scan/simulate/report/retune/eval` without overriding the roots.
