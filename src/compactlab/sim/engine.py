"""Replay simulator: cost / penalty / recall for each auto-compact threshold."""
from __future__ import annotations

import bisect
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field

from .calibrate import PenaltyParams, calibrate
from ..model import Session
from ..pricing import Price, PricingTable
from .select import gears, recommend, tiers

DEFAULT_ROT = [(0, 1.0), (100_000, 0.98), (200_000, 0.95), (400_000, 0.90), (600_000, 0.84), (1_000_000, 0.75)]


@dataclass
class SimOptions:
    grid_start: int = 100_000
    grid_stop: int | None = None          # None -> model max window
    grid_step: int = 25_000
    ttl_seconds: int | None = None        # None -> pricing default
    summary_tokens: int | None = None     # None -> observed median or 12k
    base_tokens: int | None = None        # None -> observed median first-turn ctx or 35k
    recall_floor: float = 0.90
    tolerance: float = 0.02
    range_tolerance: float = 0.05
    retention: float = 0.90               # fraction of facts kept right after a compaction (assumed)
    retention_half_turns: float = 15.0    # recovery time scale (assumed)
    rot_table: list[tuple[int, float]] = field(default_factory=lambda: list(DEFAULT_ROT))
    penalty: PenaltyParams | None = None  # None -> calibrate
    headroom_tolerance: float = 0.12      # headroom tier: allowed combined-score excess over the optimum
    headroom_step: int = 25_000
    headroom_cap: int | None = None


@dataclass
class Trace:
    g: list[int]       # tokens added to context this turn
    o: list[int]       # output tokens this turn
    gap: list[float]   # idle seconds before this turn
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


def replay(tr: Trace, T: float, price: Price, base: int, summ: int, ttl: float,
           pen: PenaltyParams | None, rot, opts: SimOptions) -> tuple[float, int, float]:
    """Returns (cost_usd, compactions, recall_sum). pen=None disables the penalty."""
    ctx = base
    cost = 0.0
    n_comp = 0
    recall_sum = 0.0
    fresh = False
    since = math.inf
    extra = 0
    avg_out = (sum(tr.o) / len(tr.o)) if tr.o else 0
    for i in range(len(tr.g)):
        g = tr.g[i] + extra
        extra = 0
        prev = ctx
        ctx += g
        ri, ro, rw, rr = price.rates(ctx)
        prefix_rate = rw if (fresh or tr.gap[i] > ttl) else rr
        cost += prev * prefix_rate + g * rw + tr.o[i] * ro
        fresh = False
        recall_sum += rot(ctx) * (1 - (1 - opts.retention) * math.exp(-since / opts.retention_half_turns))
        since += 1
        if ctx > T:
            ri, ro, rw, rr = price.rates(ctx)
            cost += ctx * rr + summ * ro            # summarizer call reads the whole context
            dropped = max(ctx - base - summ, 0)
            ctx = base + summ
            fresh = True
            since = 0
            n_comp += 1
            if pen is not None:
                extra = int(pen.rho * dropped + pen.fixed_tokens)
                _, ro2, _, rr2 = price.rates(ctx)
                cost += pen.correction_turns * (ctx * rr2 + avg_out * ro2)
    return cost, n_comp, recall_sum


def _median(xs, default):
    return int(statistics.median(xs)) if xs else default


def grid_for(opts: SimOptions, max_window: int) -> list[int]:
    stop = min(opts.grid_stop or max_window, max_window)
    return list(range(opts.grid_start, stop + 1, opts.grid_step))


def analyze(sessions: list[Session], pricing: PricingTable, opts: SimOptions | None = None) -> dict:
    opts = opts or SimOptions()
    ttl = opts.ttl_seconds or pricing.default_ttl
    rot = rot_fn(opts.rot_table)
    groups: dict[tuple[str, str], list[Session]] = defaultdict(list)
    for s in sessions:
        groups[(s.model, s.role)].append(s)
    out_groups = []
    for (model, role), ss in sorted(groups.items()):
        price = pricing.lookup(model)
        base = opts.base_tokens or min(max(_median([s.turns[0].ctx for s in ss], 35_000), 10_000), 80_000)
        obs_summ = [len(c.summary_text) // 4 for s in ss for c in s.compactions if c.summary_text]
        summ = opts.summary_tokens or _median(obs_summ, 12_000)
        pen = opts.penalty or calibrate(ss, base, summ)
        traces = [make_trace(s, base) for s in ss]
        grid = grid_for(opts, price.max_window)
        n_turns = sum(len(t.g) for t in traces)
        rows = {k: [] for k in ("cost", "penalty", "combined", "recall", "compactions")}
        for T in grid:
            c = p = 0.0
            nc = 0
            rs = 0.0
            for tr in traces:
                c0, k, r = replay(tr, T, price, base, summ, ttl, None, rot, opts)
                c1, _, _ = replay(tr, T, price, base, summ, ttl, pen, rot, opts) if k else (c0, 0, 0)
                c += c0
                p += c1 - c0
                nc += k
                rs += r
            rows["cost"].append(c)
            rows["penalty"].append(p)
            rows["combined"].append(c + p)
            rows["recall"].append(rs / max(n_turns, 1))
            rows["compactions"].append(nc)
        baseline = sum(replay(tr, math.inf, price, base, summ, ttl, None, rot, opts)[0] for tr in traces)
        rec = recommend(grid, rows["combined"], rows["recall"], opts.recall_floor, opts.tolerance, opts.range_tolerance)
        gr = gears(grid, rows["combined"], rows["recall"], opts.recall_floor, rec)
        tr_ = tiers(grid, rows["combined"], rows["recall"], rows["compactions"], opts.recall_floor, rec["threshold"],
                    opts.headroom_tolerance, opts.headroom_step, opts.headroom_cap, price.max_window)
        gr["headroom"] = tr_["headroom"]["threshold"]
        pre = [c.pre_tokens for s in ss for c in s.compactions if c.pre_tokens]
        t_hist = _median(pre, 0) or None
        th = float(t_hist) if t_hist else math.inf
        hc = hp = hr = 0.0
        for tr in traces:
            c0, k, r = replay(tr, th, price, base, summ, ttl, None, rot, opts)
            c1 = replay(tr, th, price, base, summ, ttl, pen, rot, opts)[0] if k else c0
            hc, hp, hr = hc + c0, hp + (c1 - c0), hr + r
        actual = sum(_actual_cost(s, price) for s in ss)
        harness = max({s.harness for s in ss}, key=lambda h: sum(x.harness == h for x in ss))
        out_groups.append({
            "harness": harness, "model": model, "role": role, "price_key": price.key, "price_source": price.source,
            "n_sessions": len(ss), "n_turns": n_turns, "max_window": price.max_window,
            "base_tokens": base, "summary_tokens": summ, "penalty_params": pen.to_dict(),
            "observed_compactions": sum(len(s.compactions) for s in ss),
            "peak_p50": _median([s.peak for s in ss], 0), "peak_max": max(s.peak for s in ss),
            "grid": grid, **rows, "baseline_cost": baseline, "actual_cost": actual, "recommendation": rec,
            "gears": gr, "tiers": tr_,
            "history": {"threshold": t_hist, "combined": hc + hp, "cost": hc, "recall": hr / max(n_turns, 1)}, "hist": _hist([t.ctx for s in ss for t in s.turns], price.max_window),
        })
    return {
        "schema": 2,
        "mode": "lite",
        "data_through": max((t.ts for s in sessions for t in s.turns if t.ts), default=None),
        "options": {k: (v if k != "penalty" else (v.to_dict() if v else None)) for k, v in opts.__dict__.items()},
        "pricing_note": pricing.data.get("note", ""),
        "groups": out_groups,
        "sessions": [{
            "id": s.id, "harness": s.harness, "role": s.role, "model": s.model, "turns": len(s.turns),
            "peak": s.peak, "compactions": len(s.compactions),
            "curve": _downsample([t.ctx for t in s.turns], 160),
            "growth": _bin_growth(make_trace(s, 35_000).g, 160),
            "unknown_events": s.unknown_events, "skipped_inherited": s.skipped_inherited,
        } for s in sorted(sessions, key=lambda s: (s.harness, s.role, s.id))],
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
    return [sum(g[int(i * size):int((i + 1) * size)]) for i in range(n)]


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
