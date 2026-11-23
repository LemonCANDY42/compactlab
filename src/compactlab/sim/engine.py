"""Replay simulator: cost / penalty / recall for each auto-compact threshold."""

from __future__ import annotations

import bisect
import dataclasses
import math
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..model import Session, Turn, is_auto, is_threshold
from ..pricing import Price, PricingTable
from ..pricing.ids import family as model_family
from ..pricing.ids import parse as parse_model
from .baseline import default_note_zh, default_threshold, effective_max
from .calibrate import PenaltyParams, calibrate, rediscovery_step
from .select import (
    DEFAULT_PROFILE,
    PROFILE_NAMES,
    WAIT_CAP_MIN_PER_DAY,
    InfeasibleCap,
    compare,
    pareto,
    point,
    score,
    select_all,
)
from .timing import TimeModel, fit

SESSIONS_SHOWN = 30  # per group in the report's session picker (keep in step with report/template.html sessList)
DEFAULT_ROT = [(0, 1.0), (100_000, 0.98), (200_000, 0.95), (400_000, 0.90), (600_000, 0.84), (1_000_000, 0.75)]


# Claude Code idle compaction: a long conversation left idle on the one-hour cache is summarized while that cache is
# still warm (about 55 min in; 54.5-62 min in the maintainer's logs) and the summary is applied when the user returns.
IDLE_MIN_TOKENS = 200_000  # documented default of CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS (values under 100k count as 100k)
IDLE_AFTER_S = 55 * 60
IDLE_TTL_S = 3600  # only on the one-hour cache
IDLE_MIN_OBSERVED = 5


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
    # Claude Code idle compaction (`idleCompaction`): None -> on (the harness default) unless config says otherwise
    idle_compaction: bool | None = None
    idle_min_tokens: int = IDLE_MIN_TOKENS  # CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS
    # CLAUDE_CODE_MAX_CONTEXT_TOKENS: the window Claude Code uses for model IDs it does not recognize (else 200K)
    claude_max_context: int | None = None


@dataclass
class Trace:
    g: list[int]  # tokens added to context this turn
    o: list[int]  # output tokens this turn
    gap: list[float]  # idle seconds before this turn
    boundaries: set[int]
    manual: dict[int, int] = field(default_factory=dict)  # turn after a hand-typed /compact -> context it was typed at
    models: list[str] | None = None  # per-turn model when the session switched models (else the group's price)
    # turn -> did an idle compaction come before it, for each turn the log shows was eligible (away long enough with a
    # large enough context). Whether one fires depends on the app running and the machine awake, not on the context
    # size, so a replay keeps the logged outcome; other gaps fire at the group's observed rate.
    idle_obs: dict[int, bool] = field(default_factory=dict)
    tail_gap: float = 0.0  # seconds from the last turn to the end of the data: a session left idle, never resumed
    seed: int = 0  # for the rate draw on gaps the log says nothing about


# A replay keeps a hand-typed /compact only while the replayed context is still near the size it was typed at: if a
# lower threshold already shrank the context, the user would not have needed to.
MANUAL_KEEP = 0.7


def make_trace(
    s: Session,
    base: int,
    data_end: float | None = None,
    idle_min: int = IDLE_MIN_TOKENS,
    idle_after: float = IDLE_AFTER_S,
    idle_since: float | None = None,
) -> Trace:
    g, o, gap = [], [], []
    bset = {c.turn_index for c in s.compactions}
    idle_at = {c.turn_index for c in s.compactions if c.idle}
    prev = base
    prev_ts = None
    for i, t in enumerate(s.turns):
        g.append(0 if i in bset else max(t.ctx - prev, 0))
        o.append(t.output)
        gap.append(max(t.ts - prev_ts, 0.0) if (t.ts and prev_ts) else 0.0)
        prev, prev_ts = t.ctx, t.ts or prev_ts
    models = [t.model for t in s.turns]
    manual = {
        c.turn_index: c.pre_tokens for c in s.compactions if c.trigger == "manual" and c.pre_tokens and c.turn_index
    }
    turns = s.turns
    idle_obs = idle_observations(s, idle_min, idle_after, idle_since, gap, idle_at)
    last_ts = turns[-1].ts if turns else None
    tail = max(data_end - last_ts, 0.0) if (data_end and last_ts) else 0.0
    seed = int.from_bytes(s.id.encode()[-8:].rjust(8, b"\0"), "big") % 1_000_003
    return Trace(g, o, gap, bset, models=models if len(set(models)) > 1 else None, manual=manual, idle_obs=idle_obs,
                 tail_gap=tail, seed=seed)  # fmt: skip


def idle_observations(
    s: Session,
    idle_min: int,
    idle_after: float,
    since: float | None,
    gap: list[float] | None = None,
    idle_at: set[int] | None = None,
) -> dict[int, bool]:
    """Turn -> was it preceded by an idle compaction, for every turn the log shows was eligible for one. Only from
    `since` (the first idle compaction in the logs) on: before it the harness may not have had the feature, so a gap
    without one says nothing. With `since` None there is no observation at all."""
    if since is None:
        return {}
    turns = s.turns
    if gap is None:
        prev = [turns[0], *turns[:-1]] if turns else []
        gap = [max(b.ts - a.ts, 0.0) if (a.ts and b.ts) else 0.0 for a, b in zip(prev, turns, strict=True)]
    if idle_at is None:
        idle_at = {c.turn_index for c in s.compactions if c.idle}
    return {
        i: i in idle_at
        for i in range(1, len(turns))
        if ((gap[i] >= idle_after and turns[i - 1].ctx >= idle_min) or i in idle_at) and (turns[i].ts or 0) >= since
    }


def idle_rate(obs: list[bool], pooled: list[bool]) -> tuple[float, str, int, int]:
    """(rate, scope, fired, eligible): the share of eligible idle gaps that ended in an idle compaction, from the
    group when it has enough, else the harness, else 1.0 ("assumed": the documented behaviour, on for conversations
    that meet its conditions)."""
    for xs, scope in ((obs, "group"), (pooled, "harness")):
        if len(xs) >= IDLE_MIN_OBSERVED:
            return sum(xs) / len(xs), scope, sum(xs), len(xs)
    return 1.0, "assumed", 0, 0


def _draw(seed: int, i: int) -> float:
    """A fixed pseudo-random number in [0, 1) per (session, turn): the same gap fires the same way at every
    threshold, so the sweep stays smooth."""
    return ((seed * 1_000_003 + i * 2_654_435_761) % 1_000_000_007) / 1_000_000_007


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
    idle_compactions: int = 0  # of `compactions`, the idle ones
    idle_cost: float = 0.0  # USD in `cost` for idle-compaction summaries, applied or not
    idle_tail_cost: float = 0.0  # of that, summaries made for sessions left idle and never resumed (expected value)
    manual_compactions: int = 0  # of `compactions`, the hand-typed /compact the replay kept

    @property
    def time_s(self) -> float:
        return self.t_turns + self.t_compact + self.t_redisc

    def add(self, o: Run) -> None:
        for k in ("cost", "penalty", "compactions", "recall_sum", "t_turns", "t_compact", "t_redisc",
                  "idle_compactions", "idle_cost", "idle_tail_cost", "manual_compactions"):  # fmt: skip
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
    idle_on: bool = False  # replay idle compaction (Claude Code, one-hour cache, setting on)
    idle_min: int = IDLE_MIN_TOKENS
    idle_after: float = IDLE_AFTER_S
    idle_rate: float = 1.0  # share of eligible gaps that fire, where the log does not say
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
    man = tr.manual
    ttl, rho, fixed, corr = c.ttl, c.pen.rho, c.pen.fixed_tokens, c.pen.correction_turns
    step_tokens, med_turn = max(c.step_tokens, 1), c.tm.median_turn_s
    ndec = len(dec)
    ctx = base
    cost = penalty = t_turns = t_compact = t_redisc = recall = idle_cost = 0.0
    comps = idle_comps = manual_comps = 0
    idle_on, idle_min, idle_after, idle_obs = c.idle_on, c.idle_min, c.idle_after, tr.idle_obs
    fresh = False
    since = ndec  # "long ago": decay factor 1
    extra = 0
    gs, os_, gaps = tr.g, tr.o, tr.gap
    avg_out = (sum(os_) / len(os_)) if os_ else 0
    for i in range(len(gs)):
        if models is not None:  # price each turn at the model that served it
            short, long_above, long_ = c.rate_tab(models[i])
        if idle_on and i and ctx >= idle_min:
            fired = idle_obs.get(i)  # the logged outcome where the log shows one (an idle return can be 50+ min)
            if fired is None:
                fired = gaps[i] >= idle_after and _draw(tr.seed, i) < c.idle_rate
            if fired:  # summarized while the cache was warm: a cache read and the summary; nobody waits for it
                _, ro_, _, rr_ = long_ if ctx > long_above else short
                ic = ctx * rr_ + summ * ro_
                cost += ic
                idle_cost += ic
                dropped = ctx - base - summ
                ctx = base + summ
                fresh = True  # the turn on return writes the small context to the cache
                since = 0
                comps += 1
                idle_comps += 1
                if with_penalty:
                    extra = int(rho * max(dropped, 0) + fixed)
                    _, ro2, _, rr2 = long_ if ctx > long_above else short
                    penalty += corr * (ctx * rr2 + avg_out * ro2)
                    t_redisc += (extra / step_tokens + corr) * med_turn
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
        if ctx > T or (man and ctx >= MANUAL_KEEP * man.get(i + 1, 1 << 60)):
            if ctx <= T:
                manual_comps += 1
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
    tail = 0.0
    if idle_on and tr.tail_gap >= idle_after and ctx >= idle_min:  # left idle, never resumed: a summary nobody used
        _, ro_, _, rr_ = long_ if ctx > long_above else short
        tail = c.idle_rate * (ctx * rr_ + summ * ro_)
    return Run(cost + tail, penalty, comps, recall, t_turns, t_compact, t_redisc, idle_comps, idle_cost + tail, tail,
               manual_comps)  # fmt: skip


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


ROW_KEYS = ("cost", "penalty", "combined_cost", "recall", "compactions", "time_s", "time_turns_s", "time_compact_s",
            "time_redisc_s", "idle_compactions", "idle_cost", "idle_tail_cost", "manual_compactions")  # fmt: skip
BRIEF_KEYS = ("cost", "penalty", "combined_cost", "time_s", "time_compact_s", "time_redisc_s", "compactions",
              "idle_compactions", "idle_cost", "idle_tail_cost", "recall")  # fmt: skip


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
        "idle_compactions": run.idle_compactions,
        "idle_cost": run.idle_cost,
        "idle_tail_cost": run.idle_tail_cost,
        "manual_compactions": run.manual_compactions,
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
    tol = p.get("recommendation", {}).get("tolerance", 0.02)
    if g["no_action_needed"]:
        action = "no action needed"
    # your current setting is within the tolerance of the pick: changing it would not pay for the churn (and the pick
    # may even score a little worse, since it is the largest threshold within the tolerance of the best)
    elif cur is not None and cur["combined_cost"] + cur["time_s"] / 3600 * p["value_of_time"] <= p["combined"] * (
        1 + tol
    ):
        action = "keep current"
    # not better than leaving the default alone, or better by no more than the tolerance the pick itself allows (the
    # pick is the largest threshold within it of the best score, so a default that close is as good a choice)
    elif thr >= d["threshold"] or d_score <= p["combined"] * (1 + tol):
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


GLOBAL_KEY_HARNESSES = ("codex",)  # one threshold for every model (model_auto_compact_token_limit)
SUPERSEDE_GRACE_S = 3 * 86400


def superseded(groups: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """(harness, model) -> the newer version of it that took over: same family, a higher version, used after the old
    one's last use, and the old one not used for more than SUPERSEDE_GRACE_S after the new one first appeared
    (gpt-6.1-sol -> gpt-6.2-sol). A model used alongside its successor is not superseded."""
    spans: dict[tuple[str, str], list[float]] = {}
    for g in groups:
        if g.get("span") and g.get("model"):
            s = spans.setdefault((g.get("harness", ""), g["model"]), [g["span"][0], g["span"][1]])
            s[0], s[1] = min(s[0], g["span"][0]), max(s[1], g["span"][1])
    fams = {k: model_family(parse_model(k[1]).id) for k in spans}
    out: dict[tuple[str, str], str] = {}
    for (h, a), (_, last_a) in spans.items():
        fam, va = fams[(h, a)]
        newer = [
            (fams[(h2, b)][1], b)
            for (h2, b), (first_b, last_b) in spans.items()
            if h2 == h and b != a and fam and va and fams[(h2, b)][0] == fam and fams[(h2, b)][1] > va
            and last_b > last_a and last_a <= first_b + SUPERSEDE_GRACE_S
        ]  # fmt: skip
        if newer:
            out[(h, a)] = max(newer)[1]
    return out


def _global_pick(used: list[dict[str, Any]], name: str, tol: float) -> dict[str, Any]:
    """One threshold for every model of a harness: each model is scored at min(T, its own clamp) and the per-day
    scores are summed; then the same rules as one group (the largest T within `tol` of the best, guardrails per model,
    keep current / keep default within `tol`)."""
    vot = float(used[0]["profiles"][name]["value_of_time"])

    def at(g: dict[str, Any], T: int) -> dict[str, Any] | None:
        eff = min(T, g["effective_max"])
        if eff >= g["default"]["threshold"]:
            return g["default"]
        return point(g, g["grid"].index(eff), vot) if eff in g["grid"] else None

    def total(pts: list[tuple[dict[str, Any], dict[str, Any]]]) -> float:
        return sum((p["combined_cost"] + p["time_s"] / 3600 * vot) / g["days"] for g, p in pts)

    Pts = list[tuple[dict[str, Any], dict[str, Any]]]
    scored: list[tuple[int, float, Pts]] = []
    for T in sorted({x for g in used for x in g["grid"]}):
        pts: Pts = []
        for g in used:
            p = at(g, T)
            if p is None:  # not on this model's grid
                break
            prof = g["profiles"][name]
            if p is not g["default"] and (
                prof.get("min_threshold", 0) > T or p["recall"] < prof["recommendation"]["recall_floor"]
            ):
                break  # a guardrail of this model's profile
            pts.append((g, p))
        else:
            scored.append((T, total(pts), pts))
    d_pts = [(g, g["default"]) for g in used]
    out: dict[str, Any] = {"threshold": None, "default_score": total(d_pts), "per_model": {}}
    cur = next((g["current"]["configured"] for g in used if g.get("current")), None)
    if all(g["no_action_needed"] for g in used):
        return {**out, "action": "no action needed"}
    if not scored:
        return {**out, "action": "keep default"}
    best = min(s for _, s, _ in scored)
    T, S, pts = max((x for x in scored if x[1] <= best * (1 + tol)), key=lambda x: x[0])
    out.update(threshold=T, score=S,
               d_usd_per_day=sum((p["combined_cost"] - g["default"]["combined_cost"]) / g["days"] for g, p in pts),
               d_min_per_day=sum((p["time_s"] - g["default"]["time_s"]) / 60 / g["days"] for g, p in pts),
               per_model={f"{g['model']} · {g['role']}": (None if p is g["default"] else p["threshold"])
                          for g, p in pts})  # fmt: skip
    binding = any(p is not g["default"] for g, p in pts)
    if cur is not None and total([(g, g.get("current") or g["default"]) for g in used]) <= S * (1 + tol):
        return {**out, "action": "keep current", "current": cur}
    if not binding or out["default_score"] <= S * (1 + tol):
        return {**out, "action": "keep default"}
    return {**out, "action": "set"}


def _apply_global(g: dict[str, Any], name: str, gp: dict[str, Any], harness: str, models: list[str]) -> None:
    """Make one group's comparison say what the harness-wide value does to it."""
    T = gp["threshold"] if gp["action"] == "set" else None
    eff = min(T, g["effective_max"]) if T else None
    if eff is not None and eff < g["default"]["threshold"] and eff in g["grid"]:
        override_profile(g, name, eff, "global_key")
        g["comparison"][name]["action"] = "set"
    elif g["no_action_needed"]:
        g["comparison"][name]["action"] = "no action needed"
    elif gp["action"] == "keep current" and g.get("current"):
        g["comparison"][name]["action"] = "keep current"
    else:
        g["comparison"][name]["action"] = "keep default"
    g["comparison"][name]["global"] = {"harness": harness, "action": gp["action"], "threshold": gp["threshold"],
                                       "models": models, "superseded_by": g.get("superseded_by")}  # fmt: skip


def global_keys(groups: list[dict[str, Any]], opts: dict[str, Any]) -> dict[str, Any]:
    """Harnesses with one threshold for every model (Codex): choose it across the models in use (a model a newer
    version took over is left out) and rewrite each of its groups' comparison to match. Marks `superseded_by` on
    every group, of any harness."""
    sup = superseded(groups)
    for g in groups:
        g["superseded_by"] = sup.get((g.get("harness", ""), g.get("model", "")))
    tol = opts.get("tolerance", 0.02)
    out: dict[str, Any] = {}
    for h in GLOBAL_KEY_HARNESSES:
        hs = [g for g in groups if g.get("harness") == h]
        if not hs:
            continue
        used = [g for g in hs if not g["superseded_by"]] or hs
        models = [f"{g['model']} · {g['role']}" for g in used]
        res: dict[str, Any] = {
            "models": models,
            "superseded": {g["model"]: g["superseded_by"] for g in hs if g["superseded_by"]},
            "profiles": {n: _global_pick(used, n, tol) for n in PROFILE_NAMES},
        }
        for g in hs:
            for n in PROFILE_NAMES:
                _apply_global(g, n, res["profiles"][n], h, models)
        out[h] = res
    return out


def reselect(result: dict[str, Any], profile: str | None = None, value_of_time: float | None = None) -> dict:
    """Re-run profile selection on a stored result (no replay). Mutates and returns `result`."""
    opts = result.setdefault("options", {})
    if profile:
        opts["profile"] = profile
    if value_of_time is not None:
        opts["value_of_time"] = value_of_time
    for g in result["groups"]:
        select_group(g, opts)
    result["global_keys"] = global_keys(result["groups"], opts)
    for g in result["groups"]:
        if (g.get("idle") or {}).get("alt", {}).get("rows"):
            idle_select(g)
    if "idle_compaction" in result:
        result["idle_compaction"] = idle_advice(result["groups"], opts)
    return result


def no_action(default: dict[str, Any]) -> bool:
    """The default threshold is never reached: no compaction but idle ones and the hand-typed /compact kept."""
    return default["compactions"] - default.get("idle_compactions", 0) - default.get("manual_compactions", 0) == 0


def idle_select(g: dict[str, Any]) -> None:
    """Record what would be in force with idle compaction as configured and flipped, both at the thresholds the group
    emits (its profile picks, or the current setting). The flipped sweep is not re-selected: the recommendation only
    adds the idle toggle to these picks, so the verdict must compare the two states at them."""
    alt = g["idle"]["alt"]
    ga = {**g, **alt["rows"], "default": alt["default_point"], "current": alt["current_point"]}
    alt["profiles"] = {n: in_force(ga, n) for n in PROFILE_NAMES}
    alt["current"] = in_force(ga, None)
    g["idle"]["profiles"] = {n: in_force(g, n) for n in PROFILE_NAMES}
    g["idle"]["current"] = in_force(g, None)


def _one_hour(s: Session) -> bool:
    """Most of the session's cache writes were billed at the one-hour tier (idle compaction needs it)."""
    w = sum(t.cache_write for t in s.turns)
    return w > 0 and 2 * sum(t.cache_write_1h for t in s.turns) >= w


def in_force(g: dict[str, Any], profile: str | None) -> dict[str, Any]:
    """The replayed point that would be in force: a profile's pick when its action is "set", your setting when it is
    "keep current", else the default; with `profile` None, the current setting (or the default when none is set)."""
    action = g["comparison"][profile]["action"] if profile is not None else None
    if profile is None or (action == "keep current" and g.get("current")):
        src = g.get("current") or g["default"]
    elif action == "set":
        i = g["grid"].index(g["comparison"][profile]["threshold"])
        src = {k: g[k][i] for k in BRIEF_KEYS if k in g}
        src["threshold"] = g["grid"][i]
    else:
        src = g["default"]
    return {"threshold": src.get("threshold"), **{k: src.get(k, 0.0) for k in BRIEF_KEYS}}


def idle_advice(groups: list[dict[str, Any]], opts: dict[str, Any]) -> dict[str, Any] | None:
    """Keep Claude Code's idle compaction on or off? It is one global setting, so the answer adds up every group it
    applies to, each at what would be in force (a profile's picks, or the current settings), with the setting as it is
    and flipped. Switch only when the flip beats the current state by more than the selection tolerance."""
    gs = [g for g in groups if g.get("idle", {}).get("alt")]
    if not gs:
        return None
    tol = opts.get("tolerance", 0.02)
    enabled = gs[0]["idle"]["enabled"]

    def per_day(side: str, name: str | None) -> dict[str, float]:
        tot = {k: 0.0 for k in BRIEF_KEYS if k != "recall"}
        rec = turns = 0.0
        for g in gs:
            src = g["idle"] if side == "configured" else g["idle"]["alt"]
            pt = src["current"] if name is None else src["profiles"][name]
            for k in tot:
                tot[k] += pt[k] / g["days"]
            rec += pt["recall"] * g["n_turns"]
            turns += g["n_turns"]
        tot["recall"] = rec / turns if turns else 0.0
        return tot

    def verdict(name: str | None, vot: float) -> dict[str, Any]:
        a, b = per_day("configured", name), per_day("flipped", name)
        sa = a["combined_cost"] + a["time_s"] / 3600 * vot
        sb = b["combined_cost"] + b["time_s"] / 3600 * vot
        return {
            "configured": a,
            "flipped": b,
            "d_usd_per_day": b["combined_cost"] - a["combined_cost"],
            "d_min_per_day": (b["time_s"] - a["time_s"]) / 60,
            "d_compactions_per_day": b["compactions"] - a["compactions"],
            "d_recall": b["recall"] - a["recall"],
            "d_score_pct": (sb - sa) / sa if sa else 0.0,
            "switch": sb < sa * (1 - tol),
        }

    sel = opts.get("profile") or DEFAULT_PROFILE
    vots = {n: gs[0]["profiles"][n]["value_of_time"] for n in PROFILE_NAMES}
    return {
        "enabled": enabled,
        "configured": gs[0]["idle"]["configured"],
        "min_tokens": gs[0]["idle"]["min_tokens"],
        "groups": [f"{g['model']} · {g['role']}" for g in gs],
        "profile": sel,
        "profiles": {n: verdict(n, vots[n]) for n in PROFILE_NAMES},
        "current": verdict(None, vots[sel]),
    }


CLAUDE_UNKNOWN_WINDOW = 200_000  # Claude Code's window for a model ID it does not recognize


def claude_recognizes(model: str) -> bool:
    """Claude Code sizes the window from the model ID: Claude IDs (on any platform) it knows; anything else, such as a
    third-party model behind ANTHROPIC_BASE_URL or a gateway alias, gets 200K unless CLAUDE_CODE_MAX_CONTEXT_TOKENS
    says otherwise."""
    return model in ("unknown", "<synthetic>") or parse_model(model).id.startswith("claude-")


PRICE_FIELDS = ("input", "output", "cache_write", "cache_read", "max_window")


def price_snapshot(p: Price) -> dict[str, Any]:
    """The list price a group was replayed with, kept in the result so a later price change can be noticed."""
    return {**{f: getattr(p, f) for f in PRICE_FIELDS}, "key": p.key, "source": p.source}


def price_drift(result: dict[str, Any], pricing: PricingTable) -> list[dict[str, Any]]:
    """Prices and windows that differ between a saved result and the table now (one entry per model and field)."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for g in result.get("groups", []):
        was = g.get("price")
        if not was:
            continue
        now = price_snapshot(pricing.lookup(g["model"], g.get("platform")))
        for f in PRICE_FIELDS:
            if was.get(f) is not None and float(was[f]) != float(now[f]):
                out.setdefault((g["model"], f), {"model": g["model"], "field": f, "old": was[f], "new": now[f],
                                                 "source": now["source"]})  # fmt: skip
    return list(out.values())


WINDOW_RECENT = 10
SWITCH_SHARE = 0.8  # a window change is reported when each side agrees this much (not when sessions alternate)


def _mode(xs: list[int]) -> int:
    """The most common value (the larger on a tie)."""
    c = Counter(xs)
    return max(c, key=lambda x: (c[x], x))


def group_window(
    harness: str, model: str, ss: list[Session], table_window: int, opts: SimOptions
) -> tuple[int, str, dict[str, Any] | None]:
    """(window, where it came from, a note when it moved or contradicts the logs). Harnesses change a model's window
    over time, so the latest session's logged window wins over older ones (Codex logs it per session); without one,
    the price data's. Claude Code gives a model ID it does not recognize 200K (or CLAUDE_CODE_MAX_CONTEXT_TOKENS).
    Contexts in the logs larger than that window mean it is out of date: a Claude model then has the 1M window, any
    other the largest context seen."""
    dated = sorted((s.turns[-1].ts or 0.0, int(s.context_window)) for s in ss if s.context_window and s.turns)
    note: dict[str, Any] | None = None
    if dated:
        # the most common window of the latest sessions (sessions of one model can differ, e.g. by client version);
        # a change is reported only when the earlier sessions agreed on another one
        recent, older = dated[-WINDOW_RECENT:], dated[:-WINDOW_RECENT]
        window = _mode([w for _, w in recent])
        source = "observed in logs (latest sessions)"
        was = _mode([w for _, w in older]) if older else window

        def share(block: list[tuple[float, int]], x: int) -> float:
            return sum(w == x for _, w in block) / len(block)

        if was != window and share(recent, window) >= SWITCH_SHARE and share(older, was) >= SWITCH_SHARE:
            since = next(t for t, w in recent if w == window)
            note = {"kind": "window_changed", "from": was, "to": window, "since": since}
    else:
        window, source = table_window, "pricing data"
    if harness == "claude-code" and not claude_recognizes(model):
        window = opts.claude_max_context or CLAUDE_UNKNOWN_WINDOW
        source = (
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS"
            if opts.claude_max_context
            else "Claude Code assumes 200K for a model ID it does not recognize"
        )
    peak = max((s.peak for s in ss), default=0)
    if not dated and peak > window:  # a logged window is the harness's own word; an assumed one can be stale
        was = window
        window = 1_000_000 if harness == "claude-code" and claude_recognizes(model) and peak <= 1_000_000 else peak
        source = f"contexts in the logs reached {peak:,}, beyond the {was:,} assumed"
        note = {"kind": "peak_over_window", "assumed": was, "peak": peak, "window": window}
    return window, source, note


DRIFT_RECENT = 10  # threshold compactions to look at, newest first
DRIFT_MIN = 5
DRIFT_FRAC = 0.10


def default_drift(ss: list[Session], assumed: int) -> dict[str, Any] | None:
    """With no setting of yours in force, the threshold compactions should land near the harness default. When the
    latest ones (at least DRIFT_MIN of the last DRIFT_RECENT) land more than 10% away, either the harness changed its
    default (an update) or a setting compactlab cannot see is in force (an env var, a project file, a CLI flag)."""
    recent = sorted((c for s in ss for c in s.compactions if c.pre_tokens and is_threshold(c)), key=lambda c: c.ts or 0)
    recent = recent[-DRIFT_RECENT:]
    if len(recent) < DRIFT_MIN:
        return None
    observed = int(_median([c.pre_tokens or 0 for c in recent], 0))
    if abs(observed - assumed) <= DRIFT_FRAC * assumed:
        return None
    return {"assumed": assumed, "observed": observed, "n": len(recent), "since": recent[0].ts}


def model_notes(groups: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Models priced from something other than their own row (another platform's price, a borrowed family row or the
    fallback); Claude Code models with no window of their own while other models have one (a new model the settings
    file has not caught up with), counted only when its sessions reach where the setting would act; windows that
    changed or that the logs contradict; and defaults the latest compactions do not match."""
    prices: dict[tuple[str, str], dict[str, Any]] = {}
    for g in groups:
        if g.get("price_kind", "exact") != "exact":
            prices.setdefault((g["harness"], g["model"]), {
                "harness": g["harness"], "model": g["model"], "kind": g["price_kind"], "row": g["price_key"],
                "platform": g.get("platform"), "source": g["price_source"]})  # fmt: skip
    unset = []
    for g in groups:
        if g["harness"] != "claude-code" or g["role"] != "main" or g.get("current") is not None:
            continue
        peers = [x for x in groups if x["harness"] == g["harness"] and x["model"] != g["model"] and x.get("current")]
        if not peers:
            continue
        fam = model_family(parse_model(g["model"]).id)[0]
        same = [x for x in peers if model_family(parse_model(x["model"]).id)[0] == fam]
        peer = max(same or peers, key=lambda x: x["n_turns"])
        if peer["current"]["configured"] >= g["default"]["threshold"]:
            continue  # the peer's value would not lower this model's compaction point (e.g. a 200K window)
        if g.get("peak_max", 0) < peer["current"]["configured"]:
            continue  # its sessions never get where a setting would act: nothing to catch up on yet
        unset.append({
            "harness": g["harness"], "model": g["model"], "default": g["default"]["threshold"],
            "active_days": g["active_days"], "peer": peer["model"], "peer_value": peer["current"]["configured"],
            "same_family": bool(same)})  # fmt: skip
    windows = [{"harness": g["harness"], "model": g["model"], "role": g["role"], **g["window_note"]}
               for g in groups if g.get("window_note")]  # fmt: skip
    defaults = [{"harness": g["harness"], "model": g["model"], "role": g["role"], **g["default_drift"]}
                for g in groups if g.get("default_drift")]  # fmt: skip
    last: dict[tuple[str, str, str], float] = {}
    for g in groups:
        if g.get("superseded_by"):
            key = (g["harness"], g["model"], g["superseded_by"])
            last[key] = max(last.get(key, 0.0), g["span"][1] if g.get("span") else 0.0)
    replaced = [{"harness": h, "model": m, "by": b, "last_used": t, "global_key": h in GLOBAL_KEY_HARNESSES}
                for (h, m, b), t in sorted(last.items())]  # fmt: skip
    return {"prices": list(prices.values()), "unset": unset, "windows": windows, "defaults": defaults,
            "superseded": replaced}  # fmt: skip


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
    data_end = max((t.ts for s in sessions for t in s.turns if t.ts), default=None)
    idle_enabled = True if opts.idle_compaction is None else bool(opts.idle_compaction)
    idle_min = max(int(opts.idle_min_tokens), 100_000)  # the harness raises smaller values to 100k
    # The logs only say how often idle compaction fires while it is on, and from when the harness had it: the first
    # idle compaction seen (per harness). Before that, or with the setting off, no gap is an observation.
    idle_since = {
        h: min((c.ts for s in ss for c in s.compactions if c.idle and c.ts), default=None) if idle_enabled else None
        for h, ss in by_harness.items()
    }
    pooled_idle = {  # main sessions on the one-hour cache: the ones idle compaction is for
        h: [
            v
            for s in ss
            if s.role == "main" and _one_hour(s)
            for v in idle_observations(s, idle_min, IDLE_AFTER_S, idle_since[h]).values()
        ]
        for h, ss in by_harness.items()
    }
    out_groups = []
    base_of: dict[int, int] = {}  # id(session) -> its group's base tokens
    shown: set[int] = set()  # sessions the report can list (the group's SESSIONS_SHOWN largest): full-resolution growth
    for gi, ((harness, model, role), ss) in enumerate(sorted(groups.items()), 1):
        if progress:
            progress(gi, len(groups), f"{model} · {role} ({len(ss)} sessions)")
        platform = next(iter(Counter(s.platform for s in ss if s.platform).most_common(1)), (None, 0))[0]
        list_price = pricing.lookup(model, platform)
        platform = platform or list_price.platform  # a Bedrock or Vertex ID names its platform itself
        ttl, ttl_source = group_ttl(ss, opts.ttl_seconds, default_ttl)
        price = list_price.for_ttl(ttl)
        window, window_source, window_note = group_window(harness, model, ss, price.max_window, opts)
        cap = effective_max(harness, window)
        base = opts.base_tokens or min(max(_median([s.turns[0].ctx for s in ss], 35_000), 10_000), 80_000)
        obs_summ = [len(c.summary_text) // 4 for s in ss for c in s.compactions if c.summary_text]
        summ = opts.summary_tokens or _median(obs_summ, 12_000)
        pen = opts.penalty or calibrate(ss, base, summ)
        tm = fit(ss, pooled.get(harness))
        step_tokens = rediscovery_step(ss, pen)
        turn_models = {t.model for s in ss for t in s.turns}
        prices = {m: pricing.lookup(m, platform).for_ttl(ttl) for m in turn_models} if len(turn_models) > 1 else None
        traces = [make_trace(s, base, data_end, idle_min, idle_since=idle_since[harness]) for s in ss]
        # a conversation you step away from: the main session, on the one-hour cache
        idle_applies = harness == "claude-code" and role == "main" and ttl >= IDLE_TTL_S
        obs = [v for tr in traces for v in tr.idle_obs.values()]
        rate, rate_scope, rate_fired, rate_eligible = idle_rate(obs, pooled_idle.get(harness, []))
        c = Ctx(price, base, summ, ttl, pen, tm, step_tokens, rot, opts, prices, idle_on=idle_applies and idle_enabled,
                idle_min=idle_min, idle_rate=rate)  # fmt: skip
        base_of.update({id(s): base for s in ss})
        shown.update(id(s) for s in sorted(sorted(ss, key=lambda s: s.id), key=lambda s: -s.peak)[:SESSIONS_SHOWN])
        grid = grid_for(opts, cap)
        n_turns = sum(len(t.g) for t in traces)
        pts = [_point(run_all(traces, T, c), T, n_turns) for T in grid]
        rows: dict[str, list[Any]] = {k: [p[k] for p in pts] for k in ROW_KEYS}
        tss = [t.ts for s in ss for t in s.turns if t.ts]
        days = max((max(tss) - min(tss)) / 86400, 1.0) if tss else 1.0
        # local dates with at least one turn, never more than the span (a 10.2-day span can touch 11 dates)
        active_days = min(len({time.strftime("%Y-%m-%d", time.localtime(t)) for t in tss}) or 1, days)

        d_thr, d_note = default_threshold(harness, model, window)
        default = _point(run_all(traces, d_thr, c), d_thr, n_turns)
        default["note"] = d_note
        default["note_zh"] = default_note_zh(harness, model, window)
        cur_val, cur_desc = (current or {}).get((harness, model), (None, ""))
        cur = None
        if cur_val is not None:
            eff = min(cur_val, cap)
            cur = _point(run_all(traces, eff, c), eff, n_turns)
            cur["configured"] = cur_val
            cur["source"] = cur_desc

        pre = [x.pre_tokens for s in ss for x in s.compactions if x.pre_tokens and is_threshold(x)]
        t_hist = _median(pre, 0) or None
        drift = default_drift(ss, d_thr) if cur_val is None else None
        hist_run = run_all(traces, float(t_hist) if t_hist else math.inf, c)
        g: dict[str, Any] = {
            "harness": harness,
            "model": model,
            "role": role,
            "price_key": price.key,
            "price_source": price.source,
            "price_kind": price.kind,
            "price": price_snapshot(list_price),
            "platform": platform,
            "cache_ttl": ttl,
            "cache_ttl_source": ttl_source,
            "n_sessions": len(ss),
            "n_turns": n_turns,
            "days": days,
            "active_days": active_days,
            "span": [min(tss), max(tss)] if tss else None,
            "max_window": window,
            "window_source": window_source,
            "window_note": window_note,
            "default_drift": drift,
            "effective_max": cap,
            "base_tokens": base,
            "summary_tokens": summ,
            "penalty_params": pen.to_dict(),
            "time_model": tm.summary(),
            "observed_compactions": sum(is_threshold(c) for s in ss for c in s.compactions),
            "observed_idle_compactions": sum(c.idle for s in ss for c in s.compactions),
            "observed_manual_compactions": sum(not is_auto(c) for s in ss for c in s.compactions),
            "peak_p50": _median([s.peak for s in ss], 0),
            "peak_max": max(s.peak for s in ss),
            "grid": grid,
            **rows,
            "baseline_cost": run_all(traces, math.inf, c).cost,
            "actual_cost": sum(_session_actual_cost(s, pricing) for s in ss),
            "default": default,
            "current": cur,
            "current_note": cur_desc,
            "no_action_needed": no_action(default),
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
        g["idle"] = {
            "applies": idle_applies,
            "enabled": idle_enabled,
            "min_tokens": idle_min,
            "after_s": IDLE_AFTER_S,
            "configured": opts.idle_compaction is not None,
            "rate": rate,
            "rate_scope": rate_scope,  # "group" | "harness" | "assumed"
            "rate_fired": rate_fired,
            "rate_eligible": rate_eligible,
        }
        if idle_applies:  # the same sweep with the setting flipped, kept so a saved result can be re-selected
            ca = dataclasses.replace(c, idle_on=not c.idle_on)
            apts = [_point(run_all(traces, T, ca), T, n_turns) for T in grid]
            acur = None
            if cur is not None:
                acur = {**cur, **_point(run_all(traces, cur["threshold"], ca), cur["threshold"], n_turns)}
            g["idle"]["alt"] = {
                "enabled": not idle_enabled,
                "rows": {k: [p[k] for p in apts] for k in ROW_KEYS},
                "default_point": {**_point(run_all(traces, d_thr, ca), d_thr, n_turns), "note": d_note},
                "current_point": acur,
            }
            idle_select(g)
        out_groups.append(g)
    keys = global_keys(out_groups, opt_d)  # before the notes: it marks superseded models
    return {
        "schema": 4,
        "mode": "lite",
        "data_through": max((t.ts for s in sessions for t in s.turns if t.ts), default=None),
        "options": opt_d,
        "pricing_note": pricing.data.get("note", ""),
        "groups": out_groups,
        "idle_compaction": idle_advice(out_groups, opt_d),
        "global_keys": keys,
        "model_notes": {**model_notes(out_groups), "fallback": dict(pricing.data["fallback"])},
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
                "manual": make_trace(s, base_of[id(s)]).manual if id(s) in shown else {},
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
        _actual_cost(Session(s.id, s.harness, s.role, s.path, turns=ts), pricing.lookup(m, s.platform))
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
