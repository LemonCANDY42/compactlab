"""Refresh prices from a public dataset. This is the only module that performs network I/O on its own: when the user
runs `compactlab pricing refresh`, or from background runs once they turn on `compactlab pricing auto` (see
`pricing.auto`). Everything else works from the bundled file and the last refresh.

Supported sources:
  models.dev  https://models.dev/api.json   {provider: {models: {id: {cost: {input, output, cache_read,
              cache_write, tiers?|context_over_200k?}, limit: {context}}}}}   (USD per 1M tokens)
  litellm     model_prices_and_context_window.json  {id: {input_cost_per_token, output_cost_per_token,
              cache_read_input_token_cost, cache_creation_input_token_cost, max_input_tokens, litellm_provider}}
              (per token)

Rows are kept for every model of a known vendor (see `ids.FIRST_PARTY`) under the vendor's own ID, the vendor's own
price winning over resellers', and for the platforms in `ids.PLATFORMS` (Bedrock, Vertex, Azure, OpenRouter) under
`<platform>:<id>` with that platform's price. The download is the whole public file: nothing about the user is sent.
"""

from __future__ import annotations

import json
import math
import os
import urllib.request
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from .ids import FIRST_PARTY, PLATFORMS, family, first_party, parse, platform_key, platform_name

SOURCES = {
    "models.dev": "https://models.dev/api.json",
    "litellm": "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json",
}
VENDORS = [p for p, _ in FIRST_PARTY]
# LiteLLM's names for the vendors' own APIs (rows without a provider count as the vendor's too)
LITELLM_VENDORS = {"anthropic", "openai", "text-completion-openai", "deepseek", "moonshot", "zai", "dashscope",
                   "gemini", "xai", "mistral", "minimax", ""}  # fmt: skip


def _sane(row: dict[str, Any]) -> bool:
    """Every rate finite and non-negative, every window a positive integer."""
    rates = [v for k, v in row.items() if k not in ("max_window", "long_context", "source")]
    lc = row.get("long_context") or {}
    rates += [v for k, v in lc.items() if k != "above_tokens"]
    windows = [w for w in (row.get("max_window"), lc.get("above_tokens")) if w is not None]
    return all(isinstance(v, float) and math.isfinite(v) and v >= 0 for v in rates) and all(
        isinstance(w, int) and w > 0 for w in windows
    )


def _row(cost: dict[str, Any], context: Any) -> dict[str, Any] | None:
    if "input" not in cost or "output" not in cost:
        return None
    row: dict[str, Any] = {
        "input": float(cost["input"]),
        "output": float(cost["output"]),
        "cache_write": float(cost.get("cache_write", cost["input"])),
        "cache_read": float(cost.get("cache_read", cost["input"])),
    }
    if context:
        row["max_window"] = int(context)
    tiers = cost.get("tiers") or []
    if tiers and isinstance(tiers[0], dict) and (tiers[0].get("tier") or {}).get("type") == "context":
        t = tiers[0]
        row["long_context"] = {
            "above_tokens": int(t["tier"]["size"]),
            "input": float(t.get("input", row["input"])),
            "output": float(t.get("output", row["output"])),
            "cache_write": float(t.get("cache_write", row["cache_write"])),
            "cache_read": float(t.get("cache_read", row["cache_read"])),
        }
    elif isinstance(cost.get("context_over_200k"), dict):
        t = cost["context_over_200k"]
        row["long_context"] = {
            "above_tokens": 200_000,
            **{k: float(t.get(k, row[k])) for k in ("input", "output", "cache_write", "cache_read")},
        }
    return row


def _key(mid: str, provider: str | None) -> str | None:
    """Where a dataset row is stored: the vendor's ID for vendor models, `<platform>:<id>` for a platform's own price,
    None for anything else (unknown vendors, platform variants such as OpenRouter's :batch)."""
    m = parse(mid)
    plat = platform_name(provider) or (provider if provider in PLATFORMS else None)
    if plat:
        if plat == "openrouter" and ":" in mid:
            return None
        return platform_key(plat, m) if first_party(m.id) or plat == "amazon-bedrock" else None
    return m.id if first_party(m.id) and m.platform is None else None


def _order(provider: str | None) -> int:
    """Vendors first (their own price wins for their IDs), then everyone else."""
    return VENDORS.index(provider) if provider in VENDORS else len(VENDORS)


def parse_models_dev(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    providers: list[tuple[str | None, dict[str, Any]]] = (
        sorted(((pid, v) for pid, v in data.items()), key=lambda x: _order(x[0]))
        if all(isinstance(v, dict) and "models" in v for v in data.values())
        else [(None, {"models": data})]
    )
    vendor_models: set[tuple[str, tuple[int, ...]]] = set()  # (family, version) of every model a vendor lists
    for pid, prov in providers:
        plat = pid if pid in PLATFORMS else None
        for mid, m in (prov.get("models") or {}).items():
            if not isinstance(m, dict):
                continue
            key = _key(mid, plat)
            if key is None or key in out:
                continue
            if plat is None and first_party(key) != pid and family(key) in vendor_models:
                continue  # a reseller's copy of a model its vendor lists (often re-spelled and marked up)
            try:
                row = _row(m.get("cost") or {}, (m.get("limit") or {}).get("context"))
            except (TypeError, ValueError, KeyError, OverflowError):
                continue
            if row and _sane(row):
                if pid:
                    row["provider"] = pid
                if plat is None and first_party(key) == pid:
                    vendor_models.add(family(key))
                out[key] = row
    return out


def _m(x: float) -> float:
    return round(x * 1e6, 6)


def _litellm_row(m: dict[str, Any]) -> dict[str, Any] | None:
    cost = {"input": _m(m["input_cost_per_token"]), "output": _m(m["output_cost_per_token"])}
    if m.get("cache_read_input_token_cost") is not None:
        cost["cache_read"] = _m(m["cache_read_input_token_cost"])
    if m.get("cache_creation_input_token_cost") is not None:
        cost["cache_write"] = _m(m["cache_creation_input_token_cost"])
    row = _row(cost, m.get("max_input_tokens"))
    if row and m.get("input_cost_per_token_above_200k_tokens") is not None:
        row["long_context"] = {
            "above_tokens": 200_000,
            "input": _m(m["input_cost_per_token_above_200k_tokens"]),
            "output": float((m.get("output_cost_per_token_above_200k_tokens") or m["output_cost_per_token"]) * 1e6),
            "cache_write": float(
                (
                    m.get("cache_creation_input_token_cost_above_200k_tokens")
                    or m.get("cache_creation_input_token_cost")
                    or m["input_cost_per_token"]
                )
                * 1e6
            ),
            "cache_read": float(
                (
                    m.get("cache_read_input_token_cost_above_200k_tokens")
                    or m.get("cache_read_input_token_cost")
                    or m["input_cost_per_token"]
                )
                * 1e6
            ),
        }
    return row


def parse_litellm(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    # bare vendor IDs first, then by depth: `azure/gpt-x` wins over a data-zone copy such as `azure/eu/gpt-x`
    items = sorted(data.items(), key=lambda kv: kv[0].count("/"))
    for mid, m in items:
        if not isinstance(m, dict):
            continue
        if m.get("input_cost_per_token") is None or m.get("output_cost_per_token") is None:
            continue
        prov = str(m.get("litellm_provider") or "")
        plat = (
            "google-vertex-anthropic"
            if prov == "vertex_ai-anthropic_models"
            else "google-vertex"
            if prov.startswith("vertex_ai")
            else platform_name(prov)
        )
        if plat is None:
            if prov not in LITELLM_VENDORS:
                continue  # another router's or reseller's copy: not the vendor's list price
            mid = mid.rsplit("/", 1)[-1]  # the vendor's own API under LiteLLM's prefix: deepseek/deepseek-v4-pro
        key = _key(mid, plat)
        if key is None or key in out:
            continue
        try:
            row = _litellm_row(m)
        except (TypeError, ValueError, KeyError, OverflowError):
            continue
        if row and _sane(row):
            out[key] = row
    return out


PARSERS: dict[str, Callable[[dict[str, Any]], dict[str, dict[str, Any]]]] = {
    "models.dev": parse_models_dev,
    "litellm": parse_litellm,
}


def fetch(url: str, timeout: float = 20.0) -> dict[str, Any]:
    if not url.startswith("https://"):
        raise ValueError("pricing refresh only fetches https URLs")
    req = urllib.request.Request(url, headers={"User-Agent": "compactlab-pricing-refresh"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data: dict[str, Any] = json.loads(r.read())
    return data


CHANGE_FIELDS = ("input", "output", "cache_write", "cache_read", "max_window")
MAX_CHANGES = 500


def changes_path(out: Path) -> Path:
    return out.expanduser().with_name("pricing-changes.json")


def _previous_rows(out: Path) -> dict[str, dict[str, Any]]:
    """The rows a refresh replaces: the last refreshed file, else the bundled snapshot."""
    try:
        if out.exists():
            return dict(json.loads(out.read_text()).get("models") or {})
    except (OSError, ValueError):
        pass
    from . import PricingTable

    return dict(PricingTable.bundled()["models"])


def log_changes(out: Path, rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Append every price or window that differs from the previous rows to `pricing-changes.json` (newest last, at
    most MAX_CHANGES kept), and return the new entries. A row absent before is not a change."""
    prev = _previous_rows(out)
    today = date.today().isoformat()
    new = []
    for k, row in sorted(rows.items()):
        old = prev.get(k)
        if not old:
            continue
        for f in CHANGE_FIELDS:
            a, b = old.get(f), row.get(f)
            if a is not None and b is not None and float(a) != float(b):
                new.append({"date": today, "model": k, "field": f, "old": a, "new": b, "source": row.get("source")})
    if new:
        path = changes_path(out)
        try:
            log = json.loads(path.read_text()) if path.exists() else []
        except (OSError, ValueError):
            log = []
        log = [*(log if isinstance(log, list) else []), *new][-MAX_CHANGES:]
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(log, indent=1))
        os.replace(tmp, path)
    return new


def recent_changes(out: Path, models: set[str] | None = None, limit: int = 20) -> list[dict[str, Any]]:
    try:
        log = json.loads(changes_path(out).read_text())
    except (OSError, ValueError):
        return []
    log = [c for c in log if isinstance(c, dict) and (models is None or c.get("model") in models)]
    return log[-limit:]


def refresh(
    source: str,
    out: Path,
    url: str | None = None,
    fetcher: Callable[[str], dict[str, Any]] = fetch,
    changes: list[dict[str, Any]] | None = None,
) -> int:
    """Fetch, parse and store `source` as the refreshed layer; prices and windows that moved are logged (and appended
    to `changes` when given). Returns the number of rows stored."""
    url = url or SOURCES[source]
    rows = PARSERS[source](fetcher(url))
    if not rows:
        raise ValueError(f"no usable rows from {url}; keeping the existing pricing")
    for r in rows.values():
        via = r.pop("provider", None)
        r["source"] = f"{source}{f' ({via})' if via else ''} {date.today().isoformat()}"
    out = out.expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    moved = log_changes(out, rows)
    if changes is not None:
        changes.extend(moved)
    body = json.dumps(
        {"version": 2, "retrieved": date.today().isoformat(), "source_url": url, "models": rows},
        indent=1,
        sort_keys=True,
        allow_nan=False,
    )
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(body)
    os.replace(tmp, out)  # the previous file stays intact until the new one is complete
    return len(rows)
