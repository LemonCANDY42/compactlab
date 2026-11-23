"""Opt-in refresh of model prices and windows when the logs show a model compactlab has no row for.

Off until `compactlab pricing auto on`. Only background runs (`serve`, `recalibrate`, a schedule) use it; `simulate`
and the rest stay offline and only say which models need it. A refresh downloads the whole public dataset (models.dev,
then LiteLLM if that fails), so nothing about the user or their models is sent. It runs when a model has no price row
of its own (at most once a day, or once an hour when a model appears that the last attempt did not cover), and once a
week anyway, because vendors change prices and context windows of models already in the table. Every change it
brings is logged (`pricing-changes.json`).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from . import DEFAULT_REFRESHED, PricingTable
from .refresh import fetch, refresh

AUTO_FILE = Path("~/.compactlab/pricing-auto.json")
RETRY_S = 24 * 3600
NEW_MODEL_RETRY_S = 3600
STALE_S = 7 * 86400  # refresh known models' prices and windows this often
GIVE_UP = 5  # attempts after which a model the public data never lists stops triggering refreshes of its own
ORDER = ("models.dev", "litellm")


def load_state(path: Path = AUTO_FILE) -> dict[str, Any]:
    p = path.expanduser()
    try:
        d = json.loads(p.read_text()) if p.exists() else {}
    except (OSError, ValueError):
        d = {}
    return d if isinstance(d, dict) else {}


def save_state(state: dict[str, Any], path: Path = AUTO_FILE) -> None:
    p = path.expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    os.replace(tmp, p)


def set_enabled(on: bool, path: Path = AUTO_FILE) -> dict[str, Any]:
    st = load_state(path)
    st["enabled"] = on
    save_state(st, path)
    return st


def unpriced(table: PricingTable, models: Iterable[tuple[str, str | None]]) -> list[str]:
    """Models (as logged) whose price is not their own row: borrowed, the fallback, or a platform priced at the
    vendor's list price."""
    out = set()
    for model, platform in models:
        if model in ("unknown", "<synthetic>", ""):
            continue
        if table.lookup(model, platform).kind != "exact":
            out.add(model if not platform else f"{model} @ {platform}")
    return sorted(out)


def maybe_refresh(
    table: PricingTable,
    models: Iterable[tuple[str, str | None]],
    now: float,
    reload: Callable[[], PricingTable],
    path: Path = AUTO_FILE,
    out: Path = DEFAULT_REFRESHED,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
) -> tuple[PricingTable, dict[str, Any] | None]:
    """Refresh when auto refresh is on, some model is unpriced and a retry is due; return the (re)loaded table and
    what happened (None when nothing was attempted)."""
    st = load_state(path)
    if not st.get("enabled"):
        return table, None
    models = list(models)
    missing = unpriced(table, models)
    attempts: dict[str, int] = dict(st.get("attempts") or {})
    wanted = [m for m in missing if attempts.get(m, 0) < GIVE_UP]  # e.g. a private profile ARN is never listed
    p = out.expanduser()
    fresh_at = max(float(st.get("last_ok") or 0), p.stat().st_mtime if p.exists() else 0.0)
    stale = now - fresh_at >= STALE_S
    if not wanted and not stale:
        return table, None
    last = st.get("last_attempt")
    new = set(wanted) - set(st.get("missing") or [])
    if last is not None and now - last < RETRY_S and not (new and now - last >= NEW_MODEL_RETRY_S):
        return table, None
    st.update(last_attempt=now, missing=missing)
    errors: list[str] = []
    moved: list[dict[str, Any]] = []
    for source in ORDER:
        try:
            n = refresh(source, out, None, fetcher or fetch, moved)
        except Exception as e:  # network or parse failure: keep the previous file, try the next source
            errors.append(f"{source}: {type(e).__name__}: {e}"[:300])
            continue
        st.update(last_ok=now, last_source=source, last_rows=n)
        break
    st["last_error"] = "; ".join(errors) or None
    fresh = reload() if st.get("last_ok") == now else table
    st["still_missing"] = unpriced(fresh, models)
    st["attempts"] = {m: attempts.get(m, 0) + 1 for m in st["still_missing"]}
    save_state(st, path)
    used = {fresh.lookup(m, p).key for m, p in models}  # the rows your models are priced from
    return fresh, {
        "reason": "new model" if wanted else "weekly",
        "changes": [c for c in moved if c["model"] in used],
        "refreshed": st.get("last_ok") == now,
        "source": st.get("last_source") if st.get("last_ok") == now else None,
        "missing": missing,
        "still_missing": st["still_missing"],
        "error": st["last_error"],
    }
