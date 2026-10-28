"""Versioned, user-overridable pricing. Prices are USD per million tokens."""
from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


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
    verified: bool = False

    def rates(self, ctx: int) -> tuple[float, float, float, float]:
        """(input, output, cache_write, cache_read) per token (not per million) for a request of size ctx."""
        if self.long_above is not None and ctx > self.long_above:
            r = (self.long_input, self.long_output, self.long_cache_write, self.long_cache_read)
        else:
            r = (self.input, self.output, self.cache_write, self.cache_read)
        return tuple(x / 1e6 for x in r)  # type: ignore[return-value]


class PricingTable:
    def __init__(self, data: dict):
        if data.get("version") != 1:
            raise ValueError("unsupported pricing file version: %r" % data.get("version"))
        self.data = data
        self.default_ttl = int(data.get("default_ttl_seconds", 300))

    @classmethod
    def load(cls, override: str | Path | None = None) -> "PricingTable":
        base = json.loads(resources.files("compactlab").joinpath("data/pricing.json").read_text())
        if override:
            user = json.loads(Path(override).read_text())
            base["models"].update(user.get("models", {}))
            for k in ("fallback", "default_ttl_seconds", "note"):
                if k in user:
                    base[k] = user[k]
            base["version"] = user.get("version", base["version"])
        return cls(base)

    def lookup(self, model: str) -> Price:
        m = (model or "").lower()
        best, best_len = None, -1
        for key, row in self.data["models"].items():
            for pat in row.get("match", [key]):
                if pat.lower() in m and len(pat) > best_len:
                    best, best_len = (key, row), len(pat)
        key, row = best if best else ("fallback", self.data["fallback"])
        lc = row.get("long_context") or {}
        return Price(
            input=row["input"], output=row["output"], cache_write=row["cache_write"], cache_read=row["cache_read"],
            max_window=int(row.get("max_window", 200_000)),
            long_above=lc.get("above_tokens"), long_input=lc.get("input", 0), long_output=lc.get("output", 0),
            long_cache_write=lc.get("cache_write", 0), long_cache_read=lc.get("cache_read", 0),
            key=key, verified=bool(row.get("verified", False)),
        )
