"""Replay simulator: cost / penalty / recall for each auto-compact threshold."""

from __future__ import annotations

import bisect
import math
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..model import Session, Turn
from ..pricing import Price, PricingTable
from .baseline import default_note_zh, default_threshold, effective_max
from .calibrate import PenaltyParams, calibrate
from .select import DEFAULT_PROFILE, WAIT_CAP_MIN_PER_DAY, InfeasibleCap, compare, pareto, point, score, select_all
from .timing import TimeModel, fit

SESSIONS_SHOWN = 30  # per group in the report's session picker (keep in step with report/template.html sessList)
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
    profile: str = DEFAULT_PROFILE  # "cost" | "balanced" | "time" (see sim.select.PROFILES)
    value_of_time: float | None = None  # USD/h; overrides the selected profile's value of time
    wait_cap_min_per_day: float = WAIT_CAP_MIN_PER_DAY  # guardrail on added compaction wait vs default
    max_threshold: int | None = None  # never recommend above this (user cap)


@dataclass
class Trace:
    g: list[int]  # tokens added to context this turn
    o: list[int]  # output tokens this turn
    gap: list[float]  # idle seconds before this turn
    boundaries: set[int]
    models: list[str] | None = None  # per-turn model when the session switched models (else the group's price)


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
    models = [t.model for t in s.turns]
    return Trace(g, o, gap, bset, models if len(set(models)) > 1 else None)


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
    prices: dict[str, Price] | None = None  # model -> price at the group's TTL, for sessions that switched models
    _tabs: Any = None
    _rates: Any = None

    def rate_tab(self, model: str | None) -> tuple[Any, int, Any]:
        """(short-context rates, long-context threshold, long-context rates) for a model; the group's by default."""
        if self._rates is None:
            self._rates = {}
        price = (self.prices or {}).get(model) if model else None
        key = model if price else None
        if key not in self._rates:
            p = price or self.price
            la = p.long_above if p.long_above is not None else 10**12
            self._rates[key] = (p.rates(0), la, p.rates(la + 1) if p.long_above is not None else p.rates(0))
        return self._rates[key]

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
    base, summ = c.base, c.summ
    lat, rot, dec, top = c.tables()
    short, long_above, long_ = c.rate_tab(None)
    models = tr.models
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
        if models is not None:  # price each turn at the model that served it
            short, long_above, long_ = c.rate_tab(models[i])
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


def _point(run: Run, T: float | None, n_turns: int) -> dict[str, Any]:
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
        "compactions": run.compactions,
        "recall": run.recall_sum / max(n_turns, 1),
    }


def select_group(g: dict[str, Any], opts: dict[str, Any]) -> None:
    """(Re)compute a group's profiles, comparison and Pareto set in place from its grid arrays.
    `opts` is a result's "options" dict, so a stored result can be re-selected without replaying."""
    sel = opts.get("profile") or DEFAULT_PROFILE
    profs = select_all(
        g,
        selected=sel,
        vot=opts.get("value_of_time"),
        floor=opts.get("recall_floor", 0.90),
        tol=opts.get("tolerance", 0.02),
        range_tol=opts.get("range_tolerance", 0.05),
        wait_cap=opts.get("wait_cap_min_per_day", WAIT_CAP_MIN_PER_DAY),
        cap=opts.get("max_threshold"),
    )
    d, cur = g["default"], g.get("current")
    main = profs[sel]
    g["profile"] = sel
    g["profiles"] = profs
    g["comparison"] = {name: _comparison(g, p) for name, p in profs.items()}
    g["recommendation"] = main["recommendation"]
    g["value_of_time"] = main["value_of_time"]
    g["combined"] = score(g, main["value_of_time"])
    g["pareto"] = [g["grid"][i] for i in pareto(g["combined"], g["recall"])]
    g["default"]["combined"] = d["combined_cost"] + d["time_s"] / 3600 * main["value_of_time"]
    if cur:
        cur["combined"] = cur["combined_cost"] + cur["time_s"] / 3600 * main["value_of_time"]
    apply_measured(g)  # selection works from the lite proxy; deep-mode measurements still bind


def _comparison(g: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    d, cur, thr = g["default"], g.get("current"), p["threshold"]
    d_score = d["combined_cost"] + d["time_s"] / 3600 * p["value_of_time"]
    if g["no_action_needed"]:
        action = "no action needed"
    elif thr >= d["threshold"] or p["combined"] >= d_score:  # not better than leaving the default alone
        action = "keep default"
    else:
        action = "set"
    return {
        "threshold": thr,
        "action": action,
        "vs_default": compare(p, d, g["days"], g.get("active_days")),
        "vs_current": compare(p, cur, g["days"], g.get("active_days")) if cur else None,
    }


def override_profile(g: dict[str, Any], name: str, threshold: int, flag: str) -> None:
    """Move one profile to another grid threshold (e.g. after deep mode measured its recall) and refresh it."""
    old = g["profiles"][name]
    p = point(g, g["grid"].index(threshold), old["value_of_time"])
    keep = ("profile", "value_of_time", "min_threshold", "unconstrained_threshold", "recommendation",
            "wait_cap_min_per_day")  # fmt: skip
    p.update({k: old[k] for k in keep})
    p["lite_threshold"] = old["threshold"]
    p["fraction_of_max"] = threshold / g["effective_max"] if g.get("effective_max") else 0.0
    p["flags"] = [*old["flags"], flag]
    g["profiles"][name] = p
    g["comparison"][name] = _comparison(g, p)


def apply_measured(g: dict[str, Any]) -> dict[str, int]:
    """Lower every profile whose pick has a measured recall (`g["measured"]`, from deep mode) below the floor to the
    highest measured threshold under it that clears the floor (else the lowest measured one). Returns the
    {profile: new threshold} moves. Called by select_group, so a reselected result keeps its measured guardrails."""
    pts = g.get("measured")
    if not pts:
        return {}
    floor = g["recommendation"]["recall_floor"]
    adjusted: dict[str, int] = {}
    for name, prof in g["profiles"].items():
        thr = prof["threshold"]
        hp = next((p for p in pts if p["threshold"] == thr), None)
        if hp and hp["measured_recall"] < floor:
            ok_pts = [p["threshold"] for p in pts if p["threshold"] < thr and p["measured_recall"] >= floor]
            adjusted[name] = max(ok_pts) if ok_pts else min(p["threshold"] for p in pts)
    for name, thr in adjusted.items():
        override_profile(g, name, thr, "deep_recall_below_floor")
    if g["profile"] in adjusted:
        g["recommendation"] = dict(g["recommendation"], threshold=adjusted[g["profile"]])
    return adjusted


def reselect(result: dict[str, Any], profile: str | None = None, value_of_time: float | None = None) -> dict:
    """Re-run profile selection on a stored result (no replay). Mutates and returns `result`."""
    opts = result.setdefault("options", {})
    if profile:
        opts["profile"] = profile
    if value_of_time is not None:
        opts["value_of_time"] = value_of_time
    for g in result["groups"]:
        select_group(g, opts)
    return result


def analyze(
    sessions: list[Session],
    pricing: PricingTable,
    opts: SimOptions | None = None,
    current: dict[tuple[str, str], tuple[int | None, str]] | None = None,
    progress: Any = None,
) -> dict:
    """`current` maps (harness, model) -> (configured threshold or None, description), read from config."""
    opts = opts or SimOptions()
    if opts.max_threshold is not None and opts.max_threshold < opts.grid_start:
        raise InfeasibleCap(
            f"--max-threshold {opts.max_threshold:,} is below the grid start {opts.grid_start:,}: "
            "lower the grid start (--grid) or raise the cap"
        )
    default_ttl = pricing.default_ttl
    rot = rot_fn(opts.rot_table)
    opt_d = {k: (v if k != "penalty" else (v.to_dict() if v else None)) for k, v in opts.__dict__.items()}
    groups: dict[tuple[str, str, str], list[Session]] = defaultdict(list)
    by_harness: dict[str, list[Session]] = defaultdict(list)
    for s in sessions:
        groups[(s.harness, s.model, s.role)].append(s)  # the same model name under two harnesses is two groups
        by_harness[s.harness].append(s)
    pooled = {h: fit(ss) for h, ss in by_harness.items()}
    out_groups = []
    base_of: dict[int, int] = {}  # id(session) -> its group's base tokens
    shown: set[int] = set()  # sessions the report can list (the group's SESSIONS_SHOWN largest): full-resolution growth
    for gi, ((harness, model, role), ss) in enumerate(sorted(groups.items()), 1):
        if progress:
            progress(gi, len(groups), f"{model} · {role} ({len(ss)} sessions)")
        list_price = pricing.lookup(model)
        ttl, ttl_source = group_ttl(ss, opts.ttl_seconds, default_ttl)
        price = list_price.for_ttl(ttl)
        observed_window = max((s.context_window or 0) for s in ss)
        window = observed_window or price.max_window
        cap = effective_max(harness, window)
        base = opts.base_tokens or min(max(_median([s.turns[0].ctx for s in ss], 35_000), 10_000), 80_000)
        obs_summ = [len(c.summary_text) // 4 for s in ss for c in s.compactions if c.summary_text]
        summ = opts.summary_tokens or _median(obs_summ, 12_000)
        pen = opts.penalty or calibrate(ss, base, summ)
        tm = fit(ss, pooled.get(harness))
        step_tokens = _median([x for s in ss for t in s.turns for x in t.tool_result_tokens], 2000) or 2000
        turn_models = {t.model for s in ss for t in s.turns}
        prices = {m: pricing.lookup(m).for_ttl(ttl) for m in turn_models} if len(turn_models) > 1 else None
        c = Ctx(price, base, summ, ttl, pen, tm, step_tokens, rot, opts, prices)
        traces = [make_trace(s, base) for s in ss]
        base_of.update({id(s): base for s in ss})
        shown.update(id(s) for s in sorted(sorted(ss, key=lambda s: s.id), key=lambda s: -s.peak)[:SESSIONS_SHOWN])
        grid = grid_for(opts, cap)
        n_turns = sum(len(t.g) for t in traces)
        pts = [_point(run_all(traces, T, c), T, n_turns) for T in grid]
        rows: dict[str, list[Any]] = {
            k: [p[k] for p in pts]
            for k in ("cost", "penalty", "combined_cost", "recall", "compactions", "time_s",
                      "time_turns_s", "time_compact_s", "time_redisc_s")
        }  # fmt: skip
        tss = [t.ts for s in ss for t in s.turns if t.ts]
        days = max((max(tss) - min(tss)) / 86400, 1.0) if tss else 1.0
        # local dates with at least one turn, never more than the span (a 10.2-day span can touch 11 dates)
        active_days = min(len({time.strftime("%Y-%m-%d", time.localtime(t)) for t in tss}) or 1, days)

        d_thr, d_note = default_threshold(harness, model, window)
        default = _point(run_all(traces, d_thr, c), d_thr, n_turns)
        default["note"] = d_note
        default["note_zh"] = default_note_zh(harness, model)
        cur_val, cur_desc = (current or {}).get((harness, model), (None, ""))
        cur = None
        if cur_val is not None:
            eff = min(cur_val, cap)
            cur = _point(run_all(traces, eff, c), eff, n_turns)
            cur["configured"] = cur_val
            cur["source"] = cur_desc

        pre = [x.pre_tokens for s in ss for x in s.compactions if x.pre_tokens]
        t_hist = _median(pre, 0) or None
        hist_run = run_all(traces, float(t_hist) if t_hist else math.inf, c)
        g: dict[str, Any] = {
            "harness": harness,
            "model": model,
            "role": role,
            "price_key": price.key,
            "price_source": price.source,
            "cache_ttl": ttl,
            "cache_ttl_source": ttl_source,
            "n_sessions": len(ss),
            "n_turns": n_turns,
            "days": days,
            "active_days": active_days,
            "span": [min(tss), max(tss)] if tss else None,
            "max_window": window,
            "window_source": "observed in logs" if observed_window else "pricing data",
            "effective_max": cap,
            "base_tokens": base,
            "summary_tokens": summ,
            "penalty_params": pen.to_dict(),
            "time_model": tm.summary(),
            "observed_compactions": sum(len(s.compactions) for s in ss),
            "peak_p50": _median([s.peak for s in ss], 0),
            "peak_max": max(s.peak for s in ss),
            "grid": grid,
            **rows,
            "baseline_cost": run_all(traces, math.inf, c).cost,
            "actual_cost": sum(_session_actual_cost(s, pricing) for s in ss),
            "default": default,
            "current": cur,
            "current_note": cur_desc,
            "no_action_needed": default["compactions"] == 0,
            "history": {
                "threshold": t_hist,
                "combined_cost": hist_run.cost + hist_run.penalty,
                "time_s": hist_run.time_s,
                "cost": hist_run.cost,
                "recall": hist_run.recall_sum / max(n_turns, 1),
            },
            "hist": _hist([t.ctx for s in ss for t in s.turns], window),
        }
        select_group(g, opt_d)
        out_groups.append(g)
    return {
        "schema": 4,
        "mode": "lite",
        "data_through": max((t.ts for s in sessions for t in s.turns if t.ts), default=None),
        "options": opt_d,
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
                # the in-browser replay needs every turn: binned growth would count bins as turns
                "growth": make_trace(s, base_of[id(s)]).g if id(s) in shown else [],
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


def group_ttl(ss: list[Session], override: int | None, default: int) -> tuple[int, str]:
    """Cache TTL to replay a group with: the override, else the tier most cache-write tokens were billed at."""
    if override:
        return override, "--cache-ttl"
    written = sum(t.cache_write for s in ss for t in s.turns)
    hour = sum(t.cache_write_1h for s in ss for t in s.turns)
    if not hour:
        return default, "pricing default"
    return (3600 if hour * 2 >= written else default), f"logs: {hour / written:.0%} of cache writes at the 1-hour tier"


def _session_actual_cost(s: Session, pricing: PricingTable) -> float:
    """`_actual_cost` with every turn at its own model's price (a session may switch models)."""
    by_model: dict[str, list[Turn]] = defaultdict(list)
    for t in s.turns:
        by_model[t.model].append(t)
    return sum(
        _actual_cost(Session(s.id, s.harness, s.role, s.path, turns=ts), pricing.lookup(m))
        for m, ts in by_model.items()
    )


def _actual_cost(s: Session, price: Price) -> float:
    """What the logged requests cost at list price, each cache write at the tier it was billed at."""
    hour = price.for_ttl(3600)
    c = 0.0
    for t in s.turns:
        ri, ro, rw, rr = price.rates(t.ctx)
        rw1 = hour.rates(t.ctx)[2]
        w5 = t.cache_write - t.cache_write_1h
        c += max(t.ctx - t.cache_read - t.cache_write, 0) * ri + t.cache_read * rr + w5 * rw + t.cache_write_1h * rw1
        c += t.output * ro
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
