"""Anonymous, reviewable sharing record: what compactlab estimates it saves, with nothing that identifies a person.

`build` turns a result into a small record of aggregates per (harness, model, role) group: the settings compared,
the estimated change versus the harness default (and versus the current setting) per day, and the quality markers
a researcher needs to weigh it (history length, where the time model came from, replay vs logged cost). It never
copies session ids, paths, timestamps, project names, curves or transcript text, and it writes numbers and fixed
vocabulary only. `validate` accepts exactly that shape and nothing else, so the same check runs on the user's
machine before they post and in CI on whatever arrives. `aggregate` summarises many records.

The savings are replay estimates at list prices (see docs/METHODOLOGY.md), not measured savings.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from collections.abc import Callable, Iterable
from typing import Any

from . import __version__
from .adapters import adapter_names
from .pricing import PricingTable
from .sim.select import PROFILE_NAMES, compare

SCHEMA = "compactlab.share/1"
ISSUE_URL = "https://github.com/LemonCANDY42/compactlab/issues/new?template=share_estimates.yml"
ROLES = ("main", "subagent")
CHANGING = ("set", "change")
ACTIONS = (*CHANGING, "keep default", "no action needed")
MODES = ("lite", "deep")
SLUG = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,47}$")
VERSION = re.compile(r"^\d+\.\d+\.\d+[A-Za-z0-9.+-]{0,16}$")
MAX_GROUPS = 32
MAX_RECORD_BYTES = 64_000


class ShareError(Exception):
    pass


def _sig(x: float | None, digits: int = 3) -> float | None:
    """Round to a few significant figures: enough for a statistic, too coarse to fingerprint a history."""
    if x is None:
        return None
    if x == 0 or not math.isfinite(x):
        return 0.0 if x == 0 else None
    return round(x, digits - 1 - math.floor(math.log10(abs(x))))


def _metrics(c: dict[str, Any]) -> dict[str, Any]:
    """One comparison (profile or current setting vs a reference) as per-day figures. Negative = saved."""
    im = c["impact"]
    return {
        "usd_per_day": _sig(im["usd"]["per_day"]),
        "cost_pct": _sig(c["d_cost_pct"] * 100),
        "minutes_per_day": _sig(im["minutes"]["per_day"]),
        "wait_minutes_per_day": _sig(im["wait_minutes"]["per_day"]),
        "compactions_per_day": _sig(im["compactions"]["per_day"]),
        "ref_compactions_per_day": _sig(im["ref_compactions"]["per_day"]),
        "recall_delta": _sig(c["d_recall"]),
    }


def _slug(s: str, default: str = "other") -> str:
    s = re.sub(r"[^a-z0-9_.-]+", "-", (s or "").lower()).strip("-")
    return s[:48] if SLUG.match(s[:48] or "!") else default


def _ttl_source(s: str) -> str:
    return "override" if s.startswith("--") else "logs" if s.startswith("logs") else "default"


def _price_source(s: str) -> str:
    return next((k for k in ("bundled", "refreshed", "user") if s.startswith(k)), "other")


def _group(g: dict[str, Any], public_models: set[str]) -> dict[str, Any]:
    days, active = g.get("days"), g.get("active_days")
    cur, default = g.get("current"), g["default"]
    tm, pen = g.get("time_model") or {}, g.get("penalty_params") or {}
    hist = g.get("history") or {}
    profiles = {}
    for n in PROFILE_NAMES:
        cm = g["comparison"][n]
        profiles[n] = {
            "threshold": int(cm["threshold"]),
            "action": cm["action"],
            "flags": [_slug(f) for f in g["profiles"][n].get("flags", [])][:8],
            "vs_default": _metrics(cm["vs_default"]),
            "vs_current": _metrics(cm["vs_current"]) if cm.get("vs_current") else None,
        }
    key = g.get("price_key")
    return {
        "harness": g["harness"],
        "model": key if key in public_models else "other",  # a custom name could say who you are
        "role": g["role"],
        "sessions": int(_sig(g["n_sessions"], 2) or 0),
        "turns": int(_sig(g["n_turns"], 2) or 0),
        "days": _sig(days, 2),
        "active_days": _sig(active, 2),
        "observed_compactions": int(_sig(g["observed_compactions"], 2) or 0),
        "window": int(g["max_window"]),
        "default_threshold": int(default["threshold"]),
        "current_threshold": int(cur["configured"]) if cur else None,
        "cache_ttl_s": int(g["cache_ttl"]),
        "cache_ttl_source": _ttl_source(g.get("cache_ttl_source", "")),
        "price_source": _price_source(g.get("price_source", "")),
        "turn_source": _slug(tm.get("turn_source", "")),
        "compact_source": _slug(tm.get("compact_source", "")),
        "penalty_source": _slug(pen.get("source", "")),
        "no_action_needed": bool(g["no_action_needed"]),
        # replay of the historical behaviour over what the logs priced at list price: how far to trust the replay
        "replay_over_logged": _sig(hist["cost"] / g["actual_cost"]) if hist and g.get("actual_cost") else None,
        "current_vs_default": _metrics(compare(cur, default, days or 1.0, active)) if cur else None,
        "profiles": profiles,
    }


def build(result: dict[str, Any], *, allow_demo: bool = False) -> dict[str, Any]:
    """The shareable record for a saved result. Refuses synthetic demo data (it would pollute the pool)."""
    if result.get("demo") and not allow_demo:
        raise ShareError("this result is synthetic demo data; run compactlab simulate on your own history")
    public = set(PricingTable.bundled()["models"])
    groups = [_group(g, public) for g in result["groups"]]
    if not groups:
        raise ShareError("the result has no groups to share")
    rec = {
        "schema": SCHEMA,
        "compactlab": __version__,
        "estimated": True,
        "mode": result.get("mode", "lite"),
        "groups": groups,
    }
    bad = validate(rec)
    if bad:  # a bug here must never reach a public issue
        raise ShareError("internal error, record not valid: " + "; ".join(bad[:3]))
    return rec


# ---------- validation: an allow-list, so anything unexpected is refused, not scrubbed ----------

_Check = Callable[[Any], bool]


def _num(lo: float, hi: float, nullable: bool = False) -> _Check:
    return lambda x: (
        (x is None and nullable)
        or (isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and lo <= x <= hi)
    )


def _int(lo: int, hi: int, nullable: bool = False) -> _Check:
    return lambda x: (x is None and nullable) or (isinstance(x, int) and not isinstance(x, bool) and lo <= x <= hi)


def _enum(*vals: str) -> _Check:
    return lambda x: isinstance(x, str) and x in vals


def _slug_ok(x: Any) -> bool:
    return isinstance(x, str) and bool(SLUG.match(x))


def _bool(x: Any) -> bool:
    return isinstance(x, bool)


def _any(_: Any) -> bool:
    return True


_METRIC_FIELDS: dict[str, _Check] = {
    "usd_per_day": _num(-1e7, 1e7),
    "cost_pct": _num(-1000, 1000),
    "minutes_per_day": _num(-1e5, 1e5),
    "wait_minutes_per_day": _num(-1e5, 1e5),
    "compactions_per_day": _num(0, 1e5),
    "ref_compactions_per_day": _num(0, 1e5),
    "recall_delta": _num(-1, 1),
}
_PROFILE_FIELDS: dict[str, _Check] = {
    "threshold": _int(1_000, 100_000_000),
    "action": _enum(*ACTIONS),
    "flags": lambda x: isinstance(x, list) and len(x) <= 8 and all(_slug_ok(f) for f in x),
}


def _check_fields(path: str, obj: Any, spec: dict[str, _Check], out: list[str], optional: Iterable[str] = ()) -> None:
    if not isinstance(obj, dict):
        out.append(f"{path}: expected an object")
        return
    extra = sorted(set(obj) - set(spec))
    if extra:
        out.append(f"{path}: unexpected field(s) {', '.join(_clean(k) for k in extra[:5])}")
    for k, ok in spec.items():
        if k not in obj:
            if k not in optional:
                out.append(f"{path}.{k}: missing")
        elif not ok(obj[k]):
            out.append(f"{path}.{k}: value not allowed")


def _clean(s: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.\[\]-]", "?", str(s))[:40]


def _issues(path: str, obj: Any, spec: dict[str, _Check]) -> list[str]:
    out: list[str] = []
    _check_fields(path, obj, spec, out)
    return out


_GROUP_FIELDS: dict[str, _Check] = {
    "harness": _enum(*adapter_names()),
    "model": lambda x: _slug_ok(x) and (x == "other" or x in PricingTable.bundled()["models"]),
    "role": _enum(*ROLES),
    "sessions": _int(1, 10_000_000),
    "turns": _int(1, 1_000_000_000),
    "days": _num(0, 3650),
    "active_days": _num(0, 3650, nullable=True),
    "observed_compactions": _int(0, 100_000_000),
    "window": _int(10_000, 100_000_000),
    "default_threshold": _int(1_000, 100_000_000),
    "current_threshold": _int(1_000, 100_000_000, nullable=True),
    "cache_ttl_s": _int(0, 86_400),
    "cache_ttl_source": _enum("default", "logs", "override"),
    "price_source": _enum("bundled", "refreshed", "user", "other"),
    "turn_source": _slug_ok,
    "compact_source": _slug_ok,
    "penalty_source": _slug_ok,
    "no_action_needed": _bool,
    "replay_over_logged": _num(0, 1000, nullable=True),
    "current_vs_default": lambda x: x is None or isinstance(x, dict),  # fields checked in validate()
    "profiles": _any,  # checked in validate()
}


def validate(rec: Any) -> list[str]:
    """Problems with a record, empty if it is acceptable. Never echoes more than a short, cleaned key name."""
    out: list[str] = []
    if not isinstance(rec, dict):
        return ["record: expected a JSON object"]
    top: dict[str, _Check] = {
        "schema": _enum(SCHEMA),
        "compactlab": lambda x: isinstance(x, str) and bool(VERSION.match(x)),
        "estimated": lambda x: x is True,
        "mode": _enum(*MODES),
        "groups": lambda x: isinstance(x, list) and 1 <= len(x) <= MAX_GROUPS,
    }
    _check_fields("record", rec, top, out)
    groups = rec.get("groups")
    if not isinstance(groups, list):
        return out
    for i, g in enumerate(groups[:MAX_GROUPS]):
        p = f"groups[{i}]"
        _check_fields(p, g, _GROUP_FIELDS, out)
        if not isinstance(g, dict):
            continue
        if isinstance(g.get("current_vs_default"), dict):
            out += _issues(f"{p}.current_vs_default", g["current_vs_default"], _METRIC_FIELDS)
        profs = g.get("profiles")
        if not isinstance(profs, dict) or set(profs) != set(PROFILE_NAMES):
            out.append(f"{p}.profiles: expected exactly {', '.join(PROFILE_NAMES)}")
            continue
        for n, pr in profs.items():
            pp = f"{p}.profiles.{n}"
            spec = {**_PROFILE_FIELDS, "vs_default": _any, "vs_current": _any}
            _check_fields(pp, pr, spec, out)
            if isinstance(pr, dict):
                out += _issues(f"{pp}.vs_default", pr.get("vs_default"), _METRIC_FIELDS)
                if pr.get("vs_current") is not None:
                    out += _issues(f"{pp}.vs_current", pr["vs_current"], _METRIC_FIELDS)
    if len(json.dumps(rec)) > MAX_RECORD_BYTES:
        out.append("record: too large")
    return out


# ---------- reading records back ----------

_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.S)


def parse_issue_body(body: str) -> Any:
    """The JSON record in a GitHub issue-form body (the textarea arrives in a fenced block), or raise ShareError."""
    for m in _FENCE.finditer(body or ""):
        try:
            return json.loads(m.group(1))
        except ValueError:
            continue
    try:
        return json.loads(body)
    except (TypeError, ValueError) as e:
        raise ShareError("no JSON record found; paste the whole content of the file compactlab share wrote") from e


# ---------- aggregation ----------


def _median(xs: list[float]) -> float | None:
    return _sig(statistics.median(xs)) if xs else None


def aggregate(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Medians per (harness, model, role) of the estimated change vs the default, over valid records only."""
    cells: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    n = 0
    for rec in records:
        if validate(rec):
            continue
        n += 1
        for g in rec["groups"]:
            cells.setdefault((g["harness"], g["model"], g["role"]), []).append(g)
    rows = []
    for (h, m, r), gs in sorted(cells.items()):
        act = [g for g in gs if not g["no_action_needed"]]
        row: dict[str, Any] = {
            "harness": h,
            "model": m,
            "role": r,
            "groups": len(gs),
            "no_action_needed": len(gs) - len(act),
            "median_days": _median([g["days"] for g in gs]),
            "median_replay_over_logged": _median([g["replay_over_logged"] for g in gs if g["replay_over_logged"]]),
            "profiles": {},
        }
        for p in PROFILE_NAMES:
            # "keep default" means the user changes nothing: that group saves nothing, whatever the profile's pick said
            vs = [
                g["profiles"][p]["vs_default"]
                if g["profiles"][p]["action"] in CHANGING
                else dict.fromkeys(_METRIC_FIELDS, 0.0)
                for g in act
            ]
            row["profiles"][p] = {
                "median_cost_pct": _median([v["cost_pct"] for v in vs]),
                "median_usd_per_day": _median([v["usd_per_day"] for v in vs]),
                "median_minutes_per_day": _median([v["minutes_per_day"] for v in vs]),
                "median_threshold": _median(
                    [g["profiles"][p]["threshold"] for g in act if g["profiles"][p]["action"] in CHANGING]
                ),
                "share_changing": _sig(sum(g["profiles"][p]["action"] in CHANGING for g in act) / len(act))
                if act
                else None,
            }
        cur = [g["current_vs_default"] for g in act if g["current_vs_default"]]
        row["current_vs_default"] = {
            "groups": len(cur),
            "median_cost_pct": _median([c["cost_pct"] for c in cur]),
            "median_usd_per_day": _median([c["usd_per_day"] for c in cur]),
            "median_minutes_per_day": _median([c["minutes_per_day"] for c in cur]),
        }
        rows.append(row)
    return {"records": n, "cells": rows}


def aggregate_text(agg: dict[str, Any], profile: str = "balanced") -> str:
    lines = [
        f"{agg['records']} valid record(s); medians of the estimated change vs the harness default "
        f"(replay estimate, list prices; negative = saved)",
        f"{'harness':12} {'model':20} {'role':9} {'n':>3} {'thr':>6} {'cost%':>7} {'$/day':>7} {'min/day':>8}"
        f" {'cur cost%':>10} {'replay/logged':>14}",
    ]

    def f(x: float | None, spec: str) -> str:
        return "-" if x is None else format(x, spec)

    for r in agg["cells"]:
        p, c = r["profiles"][profile], r["current_vs_default"]
        thr = f(p["median_threshold"] and p["median_threshold"] / 1000, ".0f")
        thr = thr + "k" if thr != "-" else thr
        lines.append(
            f"{r['harness']:12} {r['model']:20} {r['role']:9} {r['groups']:>3} {thr:>6}"
            f" {f(p['median_cost_pct'], '+.1f'):>7} {f(p['median_usd_per_day'], '+.2f'):>7}"
            f" {f(p['median_minutes_per_day'], '+.1f'):>8} {f(c['median_cost_pct'], '+.1f'):>10}"
            f" {f(r['median_replay_over_logged'], '.2f'):>14}"
        )
    return "\n".join(lines)


def preview(rec: dict[str, Any]) -> str:
    """A few lines a person can read before posting: what is in the record and what each group estimates."""
    out = [f"{len(rec['groups'])} group(s), compactlab {rec['compactlab']}, {rec['mode']} mode"]
    for g in rec["groups"]:
        b = g["profiles"]["balanced"]
        v = b["vs_default"]
        if g["no_action_needed"]:
            what = "no action needed (the default never compacts here)"
        elif b["action"] not in CHANGING:
            what = f"keep default (balanced {b['threshold'] // 1000}k would not beat it)"
        else:
            what = (
                f"balanced {b['threshold'] // 1000}k: {v['cost_pct']:+.1f}% cost, "
                f"{v['minutes_per_day']:+.1f} min/day wall-clock vs default"
            )
        out.append(f"  {g['harness']} · {g['model']} · {g['role']} · {g['sessions']} sessions: {what}")
    return "\n".join(out)
