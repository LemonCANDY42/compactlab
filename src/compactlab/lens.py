"""A small per-group summary for the compactlab-lens mod (mods/compactlab-lens).

The mod reads files of at most 4 MiB and only needs thresholds and per-day impact per model and role, so `simulate`
writes this alongside the full result. No session ids, paths or transcript text are included.
"""

from __future__ import annotations

from typing import Any

from .sim.select import PROFILE_NAMES

LENS_SCHEMA = 1


def _impact(c: dict[str, Any] | None) -> dict[str, Any] | None:
    im = (c or {}).get("impact")
    if not im:
        return None
    return {
        "usd_per_day": im["usd"]["per_day"],
        "usd_per_active_day": im["usd"]["per_active_day"],
        "min_per_day": im["minutes"]["per_day"],
        "wait_min_per_day": im["wait_minutes"]["per_day"],
        "turn_gain_min_per_day": im["turn_gain_minutes"]["per_day"],
        "compactions_per_day": im["compactions"]["per_day"],
        "ref_compactions_per_day": im["ref_compactions"]["per_day"],
    }


def summarize(result: dict[str, Any]) -> dict[str, Any]:
    groups = []
    for g in result["groups"]:
        cur = g.get("current")
        groups.append(
            {
                "harness": g["harness"],
                "model": g["model"],
                "role": g["role"],
                "selected": g.get("profile"),
                "default_threshold": g["default"]["threshold"],
                "current_threshold": cur.get("configured", cur["threshold"]) if cur else None,
                "effective_max": g.get("effective_max"),
                "max_window": g.get("max_window"),
                "days": g["days"],
                "active_days": g.get("active_days"),
                "no_action_needed": g["no_action_needed"],
                "profiles": {
                    n: {
                        "threshold": g["profiles"][n]["threshold"],
                        "action": g["comparison"][n]["action"],
                        "flags": list(g["profiles"][n]["flags"]),
                        "vs_default": _impact(g["comparison"][n]["vs_default"]),
                        "vs_current": _impact(g["comparison"][n].get("vs_current")),
                    }
                    for n in PROFILE_NAMES
                },
            }
        )
    return {
        "schema": LENS_SCHEMA,
        "mode": result.get("mode"),
        "data_through": result.get("data_through"),
        "demo": bool(result.get("demo")),
        "groups": groups,
    }
