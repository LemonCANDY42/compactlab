"""Replay simulator: cost / penalty / recall for each auto-compact threshold."""

from __future__ import annotations

import bisect
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..model import Session
from ..pricing import Price, PricingTable
from .baseline import default_note_zh, default_threshold, effective_max
from .calibrate import PenaltyParams, calibrate
from .select import gears, recommend, tiers
from .timing import TimeModel, fit

DEFAULT_ROT = [(0, 1.0), (100_000, 0.98), (200_000, 0.95), (400_000, 0.90), (600_000, 0.84), (1_000_000, 0.75)]


@dataclass
class SimOptions:
    grid_start: int = 100_000
    grid_stop: int | None = None  # None -> model max window
    grid_step: int = 25_000
    ttl_seconds: int | None = None  # None -> pricing default
    summary_tokens: int | None = None  # None -> observed median or 12k
    base_tokens: int | None = None  # None -> observed median first-turn ctx or 35k
    recall_floor: float = 0.90
    tolerance: float = 0.02
    range_tolerance: float = 0.05
    retention: float = 0.90  # fraction of facts kept right after a compaction (assumed)
    retention_half_turns: float = 15.0  # recovery time scale (assumed)
    rot_table: list[tuple[int, float]] = field(default_factory=lambda: list(DEFAULT_ROT))
    penalty: PenaltyParams | None = None  # None -> calibrate
    headroom_tolerance: float = 0.12  # headroom tier: allowed combined-score excess over the optimum
    headroom_step: int = 25_000
    headroom_cap: int | None = None
    value_of_time: float = 10.0  # USD per hour of agent wall-clock folded into the combined score
    view: str = "cost_time"  # which score drives tiers: "cost_time" or "cost"


@dataclass
class Trace:
    g: list[int]  # tokens added to context this turn
    o: list[int]  # output tokens this turn
    gap: list[float]  # idle seconds before this turn
    boundaries: set[int]


def make_trace(s: Session, base: int) -> Trace:
    g, o, gap = [], [], []
    bset = {c.turn_index for c in s.compactions}
    prev = base
    prev_ts = None
    for i, t in enumerate(s.turns):
        g.append(0 if i in bset else max(t.ctx - prev, 0))
        o.append(t.output)
        gap.append(max(t.ts - prev_ts, 0.0) if (t.ts and prev_ts) else 0.0)
        prev, prev_ts = t.ctx, t.ts or prev_ts
    return Trace(g, o, gap, bset)


def rot_fn(table: list[tuple[int, float]]):
    xs = [x for x, _ in table]
    ys = [y for _, y in table]

    def f(ctx: int) -> float:
        if ctx <= xs[0]:
            return ys[0]
        if ctx >= xs[-1]:
            return ys[-1]
        i = bisect.bisect_right(xs, ctx)
        w = (ctx - xs[i - 1]) / (xs[i] - xs[i - 1])
        return ys[i - 1] + w * (ys[i] - ys[i - 1])

    return f


@dataclass
class Run:
    """One replay of a set of traces at one threshold."""

    cost: float = 0.0  # USD, without penalty
    penalty: float = 0.0  # USD added by re-discovery and corrections
    compactions: int = 0
    recall_sum: float = 0.0
    t_turns: float = 0.0  # seconds spent in model turns (depends on context size)
    t_compact: float = 0.0  # seconds spent compacting
    t_redisc: float = 0.0  # seconds spent re-discovering after compactions

    @property
    def time_s(self) -> float:
        return self.t_turns + self.t_compact + self.t_redisc

    def add(self, o: Run) -> None:
        for k in ("cost", "penalty", "compactions", "recall_sum", "t_turns", "t_compact", "t_redisc"):
            setattr(self, k, getattr(self, k) + getattr(o, k))


@dataclass
class Ctx:
    """Per-group constants for replay."""

    price: Price
    base: int
    summ: int
    ttl: float
    pen: PenaltyParams
    tm: TimeModel
    step_tokens: int
    rot: Any
    opts: SimOptions
    _tabs: Any = None

    def tables(self) -> tuple[list[float], list[float], list[float], int]:
        """Lookup tables at 1k-token resolution (turn latency, recall rot) and the retention decay by turn."""
        if self._tabs is None:
            n = 2100  # up to 2.1M tokens
            lat = [self.tm.turn(i * 1000) for i in range(n)]
            rot = [self.rot(i * 1000) for i in range(n)]
            o = self.opts
            dec = [1 - (1 - o.retention) * math.exp(-k / o.retention_half_turns) for k in range(400)]
            self._tabs = (lat, rot, dec, n - 1)
        return self._tabs


def replay(tr: Trace, T: float, c: Ctx, with_penalty: bool = True) -> Run:
    """Replay one trace at threshold T. Penalty cost/time are tracked separately from base cost."""
    price, base, summ = c.price, c.base, c.summ
    lat, rot, dec, top = c.tables()
    short = price.rates(0)
    long_above = price.long_above if price.long_above is not None else 10**12
    long_ = price.rates(long_above + 1) if price.long_above is not None else short
    ttl, rho, fixed, corr = c.ttl, c.pen.rho, c.pen.fixed_tokens, c.pen.correction_turns
    step_tokens, med_turn = max(c.step_tokens, 1), c.tm.median_turn_s
    ndec = len(dec)
    ctx = base
    cost = penalty = t_turns = t_compact = t_redisc = recall = 0.0
    comps = 0
    fresh = False
    since = ndec  # "long ago": decay factor 1
    extra = 0
    gs, os_, gaps = tr.g, tr.o, tr.gap
    avg_out = (sum(os_) / len(os_)) if os_ else 0
    for i in range(len(gs)):
        g = gs[i] + extra
        prev = ctx
        ctx += g
        _, ro, rw, rr = long_ if ctx > long_above else short
        turn = prev * (rw if (fresh or gaps[i] > ttl) else rr) + g * rw + os_[i] * ro
        if extra:  # the share caused by re-discovered tokens is penalty, not base cost
            penalty += extra * rw
            turn -= extra * rw
            extra = 0
        cost += turn
        fresh = False
        ki = ctx // 1000
        if ki > top:
            ki = top
        t_turns += lat[ki]
        recall += rot[ki] * (dec[since] if since < ndec else 1.0)
        since += 1
        if ctx > T:
            cost += ctx * rr + summ * ro  # summarizer call reads the whole context
            t_compact += c.tm.compact(ctx)
            dropped = ctx - base - summ
            ctx = base + summ
            fresh = True
            since = 0
            comps += 1
            if with_penalty:
                extra = int(rho * max(dropped, 0) + fixed)
                _, ro2, _, rr2 = long_ if ctx > long_above else short
                penalty += corr * (ctx * rr2 + avg_out * ro2)
                t_redisc += (extra / step_tokens + corr) * med_turn
    return Run(cost, penalty, comps, recall, t_turns, t_compact, t_redisc)


def run_all(traces: list[Trace], T: float, c: Ctx) -> Run:
    tot = Run()
    for tr in traces:
        tot.add(replay(tr, T, c))
    return tot


def _median(xs, default):
    return int(statistics.median(xs)) if xs else default


def grid_for(opts: SimOptions, max_threshold: int) -> list[int]:
    stop = min(opts.grid_stop or max_threshold, max_threshold)
    return list(range(opts.grid_start, stop + 1, opts.grid_step))


def _point(run: Run, T: float | None, n_turns: int, vot: float) -> dict[str, Any]:
    combined_cost = run.cost + run.penalty
    return {
        "threshold": None if T is None or math.isinf(T) else int(T),
        "cost": run.cost,
        "penalty": run.penalty,
        "combined_cost": combined_cost,
        "time_s": run.time_s,
        "time_turns_s": run.t_turns,
        "time_compact_s": run.t_compact,
        "time_redisc_s": run.t_redisc,
        "combined": combined_cost + run.time_s / 3600 * vot,
        "compactions": run.compactions,
        "recall": run.recall_sum / max(n_turns, 1),
    }


def compare(tier: dict[str, Any], ref: dict[str, Any]) -> dict[str, Any]:
    """Tier minus reference (default or current)."""
    dc = tier["combined_cost"] - ref["combined_cost"]
    return {
        "d_cost": dc,
        "d_cost_pct": dc / ref["combined_cost"] if ref["combined_cost"] else 0.0,
        "d_time_s": tier["time_s"] - ref["time_s"],
        "d_time_pct": (tier["time_s"] - ref["time_s"]) / ref["time_s"] if ref["time_s"] else 0.0,
        "time_saved_context_s": ref["time_turns_s"] - tier["time_turns_s"],
        "time_added_compaction_s": (tier["time_compact_s"] + tier["time_redisc_s"])
        - (ref["time_compact_s"] + ref["time_redisc_s"]),
        "compactions": tier["compactions"],
        "ref_compactions": ref["compactions"],
        "d_recall": tier["recall"] - ref["recall"],
    }


def _select(grid, combined, recall, compactions, opts, cap):
    rec = recommend(grid, combined, recall, opts.recall_floor, opts.tolerance, opts.range_tolerance)
    gr = gears(grid, combined, recall, opts.recall_floor, rec)
    tr = tiers(grid, combined, recall, compactions, opts.recall_floor, rec["threshold"], opts.headroom_tolerance,
               opts.headroom_step, opts.headroom_cap, cap)  # fmt: skip
    gr["headroom"] = tr["headroom"]["threshold"]
    return {"recommendation": rec, "gears": gr, "tiers": tr}


def analyze(
    sessions: list[Session],
    pricing: PricingTable,
    opts: SimOptions | None = None,
    current: dict[tuple[str, str], tuple[int | None, str]] | None = None,
    progress: Any = None,
) -> dict:
    """`current` maps (harness, model) -> (configured threshold or None, description), read from config."""
    opts = opts or SimOptions()
    ttl = opts.ttl_seconds or pricing.default_ttl
    rot = rot_fn(opts.rot_table)
    vot = opts.value_of_time
    groups: dict[tuple[str, str], list[Session]] = defaultdict(list)
    for s in sessions:
        groups[(s.model, s.role)].append(s)
    out_groups = []
    for gi, ((model, role), ss) in enumerate(sorted(groups.items()), 1):
        if progress:
            progress(gi, len(groups), f"{model} · {role} ({len(ss)} sessions)")
        harness = max({s.harness for s in ss}, key=lambda h: sum(x.harness == h for x in ss))
        price = pricing.lookup(model)
        observed_window = max((s.context_window or 0) for s in ss)
        window = observed_window or price.max_window
        cap = effective_max(harness, window)
        base = opts.base_tokens or min(max(_median([s.turns[0].ctx for s in ss], 35_000), 10_000), 80_000)
        obs_summ = [len(c.summary_text) // 4 for s in ss for c in s.compactions if c.summary_text]
        summ = opts.summary_tokens or _median(obs_summ, 12_000)
        pen = opts.penalty or calibrate(ss, base, summ)
        tm = fit(ss)
        step_tokens = _median([x for s in ss for t in s.turns for x in t.tool_result_tokens], 2000) or 2000
        c = Ctx(price, base, summ, ttl, pen, tm, step_tokens, rot, opts)
        traces = [make_trace(s, base) for s in ss]
        grid = grid_for(opts, cap)
        n_turns = sum(len(t.g) for t in traces)
        pts = [_point(run_all(traces, T, c), T, n_turns, vot) for T in grid]
        rows: dict[str, list[Any]] = {
            k: [p[k] for p in pts]
            for k in ("cost", "penalty", "combined_cost", "combined", "recall", "compactions", "time_s",
                      "time_turns_s", "time_compact_s", "time_redisc_s")
        }  # fmt: skip
        views = {
            "cost_time": _select(grid, rows["combined"], rows["recall"], rows["compactions"], opts, cap),
            "cost": _select(grid, rows["combined_cost"], rows["recall"], rows["compactions"], opts, cap),
        }
        main = views[opts.view]
        # tier points with full detail
        for v in views.values():
            for name in ("standard", "headroom"):
                t = v["tiers"][name]
                t.update({k: val for k, val in pts[grid.index(t["threshold"])].items() if k != "threshold"})

        d_thr, d_note = default_threshold(harness, model, window)
        default = _point(run_all(traces, d_thr, c), d_thr, n_turns, vot)
        default["note"] = d_note
        default["note_zh"] = default_note_zh(harness, model)
        cur_val, cur_desc = (current or {}).get((harness, model), (None, ""))
        cur = None
        if cur_val is not None:
            eff = min(cur_val, cap)
            cur = _point(run_all(traces, eff, c), eff, n_turns, vot)
            cur["configured"] = cur_val
            cur["source"] = cur_desc
        no_action = default["compactions"] == 0
        comparison = {}
        for name in ("standard", "headroom"):
            t = main["tiers"][name]
            action = "no action needed" if no_action else ("keep default" if t["threshold"] >= d_thr else "set")
            comparison[name] = {
                "threshold": t["threshold"],
                "action": action,
                "vs_default": compare(t, default),
                "vs_current": compare(t, cur) if cur else None,
            }

        pre = [x.pre_tokens for s in ss for x in s.compactions if x.pre_tokens]
        t_hist = _median(pre, 0) or None
        hist_run = run_all(traces, float(t_hist) if t_hist else math.inf, c)
        out_groups.append(
            {
                "harness": harness,
                "model": model,
                "role": role,
                "price_key": price.key,
                "price_source": price.source,
                "n_sessions": len(ss),
                "n_turns": n_turns,
                "max_window": window,
                "window_source": "observed in logs" if observed_window else "pricing data",
                "effective_max": cap,
                "base_tokens": base,
                "summary_tokens": summ,
                "penalty_params": pen.to_dict(),
                "time_model": tm.summary(),
                "value_of_time": vot,
                "observed_compactions": sum(len(s.compactions) for s in ss),
                "peak_p50": _median([s.peak for s in ss], 0),
                "peak_max": max(s.peak for s in ss),
                "grid": grid,
                **rows,
                "baseline_cost": run_all(traces, math.inf, c).cost,
                "actual_cost": sum(_actual_cost(s, price) for s in ss),
                "view": opts.view,
                "views": views,
                "recommendation": main["recommendation"],
                "gears": main["gears"],
                "tiers": main["tiers"],
                "default": default,
                "current": cur,
                "current_note": cur_desc,
                "no_action_needed": no_action,
                "comparison": comparison,
                "history": {
                    "threshold": t_hist,
                    "combined": _point(hist_run, None, n_turns, vot)["combined"],
                    "cost": hist_run.cost,
                    "recall": hist_run.recall_sum / max(n_turns, 1),
                },
                "hist": _hist([t.ctx for s in ss for t in s.turns], window),
            }
        )
    return {
        "schema": 3,
        "mode": "lite",
        "data_through": max((t.ts for s in sessions for t in s.turns if t.ts), default=None),
        "options": {k: (v if k != "penalty" else (v.to_dict() if v else None)) for k, v in opts.__dict__.items()},
        "pricing_note": pricing.data.get("note", ""),
        "groups": out_groups,
        "sessions": [
            {
                "id": s.id,
                "harness": s.harness,
                "role": s.role,
                "model": s.model,
                "turns": len(s.turns),
                "peak": s.peak,
                "compactions": len(s.compactions),
                "curve": _downsample([t.ctx for t in s.turns], 160),
                "growth": _bin_growth(make_trace(s, 35_000).g, 160),
                "unknown_events": s.unknown_events,
                "skipped_inherited": s.skipped_inherited,
            }
            for s in sorted(sessions, key=lambda s: (s.harness, s.role, s.id))
        ],
    }


def _downsample(xs: list[int], n: int) -> list[int]:
    if len(xs) <= n:
        return xs
    step = len(xs) / n
    return [xs[int(i * step)] for i in range(n)]


def _bin_growth(g: list[int], n: int) -> list[int]:
    if len(g) <= n:
        return list(g)
    size = len(g) / n
    return [sum(g[int(i * size) : int((i + 1) * size)]) for i in range(n)]


def _actual_cost(s: Session, price: Price) -> float:
    c = 0.0
    for t in s.turns:
        ri, ro, rw, rr = price.rates(t.ctx)
        c += max(t.ctx - t.cache_read - t.cache_write, 0) * ri + t.cache_read * rr + t.cache_write * rw + t.output * ro
    return c


def _hist(ctxs: list[int], max_window: int, size: int = 50_000) -> dict:
    n = max(max_window // size, 1)
    turns, tok = [0] * n, [0] * n
    for x in ctxs:
        i = min(x // size, n - 1)
        turns[i] += 1
        tok[i] += x
    tt, ts = max(sum(turns), 1), max(sum(tok), 1)
    return {"bin": size, "turns": [v / tt for v in turns], "share": [v / ts for v in tok]}
