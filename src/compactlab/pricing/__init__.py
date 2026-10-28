"""Versioned pricing with provenance. Prices are USD per million tokens.

Load order (later wins per model row): bundled snapshot -> refreshed cache (`compactlab pricing
refresh`, skipped with offline=True) -> user override file. Every `Price` records where it came from.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

DEFAULT_REFRESHED = Path("~/.compactlab/pricing-refreshed.json")


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_write: float
    cache_read: float
    max_window: int = 200_000
    long_above: int | None = None       # requests with ctx above this use the long_* prices
    long_input: float = 0.0
    long_output: float = 0.0
    long_cache_write: float = 0.0
    long_cache_read: float = 0.0
    key: str = "fallback"
    source: str = "bundled"

    def rates(self, ctx: int) -> tuple[float, float, float, float]:
        """(input, output, cache_write, cache_read) per token for a request of size ctx."""
        if self.long_above is not None and ctx > self.long_above:
            r = (self.long_input, self.long_output, self.long_cache_write, self.long_cache_read)
        else:
            r = (self.input, self.output, self.cache_write, self.cache_read)
        return (r[0] / 1e6, r[1] / 1e6, r[2] / 1e6, r[3] / 1e6)


class PricingTable:
    def __init__(self, data: dict[str, Any]):
        if data.get("version") not in (1, 2):
            raise ValueError(f"unsupported pricing file version: {data.get('version')!r}")
        self.data = data
        self.default_ttl = int(data.get("default_ttl_seconds", 300))

    @staticmethod
    def bundled() -> dict[str, Any]:
        d: dict[str, Any] = json.loads(resources.files("compactlab").joinpath("data/pricing.json").read_text())
        for row in d["models"].values():
            row.setdefault("source", f"bundled snapshot {d.get('retrieved', '')}".strip())
        return d

    @classmethod
    def load(cls, override: str | Path | None = None, refreshed: str | Path | None = DEFAULT_REFRESHED,
             offline: bool = False) -> PricingTable:
        base = cls.bundled()
        layers: list[tuple[Path, str]] = []
        if refreshed and not offline:
            layers.append((Path(refreshed).expanduser(), "refreshed"))
        if override:
            layers.append((Path(override).expanduser(), "user"))
        for path, label in layers:
            if not path.exists():
                continue
            user = json.loads(path.read_text())
            for k, row in user.get("models", {}).items():
                prev = base["models"].get(k, {})
                row = {**{x: prev[x] for x in ("match", "long_context") if x in prev}, **row}
                row.setdefault("source", f"{label}: {path.name} {user.get('retrieved', '')}".strip())
                base["models"][k] = row
            for k in ("fallback", "default_ttl_seconds", "note"):
                if k in user:
                    base[k] = user[k]
        return cls(base)

    def lookup(self, model: str) -> Price:
        """Exact key first, then the longest `match` substring, else the fallback row."""
        m = (model or "").lower()
        rows = self.data["models"]
        if m in rows:
            key, row = m, rows[m]
        else:
            best: tuple[str, dict[str, Any]] | None = None
            best_len = -1
            for k, r in rows.items():
                for pat in r.get("match", [k]):
                    if pat.lower() in m and len(pat) > best_len:
                        best, best_len = (k, r), len(pat)
            key, row = best if best else ("fallback", self.data["fallback"])
        lc = row.get("long_context") or {}
        return Price(
            input=row["input"], output=row["output"], cache_write=row.get("cache_write", row["input"]),
            cache_read=row.get("cache_read", row["input"]), max_window=int(row.get("max_window", 200_000)),
            long_above=lc.get("above_tokens"), long_input=lc.get("input", 0), long_output=lc.get("output", 0),
            long_cache_write=lc.get("cache_write", 0), long_cache_read=lc.get("cache_read", 0),
            key=key, source=str(row.get("source", "fallback row" if key == "fallback" else "bundled")),
        )
