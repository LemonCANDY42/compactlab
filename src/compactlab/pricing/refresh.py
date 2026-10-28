"""Refresh prices from a public dataset. This is the only module that performs network I/O on its own,
and only when the user runs `compactlab pricing refresh`. Everything else works from the bundled file.

Supported sources:
  models.dev  https://models.dev/api.json   {provider: {models: {id: {cost: {input, output, cache_read,
              cache_write, tiers?|context_over_200k?}, limit: {context}}}}}   (USD per 1M tokens)
  litellm     model_prices_and_context_window.json  {id: {input_cost_per_token, output_cost_per_token,
              cache_read_input_token_cost, cache_creation_input_token_cost, max_input_tokens, ...}}  (per token)
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

SOURCES = {
    "models.dev": "https://models.dev/api.json",
    "litellm": "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json",
}
KEEP = re.compile(r"^(claude-|gpt-|o\d|codex)", re.I)


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


def parse_models_dev(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    providers = (
        data.values() if all(isinstance(v, dict) and "models" in v for v in data.values()) else [{"models": data}]
    )
    for prov in providers:
        for mid, m in (prov.get("models") or {}).items():
            if not isinstance(m, dict) or not KEEP.search(mid) or mid in out:
                continue
            row = _row(m.get("cost") or {}, (m.get("limit") or {}).get("context"))
            if row:
                out[mid.lower()] = row
    return out


def _m(x: float) -> float:
    return round(x * 1e6, 6)


def parse_litellm(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for mid, m in data.items():
        if not isinstance(m, dict) or "/" in mid or not KEEP.search(mid):
            continue
        if m.get("input_cost_per_token") is None or m.get("output_cost_per_token") is None:
            continue
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
                "output": (m.get("output_cost_per_token_above_200k_tokens") or m["output_cost_per_token"]) * 1e6,
                "cache_write": (
                    m.get("cache_creation_input_token_cost_above_200k_tokens")
                    or m.get("cache_creation_input_token_cost")
                    or m["input_cost_per_token"]
                )
                * 1e6,
                "cache_read": (
                    m.get("cache_read_input_token_cost_above_200k_tokens")
                    or m.get("cache_read_input_token_cost")
                    or m["input_cost_per_token"]
                )
                * 1e6,
            }
        if row:
            out[mid.lower()] = row
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


def refresh(source: str, out: Path, url: str | None = None, fetcher: Callable[[str], dict[str, Any]] = fetch) -> int:
    url = url or SOURCES[source]
    rows = PARSERS[source](fetcher(url))
    if not rows:
        raise ValueError(f"no usable rows from {url}; keeping the existing pricing")
    for r in rows.values():
        r["source"] = f"{source} {date.today().isoformat()}"
    out = out.expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"version": 2, "retrieved": date.today().isoformat(), "source_url": url, "models": rows},
            indent=1,
            sort_keys=True,
        )
    )
    return len(rows)
