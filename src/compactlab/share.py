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

SCHEMA_V1 = "compactlab.share/1"
SCHEMA = "compactlab.share/2"  # /2 adds the optional per-group `verification` block
SCHEMAS = (SCHEMA_V1, SCHEMA)
ISSUE_URL = "https://github.com/LemonCANDY42/compactlab/issues/new?template=share_estimates.yml"
ROLES = ("main", "subagent")
CHANGING = ("set", "change")
ACTIONS = (*CHANGING, "keep default", "no action needed")
MODES = ("lite", "deep")
VERDICTS = ("too_early", "unclear", "consistent", "less", "more")
IN_EFFECT = ("yes", "no", "none seen", "n/a")
SLUG = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,47}$")
VERSION = re.compile(r"^\d+\.\d+\.\d+((a|b|rc)\d+)?(\.dev\d+)?$")
MAX_GROUPS = 32
MAX_RECORD_BYTES = 64_000
MAX_BODY_CHARS = 200_000
# Free text never reaches a record: every string is one of these words (or "other").
FLAGS = ("min_window_unreachable", "wait_cap_unreachable", "guardrail_applied", "recall_floor_not_met",
         "below_quarter_window", "wait_over_cap", "deep_recall_below_floor")  # fmt: skip
TIME_SOURCES = ("own", "pooled", "prior")
PENALTY_SOURCES = ("observed", "prior")


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


def _vocab(s: Any, allowed: tuple[str, ...]) -> str:
    return s if s in allowed else "other"


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
            "flags": [_vocab(f, FLAGS) for f in g["profiles"][n].get("flags", [])][:8],
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
        "turn_source": _vocab(tm.get("turn_source"), TIME_SOURCES),
        "compact_source": _vocab(tm.get("compact_source"), TIME_SOURCES),
        "penalty_source": _vocab(pen.get("source"), PENALTY_SOURCES),
        "no_action_needed": bool(g["no_action_needed"]),
        # replay of the historical behaviour over what the logs priced at list price: how far to trust the replay
        "replay_over_logged": _sig(hist["cost"] / g["actual_cost"]) if hist and g.get("actual_cost") else None,
        "current_vs_default": _metrics(compare(cur, default, days or 1.0, active)) if cur else None,
        "profiles": profiles,
    }


def _verification(v: dict[str, Any]) -> dict[str, Any]:
    """One `verify` group as flat, rounded figures: what was predicted, what the logs showed, and how much data."""
    o, p, r = v["observed"], v["predicted"], v.get("replay_after") or {}
    iv = o["cost_pct_interval"] or [None, None]
    return {
        "old_threshold": v["old"],  # None = the harness default
        "new_threshold": int(v["new"]),
        "before_sessions": int(_sig(v["before"]["sessions"], 2) or 0),
        "after_sessions": int(_sig(v["after"]["sessions"], 2) or 0),
        "before_active_days": v["before"]["active_days"],
        "after_active_days": v["after"]["active_days"],
        "verdict": v["verdict"],
        "in_effect": v["in_effect"],
        "observed_cost_pct": _sig(o["cost_pct"]),
        "observed_cost_pct_low": _sig(iv[0]),
        "observed_cost_pct_high": _sig(iv[1]),
        "observed_compactions_per_kturn": _sig(o["compactions_per_kturn"]),
        "predicted_cost_pct": _sig(p["cost_pct"]),
        "predicted_compactions_per_kturn": _sig(p["compactions_per_kturn"]),
        "replay_after_cost_pct": _sig(r.get("cost_pct")),
        "replay_after_compactions_per_kturn": _sig(r.get("compactions_per_kturn")),
    }


def build(
    result: dict[str, Any], *, allow_demo: bool = False, verification: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The shareable record for a saved result. Refuses synthetic demo data (it would pollute the pool).
    `verification` is a `verify.verify` result; each of its groups is attached to the matching result group."""
    if result.get("demo") and not allow_demo:
        raise ShareError("this result is synthetic demo data; run compactlab simulate on your own history")
    public = set(PricingTable.bundled()["models"])
    pairs = [(_group(rg, public), rg) for rg in result["groups"]]
    for g, rg in pairs:
        g["verification"] = None
        for v in (verification or {}).get("groups", []):
            if v["verdict"] != "no_data" and (v["harness"], v["model"], v["role"]) == (
                rg["harness"],
                rg["model"],
                rg["role"],
            ):
                g["verification"] = _verification(v)
    # Two raw model names (say a dated and an undated id) can share one public name; a record holds one group
    # per harness, model and role, so the one with the most turns stands for them.
    best: dict[tuple[str, str, str], dict[str, Any]] = {}
    for g, _ in pairs:
        ident = (g["harness"], g["model"], g["role"])
        if ident not in best or g["turns"] > best[ident]["turns"]:
            best[ident] = g
    groups = list(best.values())
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
    def ok(x: Any) -> bool:
        if x is None:
            return nullable
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            return False
        # compare before any float conversion: a 400-digit integer must be refused, not raise
        return (isinstance(x, int) or math.isfinite(x)) and lo <= x <= hi

    return ok


def _rounded(check: _Check, digits: int) -> _Check:
    """`check`, and the value is already rounded to `digits` significant figures (as `build` writes it)."""
    return lambda x: check(x) and (x is None or _sig(x, digits) == x)


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
    "usd_per_day": _rounded(_num(-1e7, 1e7), 3),
    "cost_pct": _rounded(_num(-1000, 1000), 3),
    "minutes_per_day": _rounded(_num(-1e5, 1e5), 3),
    "wait_minutes_per_day": _rounded(_num(-1e5, 1e5), 3),
    "compactions_per_day": _rounded(_num(0, 1e5), 3),
    "ref_compactions_per_day": _rounded(_num(0, 1e5), 3),
    "recall_delta": _rounded(_num(-1, 1), 3),
}
_PROFILE_FIELDS: dict[str, _Check] = {
    "threshold": _int(1_000, 100_000_000),
    "action": _enum(*ACTIONS),
    "flags": lambda x: isinstance(x, list) and len(x) <= 8 and all(f in (*FLAGS, "other") for f in x),
}


def _check_fields(path: str, obj: Any, spec: dict[str, _Check], out: list[str], optional: Iterable[str] = ()) -> None:
    if not isinstance(obj, dict):
        out.append(f"{path}: expected an object")
        return
    extra = sorted(set(obj) - set(spec))
    if extra:
        out.append(f"{path}: {len(extra)} unexpected field(s)")  # never repeat what was submitted
    for k, ok in spec.items():
        if k not in obj:
            if k not in optional:
                out.append(f"{path}.{k}: missing")
        elif not ok(obj[k]):
            out.append(f"{path}.{k}: value not allowed")


def _issues(path: str, obj: Any, spec: dict[str, _Check]) -> list[str]:
    out: list[str] = []
    _check_fields(path, obj, spec, out)
    return out


_PCT = _rounded(_num(-1000, 1000, nullable=True), 3)
_PER_KTURN = _rounded(_num(-1e5, 1e5, nullable=True), 3)
_VERIFY_FIELDS: dict[str, _Check] = {
    "old_threshold": _int(1_000, 100_000_000, nullable=True),
    "new_threshold": _int(1_000, 100_000_000),
    "before_sessions": _rounded(_int(0, 10_000_000), 2),
    "after_sessions": _rounded(_int(0, 10_000_000), 2),
    "before_active_days": _int(0, 3650),
    "after_active_days": _int(0, 3650),
    "verdict": _enum(*VERDICTS),
    "in_effect": _enum(*IN_EFFECT),
    "observed_cost_pct": _PCT,
    "observed_cost_pct_low": _PCT,
    "observed_cost_pct_high": _PCT,
    "observed_compactions_per_kturn": _PER_KTURN,
    "predicted_cost_pct": _PCT,
    "predicted_compactions_per_kturn": _PER_KTURN,
    "replay_after_cost_pct": _PCT,
    "replay_after_compactions_per_kturn": _PER_KTURN,
}
_GROUP_FIELDS: dict[str, _Check] = {
    "harness": _enum(*adapter_names()),
    "model": lambda x: _slug_ok(x) and (x == "other" or x in PricingTable.bundled()["models"]),
    "role": _enum(*ROLES),
    "sessions": _rounded(_int(1, 10_000_000), 2),
    "turns": _rounded(_int(1, 1_000_000_000), 2),
    "days": _rounded(_num(0, 3650), 2),
    "active_days": _rounded(_num(0, 3650, nullable=True), 2),
    "observed_compactions": _rounded(_int(0, 100_000_000), 2),
    "window": _int(10_000, 100_000_000),
    "default_threshold": _int(1_000, 100_000_000),
    "current_threshold": _int(1_000, 100_000_000, nullable=True),
    "cache_ttl_s": _int(0, 86_400),
    "cache_ttl_source": _enum("default", "logs", "override"),
    "price_source": _enum("bundled", "refreshed", "user", "other"),
    "turn_source": _enum(*TIME_SOURCES, "other"),
    "compact_source": _enum(*TIME_SOURCES, "other"),
    "penalty_source": _enum(*PENALTY_SOURCES, "other"),
    "no_action_needed": _bool,
    "replay_over_logged": _rounded(_num(0, 1000, nullable=True), 3),
    "current_vs_default": lambda x: x is None or isinstance(x, dict),  # fields checked in validate()
    "profiles": _any,  # checked in validate()
    "verification": lambda x: x is None or isinstance(x, dict),  # /2 only; fields checked in validate()
}


def validate(rec: Any) -> list[str]:
    """Problems with a record, empty if it is acceptable. Never echoes more than a short, cleaned key name."""
    out: list[str] = []
    if not isinstance(rec, dict):
        return ["record: expected a JSON object"]
    top: dict[str, _Check] = {
        "schema": _enum(*SCHEMAS),
        "compactlab": lambda x: isinstance(x, str) and bool(VERSION.match(x)),
        "estimated": lambda x: x is True,
        "mode": _enum(*MODES),
        "groups": lambda x: isinstance(x, list) and 1 <= len(x) <= MAX_GROUPS,
    }
    _check_fields("record", rec, top, out)
    groups = rec.get("groups")
    if not isinstance(groups, list):
        return out
    seen: set[tuple[Any, Any, Any]] = set()
    for i, g in enumerate(groups[:MAX_GROUPS]):
        p = f"groups[{i}]"
        if isinstance(g, dict):
            ident = (g.get("harness"), g.get("model"), g.get("role"))
            if all(isinstance(x, str) for x in ident):  # never hash an unvetted object; wrong types are reported below
                if ident in seen:
                    out.append(f"{p}: the same harness, model and role appears twice")
                seen.add(ident)
        _check_fields(p, g, _GROUP_FIELDS, out, optional=("verification",))
        if not isinstance(g, dict):
            continue
        if isinstance(g.get("verification"), dict):
            if rec.get("schema") == SCHEMA_V1:
                out.append(f"{p}.verification: not part of schema 1")
            out += _issues(f"{p}.verification", g["verification"], _VERIFY_FIELDS)
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


def _no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def loads(text: str) -> Any:
    """Strict JSON: bounded size, and a field that appears twice is an error (the first value would hide)."""
    if len(text) > MAX_BODY_CHARS:
        raise ShareError("the record is too large")
    try:
        return json.loads(text, object_pairs_hook=_no_dupes)
    except (ValueError, RecursionError) as e:
        raise ShareError("not valid JSON, or a field appears twice") from e


def parse_issue_body(body: str) -> Any:
    """The JSON record in a GitHub issue-form body (the textarea arrives in a fenced block), or raise ShareError."""
    body = body or ""
    if len(body) > MAX_BODY_CHARS:
        raise ShareError("the record is too large")
    err: ShareError | None = None
    for m in _FENCE.finditer(body):
        try:
            return loads(m.group(1))
        except ShareError as e:
            err = e
    try:
        return loads(body)
    except ShareError:
        msg = "no JSON record found; paste the whole content of the file compactlab share wrote"
        raise err or ShareError(msg) from None


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
        act = gs  # a group with nothing to change saves nothing: it stays in the medians as zero
        row: dict[str, Any] = {
            "harness": h,
            "model": m,
            "role": r,
            "groups": len(gs),
            "no_action_needed": sum(g["no_action_needed"] for g in gs),
            "median_days": _median([g["days"] for g in gs]),
            "median_replay_over_logged": _median([g["replay_over_logged"] for g in gs if g["replay_over_logged"]]),
            "profiles": {},
        }
        for p in PROFILE_NAMES:
            # "keep default" means the user changes nothing: that group saves nothing, whatever the profile's pick said
            vs = [
                g["profiles"][p]["vs_default"]
                if g["profiles"][p]["action"] in CHANGING and not g["no_action_needed"]
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
        ver = [
            v
            for g in gs
            if (v := g.get("verification"))
            and v["verdict"] in ("consistent", "less", "more")
            and v["observed_cost_pct"] is not None
            and v["predicted_cost_pct"] is not None
        ]
        row["verification"] = {
            "groups": len(ver),
            "consistent": sum(v["verdict"] == "consistent" for v in ver),
            "not_in_effect": sum(v["in_effect"] == "no" for v in ver),
            "median_observed_cost_pct": _median([v["observed_cost_pct"] for v in ver]),
            "median_predicted_cost_pct": _median([v["predicted_cost_pct"] for v in ver]),
            # observed minus predicted, in percentage points: positive = saved less than the replay said
            "median_error_pts": _median([v["observed_cost_pct"] - v["predicted_cost_pct"] for v in ver]),
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
    checked = [r for r in agg["cells"] if r["verification"]["groups"]]
    if checked:
        lines += [
            "",
            "checked against the logs after the change (cost per 1k turns; error = observed - predicted, points):",
            f"{'harness':12} {'model':20} {'role':9} {'n':>3} {'observed%':>10} {'predicted%':>11} {'error pts':>10}"
            f" {'consistent':>11} {'not applied':>12}",
        ]
        for r in checked:
            v = r["verification"]
            lines.append(
                f"{r['harness']:12} {r['model']:20} {r['role']:9} {v['groups']:>3}"
                f" {f(v['median_observed_cost_pct'], '+.1f'):>10} {f(v['median_predicted_cost_pct'], '+.1f'):>11}"
                f" {f(v['median_error_pts'], '+.1f'):>10} {v['consistent']:>11} {v['not_in_effect']:>12}"
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
        if x := g.get("verification"):
            pct = lambda v: "-" if v is None else f"{v:+.1f}%"  # noqa: E731
            out.append(
                f"    checked after the change ({x['verdict']}, setting in effect: {x['in_effect']}): "
                f"cost {pct(x['observed_cost_pct'])} observed vs {pct(x['predicted_cost_pct'])} predicted"
            )
    return "\n".join(out)
