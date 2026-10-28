"""Threshold selection: three profiles over the replay grid, with guardrails.

A profile is a value of time (USD per hour of agent wall-clock folded into the score) plus guardrails:

  cost      money first      $10/h   no minimum window (warns below 25% of the max window)
  balanced  default          $100/h  never below 25% of the max window, <= 30 min/day of added compaction wait
  time      time first       $400/h  never below 35% of the max window, <= 30 min/day of added compaction wait

Within the allowed thresholds the pick is the largest threshold whose score is within `tol` of the lowest
(`recommend`). Every profile keeps the recall floor. Selection works on the stored grid arrays only, so a result
file can be re-selected with another profile or value of time without replaying.
"""

from __future__ import annotations

import math
from typing import Any

PROFILES: dict[str, dict[str, Any]] = {
    "cost": {"value_of_time": 10.0, "min_fraction": 0.0, "enforce_wait_cap": False},
    "balanced": {"value_of_time": 100.0, "min_fraction": 0.25, "enforce_wait_cap": True},
    "time": {"value_of_time": 400.0, "min_fraction": 0.35, "enforce_wait_cap": True},
}
PROFILE_NAMES = tuple(PROFILES)
DEFAULT_PROFILE = "balanced"
WARN_FRACTION = 0.25  # any profile below this share of the max window gets a warning
WAIT_CAP_MIN_PER_DAY = 30.0  # added compaction + re-discovery wait vs the harness default


def recommend(
    grid: list[int],
    combined: list[float],
    recall: list[float],
    floor: float,
    tol: float = 0.02,
    range_tol: float = 0.05,
    allowed: list[int] | None = None,
) -> dict:
    """Largest threshold within `tol` of the lowest score among `allowed` indexes that meet the recall floor."""
    idx = allowed if allowed else list(range(len(grid)))
    ok = [i for i in idx if recall[i] >= floor]
    floor_ok = bool(ok)
    if not ok:  # nothing meets the floor: fall back to the highest-recall threshold(s)
        best_r = max(recall[i] for i in idx)
        ok = [i for i in idx if recall[i] >= best_r - 1e-9]
    lo = min(combined[i] for i in ok)
    best = max(i for i in ok if combined[i] <= lo * (1 + tol))
    rng = [i for i in ok if combined[i] <= lo * (1 + range_tol)]
    return {
        "threshold": grid[best],
        "range": [grid[min(rng)], grid[max(rng)]],
        "floor_met": floor_ok,
        "recall_floor": floor,
        "min_combined_threshold": grid[min(ok, key=lambda i: combined[i])],
        "combined_at_best": combined[best],
        "recall_at_best": recall[best],
        "note": "" if floor_ok else "No threshold met the recall floor; showing highest-recall candidates.",
    }


def pareto(combined: list[float], recall: list[float]) -> list[int]:
    """Indexes not dominated on (lower combined cost, higher recall)."""
    out = []
    for i in range(len(combined)):
        dom = any(
            combined[j] <= combined[i]
            and recall[j] >= recall[i]
            and (combined[j] < combined[i] or recall[j] > recall[i])
            for j in range(len(combined))
        )
        if not dom:
            out.append(i)
    return out


def score(g: dict[str, Any], vot: float) -> list[float]:
    """cost + penalty + value of time × wall-clock, per grid point."""
    return [c + t / 3600 * vot for c, t in zip(g["combined_cost"], g["time_s"], strict=True)]


def point(g: dict[str, Any], i: int, vot: float) -> dict[str, Any]:
    keys = ("cost", "penalty", "combined_cost", "time_s", "time_turns_s", "time_compact_s", "time_redisc_s",
            "compactions", "recall")  # fmt: skip
    p = {k: g[k][i] for k in keys}
    p["threshold"] = g["grid"][i]
    p["combined"] = p["combined_cost"] + p["time_s"] / 3600 * vot
    return p


def compare(p: dict[str, Any], ref: dict[str, Any], days: float = 1.0) -> dict[str, Any]:
    """Profile (or any point) minus a reference point (default or current)."""
    dc = p["combined_cost"] - ref["combined_cost"]
    added = (p["time_compact_s"] + p["time_redisc_s"]) - (ref["time_compact_s"] + ref["time_redisc_s"])
    dt = p["time_s"] - ref["time_s"]
    return {
        "d_cost": dc,
        "d_cost_pct": dc / ref["combined_cost"] if ref["combined_cost"] else 0.0,
        "d_time_s": dt,
        "d_time_pct": dt / ref["time_s"] if ref["time_s"] else 0.0,
        "d_time_min_per_day": dt / 60 / days,
        "time_saved_context_s": ref["time_turns_s"] - p["time_turns_s"],
        "time_added_compaction_s": added,
        "added_wait_min_per_day": added / 60 / days,
        "compactions": p["compactions"],
        "ref_compactions": ref["compactions"],
        "compactions_per_day": p["compactions"] / days,
        "ref_compactions_per_day": ref["compactions"] / days,
        "d_recall": p["recall"] - ref["recall"],
    }


def select_profile(
    g: dict[str, Any],
    name: str,
    *,
    vot: float | None = None,
    floor: float = 0.90,
    tol: float = 0.02,
    range_tol: float = 0.05,
    wait_cap: float = WAIT_CAP_MIN_PER_DAY,
    cap: int | None = None,
) -> dict[str, Any]:
    """Pick one profile's threshold from a group's grid arrays. `g` needs grid, cost arrays, time arrays,
    compactions, recall, effective_max, days and default (a point at the harness default)."""
    spec = PROFILES[name]
    v = spec["value_of_time"] if vot is None else float(vot)
    grid, days, d = g["grid"], max(float(g.get("days") or 1.0), 1.0), g["default"]
    comb = score(g, v)
    emax = int(g.get("effective_max") or grid[-1])
    step = (grid[1] - grid[0]) if len(grid) > 1 else 1
    min_thr = math.ceil(emax * spec["min_fraction"] / step) * step if spec["min_fraction"] else 0
    limit = min(x for x in (cap, emax, grid[-1]) if x is not None)
    in_range = [i for i, T in enumerate(grid) if min_thr <= T <= limit]
    flags: list[str] = []
    if not in_range:  # the grid does not reach the minimum: use the largest allowed threshold
        in_range = [max((i for i, T in enumerate(grid) if limit >= T), default=len(grid) - 1)]
        flags.append("min_window_unreachable")

    def wait(i: int) -> float:
        added = (g["time_compact_s"][i] + g["time_redisc_s"][i]) - (d["time_compact_s"] + d["time_redisc_s"])
        return added / 60 / days

    allowed = in_range
    if spec["enforce_wait_cap"]:
        under = [i for i in in_range if wait(i) <= wait_cap]
        if under:
            allowed = under
        else:
            flags.append("wait_cap_unreachable")
    rec = recommend(grid, comb, g["recall"], floor, tol, range_tol, allowed)
    i = grid.index(rec["threshold"])
    unconstrained = recommend(grid, comb, g["recall"], floor, tol, range_tol)["threshold"]
    if rec["threshold"] != unconstrained:
        flags.append("guardrail_applied")
    if not rec["floor_met"]:
        flags.append("recall_floor_not_met")
    if rec["threshold"] < WARN_FRACTION * emax:
        flags.append("below_quarter_window")
    if wait(i) > wait_cap:
        flags.append("wait_over_cap")
    p = point(g, i, v)
    p.update(
        {
            "profile": name,
            "value_of_time": v,
            "min_threshold": min_thr,
            "unconstrained_threshold": unconstrained,
            "fraction_of_max": rec["threshold"] / emax if emax else 0.0,
            "recommendation": rec,
            "flags": flags,
            "wait_cap_min_per_day": wait_cap,
        }
    )
    return p


def select_all(g: dict[str, Any], *, selected: str = DEFAULT_PROFILE, vot: float | None = None, **kw: Any) -> dict:
    """All three profiles; `vot` overrides only the selected profile's value of time."""
    return {n: select_profile(g, n, vot=vot if n == selected else None, **kw) for n in PROFILE_NAMES}
