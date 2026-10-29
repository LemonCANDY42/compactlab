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
           "vs_default": "per day vs default", "set": "set", "keep": "keep default", "noop": "no change",
           "no_change": "No change needed", "never": "never reaches the default", "recs": "Recommended settings",
           "by_thr": "cost by threshold", "foot": "Δ vs the harness default · list-price estimates · recall is a "
           "lite-mode proxy · --plain prints the full tables", "models_set": "{n} model(s) set", "ago": "{t} ago",
           "next": "next {t}", "up": "up", "down": "not running", "none": "–",
           "kw_above": "{a} is not below the default {b}",
           "kw_score": "no net gain at ${v:g}/h: money {u}, time {m} (= {tv}) → {n}/day",
           "unsafe": "░ recall < {f:g}"},
    "zh": {"history": "历史", "harnesses": "工具", "models": "模型", "calibrated": "校准", "service": "服务",
           "impact": "若应用", "sessions": "会话", "turns": "轮", "days": "天", "cost": "省钱", "balanced": "均衡",
           "time": "省时", "was": "原来", "now": "现在", "per_day": "/天", "vs_default": "每天,对默认",
           "set": "设置", "keep": "保持默认", "noop": "无需调整", "no_change": "无需调整", "never": "从未达到默认阈值",
           "recs": "推荐设置", "by_thr": "各阈值成本", "foot": "差值相对 harness 默认值 · 按公开标价估算 · 召回为 "
           "lite 代理值 · --plain 输出完整表格", "models_set": "{n} 个模型设置", "ago": "{t}前", "next": "下次 {t}",
           "up": "运行中", "down": "未运行", "none": "–", "kw_above": "{a} 不低于默认值 {b}",
           "kw_score": "按 ${v:g}/小时 没有净收益:费用 {u},时间 {m}(= {tv})→ 每天 {n}", "unsafe": "░ 召回 < {f:g}"},
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
    return "noop" if a == "no action needed" else "keep" if a == "keep default" else "set"


def keep_reason(g: dict[str, Any], prof: str, lang: str = "en") -> str:
    """The engine's keep-default tests, in words (compactlab.sim.engine._comparison)."""
    w = W[lang]
    p, d = g["profiles"][prof], g["default"]
    if p["threshold"] >= d["threshold"]:
        return w["kw_above"].format(a=k(p["threshold"]), b=k(d["threshold"]))
    f = figs(g, prof)
    assert f is not None
    vot = float(p["value_of_time"])
    tv = f["dm"] / 60 * vot
    return w["kw_score"].format(v=vot, u=fusd(f["d"], True), m=fmin(f["dm"], True), tv=fusd(tv, True),
                                n=fusd(f["d"] + tv, True))  # fmt: skip


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
                  [(f"{g['days']:.1f} {w['days']}", "dim")]], iw, " · ")  # fmt: skip
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
            [*chip(f["pc"], None, unit), (" " + w["cost"], "dim")],
            [*chip(f["pt"], None, unit), (" " + w["time"], "dim")],
            [(f"{fusd(f['d'])} · {fmin(f['dm'])} {w['vs_default']} {k(d['threshold'])}", "dim")],
        ]
        if unit != "pct":
            ans[1] = [(fusd(f["d"], True), (_t(f["d"]) or "ink") + "_b"), (" " + w["cost"], "dim")]
            ans[2] = [(fmin(f["dm"], True), (_t(f["dm"]) or "ink") + "_b"), (" " + w["time"], "dim")]
        body += pack(ans, iw)
        if st == "keep":
            body += pack([[("▪ " + w["keep"] + ": ", "dim"), (keep_reason(g, prof, lang), "dim")]], iw)
        lab = max(cw(w["was"]), cw(w["now"])) + cw(w["cost"]) + 2
        bw = max(min(iw - lab - 22, 56), 6)
        for key, was, now in (("cost", f["was"], f["now"]), ("time", f["wasm"], f["nowm"])):
            a, b = was_now(was, now, bw)
            fmt = money if key == "cost" else (lambda v: f"{v:.0f} min")
            name = w[key]
            body.append([(name + " " * (lab - cw(name) - cw(w["was"]) - 1), "dim"), (w["was"] + " ", "dim"), *a,
                         (f" {fmt(was)}{w['per_day']}", "dim")])  # fmt: skip
            pc = f["pc"] if key == "cost" else f["pt"]
            body.append([(" " * (lab - cw(w["now"]) - 1), None), (w["now"] + " ", "dim"), *b,
                         (f" {fmt(now)}{w['per_day']} ", "ink"), (fpct(pc), _t(pc))])  # fmt: skip
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
                                                     *chip(f["pt"], None, unit), (" " + w["time"], "dim")]  # fmt: skip
        why = keep_reason(g, prof, lang) if st == "keep" else ""
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


def summary_view(res: dict[str, Any], cols: int, lang: str = "en", unit: str = "pct",
                 service: str | None = None) -> list[Line]:  # fmt: skip
    cols = max(30, min(cols, 200))
    lines = header(summary_facts(res, lang, unit, service), cols)
    lines.append([])
    gs = [g for g in res["groups"] if not g["no_action_needed"]]
    gs.sort(key=lambda g: (status(g, g["profile"]) != "set", -abs((figs(g, g["profile"]) or {"d": 0})["d"])))
    pw = min(cols, 120)
    for g in gs:
        lines += group_panel(g, pw, lang, unit)
    nl = noop_lines(res, pw, lang)
    if nl:
        lines += [[], *nl]
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
