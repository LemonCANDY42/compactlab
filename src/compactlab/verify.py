"""Check a threshold change against what happened: predicted vs observed, from the same logs.

After `apply` the harness keeps writing session logs. This splits them at the apply time (from the manifest) and
compares, per model and role:

  observed          logged cost and compactions per 1k turns, before vs after (list price, each cache write at the
                    tier it was billed at), with a bootstrap interval over sessions
  predicted         a replay of the BEFORE sessions at the old and at the new threshold (no data from after)
  replay on after   the same replay on the AFTER sessions; if it disagrees with `predicted`, the workload changed

Workload is divided out (per 1k turns), not matched: a different mix of tasks before and after still moves the
observed figure, so the interval and the replay-on-after column are the guard. Wall-clock is not verified here
(timestamps include tool and network time); the report says so. Sessions that straddle the apply time are dropped.
"""

from __future__ import annotations

import random
import statistics
import time
from dataclasses import dataclass
from typing import Any

from .model import Session
from .pricing import PricingTable
from .sim.engine import SimOptions, _actual_cost, analyze

MIN_SESSIONS = 3
MIN_ACTIVE_DAYS = 2
BOOTSTRAP = 400
NEAR = (0.8, 1.05)  # median size at compaction vs the new threshold: "setting is in effect" band


@dataclass
class Change:
    harness: str
    model: str  # "" = any model of the harness (Codex has one global key)
    old: int | None  # None = was unset, i.e. the harness default
    new: int


def changes_from_manifest(manifest: dict[str, Any]) -> list[Change]:
    out = []
    for ent in manifest.get("entries", []):
        for ch in ent.get("changes", []):
            old = ch.get("old")
            out.append(
                Change(
                    ch["harness"],
                    ch.get("model") or "",
                    old if isinstance(old, int) and not isinstance(old, bool) else None,
                    int(ch["new"]),
                )
            )
    return out


def split(sessions: list[Session], t: float) -> tuple[list[Session], list[Session], int]:
    """(before, after, dropped): by first/last timestamp; sessions without times or spanning `t` are dropped."""
    before, after, dropped = [], [], 0
    for s in sessions:
        ts = [x.ts for x in s.turns if x.ts]
        if not ts:
            dropped += 1
        elif max(ts) < t:
            before.append(s)
        elif min(ts) >= t:
            after.append(s)
        else:
            dropped += 1
    return before, after, dropped


def _active_days(ss: list[Session]) -> int:
    return len({time.strftime("%Y-%m-%d", time.localtime(x.ts)) for s in ss for x in s.turns if x.ts})


def observe(ss: list[Session], pricing: PricingTable) -> dict[str, Any]:
    """Logged figures for one period of one group."""
    if not ss:
        return {"sessions": 0, "turns": 0, "active_days": 0, "cost": 0.0, "compactions": 0, "per_session": [],
                "median_compaction_tokens": None}  # fmt: skip
    price = pricing.lookup(ss[0].model)
    per = [(_actual_cost(s, price), len(s.turns), len(s.compactions)) for s in ss]
    pre = [c.pre_tokens for s in ss for c in s.compactions if c.pre_tokens]
    return {
        "sessions": len(ss),
        "turns": sum(p[1] for p in per),
        "active_days": _active_days(ss),
        "cost": sum(p[0] for p in per),
        "compactions": sum(p[2] for p in per),
        "per_session": per,
        "median_compaction_tokens": int(statistics.median(pre)) if pre else None,
    }


def _cost_per_kturn(rows: list[tuple[float, int, int]]) -> float | None:
    turns = sum(r[1] for r in rows)
    return sum(r[0] for r in rows) / turns * 1000 if turns else None


def _comp_per_kturn(rows: list[tuple[float, int, int]]) -> float | None:
    turns = sum(r[1] for r in rows)
    return sum(r[2] for r in rows) / turns * 1000 if turns else None


def _change(b: float | None, a: float | None, pct: bool) -> float | None:
    if b is None or a is None or (pct and not b):
        return None
    return (a / b - 1) * 100 if pct else a - b


def _interval(before: list, after: list, fn: Any, pct: bool) -> list[float] | None:
    """80% bootstrap interval of the change in `fn` (cost or compactions per 1k turns), resampling sessions."""
    if len(before) < 2 or len(after) < 2:
        return None
    rng = random.Random(7)
    vals = []
    for _ in range(BOOTSTRAP):
        v = _change(fn([rng.choice(before) for _ in before]), fn([rng.choice(after) for _ in after]), pct)
        if v is not None:
            vals.append(v)
    if len(vals) < BOOTSTRAP // 2:
        return None
    vals.sort()
    return [vals[int(0.1 * len(vals))], vals[int(0.9 * len(vals)) - 1]]


def replay_pair(
    ss: list[Session], pricing: PricingTable, harness: str, model: str, old: int | None, new: int
) -> dict[str, dict[str, Any]]:
    """Replay a period's sessions at the old (None = harness default) and the new threshold, per role."""
    opts = SimOptions(grid_start=100_000, grid_stop=100_000)  # the grid is unused; only default / current points are
    at_new = analyze(ss, pricing, opts, current={(harness, model): (new, "")})
    at_old = analyze(ss, pricing, opts, current={(harness, model): (old, "")}) if old is not None else None
    out = {}
    for g in at_new["groups"]:
        o = g["default"]
        if at_old is not None:
            o = next(x["current"] for x in at_old["groups"] if x["role"] == g["role"] and x["model"] == g["model"])
        out[g["role"]] = {
            "old": o,
            "new": g["current"],
            "turns": g["n_turns"],
            "old_threshold": g["default"]["threshold"],
        }
    return out


def _predicted(r: dict[str, Any]) -> dict[str, float | None]:
    o, n, turns = r["old"], r["new"], max(r["turns"], 1)
    return {
        "cost_pct": (n["cost"] / o["cost"] - 1) * 100 if o["cost"] else None,
        "compactions_per_kturn": (n["compactions"] - o["compactions"]) / turns * 1000,
    }


def _verdict(g: dict[str, Any], min_sessions: int, min_days: int) -> tuple[str, str]:
    b, a = g["before"], g["after"]
    if min(b["sessions"], a["sessions"]) < min_sessions or min(b["active_days"], a["active_days"]) < min_days:
        return "too_early", (
            f"too early: need {min_sessions}+ sessions and {min_days}+ active days on each side "
            f"(have {b['sessions']}/{a['sessions']} sessions, {b['active_days']}/{a['active_days']} days)"
        )
    obs, pred, ci = g["observed"]["cost_pct"], g["predicted"]["cost_pct"], g["observed"]["cost_pct_interval"]
    if obs is None or pred is None or ci is None:
        return "unclear", "not enough turns on one side to compare cost"
    after_rep = (g["replay_after"] or {}).get("cost_pct")
    shifted = after_rep is not None and abs(after_rep - pred) > 10
    note = " (the replay on the after sessions differs from the prediction: the workload changed)" if shifted else ""
    if ci[0] <= pred <= ci[1]:
        return "consistent", "consistent with the prediction, within the noise" + note
    if obs > pred:
        return "less", "saved less than predicted (or cost more)" + note
    return "more", "saved more than predicted" + note


def verify(
    sessions: list[Session],
    manifest: dict[str, Any],
    pricing: PricingTable,
    *,
    min_sessions: int = MIN_SESSIONS,
    min_days: int = MIN_ACTIVE_DAYS,
) -> dict[str, Any]:
    t = float(manifest["created"])
    groups = []
    dropped_total = 0
    for ch in changes_from_manifest(manifest):
        mine = [s for s in sessions if s.harness == ch.harness and (not ch.model or s.model == ch.model)]
        for model in sorted({s.model for s in mine}):
            ms = [s for s in mine if s.model == model]
            before, after, dropped = split(ms, t)
            dropped_total += dropped
            if not before or not after:
                groups.append(_empty(ch, model, t, bool(before)))
                continue
            pb = replay_pair(before, pricing, ch.harness, model, ch.old, ch.new)
            pa = replay_pair(after, pricing, ch.harness, model, ch.old, ch.new)
            for role in sorted(pb):
                rb = [s for s in before if s.role == role]
                ra = [s for s in after if s.role == role]
                ob, oa = observe(rb, pricing), observe(ra, pricing)
                g: dict[str, Any] = {
                    "harness": ch.harness,
                    "model": model,
                    "role": role,
                    "applied_at": t,
                    "old": ch.old,
                    "old_effective": pb[role]["old_threshold"],
                    "new": ch.new,
                    "before": {k: v for k, v in ob.items() if k != "per_session"},
                    "after": {k: v for k, v in oa.items() if k != "per_session"},
                    "predicted": _predicted(pb[role]),
                    "replay_after": _predicted(pa[role]) if role in pa else None,
                    "observed": {
                        "cost_per_kturn_before": _cost_per_kturn(ob["per_session"]),
                        "cost_per_kturn_after": _cost_per_kturn(oa["per_session"]),
                        "cost_pct": _change(
                            _cost_per_kturn(ob["per_session"]), _cost_per_kturn(oa["per_session"]), True
                        ),
                        "cost_pct_interval": _interval(ob["per_session"], oa["per_session"], _cost_per_kturn, True),
                        "compactions_per_kturn_before": _comp_per_kturn(ob["per_session"]),
                        "compactions_per_kturn_after": _comp_per_kturn(oa["per_session"]),
                        "compactions_per_kturn": _change(
                            _comp_per_kturn(ob["per_session"]), _comp_per_kturn(oa["per_session"]), False
                        ),
                        "compactions_interval": _interval(ob["per_session"], oa["per_session"], _comp_per_kturn, False),
                    },
                }
                g["in_effect"] = _in_effect(g)
                g["verdict"], g["summary"] = _verdict(g, min_sessions, min_days)
                groups.append(g)
    return {"applied_at": t, "groups": groups, "dropped_sessions": dropped_total}


def _empty(ch: Change, model: str, t: float, has_before: bool) -> dict[str, Any]:
    side = "after" if has_before else "before"
    return {"harness": ch.harness, "model": model, "role": "*", "applied_at": t, "old": ch.old, "new": ch.new,
            "verdict": "no_data", "summary": f"no sessions {side} the change for this model"}  # fmt: skip


def _in_effect(g: dict[str, Any]) -> str:
    """Did compactions after the change happen near the new threshold? 'yes' / 'no' / 'none seen'."""
    med, new = g["after"]["median_compaction_tokens"], g["new"]
    if med is None:
        return "none seen" if (g["predicted"]["compactions_per_kturn"] or 0) > 0 else "n/a"
    return "yes" if NEAR[0] * new <= med <= NEAR[1] * new else "no"


# ---------- text ----------


def _fmt(x: float | None, spec: str) -> str:
    return "-" if x is None else format(x, spec)


def _range(iv: list[float] | None, spec: str) -> str:
    return "" if not iv else f" (80%: {iv[0]:{spec}}..{iv[1]:{spec}})"


def _k(n: int | None) -> str:
    return "default" if n is None else f"{n // 1000}k"


def render(res: dict[str, Any]) -> str:
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(res["applied_at"]))
    lines = [f"Verification of the change applied {when}"]
    for g in res["groups"]:
        lines.append("")
        if g["verdict"] == "no_data":
            lines += [f"{g['model']} · {g['harness']}: {g['summary']}"]
            continue
        old = g["old_effective"] if g["old"] is None else g["old"]
        lines.append(f"{g['model']} · {g['role']}   {_k(g['old'])} ({old // 1000}k) → {_k(g['new'])}")
        b, a, o, p, r = g["before"], g["after"], g["observed"], g["predicted"], g["replay_after"] or {}
        lines += [
            f"  {'':26}{'before':>9}{'after':>9}   {'observed change':<30}{'predicted':>10}{'replay on after':>17}",
            f"  {'sessions / active days':26}{str(b['sessions']) + ' / ' + str(b['active_days']):>9}"
            f"{str(a['sessions']) + ' / ' + str(a['active_days']):>9}",
            f"  {'cost per 1k turns':26}{'$' + _fmt(o['cost_per_kturn_before'], '.2f'):>9}"
            f"{'$' + _fmt(o['cost_per_kturn_after'], '.2f'):>9}   "
            f"{(_fmt(o['cost_pct'], '+.1f') + '%' + _range(o['cost_pct_interval'], '+.0f')):<30}"
            f"{_fmt(p['cost_pct'], '+.1f') + '%':>10}{_fmt(r.get('cost_pct'), '+.1f') + '%':>17}",
            f"  {'compactions per 1k turns':26}{_fmt(o['compactions_per_kturn_before'], '.2f'):>9}"
            f"{_fmt(o['compactions_per_kturn_after'], '.2f'):>9}   "
            f"{(_fmt(o['compactions_per_kturn'], '+.2f') + _range(o['compactions_interval'], '+.1f')):<30}"
            f"{_fmt(p['compactions_per_kturn'], '+.2f'):>10}{_fmt(r.get('compactions_per_kturn'), '+.2f'):>17}",
        ]
        med = a["median_compaction_tokens"]
        eff = {"yes": f"✓ near {_k(g['new'])}", "no": f"✗ far from {_k(g['new'])}: the setting may be overridden "
               "(project settings, env vars, CLI flags)", "none seen": "no compaction after the change",
               "n/a": "-"}[g["in_effect"]]  # fmt: skip
        before_med = f"{b['median_compaction_tokens'] // 1000}k" if b["median_compaction_tokens"] else "-"
        after_med = f"{med // 1000}k" if med else "-"
        lines.append(f"  {'median size at compaction':26}{before_med:>9}{after_med:>9}   {eff}")
        lines.append(f"  → {g['summary']}")
    lines += [
        "",
        "Cost is the logged list-price figure per 1k turns, so a busier week does not look like a cost rise.",
        "It is not your invoice and does not control for a change in what you worked on. Wall-clock is not verified.",
    ]
    if res["dropped_sessions"]:
        lines.append(f"{res['dropped_sessions']} session(s) spanning the change, or without timestamps, were left out.")
    return "\n".join(lines)
