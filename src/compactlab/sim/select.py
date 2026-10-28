"""Combined selection: largest threshold with (near-)lowest cost+penalty subject to a recall floor."""

from __future__ import annotations


def recommend(
    grid: list[int],
    combined: list[float],
    recall: list[float],
    floor: float,
    tol: float = 0.02,
    range_tol: float = 0.05,
) -> dict:
    ok = [i for i, r in enumerate(recall) if r >= floor]
    floor_ok = bool(ok)
    if not ok:  # nothing meets the floor: fall back to the highest-recall threshold(s)
        best_r = max(recall)
        ok = [i for i, r in enumerate(recall) if r >= best_r - 1e-9]
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


def gears(grid: list[int], combined: list[float], recall: list[float], floor: float, rec: dict) -> dict:
    """Presets for the report's gear switch: eco (cheapest Pareto point meeting the floor), standard
    (recommendation), recall (highest recall on the frontier, cheapest among ties). Headroom comes from tiers()."""
    front = pareto(combined, recall)
    ok = [i for i in front if recall[i] >= floor] or front
    eco = min(ok, key=lambda i: combined[i])
    hi = max(recall[i] for i in front)
    rcl = min((i for i in front if recall[i] >= hi - 1e-9), key=lambda i: combined[i])
    return {"eco": grid[eco], "standard": rec["threshold"], "recall": grid[rcl], "pareto": [grid[i] for i in front]}


def tiers(
    grid: list[int],
    combined: list[float],
    recall: list[float],
    compactions: list[int],
    floor: float,
    standard: int,
    tolerance: float = 0.12,
    step: int = 25_000,
    cap: int | None = None,
    max_window: int | None = None,
) -> dict:
    """Two result tiers.

    standard: the combined-score optimum under the recall floor (the recommendation).
    headroom: the largest threshold whose combined score is within `tolerance` of the minimum feasible
      combined score, with recall >= floor, rounded down to `step`, never above `cap` or the model window,
      never below standard. It trades a bounded amount of cost for fewer lossy compactions.
    """
    si = grid.index(standard)
    ok = [i for i, r in enumerate(recall) if r >= floor] or [si]
    lo = min(combined[i] for i in ok)
    limit = min(x for x in (cap, max_window, grid[-1]) if x is not None)
    cand = [
        i
        for i in ok
        if combined[i] <= lo * (1 + tolerance) and grid[i] <= limit and grid[i] >= standard and grid[i] % step == 0
    ]
    hi = max(cand, key=lambda i: grid[i]) if cand else si

    def row(i: int) -> dict:
        return {"threshold": grid[i], "combined": combined[i], "recall": recall[i], "compactions": compactions[i]}

    head = row(hi)
    head["extra_cost_pct"] = (combined[hi] / combined[si] - 1) if combined[si] else 0.0
    head["fewer_compactions"] = compactions[si] - compactions[hi]
    head["rule"] = {"tolerance": tolerance, "step": step, "cap": cap, "floor": floor}
    return {"standard": row(si), "headroom": head, "default": "headroom"}
