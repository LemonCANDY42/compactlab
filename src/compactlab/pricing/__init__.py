"""Versioned pricing with provenance. Prices are USD per million tokens.

Load order (later wins per model row): bundled snapshot -> refreshed cache (`compactlab pricing
refresh`, skipped with offline=True) -> user override file. Every `Price` records where it came from and how the model
was matched (`kind`): its own row, the vendor's price for a platform with no row of its own, a borrowed row from the
same model family, or the fallback row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Any

from .ids import family, first_party, nearest, parse, platform_key, platform_name

DEFAULT_REFRESHED = Path("~/.compactlab/pricing-refreshed.json")


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_write: float
    cache_read: float
    max_window: int = 200_000
    long_above: int | None = None  # requests with ctx above this use the long_* prices
    long_input: float = 0.0
    long_output: float = 0.0
    long_cache_write: float = 0.0
    long_cache_read: float = 0.0
    cache_write_1h: float | None = None  # 1-hour cache-write price, where the provider has that tier
    long_cache_write_1h: float | None = None
    key: str = "fallback"
    source: str = "bundled"
    # "exact": the model's own row (for its platform, if any); "vendor": the vendor's list price, used for a platform
    # with no row of its own; "family": borrowed from the closest model of the same family; "fallback": nothing matched
    kind: str = "exact"
    platform: str | None = None

    def for_ttl(self, ttl: float) -> Price:
        """This price with cache writes at the tier a cache of `ttl` seconds is billed at (unchanged without one)."""
        if ttl < 3600 or self.cache_write_1h is None:
            return self
        long_w = self.long_cache_write_1h if self.long_cache_write_1h is not None else self.long_cache_write
        return replace(self, cache_write=self.cache_write_1h, long_cache_write=long_w)

    def rates(self, ctx: int) -> tuple[float, float, float, float]:
        """(input, output, cache_write, cache_read) per token for a request of size ctx."""
        if self.long_above is not None and ctx > self.long_above:
            r = (self.long_input, self.long_output, self.long_cache_write, self.long_cache_read)
        else:
            r = (self.input, self.output, self.cache_write, self.cache_read)
        return (r[0] / 1e6, r[1] / 1e6, r[2] / 1e6, r[3] / 1e6)


def _base(r: dict[str, Any], k: str) -> float:
    return float(r.get(k, r["input"]))


def _scaled(v: float, old: float, new: float) -> float:
    """A tier price that was a fixed multiple of `old`, re-expressed against the new base price."""
    return round(v * new / old, 6) if old else v


def _merge_row(prev: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """`row` over the same model's previous row. Tiers the new row omits are carried from `prev` but re-scaled
    to the new base prices, so a refreshed input/cache price never keeps a stale 1-hour or long-context price."""
    out = {**({"match": prev["match"]} if "match" in prev else {}), **row}
    if "input" not in prev or "input" not in row:
        return out
    ratio = {k: (_base(prev, k), _base(row, k)) for k in ("input", "output", "cache_write", "cache_read")}
    if "cache_write_1h" not in row and "cache_write_1h" in prev:
        out["cache_write_1h"] = _scaled(prev["cache_write_1h"], *ratio["cache_write"])
    plc, nlc = prev.get("long_context"), row.get("long_context")
    if isinstance(plc, dict):
        if nlc is None:  # no long-context tier in the refresh: keep the old one, moved with the base prices
            out["long_context"] = {
                k: (v if k == "above_tokens" else _scaled(v, *ratio["cache_write" if k == "cache_write_1h" else k]))
                for k, v in plc.items()
            }
        elif isinstance(nlc, dict) and "cache_write_1h" not in nlc and "cache_write_1h" in plc:
            old, new = _base(plc, "cache_write"), _base(nlc, "cache_write")
            out["long_context"] = {**nlc, "cache_write_1h": _scaled(plc["cache_write_1h"], old, new)}
    return out


class PricingTable:
    def __init__(self, data: dict[str, Any]):
        if data.get("version") not in (1, 2):
            raise ValueError(f"unsupported pricing file version: {data.get('version')!r}")
        self.data = data
        self.default_ttl = int(data.get("default_ttl_seconds", 300))
        self._memo: dict[tuple[str, str | None], Price] = {}
        self._alias: dict[str, str] = {}  # exact alternative IDs from each row's `match` list
        self._families: dict[str, dict[tuple[int, ...], str]] = {}  # family -> version -> row key (vendor rows)
        for k, row in data["models"].items():
            if ":" in k:  # a platform's own row: never borrowed for another model
                continue
            for pat in row.get("match", []):
                self._alias.setdefault(pat.lower(), k)
            fam, ver = family(parse(k).id)
            self._families.setdefault(fam, {}).setdefault(ver, k)

    @staticmethod
    def bundled() -> dict[str, Any]:
        d: dict[str, Any] = json.loads(resources.files("compactlab").joinpath("data/pricing.json").read_text())
        for row in d["models"].values():
            row.setdefault("source", f"bundled snapshot {d.get('retrieved', '')}".strip())
        return d

    @classmethod
    def load(
        cls, override: str | Path | None = None, refreshed: str | Path | None = DEFAULT_REFRESHED, offline: bool = False
    ) -> PricingTable:
        base = cls.bundled()
        layers: list[tuple[Path, str]] = []
        if refreshed and not offline:
            layers.append((Path(refreshed).expanduser(), "refreshed"))
        if override:
            layers.append((Path(override).expanduser(), "user"))
        bundled_date = str(base.get("retrieved", ""))
        for path, label in layers:
            if not path.exists():
                continue
            user = json.loads(path.read_text())
            # a refresh older than the bundled snapshot (made before upgrading compactlab) only adds models the
            # bundle lacks: the newer bundled price wins where both have one
            stale = label == "refreshed" and str(user.get("retrieved") or "9999") < bundled_date
            for k, row in user.get("models", {}).items():
                if stale and k in base["models"]:
                    continue
                prev = base["models"].get(k, {})
                row = _merge_row(prev, row)
                row.setdefault("source", f"{label}: {path.name} {user.get('retrieved', '')}".strip())
                base["models"][k] = row
            for k in ("fallback", "default_ttl_seconds", "note"):
                if k in user:
                    base[k] = user[k]
        return cls(base)

    def lookup(self, model: str, platform: str | None = None) -> Price:
        """The price for `model` as a harness logged it, on `platform` (a models.dev provider ID or a harness's name
        for it; the ID's own shape is used when None). Order: the platform's own row, the model's row (exact key, then
        a `match` alias), the closest row of the same family, the fallback row."""
        ck = (model, platform)
        if ck not in self._memo:
            self._memo[ck] = self._lookup(model, platform)
        return self._memo[ck]

    def _lookup(self, model: str, platform: str | None) -> Price:
        mid = parse(model)
        plat = platform_name(platform) or mid.platform
        vendor = first_party(mid.id)
        rows = self.data["models"]
        if plat and plat != vendor:
            for k in (platform_key(plat, mid), platform_key(plat, parse(mid.id))):
                if k in rows:
                    # tiers the platform row omits (1-hour cache writes, long context) follow the vendor's, scaled
                    own = rows.get(mid.id) or {}
                    return self._price(k, _merge_row(own, rows[k]) if own else rows[k], "exact", plat, mid.one_m)
        raw = (model or "").strip().lower()
        own = "vendor" if plat and plat != vendor else "exact"
        for k in (raw, mid.id):
            if k in rows:
                return self._price(k, rows[k], own, plat, mid.one_m)
        alias = self._alias.get(raw) or self._alias.get(mid.id)
        if alias:
            return self._price(alias, rows[alias], own, plat, mid.one_m)
        fam, ver = family(mid.id)
        cands = self._families.get(fam, {})
        v = nearest(ver, list(cands)) if mid.id else None
        if v is not None:
            return self._price(cands[v], rows[cands[v]], "family", plat, mid.one_m)
        # last resort: the longest `match` pattern contained in the ID (gpt-5.1-codex-max -> the gpt-5-codex row);
        # override files written for the old substring matching keep working this way
        best, best_len = None, 0
        for k, row in rows.items():
            for pat in row.get("match", []) if ":" not in k else []:
                p = pat.lower()
                if (p in raw or p in mid.id) and len(p) > best_len:
                    best, best_len = k, len(p)
        if best:
            return self._price(best, rows[best], "family", plat, mid.one_m)
        return self._price("fallback", self.data["fallback"], "fallback", plat, mid.one_m)

    def _price(self, key: str, row: dict[str, Any], kind: str, platform: str | None, one_m: bool) -> Price:
        lc = row.get("long_context") or {}
        window = int(row.get("max_window", 200_000))
        return Price(
            input=row["input"],
            output=row["output"],
            cache_write=row.get("cache_write", row["input"]),
            cache_read=row.get("cache_read", row["input"]),
            max_window=max(window, 1_000_000) if one_m else window,  # the [1m] variant of a 200K model
            long_above=lc.get("above_tokens"),
            long_input=lc.get("input", 0),
            long_output=lc.get("output", 0),
            long_cache_write=lc.get("cache_write", 0),
            long_cache_read=lc.get("cache_read", 0),
            cache_write_1h=row.get("cache_write_1h"),
            long_cache_write_1h=lc.get("cache_write_1h"),
            key=key,
            source=str(row.get("source", "fallback row" if key == "fallback" else "bundled")),
            kind=kind,
            platform=platform,
        )
