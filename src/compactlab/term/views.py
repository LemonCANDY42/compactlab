"""Terminal views of a result: header, a panel per group, the recommendations table; doctor and serve banners.

Figures follow the report: percent of the harness default first (cost vs its window cost, time vs its wall-clock,
the engine's d_cost_pct / d_time_pct), the per-day amount second, ▼ saves (green) / ▲ adds (amber), grey = the
default. Aggregates divide summed amounts by the summed default of the models with a "set" group.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any

from .style import Line, clip, cw, pad, width
from .widgets import (
    chip,
    fmin,
    fpct,
    fusd,
    gauge,
    header,
    k,
    money,
    pack,
    panel,
    spark,
    was_now,
)

PROFILES = ("cost", "balanced", "time")
PGLYPH = {"cost": "$", "balanced": "◆", "time": "◷"}
W: dict[str, dict[str, str]] = {
    "en": {"history": "history", "harnesses": "harnesses", "models": "models", "calibrated": "calibrated",
           "service": "service", "impact": "if applied", "sessions": "sessions", "turns": "turns", "days": "d",
           "cost": "cost", "balanced": "balanced", "time": "time", "was": "was", "now": "now", "per_day": "/day",
           "vs_default": "per day vs default", "set": "change", "keep": "keep default", "cur": "keep current",
           "wall": "wall-clock",
           "costm": "cost", "noop": "no change",
           "no_change": "No change needed", "never": "never reaches the default", "recs": "Recommended settings",
           "by_thr": "cost by threshold", "foot": "Δ vs the harness default · list-price estimates · recall is a "
           "lite-mode proxy · --plain prints the full tables", "models_set": "{n} model(s) to change", "ago": "{t} ago",
           "next": "next {t}", "up": "up", "down": "not running", "none": "–",
           "kw_above": "{a} is not below the default {b}",
           "kw_score": "no net gain at ${v:g}/h (net {n}/day)",
           "kw_small": "net gain under {tol:g}% at ${v:g}/h ({n}/day)",
           "kw_cur": "{p} would not beat your {c} by more than {tol:g}% at ${v:g}/h (net {n}/day)",
           "kw_glob": "Codex has one limit for every model; across {ms} it is {a}",
           "kw_sup": "replaced by {b}: Codex's one limit follows the models in use",
           "kw_worth": "not worth it at ${v:g}/h: saves {u}/day but adds {m}/day of wall-clock (= {tv})",
           "unsafe": "░ recall < {f:g}", "saved": "░ = saved", "cache": "cache {t}", "to": "set", "more": "+{n} more"},
    "zh": {"history": "历史", "harnesses": "工具", "models": "模型", "calibrated": "校准", "service": "服务",
           "impact": "若应用", "sessions": "会话", "turns": "轮", "days": "天", "cost": "省钱", "balanced": "均衡",
           "time": "省时", "was": "原来", "now": "现在", "per_day": "/天", "vs_default": "每天,对默认",
           "set": "建议改", "keep": "保持默认", "cur": "保持当前", "wall": "总耗时", "costm": "成本",
           "noop": "无需调整",
           "no_change": "无需调整", "never": "从未达到默认阈值",
           "recs": "推荐设置", "by_thr": "各阈值成本", "foot": "差值相对 harness 默认值 · 按公开标价估算 · 召回为 "
           "lite 代理值 · --plain 输出完整表格", "models_set": "{n} 个模型建议改", "ago": "{t}前", "next": "下次 {t}",
           "up": "运行中", "down": "未运行", "none": "–", "kw_above": "{a} 不低于默认值 {b}",
           "kw_score": "按 ${v:g}/小时 没有净收益(每天净 {n})",
           "kw_small": "按 ${v:g}/小时 净收益不到 {tol:g}%(每天 {n})",
           "kw_cur": "按 ${v:g}/小时,{p} 比你现在的 {c} 好不到 {tol:g}%(每天净 {n})",
           "kw_glob": "Codex 所有模型共用一个阈值;综合 {ms} 的结论是{a}",
           "kw_sup": "已被 {b} 取代:Codex 的共用阈值按在用的模型定",
           "kw_worth": "按 ${v:g}/小时 不划算:每天省 {u},但总耗时每天多 {m}(折合 {tv})", "unsafe": "░ 召回 < {f:g}",
           "saved": "░ = 省下的部分", "cache": "缓存 {t}", "to": "设为", "more": "另 {n} 个"},
}  # fmt: skip


def label(model: str) -> str:
    """'claude-opus-5-5' -> 'Opus 5.5'; date suffixes dropped (the id stays in --plain and JSON)."""
    b = re.sub(r"-\d{8}$", "", model)
    m = re.fullmatch(r"claude-([a-z]+)-(\d+(?:-\d+)*)", b)
    if m:
        return f"{m.group(1).capitalize()} {m.group(2).replace('-', '.')}"
    m = re.fullmatch(r"gpt-(.+)", b, re.I)
    return f"GPT-{m.group(1)}" if m else b


def per_day(g: dict[str, Any], key: str) -> float:
    return float(g["default"][key]) / max(float(g["days"] or 1), 1.0)


def figs(g: dict[str, Any], prof: str, ref: str = "vs_default") -> dict[str, Any] | None:
    """Was / now / change per day for one profile against the default or the current setting."""
    c = g["comparison"][prof].get(ref)
    point = g["default"] if ref == "vs_default" else g.get("current")
    if not c or not point:
        return None
    days = max(float(g["days"] or 1), 1.0)
    im = c["impact"]
    was, d = point["combined_cost"] / days, im["usd"]["per_day"]
    wasm, dm = point["time_s"] / 60 / days, im["minutes"]["per_day"]
    return {"was": was, "now": was + d, "d": d, "pc": c["d_cost_pct"] if point["combined_cost"] else None,
            "wasm": wasm, "nowm": wasm + dm, "dm": dm, "pt": c["d_time_pct"] if point["time_s"] else None}  # fmt: skip


def status(g: dict[str, Any], prof: str) -> str:
    a = g["comparison"][prof]["action"]
    return {"no action needed": "noop", "keep default": "keep", "keep current": "cur"}.get(a, "set")


def keep_reason(g: dict[str, Any], prof: str, lang: str = "en") -> str:
    """The engine's keep-current and keep-default tests, in words (compactlab.sim.engine._comparison)."""
    w = W[lang]
    p, d = g["profiles"][prof], g["default"]
    glob = g["comparison"][prof].get("global")
    if glob and status(g, prof) != "set":
        if glob.get("superseded_by"):
            return w["kw_sup"].format(b=label(glob["superseded_by"]))
        act = {"keep default": w["keep"], "keep current": w["cur"], "no action needed": w["noop"]}.get(
            glob["action"], glob["action"])  # fmt: skip
        return w["kw_glob"].format(ms=", ".join(glob["models"]), a=act)
    if status(g, prof) == "cur":
        im = g["comparison"][prof]["vs_current"]["impact"]
        vot = float(p["value_of_time"])
        net = im["usd"]["per_day"] + im["minutes"]["per_day"] / 60 * vot
        tol = p.get("recommendation", {}).get("tolerance", 0.02) * 100
        return w["kw_cur"].format(p=k(p["threshold"]), c=k(g["current"]["configured"]), tol=tol, v=vot,
                                  n=fusd(net, True))  # fmt: skip
    if p["threshold"] >= d["threshold"]:
        return w["kw_above"].format(a=k(p["threshold"]), b=k(d["threshold"]))
    f = figs(g, prof)
    assert f is not None
    vot = float(p["value_of_time"])
    tv = f["dm"] / 60 * vot
    if f["d"] + tv < 0:  # better than the default, but within the selection tolerance
        tol = p.get("recommendation", {}).get("tolerance", 0.02) * 100
        return w["kw_small"].format(v=vot, n=fusd(f["d"] + tv, True), tol=tol)
    if f["d"] < 0 < f["dm"]:
        return w["kw_worth"].format(v=vot, u=f"${-f['d']:,.1f}", m=f"{f['dm']:.1f} min", tv=f"${tv:,.1f}")
    return w["kw_score"].format(v=vot, n=fusd(f["d"] + tv, True))


def totals(res: dict[str, Any], prof: str | None = None) -> dict[str, Any]:
    """Summed change of the "set" groups over the summed default of the models that have one (the report's base)."""
    groups = res["groups"]
    pr = lambda g: prof or g.get("profile") or "balanced"  # noqa: E731
    setm = {(g["harness"], g["model"]) for g in groups if status(g, pr(g)) == "set"}
    base = [g for g in groups if (g["harness"], g["model"]) in setm]
    was, wasm = sum(per_day(g, "combined_cost") for g in base), sum(per_day(g, "time_s") / 60 for g in base)
    d = dm = 0.0
    for g in groups:
        if status(g, pr(g)) == "set":
            f = figs(g, pr(g))
            assert f is not None
            d, dm = d + f["d"], dm + f["dm"]
    return {"n": len(setm), "was": was, "d": d, "pc": d / was if was else None, "wasm": wasm, "dm": dm,
            "pt": dm / wasm if wasm else None}  # fmt: skip


def _ago(sec: float) -> str:
    sec = max(sec, 0)
    return f"{sec / 60:.0f}m" if sec < 3600 else f"{sec / 3600:.1f}h" if sec < 86400 else f"{sec / 86400:.1f}d"


def _ttl(g: dict[str, Any], w: dict[str, str]) -> list[Line]:
    sec = g.get("cache_ttl")
    if not sec or g.get("harness") != "claude-code":  # only Anthropic bills by cache tier
        return []
    return [[(w["cache"].format(t=f"{sec // 3600} h" if sec >= 3600 else f"{sec // 60} min"), "dim")]]


def verdict(res: dict[str, Any], lang: str = "en", prof: str | None = None) -> Line:
    """One line to act on and share: the group with the largest saving among those to change, or nothing."""
    w = W[lang]
    picks = []
    for g in res["groups"]:
        p = prof or g.get("profile") or "balanced"
        f = figs(g, p) if status(g, p) == "set" else None
        if f:
            picks.append((f["d"], g, p, f))
    if not picks:
        return []
    picks.sort(key=lambda x: x[0])
    _, g, p, f = picks[0]
    line: Line = [("→ ", "acc_b"), (f"{label(g['model'])} {g['role']}", "ink_b"), (": " + w["to"] + " ", "dim"),
                  (k(g["profiles"][p]["threshold"]), "acc_b"), ("  ", None), (fpct(f["pc"]), _t(f["d"])),
                  (" " + w["costm"] + "  ", "dim"), (fmin(f["dm"], True) + w["per_day"], _t(f["dm"])),
                  (" " + w["wall"], "dim")]  # fmt: skip
    if len(picks) > 1:
        line.append(("  · " + w["more"].format(n=len(picks) - 1), "dim"))
    return line


def summary_facts(res: dict[str, Any], lang: str = "en", unit: str = "pct", service: str | None = None,
                  now: float | None = None) -> list[tuple[str, Line]]:  # fmt: skip
    w = W[lang]
    gs = res["groups"]
    spans = [g["span"] for g in gs if g.get("span")]
    day = lambda ts: datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")  # noqa: E731
    days = (max(s[1] for s in spans) - min(s[0] for s in spans)) / 86400 if spans else max(g["days"] for g in gs)
    hist: Line = [(f"{day(min(s[0] for s in spans))} → {day(max(s[1] for s in spans))}", "ink")] if spans else []
    hist += [(f"  {days:.1f} {w['days']}", "dim")]
    models = {(g["harness"], g["model"]) for g in gs}
    facts: list[tuple[str, Line]] = [
        (w["history"], hist),
        (w["harnesses"], [(", ".join(sorted({g["harness"] for g in gs})), "ink")]),
        (w["models"], [(f"{len(models)}", "ink_b"), (f"  {sum(g['n_sessions'] for g in gs)} {w['sessions']} · "
                                                     f"{sum(g['n_turns'] for g in gs):,} {w['turns']}", "dim")]),
    ]  # fmt: skip
    cal = res.get("calibration")
    if cal and cal.get("at"):
        facts.append((w["calibrated"], [(w["ago"].format(t=_ago((now or time.time()) - cal["at"])), "ink")]))
    if service:
        facts.append((w["service"], [("● ", "good" if service == "up" else "dim"), (w.get(service, service), "ink")]))
    t = totals(res)
    if t["n"]:
        nset = w["models_set"].format(n=t["n"]).replace("model(s)", "model" if t["n"] == 1 else "models")
        cost, tm = chip(t["pc"], fusd(t["d"]) + w["per_day"], unit), chip(t["pt"], fmin(t["dm"]) + w["per_day"], unit)
        facts.append((w["impact"], [*cost, ("  ", None), *tm, ("  " + nset, "dim")]))
    if res.get("demo"):
        facts.append(("", [("synthetic demo data", "warn")]))
    return facts


def group_panel(
    g: dict[str, Any], cols: int, lang: str = "en", unit: str = "pct", prof: str | None = None
) -> list[Line]:
    w = W[lang]
    prof = prof or g.get("profile") or "balanced"
    iw = cols - 4
    p, d = g["profiles"][prof], g["default"]
    title: Line = [(label(g["model"]), "title"), (f" · {g['role']} · {g['harness']}", "dim")]
    body: list[Line] = []
    body += pack([[(f"{g['n_sessions']} {w['sessions']}", "dim")], [(f"{g['n_turns']:,} {w['turns']}", "dim")],
                  [(f"{g['days']:.1f} {w['days']}", "dim")], *_ttl(g, w)], iw, " · ")  # fmt: skip
    # gauge: default, current and the three profiles on the threshold axis; red where recall is below the floor
    grid = g["grid"]
    floor = g["recommendation"]["recall_floor"]
    unsafe, i = [], 0
    while i < len(grid):
        if g["recall"][i] < floor:
            j = i
            while j + 1 < len(grid) and g["recall"][j + 1] < floor:
                j += 1
            unsafe.append((grid[i], grid[j]))
            i = j + 1
        else:
            i += 1
    marks = [(d["threshold"], "┊", "dim")]
    if g.get("current"):
        marks.append((g["current"]["threshold"], "╎", "dim"))
    marks += [
        (g["profiles"][n]["threshold"], PGLYPH[n], "acc_b" if n == prof else "acc") for n in PROFILES if n != prof
    ]
    marks.append((p["threshold"], PGLYPH[prof], "acc_b"))
    body += gauge(iw, grid[0], grid[-1], marks, unsafe)
    if unsafe:
        body.append([(w["unsafe"].format(f=floor), "bad"), ("  ┊ " + k(d["threshold"]), "dim")])
    # the answer, then was / now bars for cost and time
    f = figs(g, prof)
    st = status(g, prof)
    if f:
        ans: list[Line] = [
            [(k(p["threshold"]), "big")],
            [*chip(f["pc"], None, unit), (" " + w["costm"], "dim")],
            [*chip(f["pt"], None, unit), (" " + w["wall"], "dim")],
            [(f"{fusd(f['d'])} · {fmin(f['dm'])} {w['vs_default']} {k(d['threshold'])}", "dim")],
        ]
        if unit != "pct":
            ans[1] = [(fusd(f["d"], True), (_t(f["d"]) or "ink") + "_b"), (" " + w["costm"], "dim")]
            ans[2] = [(fmin(f["dm"], True), (_t(f["dm"]) or "ink") + "_b"), (" " + w["wall"], "dim")]
        body += pack(ans, iw)
        if st in ("keep", "cur"):
            body += pack([[("▪ " + w[st] + ": ", "dim"), (keep_reason(g, prof, lang), "dim")]], iw)
        lab = max(cw(w["was"]), cw(w["now"])) + max(cw(w["costm"]), cw(w["wall"])) + 2
        bw = max(min(iw - lab - 22, 56), 6)
        for key, was, now in (("cost", f["was"], f["now"]), ("time", f["wasm"], f["nowm"])):
            a, b = was_now(was, now, bw)
            fmt = money if key == "cost" else (lambda v: f"{v:.0f} min")
            name = w["costm"] if key == "cost" else w["wall"]
            body.append([(name + " " * (lab - cw(name) - cw(w["was"]) - 1), "dim"), (w["was"] + " ", "dim"), *a,
                         (f" {fmt(was)}{w['per_day']}", "dim")])  # fmt: skip
            pc = f["pc"] if key == "cost" else f["pt"]
            body.append([(" " * (lab - cw(w["now"]) - 1), None), (w["now"] + " ", "dim"), *b,
                         (f" {fmt(now)}{w['per_day']} ", "ink"), (fpct(pc), _t(pc))])  # fmt: skip
        if f["d"] < 0:
            body.append([(" " * lab, None), (w["saved"], "good")])
        cost = [c / max(g["days"], 1) for c in g["combined_cost"]]
        sel = grid.index(p["threshold"]) if p["threshold"] in grid else None
        sw = max(iw - cw(w["by_thr"]) - 14, 6)
        body.append([(w["by_thr"] + " ", "dim"), *spark(cost, sw, sel), (f" {k(grid[0])}→{k(grid[-1])}", "dim")])
    # the three profiles as chips
    toks = []
    for n in PROFILES:
        fn = figs(g, n)
        star = "★" if n == g.get("profile") else ""
        on = "ink" if n == prof else "dim"
        tok: Line = [(PGLYPH[n] + " ", "acc" if n == prof else "dim"), (w[n] + star + " ", on),
                     (k(g["profiles"][n]["threshold"]) + " ", "ink")]  # fmt: skip
        if fn:
            tok += [(fpct(fn["pc"]) if unit == "pct" else fusd(fn["d"], True), _t(fn["d"]))]
        tok += [(f" [{w[status(g, n)]}]", "acc" if status(g, n) == "set" else "dim")]
        toks.append(tok)
    body += pack(toks, iw, "   ")
    for fl in g["profiles"][prof]["flags"]:
        if fl in ("guardrail_applied", "deep_recall_below_floor"):
            continue
        body += pack([[("⚠ ", "warn" if fl not in BROKEN else "bad"), (fl.replace("_", " "), "dim")]], iw)
    return panel(title, body, cols)


BROKEN = {"recall_floor_not_met", "wait_cap_unreachable", "min_window_unreachable"}


def _t(v: float | None) -> str | None:
    return None if v is None or abs(v) < 1e-9 else "good" if v < 0 else "warn"


def noop_lines(res: dict[str, Any], cols: int, lang: str = "en") -> list[Line]:
    w = W[lang]
    gs = [g for g in res["groups"] if g["no_action_needed"]]
    if not gs:
        return []
    toks = [[("✓ ", "good"), (f"{label(g['model'])} {g['role']}", "ink"),
             (f" {k(g.get('peak_max'))} < {k(g['default']['threshold'])}", "dim")] for g in gs]  # fmt: skip
    return [[("✓ " + w["no_change"], "good_b"), (" · " + w["never"], "dim")], *pack(toks, cols)]


def recs_table(res: dict[str, Any], cols: int, lang: str = "en", unit: str = "pct") -> list[Line]:
    """One row per group with an action: model, role, threshold, action chip, change, and the keep-default reason."""
    w = W[lang]
    rows = []
    for g in sorted(
        res["groups"], key=lambda g: (g["no_action_needed"], -abs((figs(g, g["profile"]) or {"d": 0})["d"]))
    ):
        prof = g.get("profile") or "balanced"
        st = status(g, prof)
        f = figs(g, prof)
        name: Line = [(label(g["model"]), "ink"), (f" {g['role']}", "dim")]
        thr: Line = [("–" if st == "noop" else k(g["profiles"][prof]["threshold"]), "ink")]
        act: Line = [(f"[{w[st]}]", "acc_b" if st == "set" else "dim")]
        ch: Line = [] if st == "noop" or not f else [*chip(f["pc"], fusd(f["d"]) + w["per_day"], unit), ("  ", None),
                                                     *chip(f["pt"], None, unit), (" " + w["wall"], "dim")]  # fmt: skip
        why = keep_reason(g, prof, lang) if st in ("keep", "cur") else ""
        rows.append((name, thr, act, ch, why))
    nw = max((width(r[0]) for r in rows), default=0)
    out: list[Line] = [[(w["recs"], "title")]]
    wide = cols >= 90
    for name, thr, act, ch, why in rows:
        if wide:
            line = [*pad(name, min(nw, 26)), ("  ", None), *pad(thr, 5), ("  ", None), *pad(act, 15), *ch]
            out.append(clip(line, cols))
        else:
            out += pack([name, thr, act], cols, "  ")
            if ch:
                out.append(clip([("  ", None), *ch], cols))
        if why:
            out += pack([[("    ↳ ", "dim"), (why, "dim")]], cols)
    return out


IDLE_W: dict[str, dict[str, Any]] = {
    "en": {
        "head": "idle compaction (Claude Code idleCompaction: {state}{src}; min context {m}) · {groups}",
        "on": "on", "off": "off", "default": ", the default",
        "rate": "{g}: fires on {r:.0%} of eligible idle gaps ({src}); {n} in the logs",
        "scope": {"group": "{f} of {e} in this group", "harness": "{f} of {e} across this harness",
                  "assumed": "assumed: too few eligible gaps in the logs"},
        "with": {"profile": "with the {p} picks", "current": "with your current settings"},
        "flip": {True: "turning it off", False: "turning it on"},
        "line": "{w}, {f}: {u}/day ({pc}), {m}/day wall-clock, {c:+.1f} compactions/day, recall {r:+.3f} → {v}",
        "verdict": {(True, True): "turn it off", (True, False): "turn it on",
                    (False, True): "keep it on", (False, False): "keep it off"},
        "tail": "includes {u}/day of summaries for sessions left idle and never resumed (expected; not in the logs)",
    },
    "zh": {
        "head": "空闲压缩(Claude Code idleCompaction:{state}{src};最小上下文 {m})· {groups}",
        "on": "开", "off": "关", "default": ",默认",
        "rate": "{g}:符合条件的空闲间隔里有 {r:.0%} 触发({src});日志里 {n} 次",
        "scope": {"group": "本组 {e} 次里 {f} 次", "harness": "该 harness 全部会话 {e} 次里 {f} 次",
                  "assumed": "假定:日志里符合条件的间隔太少"},
        "with": {"profile": "按{p}方案的阈值", "current": "按你当前的设置"},
        "flip": {True: "关掉它", False: "打开它"},
        "line": "{w},{f}:每天 {u}({pc}),耗时每天 {m},压缩每天 {c:+.1f} 次,召回 {r:+.3f} → {v}",
        "verdict": {(True, True): "建议关掉", (True, False): "建议打开",
                    (False, True): "保持开启", (False, False): "保持关闭"},
        "tail": "其中每天 {u} 是空闲后没再回来的会话做的摘要(期望值,日志里看不到)",
    },
}  # fmt: skip
PROFILE_ZH = {"cost": "省钱", "balanced": "均衡", "time": "省时"}


def idle_lines(res: dict[str, Any], lang: str = "en") -> list[str]:
    """Keep Claude Code's idle compaction on or off, in plain lines (empty when it applies to no group)."""
    adv = res.get("idle_compaction")
    if not adv:
        return []
    w = IDLE_W[lang]
    on = adv["enabled"]
    gs = [g for g in res["groups"] if g.get("idle", {}).get("alt")]
    out = [w["head"].format(state=w["on"] if on else w["off"], src="" if adv.get("configured") else w["default"],
                            m=k(adv["min_tokens"]), groups=", ".join(adv["groups"]))]  # fmt: skip
    for g in gs:
        if g["idle"]["rate_scope"] == "group":  # groups with enough eligible gaps of their own
            i = g["idle"]
            src = w["scope"][i["rate_scope"]].format(f=i["rate_fired"], e=i["rate_eligible"])
            out.append(
                "  · "
                + w["rate"].format(
                    g=f"{label(g['model'])} {g['role']}", r=i["rate"], src=src, n=g["observed_idle_compactions"]
                )
            )
    rows = [("profile", adv["profiles"][adv["profile"]]), ("current", adv["current"])]
    for which, v in rows:
        base = v["configured"]["combined_cost"]
        pname = adv["profile"] if lang == "en" else PROFILE_ZH.get(adv["profile"], adv["profile"])
        out.append("  · " + w["line"].format(
            w=w["with"][which].format(p=pname), f=w["flip"][on], u=fusd(v["d_usd_per_day"], True),
            pc=fpct(v["d_usd_per_day"] / base if base else None), m=fmin(v["d_min_per_day"], True),
            c=v["d_compactions_per_day"], r=v["d_recall"], v=w["verdict"][(v["switch"], on)]))  # fmt: skip
    tail = adv["profiles"][adv["profile"]]["configured"].get("idle_tail_cost", 0.0) if on else 0.0
    if tail >= 0.05:
        out.append("  · " + w["tail"].format(u=fusd(tail)))
    return out


MODEL_W: dict[str, dict[str, Any]] = {
    "en": {
        "head": "models",
        "family": "{m}: no price of its own; priced as {row}, the closest model of the same family",
        "fallback": "{m}: unknown model; priced with the fallback row (${i:g} in / ${o:g} out per 1M, {w} window)",
        "vendor": "{m} on {p}: no {p} price; priced at the vendor's list price ({row})",
        "fix_auto": "automatic price refresh is on: the background service fetches the public data for it",
        "fix": "fix: compactlab pricing refresh (or `compactlab pricing auto on` to do it whenever a new model "
               "appears; downloads public data only)",
        "unset": "{m}: no autoCompactWindow of its own, so it compacts at the default {d}; {peer} is set to {v}",
        "thin": " (only {n:.0f} active days of its own data: {peer}'s {v} is a reasonable stand-in "
                "until there is more)",
        "own": " (`compactlab recommend` gives it a value from its own data)",
        "window_changed": "{m} {r}: {h} changed its window from {a} to {b} around {d}; the replay uses {b}",
        "peak_over_window": "{m} {r}: contexts reached {peak}, beyond the {a} window assumed, so it is replayed with "
                            "{w} (the window has probably grown: refresh the price data, or "
                            "CLAUDE_CODE_MAX_CONTEXT_TOKENS is set where compactlab cannot see it)",
        "drift": "{m} {r}: the last {n} threshold compactions were at about {o}, not the default {a} assumed: "
                 "{h} may have changed its default, or a setting compactlab cannot read is in force (an env var, a "
                 "project settings file, a CLI flag)",
        "price_change": "price data {d}: {m} {f} {o} → {n} (history is replayed at today's prices)",
        "superseded": "{m} ({h}): replaced by {b} (last used {d})",
        "superseded_glob": "; not counted for its one setting for every model",
        "global": "{h} (one limit for every model), {p}: {a} · across {ms}",
        "global_set": "set {t}: {u}/day, {mn}/day wall-clock",
    },
    "zh": {
        "head": "模型",
        "family": "{m}：没有自己的价格，借用同族最接近的 {row}",
        "fallback": "{m}：未知模型，按兜底价格计算（输入 ${i:g} / 输出 ${o:g} 每百万 token，窗口 {w}）",
        "vendor": "{m}（{p}）：没有 {p} 的价格，按厂商官方价 {row} 计算",
        "fix_auto": "已开启自动更新价格：后台服务会去公开数据里取",
        "fix": "处理：compactlab pricing refresh（或 `compactlab pricing auto on`，"
               "出现新模型时自动更新；只下载公开数据）",
        "unset": "{m}：没有单独设置 autoCompactWindow，按默认 {d} 压缩；{peer} 设为 {v}",
        "thin": "（自己的数据只有 {n:.0f} 个活跃天，先参照 {peer} 的 {v}）",
        "own": "（`compactlab recommend` 会按它自己的数据给值）",
        "window_changed": "{m} {r}：{h} 在 {d} 前后把窗口从 {a} 改成 {b}，按 {b} 回放",
        "peak_over_window": "{m} {r}：上下文到过 {peak}，超出假设的 {a} 窗口，按 {w} 回放（窗口多半变大了："
                            "更新价格数据，或 CLAUDE_CODE_MAX_CONTEXT_TOKENS 设在 compactlab 看不到的地方）",
        "drift": "{m} {r}：最近 {n} 次阈值压缩在约 {o}，不是假设的默认 {a}：{h} 可能改了默认值，"
                 "或有 compactlab 读不到的设置在生效（环境变量、项目设置文件、命令行参数）",
        "price_change": "价格数据 {d}：{m} {f} {o} → {n}（历史按现价回放）",
        "superseded": "{m}（{h}）：已被 {b} 取代（最后使用 {d}）",
        "superseded_glob": "，所有模型共用的那个设置不再算它",
        "global": "{h}（所有模型共用一个阈值），{p}：{a} · 综合 {ms}",
        "global_set": "设为 {t}：每天 {u}，耗时每天 {mn}",
    },
}  # fmt: skip
THIN_DAYS = 3


def _day(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "?"


def model_lines(res: dict[str, Any], lang: str = "en") -> list[str]:
    """Models priced from someone else's row; windows that changed or that the logs contradict; defaults the latest
    compactions do not match; recent price changes; Claude Code models with no window of their own while others have
    one (empty when there is nothing to say)."""
    notes = res.get("model_notes") or {}
    w = MODEL_W[lang]
    out = []
    fb = notes.get("fallback") or {}
    for p in notes.get("prices", []):
        out.append("  · " + w[p["kind"]].format(
            m=label(p["model"]), row=p["row"], p=p.get("platform") or "", i=fb.get("input", 3), o=fb.get("output", 15),
            w=k(fb.get("max_window", 200_000))))  # fmt: skip
    if notes.get("prices"):
        out.append("    " + w["fix_auto" if notes.get("auto_refresh") else "fix"])
    for x in notes.get("windows", []):
        args = {"m": label(x["model"]), "r": x["role"], "h": x["harness"]}
        if x["kind"] == "window_changed":
            out.append("  · " + w["window_changed"].format(a=k(x["from"]), b=k(x["to"]), d=_day(x["since"]), **args))
        else:
            out.append("  · " + w["peak_over_window"].format(peak=k(x["peak"]), a=k(x["assumed"]), w=k(x["window"]),
                                                            **args))  # fmt: skip
    for x in notes.get("defaults", []):
        out.append("  · " + w["drift"].format(m=label(x["model"]), r=x["role"], h=x["harness"], n=x["n"],
                                              o=k(x["observed"]), a=k(x["assumed"])))  # fmt: skip
    for x in notes.get("superseded", []):
        out.append("  · " + w["superseded"].format(m=label(x["model"]), h=x["harness"], b=label(x["by"]),
                                                   d=_day(x["last_used"]))
                   + (w["superseded_glob"] if x.get("global_key") else ""))  # fmt: skip
    for h, gk in (res.get("global_keys") or {}).items():
        prof = (res.get("options") or {}).get("profile") or "balanced"
        gp = gk["profiles"][prof]
        act = (
            w["global_set"].format(t=k(gp["threshold"]), u=fusd(gp["d_usd_per_day"], True),
                                   mn=fmin(gp["d_min_per_day"], True))
            if gp["action"] == "set"
            else W[lang][{"keep default": "keep", "keep current": "cur"}.get(gp["action"], "noop")]
        )  # fmt: skip
        pname = prof if lang == "en" else PROFILE_ZH.get(prof, prof)
        out.append("  · " + w["global"].format(h=h, p=pname, a=act, ms=", ".join(gk["models"])))
    for c in notes.get("price_changes", []):
        out.append("  · " + w["price_change"].format(d=c["date"], m=c["model"], f=c["field"], o=c["old"], n=c["new"]))
    for u in notes.get("unset", []):
        line = w["unset"].format(m=label(u["model"]), d=k(u["default"]), peer=label(u["peer"]), v=k(u["peer_value"]))
        thin = u["active_days"] < THIN_DAYS
        out.append("  · " + line + (w["thin"] if thin else w["own"]).format(
            n=u["active_days"], peer=label(u["peer"]), v=k(u["peer_value"])))  # fmt: skip
    return [w["head"], *out] if out else []


def summary_view(res: dict[str, Any], cols: int, lang: str = "en", unit: str = "pct",
                 service: str | None = None) -> list[Line]:  # fmt: skip
    cols = max(30, min(cols, 200))
    lines = header(summary_facts(res, lang, unit, service), cols)
    v = verdict(res, lang)
    lines += [[], v] if v else []
    lines.append([])
    gs = [g for g in res["groups"] if not g["no_action_needed"]]
    gs.sort(key=lambda g: (status(g, g["profile"]) != "set", -abs((figs(g, g["profile"]) or {"d": 0})["d"])))
    pw = min(cols, 120)
    for g in gs:
        lines += group_panel(g, pw, lang, unit)
    nl = noop_lines(res, pw, lang)
    if nl:
        lines += [[], *nl]
    il = idle_lines(res, lang)
    if il:
        lines += [[], [(il[0], "ink_b")], *([(x, "dim")] for x in il[1:])]
    ml = model_lines(res, lang)
    if ml:
        lines += [[], [(ml[0], "ink_b")], *([(x, "dim")] for x in ml[1:])]
    lines += [
        [],
        *recs_table(res, pw, lang, unit),
        [],
        *pack([[(x, "dim")] for x in W[lang]["foot"].split(" · ")], pw, " · "),
    ]
    return [clip(x, cols) for x in lines]


# ---------- doctor and serve ----------


def doctor_view(report: dict[str, Any], cols: int) -> list[Line]:
    cols = max(30, min(cols, 200))
    facts: list[tuple[str, Line]] = [
        ("version", [(report["compactlab"], "ink"), (f"  Python {report['python']}", "dim")]),
        ("pricing", [(f"bundled {report['pricing']['bundled']}", "ink"),
                     ("  refreshed" if report["pricing"]["refreshed"] else "  not refreshed", "dim")]),
        ("cache", [("✓ " if report["cache"]["exists"] else "· ", "good" if report["cache"]["exists"] else "dim"),
                   (f"{report['cache']['bytes'] / 1e6:.1f} MB" if report["cache"]["exists"] else "none", "ink")]),
    ]  # fmt: skip
    out = header(facts, cols, "compactlab doctor")
    pw = min(cols, 120)
    for h in report["harnesses"]:
        body: list[Line] = [[("✓ " if h["cli"] else "✗ ", "good" if h["cli"] else "warn"),
                             ("CLI ", "dim"), (h["cli"] or "not found", "ink")]]  # fmt: skip
        for r in h["log_roots"]:
            body.append([("✓ " if r["exists"] else "· ", "good" if r["exists"] else "dim"), ("logs   ", "dim"),
                         (r["path"], "ink"), (f"  {r['jsonl_files']} files", "dim")])  # fmt: skip
        for c in h["config"]:
            body.append([("✓ " if c["exists"] else "· ", "good" if c["exists"] else "dim"), ("config ", "dim"),
                         (c["path"], "ink")])  # fmt: skip
        out += [[], *panel([(h["name"], "title")], body, pw)]
    return [clip(x, cols) for x in out]


def serve_view(url: str, token_path: str, next_cal: str, state_dir: str, auto_apply: bool, cols: int) -> list[Line]:
    facts: list[tuple[str, Line]] = [
        ("report", [(url, "acc_b")]),
        ("token", [(token_path, "dim")]),
        ("next calibration", [(next_cal, "ink")]),
        ("auto-apply", [("on" if auto_apply else "off", "warn" if auto_apply else "dim")]),
        ("state", [(state_dir, "dim")]),
        ("", [("Ctrl-C stops · localhost only · never writes settings without consent", "dim")]),
    ]
    return [clip(x, max(30, cols)) for x in header(facts, max(30, min(cols, 200)), "compactlab serve")]
