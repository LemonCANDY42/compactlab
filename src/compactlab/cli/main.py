"""compactlab: measure where to auto-compact from your own agent history.

Modes: lite (default; offline, zero cost) and deep (opt-in; adds LLM-driven checks, needs --yes and --budget).
Profiles: cost ($10/h of agent time), balanced (default, $250/h), time ($600/h), each with guardrails.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any

from .. import __version__, synth
from .. import apply as ap
from ..adapters import adapter_names, get_adapter
from ..adapters.cache import ScanCache
from ..eval.deep import EvalError, MockClient, deep_analyze, load_client, plan_deep
from ..model import Session
from ..pricing import DEFAULT_REFRESHED, PricingTable
from ..pricing.refresh import SOURCES, refresh
from ..report.html import render_html
from ..sim.baseline import read_current
from ..sim.engine import SimOptions, analyze, reselect
from ..sim.select import DEFAULT_PROFILE, PROFILE_NAMES, PROFILES, WAIT_CAP_MIN_PER_DAY

STATE = "~/.compactlab"
EXAMPLES = """examples:
  compactlab demo                                   synthetic data -> report, opens it
  compactlab doctor                                 where are my logs and configs?
  compactlab simulate --out result.json             lite analysis of the last 30 days
  compactlab report --result result.json            HTML report from a saved result
  compactlab apply --result result.json --claude-settings ~/.claude/settings.json   dry-run diff
  compactlab simulate --mode deep --budget 2       print the deep cost estimate (add --yes to run)
"""


class CLIError(Exception):
    pass


def _k(s: str) -> int:
    s = s.strip().lower().replace("_", "")
    try:
        if s.endswith("k"):
            return int(float(s[:-1]) * 1000)
        if s.endswith("m"):
            return int(float(s[:-1]) * 1_000_000)
        return int(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"expected a token count like 250k, 1m or 300000, got {s!r}") from e


def _out(a: argparse.Namespace, obj: Any, text: str) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str) if getattr(a, "json", False) else text)


# ---------- shared option groups ----------


def _source(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("history source")
    g.add_argument(
        "--harness", action="append", choices=adapter_names(), help="limit to a harness (repeatable; default all)"
    )
    g.add_argument(
        "--claude-root",
        action="append",
        help="Claude Code projects dir (default $CLAUDE_CONFIG_DIR/projects or ~/.claude/projects)",
    )
    g.add_argument(
        "--codex-root", action="append", help="Codex sessions dir (default $CODEX_HOME/sessions or ~/.codex/sessions)"
    )
    g.add_argument(
        "--dsh-root",
        action="append",
        help="DeepSeek Harness sessions dir (default $DSH_HOME/sessions or ~/.dsh/sessions)",
    )
    g.add_argument(
        "--since-days", type=float, default=30, help="only files modified in the last N days (0 = all; default 30)"
    )
    g.add_argument("--no-cache", action="store_true", help="do not read or write the scan cache")
    g.add_argument("--cache-dir", default=f"{STATE}/cache", help="scan cache location (default ~/.compactlab/cache)")
    p.add_argument("--json", action="store_true", help="machine-readable output")


def _sim(p: argparse.ArgumentParser) -> None:
    _source(p)
    g = p.add_argument_group("simulation")
    g.add_argument("--pricing", help="pricing override JSON (same schema as the bundled data/pricing.json)")
    g.add_argument(
        "--offline-pricing", action="store_true", help="ignore refreshed prices; use bundled + --pricing only"
    )
    g.add_argument(
        "--grid", default="100k:1m:25k", help="threshold sweep start:stop:step (stop capped per model window)"
    )
    g.add_argument(
        "--recall-floor", type=float, default=0.90, help="minimum assumed (or measured) recall (default 0.90)"
    )
    g.add_argument("--max-threshold", type=_k, help="never recommend a threshold above this")
    g.add_argument(
        "--wait-cap",
        type=float,
        default=WAIT_CAP_MIN_PER_DAY,
        help="guardrail: max added compaction wait vs default, minutes/day (default 30)",
    )
    g.add_argument("--cache-ttl", type=int, help="prompt-cache TTL in seconds (default from pricing file: 300)")
    g.add_argument("--summary-tokens", type=_k, help="compaction summary size (default: observed median, else 12k)")
    g.add_argument("--base-tokens", type=_k, help="post-compaction base context (default: observed, else 35k)")
    _profile_args(g)
    c = p.add_argument_group("current config (read-only, for comparison)")
    _config_args(c)
    c.add_argument("--no-current", action="store_true", help="do not read current harness config")
    p.add_argument("--lang", choices=["both", "en", "zh"], default="both", help="output language (default: both 中英)")
    h = p.add_argument_group("deep mode (opt-in, costs money)")
    h.add_argument(
        "--mode", choices=["lite", "deep"], default="lite", help="lite: offline (default); deep: adds LLM checks"
    )
    h.add_argument("--executor", default="claude-code", help="claude-code | codex | mock | package.module:Class")
    h.add_argument("--model", default="claude-sonnet-5-5", help="model that re-summarizes and answers probes")
    h.add_argument("--judge-model", default="claude-haiku-5-5", help="judge model (must differ from --model)")
    h.add_argument("--repeats", type=int, default=3, help="judge repeats per probe (majority vote)")
    h.add_argument("--sessions-per-group", type=int, default=3)
    h.add_argument("--budget", type=float, help="hard USD cap for deep mode (required with --mode deep)")
    h.add_argument("--yes", action="store_true", help="confirm deep-mode spending after reviewing the estimate")


def _profile_args(g: Any) -> None:
    vots = ", ".join(f"{n} ${PROFILES[n]['value_of_time']:g}/h" for n in PROFILE_NAMES)
    g.add_argument(
        "--profile",
        choices=PROFILE_NAMES,
        help=f"which profile to use/apply (default {DEFAULT_PROFILE}; all three are always computed): {vots}",
    )
    g.add_argument(
        "--value-of-time",
        type=float,
        help="USD per hour of agent wall-clock; overrides the selected profile's value (0 = cost only)",
    )


def _reselect(a: argparse.Namespace, res: dict[str, Any]) -> dict[str, Any]:
    """Apply --profile / --value-of-time to a saved result (selection only, no replay)."""
    if getattr(a, "profile", None) or getattr(a, "value_of_time", None) is not None:
        reselect(res, getattr(a, "profile", None), getattr(a, "value_of_time", None))
    return res


def _config_args(g: Any) -> None:
    g.add_argument("--claude-settings", help="Claude Code settings.json (default: ~/.claude/settings.json)")
    g.add_argument("--codex-config", help="Codex config.toml (default: ~/.codex/config.toml)")


def _roots(a: argparse.Namespace) -> dict[str, list[str] | None]:
    return {"claude-code": a.claude_root, "codex": a.codex_root, "dsh": a.dsh_root}


def _ttl_note(g: dict[str, Any], lang: str) -> str:
    """The cache TTL a group was replayed with and where it came from, in one language."""
    sec, src = int(g["cache_ttl"]), str(g.get("cache_ttl_source", ""))
    if lang == "zh":
        dur = f"{sec // 3600} 小时" if sec >= 3600 else f"{sec // 60} 分钟"
        m = re.match(r"logs: (\d+%)", src)
        why = (
            f"日志:{m.group(1)} 的缓存写入是 1 小时档"
            if m
            else ("--cache-ttl" if src == "--cache-ttl" else "价格文件默认值")
        )
        return f"缓存时长:{dur}({why}),缓存写入按该档计价"
    dur = f"{sec // 3600} h" if sec >= 3600 else f"{sec // 60} min"
    return f"cache: {dur} ({src}); cache writes priced at that tier"


def _progress(msg: str) -> None:
    """One in-place progress line on stderr, only when it is a terminal. The line is cut to the terminal width at a
    space (never mid-field): a wrapped line would leave one stale row per update behind the carriage return."""
    if not sys.stderr.isatty():
        return
    width = max(shutil.get_terminal_size((80, 24)).columns - 1, 10)
    if len(msg) > width:
        msg = msg[: msg.rfind(" ", 0, width) if " " in msg[:width] else width]
    print(f"\r\033[K{msg}", end="", file=sys.stderr, flush=True)


def load_sessions(a: argparse.Namespace) -> list[Session]:
    import time as _time

    cache = None if a.no_cache else ScanCache(a.cache_dir)
    out: list[Session] = []
    cut = _time.time() - a.since_days * 86400 if a.since_days else None
    quiet, total = getattr(a, "service", False), 0
    try:
        for h in a.harness or adapter_names():
            ad = get_adapter(h)(_roots(a).get(h))
            files = list(ad.find_files())
            total += len(files)
            for i, path in enumerate(files, 1):
                if i % 20 == 0 or i == len(files):
                    stats = "" if quiet or not cache else f" · cached {cache.stats['hit'] + cache.stats['resumed']}"
                    _progress(f"scanning {h} {i}/{len(files)} files{stats}")
                try:
                    if cut and path.stat().st_mtime < cut:
                        continue
                    s = cache.parse(ad, path) if cache else ad.parse_file(path)
                except OSError:
                    continue
                if s.turns:
                    out.append(s)
                if cache and i % 500 == 0:
                    cache.save()  # keep progress if the scan is interrupted
    finally:
        if cache:
            cache.save()
        _progress("")
    st = cache.stats if cache else {"hit": 0, "resumed": 0, "parsed": total}
    a.scan_stats = {"files": total, "cached": st["hit"] + st["resumed"], "parsed": st["parsed"]}
    return out


def _opts(a: argparse.Namespace) -> SimOptions:
    try:
        lo, hi, st = (_k(x) for x in a.grid.split(":"))
    except (ValueError, argparse.ArgumentTypeError) as e:
        raise CLIError(f"--grid must look like 100k:1m:25k ({e})") from e
    return SimOptions(
        profile=a.profile or DEFAULT_PROFILE,
        value_of_time=a.value_of_time,
        wait_cap_min_per_day=a.wait_cap,
        max_threshold=a.max_threshold,
        grid_start=lo,
        grid_stop=hi,
        grid_step=st,
        recall_floor=a.recall_floor,
        ttl_seconds=a.cache_ttl,
        summary_tokens=a.summary_tokens,
        base_tokens=a.base_tokens,
    )


def _pricing(a: argparse.Namespace) -> PricingTable:
    return PricingTable.load(getattr(a, "pricing", None), offline=getattr(a, "offline_pricing", False))


def _current(a: argparse.Namespace, sessions: list[Session]) -> dict[tuple[str, str], tuple[int | None, str]]:
    """Read the configured threshold per (harness, model) from config files, read-only."""
    if getattr(a, "no_current", False):
        return {}
    paths: dict[str, Path | None] = {}
    for h, arg in (("claude-code", a.claude_settings), ("codex", a.codex_config)):
        if arg:
            paths[h] = Path(arg).expanduser()
        else:
            cands = get_adapter(h).config_paths() if h in adapter_names() else []
            paths[h] = next((c for c in cands if c.exists()), None)
    out = {}
    for h, m in {(x.harness, x.model) for x in sessions}:
        try:
            out[(h, m)] = read_current(h, m, paths.get(h))
        except (OSError, ValueError) as e:
            out[(h, m)] = (None, f"unreadable: {e}")
    return out


def _load_result(path: str) -> dict[str, Any]:
    p = Path(path).expanduser()
    if not p.exists():
        raise CLIError(f"{p} does not exist. Create it first with: compactlab simulate --out {path}")
    res: dict[str, Any] = json.loads(p.read_text())
    if res.get("schema", 0) < 4:
        raise CLIError(f"{p} was made by an older compactlab (schema {res.get('schema')}); re-run compactlab simulate")
    if any("impact" not in c["vs_default"] for g in res["groups"] for c in g.get("comparison", {}).values()):
        reselect(res)  # results saved before per-day impact existed: recompute selection (no replay)
    return res


def run_analysis(a: argparse.Namespace) -> dict[str, Any] | None:
    sessions = load_sessions(a)
    if not sessions:
        roots = [str(r) for h in (a.harness or adapter_names()) for r in get_adapter(h)(_roots(a).get(h)).roots]
        raise CLIError(
            "no sessions with usage data found under:\n  "
            + "\n  ".join(roots)
            + "\nRun `compactlab doctor` to check locations, `--since-days 0` to include older files, "
            "or `compactlab demo` to see a report built from synthetic data."
        )
    pricing = _pricing(a)
    quiet = getattr(a, "service", False)

    def step(i: int, n: int, name: str) -> None:
        _progress(f"simulating {i}/{n}" + ("" if quiet else f": {name}"))

    res = analyze(sessions, pricing, _opts(a), current=_current(a, sessions), progress=step)
    _progress("")
    if a.mode == "deep":
        return _deep(a, sessions, res, pricing)
    return res


def _deep(
    a: argparse.Namespace, sessions: list[Session], res: dict[str, Any], pricing: PricingTable
) -> dict[str, Any] | None:
    if a.budget is None:
        raise CLIError("--mode deep needs --budget USD (a hard cap)")
    plan = plan_deep(sessions, res, pricing, a.model, a.judge_model, a.repeats, a.sessions_per_group)
    print(
        f"deep plan: {len(plan.pairs)} re-summarizations via {a.executor} ({a.model}, judge {a.judge_model}); "
        f"estimated ~${plan.est_usd:.2f}; hard cap ${a.budget:.2f}",
        file=sys.stderr,
    )
    if not a.yes:
        print(
            "not running deep: review the estimate, then re-run with --yes (lite results are unaffected)",
            file=sys.stderr,
        )
        return None
    from ..eval.executors import EXECUTORS

    client = (
        MockClient()
        if a.executor == "mock"
        else (EXECUTORS[a.executor]() if a.executor in EXECUTORS else load_client(a.executor))
    )
    try:
        return deep_analyze(
            plan,
            res,
            client,
            model=a.model,
            judge_model=a.judge_model,
            pricing=pricing,
            budget_usd=a.budget,
            repeats=a.repeats,
            confirm=True,
        )
    except EvalError as e:
        raise CLIError(str(e)) from e


def _k_(v: int | None) -> str:
    return "-" if v is None else f"{v // 1000}k"


def _hrs(sec: float) -> str:
    return f"{sec / 3600:+.1f}h" if abs(sec) >= 360 else f"{sec / 60:+.0f}m"


WIDTHS = ((16, "<"), (8, ">"), (22, "<"), (18, ">"), (16, ">"), (14, ">"), (16, ">"))


def _pad(text: str, width: int, align: str) -> str:
    """Pad by display width (CJK characters take two columns)."""
    import unicodedata

    w = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    fill = " " * max(width - w, 0)
    return (fill + text if align == ">" else text + fill) + " "


T: dict[str, dict[str, Any]] = {
    "en": {
        "cols": ("profile", "window", "action", "Δcost vs default", "Δtime (/day)", "compactions/d", "recall"),
        "default": "default",
        "current": "current",
        "cost": "cost",
        "balanced": "balanced",
        "time": "time",
        "vs_current": "vs current",
        "no_action": "no action needed: the default never compacts in these sessions",
        "time_src": "time model: turn latency from {t}, compaction latency from {c} ({n} own samples)",
        "src": {"own": "this group's data", "pooled": "all {h} sessions (this group is thin)", "prior": "a prior"},
        "flags": {
            "below_quarter_window": "{p}: below 25% of the max window; expect frequent lossy compactions",
            "wait_over_cap": "{p}: adds more than {cap:g} min/day of compaction wait vs default",
            "guardrail_applied": "{p}: guardrail moved the pick from {u} to {t}",
            "recall_floor_not_met": "{p}: no threshold met the recall floor",
            "wait_cap_unreachable": "{p}: no threshold keeps added wait under {cap:g} min/day",
            "min_window_unreachable": "{p}: the grid does not reach the profile's minimum window",
            "deep_recall_below_floor": "{p}: deep mode measured recall below the floor; lowered to {t}",
        },
        "note": "Profile shown first is selected (--profile, default balanced). Δ vs the harness default. "
        "Costs are list-price estimates; recall is an assumed proxy in lite mode.",
    },
    "zh": {
        "cols": ("方案", "阈值", "建议", "成本差(对默认)", "时间差(每天)", "每天压缩", "召回"),
        "default": "默认",
        "current": "当前",
        "cost": "省钱",
        "balanced": "均衡",
        "time": "省时",
        "vs_current": "对当前",
        "no_action": "无需调整:默认设置在这些会话里从未触发压缩",
        "time_src": "时间模型:每轮耗时来自{t},压缩耗时来自{c}(本组 {n} 个样本)",
        "src": {"own": "本组数据", "pooled": "全部 {h} 会话(本组样本太少)", "prior": "先验值"},
        "flags": {
            "below_quarter_window": "{p}:低于最大窗口的 25%,会频繁做有损压缩",
            "wait_over_cap": "{p}:比默认每天多等压缩超过 {cap:g} 分钟",
            "guardrail_applied": "{p}:护栏把选择从 {u} 调到 {t}",
            "recall_floor_not_met": "{p}:没有阈值达到召回下限",
            "wait_cap_unreachable": "{p}:没有阈值能把额外等待压到每天 {cap:g} 分钟以内",
            "min_window_unreachable": "{p}:网格达不到该方案的最小窗口",
            "deep_recall_below_floor": "{p}:deep 模式实测召回低于下限,已降到 {t}",
        },
        "note": "第一行方案是当前选择(--profile,默认均衡)。差值相对 harness 默认值。"
        "成本按公开标价估算;lite 模式下召回率是假设代理值。",
    },
}
ACTION_ZH = {"set": "设置", "keep default": "保持默认", "no action needed": "无需调整", "-": "-"}


def _dtime(c: dict[str, Any]) -> str:
    return f"{_hrs(c['d_time_s'])} ({c['d_time_min_per_day']:+.0f}m)"


def _term(a: argparse.Namespace, stream: Any = None) -> tuple[Any, int] | None:
    """(style, columns) for the terminal views, or None for the plain tables: --plain, or --color auto off a TTY."""
    from ..term.style import Style

    stream = stream or sys.stdout
    mode = getattr(a, "color", "auto")
    if getattr(a, "plain", False) or (mode == "auto" and not stream.isatty()):
        return None
    return Style.detect(stream, mode), max(30, min(shutil.get_terminal_size((100, 30)).columns, 200))


def _lang1(a: argparse.Namespace) -> str:
    return "zh" if getattr(a, "lang", "en") == "zh" else "en"


def _summary_out(a: argparse.Namespace, res: dict[str, Any]) -> str:
    """The terminal view on a TTY, the full plain tables otherwise."""
    t = _term(a)
    if not t:
        return _summary(res, getattr(a, "lang", "both"))
    from ..term.views import summary_view

    style, cols = t
    return str(style.lines(summary_view(res, cols, _lang1(a), getattr(a, "unit", "pct"))))


def _summary(res: dict[str, Any], lang: str = "both") -> str:
    from ..sim.select import compare

    langs = ["en", "zh"] if lang == "both" else [lang]

    def name(key: str) -> str:
        return "/".join(T[x][key] for x in langs)

    def act(action: str) -> str:
        if action == "-" or lang == "en":
            return action
        return ACTION_ZH.get(action, action) if lang == "zh" else f"{action}/{ACTION_ZH.get(action, action)}"

    head = f"mode: {res['mode']}" + (f"  (deep spent ${res['deep']['spent_usd']:.2f})" if res.get("deep") else "")
    from ..term.views import verdict

    lines = [head]
    for x in langs:
        v = "".join(t for t, _ in verdict(res, x))
        if v:
            lines.append(v)
    for g in res["groups"]:
        d, days, sel = g["default"], g["days"], g["profile"]
        lines.append(
            f"\n{g['model']} · {g['role']} · {g['n_sessions']} sessions · {g['n_turns']} turns"
            f" · {days:.0f} days · window {_k_(g['max_window'])}"
        )
        for hdr in [T[x]["cols"] for x in langs]:
            lines.append("  " + "".join(_pad(h, w, al) for h, (w, al) in zip(hdr, WIDTHS, strict=True)))

        def row(cells: tuple[str, ...]) -> str:
            return "  " + "".join(_pad(x, w, al) for x, (w, al) in zip(cells, WIDTHS, strict=True))

        lines.append(row((name("default"), _k_(d["threshold"]), "-", f"${d['combined_cost']:,.0f} (base)", "0",
                          f"{d['compactions'] / days:.1f}", f"{d['recall']:.3f}")))  # fmt: skip
        if g.get("current"):
            c = g["current"]
            cm = compare(c, d, days)
            lines.append(row((name("current"), _k_(c["configured"]), "-", _dc(cm), _dtime(cm),
                              f"{cm['compactions_per_day']:.1f}", f"{c['recall']:.3f}")))  # fmt: skip
        order = [sel] + [n for n in PROFILE_NAMES if n != sel]
        for n in order:
            cm, p = g["comparison"][n], g["profiles"][n]
            v = cm["vs_default"]
            label = name(n) + ("*" if n == sel else "")
            if cm["action"] == "no action needed":
                lines.append(row((label, "-", act(cm["action"]), "-", "-", f"{v['compactions_per_day']:.1f}",
                                  f"{p['recall']:.3f}")))  # fmt: skip
                continue
            rc = f"{p['recall']:.3f} ({v['d_recall']:+.3f})"
            lines.append(row((label, _k_(cm["threshold"]), act(cm["action"]), _dc(v), _dtime(v),
                              f"{v['compactions_per_day']:.1f}", rc)))  # fmt: skip
        notes = []
        if g.get("current") and not g["no_action_needed"]:
            vc = {n: g["comparison"][n]["vs_current"] for n in PROFILE_NAMES}
            parts = [f"{name(n)} {_dc(v)} {_hrs(v['d_time_s'])}" for n, v in vc.items()]
            notes.append(f"{name('vs_current')}: " + "; ".join(parts))
        if g["no_action_needed"]:
            notes += [T[x]["no_action"] for x in langs]
        tmod = g["time_model"]
        for x in langs:
            src = T[x]["src"]
            notes.append(T[x]["time_src"].format(t=src[tmod.get("turn_source", "prior")].format(h=g["harness"]),
                                                 c=src[tmod.get("compact_source", "prior")].format(h=g["harness"]),
                                                 n=tmod["n_compact_samples"]))  # fmt: skip
        if not g["no_action_needed"]:
            for n in order:
                p = g["profiles"][n]
                fmt = {"cap": p["wait_cap_min_per_day"], "u": _k_(p["unconstrained_threshold"]),
                       "t": _k_(p["threshold"])}  # fmt: skip
                for f in p["flags"]:
                    notes += [T[x]["flags"][f].format(p=T[x][n], **fmt) for x in langs if f in T[x]["flags"]]
        if g.get("cache_ttl"):
            notes += [_ttl_note(g, x) for x in langs]
        if "en" in langs:
            notes.append(f"default: {d['note']}" + (f"; current: {g['current_note']}" if g.get("current_note") else ""))
        if "zh" in langs:
            notes.append(
                f"默认依据:{d.get('note_zh', d['note'])}"
                + (f";当前:{g['current_note']}" if g.get("current_note") else "")
            )
        lines += [f"  · {n}" for n in notes]
    lines.append("")
    lines += [T[x]["note"] for x in langs]
    return "\n".join(ln.rstrip() for ln in lines)


def _dc(c: dict[str, Any]) -> str:
    sign = "+" if c["d_cost"] >= 0 else "-"
    return f"{sign}${abs(c['d_cost']):,.0f} ({c['d_cost_pct'] * 100:+.0f}%)"


IMPACT: dict[str, dict[str, Any]] = {
    "en": {
        "title": "Per-day impact · {m} · {r} · history {d:.1f} calendar days, {a} active days (with ≥1 turn)",
        "vs": {"vs_default": "vs harness default {t}", "vs_current": "vs your current {t}"},
        "cols": (
            "profile",
            "window",
            "$/day",
            "$/active d",
            "$/month",
            "min/day",
            "wait",
            "turns",
            "min/active d",
            "h/month",
            "compact/d",
            "/month",
        ),
        "legend": "Sign: -$ / -min = saves, + = adds. min/day = wait (compaction + re-discovery) - turns "
        "(faster turns from a smaller context). Month = 30 days. List-price estimates; recall is a lite-mode proxy.",
        "keep": "{p}: picks {t}, which is not better than the default: recommend keeps the default",
    },
    "zh": {
        "title": "每日影响 · {m} · {r} · 历史覆盖 {d:.1f} 个自然日,其中 {a} 个活跃日(至少一轮)",
        "vs": {"vs_default": "对 harness 默认 {t}", "vs_current": "对你当前的 {t}"},
        "cols": (
            "方案",
            "阈值",
            "$/天",
            "$/活跃天",
            "$/月",
            "分钟/天",
            "等待",
            "每轮",
            "分钟/活跃天",
            "小时/月",
            "压缩/天",
            "/月",
        ),
        "legend": "符号:-$ / -分钟 = 节省,+ = 增加。分钟/天 = 等待(压缩 + 重新查找)- 每轮提速(上下文更小)。"
        "一个月按 30 天。成本按公开标价估算;lite 模式下召回率是代理值。",
        "keep": "{p}:选出 {t},不比默认值好,recommend 会保持默认",
    },
}
IWIDTHS = ((14, "<"), (7, ">"), (10, ">"), (11, ">"), (11, ">"), (9, ">"), (8, ">"), (8, ">"), (13, ">"),
           (9, ">"), (10, ">"), (7, ">"))  # fmt: skip


def _usd(x: float | None) -> str:
    if x is None:
        return "-"
    if abs(x) < 0.05:
        return "$0"
    return f"{'-' if x < 0 else '+'}${abs(x):,.1f}" if abs(x) < 1000 else f"{'-' if x < 0 else '+'}${abs(x):,.0f}"


def _mins(x: float | None) -> str:
    return "-" if x is None else ("0.0" if abs(x) < 0.05 else f"{x:+.1f}")


def impact_text(res: dict[str, Any], lang: str = "both") -> str:
    """Per-day / per-active-day / per-month table for every profile of every group, vs default and current."""
    langs = ["en", "zh"] if lang == "both" else [lang]
    lines: list[str] = []
    for g in res["groups"]:
        if g["no_action_needed"]:
            continue
        sel = g["profile"]
        active = g.get("active_days")
        act = f"{active:.3g}" if active is not None else "?"
        lines.append("")
        lines += [IMPACT[x]["title"].format(m=g["model"], r=g["role"], d=g["days"], a=act) for x in langs]
        for ref in ("vs_default", "vs_current"):
            refpt = g["default"] if ref == "vs_default" else g.get("current")
            if not refpt:
                continue
            thr = _k_(refpt.get("configured", refpt["threshold"]))
            lines.append("  " + " / ".join(IMPACT[x]["vs"][ref].format(t=thr) for x in langs))
            for x in langs:
                lines.append(
                    "    " + "".join(_pad(h, w, al) for h, (w, al) in zip(IMPACT[x]["cols"], IWIDTHS, strict=True))
                )
            for n in [sel] + [n for n in PROFILE_NAMES if n != sel]:
                cm = g["comparison"][n]
                im = (cm.get(ref) or {}).get("impact")
                if not im:
                    continue
                u, m, w, tg, c = (
                    im["usd"],
                    im["minutes"],
                    im["wait_minutes"],
                    im["turn_gain_minutes"],
                    im["compactions"],
                )
                cells = ("/".join(T[x][n] for x in langs) + ("*" if n == sel else ""), _k_(cm["threshold"]),
                         _usd(u["per_day"]), _usd(u["per_active_day"]), _usd(u["per_month"]),
                         _mins(m["per_day"]), _mins(w["per_day"]), _mins(-tg["per_day"]),
                         _mins(m["per_active_day"]), _mins(m["per_month"] / 60), f"{c['per_day']:.1f}",
                         f"{c['per_month']:.0f}")  # fmt: skip
                lines.append("    " + "".join(_pad(v, w_, al) for v, (w_, al) in zip(cells, IWIDTHS, strict=True)))
        for n in PROFILE_NAMES:
            if g["comparison"][n]["action"] == "keep default":
                lines += ["  · " + IMPACT[x]["keep"].format(p=T[x][n], t=_k_(g["profiles"][n]["threshold"]))
                          for x in langs]  # fmt: skip
    if lines:
        lines.append("")
        lines += [IMPACT[x]["legend"] for x in langs]
    return "\n".join(ln.rstrip() for ln in lines)


# ---------- commands ----------


def cmd_scan(a: argparse.Namespace) -> int:
    ss = load_sessions(a)
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    unknown: dict[str, int] = {}
    for s in ss:
        d = rows.setdefault((s.harness, s.role, s.model), {"sessions": 0, "turns": 0, "compactions": 0, "peak": 0})
        d["sessions"] += 1
        d["turns"] += len(s.turns)
        d["compactions"] += len(s.compactions)
        d["peak"] = max(d["peak"], s.peak)
        for k, v in s.unknown_events.items():
            unknown[f"{s.harness}:{k}"] = unknown.get(f"{s.harness}:{k}", 0) + v
    obj = {
        "sessions": len(ss),
        "groups": [{"harness": h, "role": r, "model": m, **d} for (h, r, m), d in sorted(rows.items())],
        "unknown_event_types": unknown,
    }
    text = [
        f"{len(ss)} sessions",
        f"{'harness':12} {'role':9} {'model':26} {'sess':>5} {'turns':>7} {'compact':>8} {'peak':>7}",
    ]
    text += [
        f"{h:12} {r:9} {m[:26]:26} {d['sessions']:>5} {d['turns']:>7} {d['compactions']:>8} {d['peak'] // 1000:>6}k"
        for (h, r, m), d in sorted(rows.items())
    ]
    if unknown:
        text.append(
            "unrecognized event types (ignored, counted): " + ", ".join(f"{k}={v}" for k, v in sorted(unknown.items()))
        )
    _out(a, obj, "\n".join(text))
    return 0


def cmd_simulate(a: argparse.Namespace) -> int:
    res = run_analysis(a)
    if res is None:
        return 2
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, sort_keys=True))
    if a.lens_out:
        from ..lens import summarize

        lens = Path(a.lens_out).expanduser()
        lens.parent.mkdir(parents=True, exist_ok=True)
        lens.write_text(json.dumps(summarize(res), sort_keys=True))
    _out(
        a,
        {
            "result": a.out,
            "mode": res["mode"],
            "groups": [
                {k: g[k] for k in ("harness", "model", "role", "profile", "profiles", "comparison")}
                for g in res["groups"]
            ],
        },
        _summary_out(a, res) + f"\n\nwrote {a.out}",
    )
    return 0


def cmd_report(a: argparse.Namespace) -> int:
    res = _reselect(a, _load_result(a.result)) if a.result else run_analysis(a)
    if res is None:
        return 2
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(render_html(res, redact=a.redact))
    print(f"wrote {a.out}")
    if a.open:
        _open(Path(a.out))
    return 0


def _open(p: Path) -> None:
    if platform.system() == "Darwin" and shutil.which("open"):
        subprocess.run(["open", str(p)], check=False)
    else:
        webbrowser.open(p.resolve().as_uri())


def _apply_opts(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("apply")
    g.add_argument(
        "--preauth", help="pre-authorization JSON: allowed files, key patterns, models, bounds (docs/SAFETY.md)"
    )
    g.add_argument(
        "--i-authorize", action="store_true", help="explicit one-off authorization instead of a preauth file"
    )
    g.add_argument("--models", nargs="*", help="models allowed with --i-authorize")
    g.add_argument("--min", type=_k, default=100_000, help="lower bound with --i-authorize (default 100k)")
    g.add_argument("--max", type=_k, default=1_000_000, help="upper bound with --i-authorize (default 1m)")
    g.add_argument("--write", action="store_true", help="actually write; without it apply is a dry-run")
    g.add_argument("--state-dir", default=STATE, help="backups and manifests (default ~/.compactlab)")


def _preauth(a: argparse.Namespace) -> ap.Preauth:
    if a.preauth:
        return ap.load_preauth(a.preauth)
    if not a.i_authorize:
        raise ap.ApplyError(
            "writing needs --preauth FILE, or --i-authorize with explicit --claude-settings/--codex-config and --models"
        )
    files = {h: Path(f) for h, f in (("claude-code", a.claude_settings), ("codex", a.codex_config)) if f}
    if not files or not a.models:
        raise ap.ApplyError("--i-authorize needs explicit config file paths and --models")
    return ap.explicit_preauth(files, a.models, a.min, a.max)


def _recs(res: dict[str, Any]) -> list[dict[str, Any]]:
    """The selected profile's threshold per main-session model; groups where the default is fine are left alone."""
    out = []
    for g in res["groups"]:
        if g["role"] != "main" or g["comparison"][g["profile"]]["action"] in ("no action needed", "keep default"):
            continue
        out.append({"harness": g["harness"], "model": g["model"], "value": g["profiles"][g["profile"]]["threshold"]})
    return out


def _do_apply(a: argparse.Namespace, res: dict[str, Any], retune: bool = False) -> int:
    try:
        if a.write or retune:
            pre = _preauth(a)
            if retune and not pre.allow_retune:
                raise ap.ApplyError('retune needs a --preauth file with "allow_retune": true')
        elif a.preauth:
            pre = ap.load_preauth(a.preauth)
        else:
            files = {h: Path(f) for h, f in (("claude-code", a.claude_settings), ("codex", a.codex_config)) if f}
            pre = ap.explicit_preauth(files, a.models or [g["model"] for g in res["groups"]], a.min, a.max)
        changes, rejected = ap.plan_changes(_recs(res), pre)
        rendered = ap.render(changes)
    except ap.ApplyError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    plan = {
        "profile": res["groups"][0]["profile"] if res["groups"] else (a.profile or DEFAULT_PROFILE),
        "dry_run": not a.write,
        "changes": [
            {"harness": c.harness, "file": str(c.file), "key": c.key, "old": c.old, "new": c.new} for c in changes
        ],
        "skipped": rejected,
    }
    text = [f"skip: {r}" for r in rejected]
    text += [f"{c.harness}: {c.key}: {c.old!r} -> {c.new}  ({c.file})" for c in changes]
    if changes:
        text.append(ap.diff_text(rendered))
        text += [f"note: {n}" for n in ap.formatting_note(rendered, changes)]
    else:
        text.append("nothing to change")
    if changes and not a.write:
        text.append("dry-run: nothing written (add --write with authorization to apply)")
    if changes and a.write:
        m = ap.write_changes(changes, Path(a.state_dir).expanduser(), pre)
        plan["manifest"] = str(m)
        text.append(f"applied; manifest {m}\nundo with: compactlab revert --state-dir {a.state_dir}")
    _out(a, plan, "\n".join(text))
    return 0


def cmd_apply(a: argparse.Namespace) -> int:
    return _do_apply(a, _reselect(a, _load_result(a.result)))


def cmd_retune(a: argparse.Namespace) -> int:
    if not a.preauth:
        print('refused: retune needs --preauth (with "allow_retune": true)', file=sys.stderr)
        return 2
    res = run_analysis(a)
    return 2 if res is None else _do_apply(a, res, retune=True)


def _forget_applied(state: Path, manifest_id: str) -> None:
    """A reverted auto-apply is no longer what compactlab last wrote: drop it so the next run does not read the
    restored value as a manual edit."""
    from ..service.policy import journal, load_applied

    p = state / "service" / "applied.json"
    applied = load_applied(p)
    keep = {k: v for k, v in applied.items() if v.get("manifest") != manifest_id}
    if keep != applied:
        p.write_text(json.dumps(keep, indent=2))
        journal(state / "service" / "journal.jsonl", {"reverted": manifest_id})


def cmd_revert(a: argparse.Namespace) -> int:
    sd = Path(a.state_dir).expanduser()
    try:
        m = sd / "manifests" / f"{a.id}.json" if a.id else ap.latest_manifest(sd)
        for line in ap.revert(m, a.force):
            print(line)
        _forget_applied(sd, m.stem)
    except (ap.ApplyError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


def _version(binary: str) -> str | None:
    path = shutil.which(binary)
    if not path:
        return None
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
        return (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else "installed"
    except (OSError, subprocess.TimeoutExpired):
        return "installed (version unknown)"


def cmd_doctor(a: argparse.Namespace) -> int:
    report: dict[str, Any] = {
        "compactlab": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "harnesses": [],
        "pricing": {},
        "cache": {},
    }
    bins = {"claude-code": "claude", "codex": "codex", "dsh": "dsh"}
    for name in adapter_names():
        ad = get_adapter(name)
        roots = ad.default_roots()
        report["harnesses"].append(
            {
                "name": name,
                "cli": _version(bins.get(name, name)),
                "log_roots": [
                    {
                        "path": str(r),
                        "exists": r.exists(),
                        "jsonl_files": sum(1 for _ in ad(r).find_files()) if r.exists() else 0,
                    }
                    for r in roots
                ],
                "config": [{"path": str(c), "exists": c.exists()} for c in ad.config_paths()],
            }
        )
        if name == "dsh":
            from ..adapters.dsh import zstd_available

            report["harnesses"][-1]["zstd"] = zstd_available()
    rp = DEFAULT_REFRESHED.expanduser()
    report["pricing"] = {
        "bundled": PricingTable.bundled().get("retrieved"),
        "refreshed_file": str(rp),
        "refreshed": rp.exists(),
    }
    cf = Path(f"{STATE}/cache/scan-v1.json").expanduser()
    report["cache"] = {"path": str(cf), "exists": cf.exists(), "bytes": cf.stat().st_size if cf.exists() else 0}
    lines = [f"compactlab {__version__} · Python {report['python']} · {platform.system()}"]
    for h in report["harnesses"]:
        lines.append(f"\n{h['name']}: CLI {h['cli'] or 'not found'}")
        lines += [
            f"  logs   {'ok ' if r['exists'] else '-- '} {r['path']}  ({r['jsonl_files']} files)"
            for r in h["log_roots"]
        ]
        lines += [f"  config {'ok ' if c['exists'] else '-- '} {c['path']}" for c in h["config"]]
        if h["name"] == "dsh":
            zs = "decoder available" if h["zstd"] else "no decoder: Python 3.14+ or the zstd command"
            lines.append(f"  zstd   {'ok ' if h['zstd'] else '-- '} {zs}")
    lines.append(
        f"\npricing: bundled snapshot {report['pricing']['bundled']}; "
        f"refreshed: {'yes' if report['pricing']['refreshed'] else 'no'}"
    )
    lines.append(f"scan cache: {cf} ({'present' if cf.exists() else 'none'})")
    t = _term(a)
    if t and not a.json:
        from ..term.views import doctor_view

        print(t[0].lines(doctor_view(report, t[1])))
        return 0
    _out(a, report, "\n".join(lines))
    return 0


def cmd_pricing(a: argparse.Namespace) -> int:
    if a.action == "refresh":
        try:
            n = refresh(a.source, DEFAULT_REFRESHED, a.url)
        except Exception as e:  # network/parse errors: keep bundled pricing
            print(f"refresh failed, bundled pricing still in use: {e}", file=sys.stderr)
            return 2
        print(f"stored {n} model rows from {a.source} in {DEFAULT_REFRESHED}")
        return 0
    t = PricingTable.load(a.pricing, offline=a.offline_pricing)
    keys = a.model or sorted(t.data["models"])
    rows = []
    for k in keys:
        p = t.lookup(k)
        rows.append(
            {
                "model": k,
                "row": p.key,
                "input": p.input,
                "output": p.output,
                "cache_write": p.cache_write,
                "cache_read": p.cache_read,
                "window": p.max_window,
                "long_above": p.long_above,
                "source": p.source,
            }
        )
    text = [f"{'model':24} {'row':20} {'in':>6} {'out':>6} {'cw':>6} {'cr':>6} {'window':>8}  source"]
    text += [
        f"{r['model'][:24]:24} {r['row'][:20]:20} {r['input']:>6g} {r['output']:>6g} {r['cache_write']:>6g} "
        f"{r['cache_read']:>6g} {r['window'] // 1000:>7}k  {r['source']}"
        for r in rows
    ]
    _out(a, rows, "\n".join(text) + "\nUSD per million tokens.")
    return 0


def cmd_cache(a: argparse.Namespace) -> int:
    c = ScanCache(a.cache_dir)
    if a.action == "clear":
        c.clear()
        print(f"cleared {c.file}")
    else:
        _out(a, {"path": str(c.file), "files": len(c.entries)}, f"{c.file}: {len(c.entries)} files cached")
    return 0


def cmd_recommend(a: argparse.Namespace) -> int:
    from ..recommend import Snippet, check, generate

    res = _reselect(a, _load_result(a.result)) if a.result else run_analysis(a)
    if res is None:
        return 2
    harness = a.harness[0] if a.harness and len(a.harness) == 1 else None
    profiles = PROFILE_NAMES if a.all_profiles else (None,)
    items = [x for pr in profiles for x in generate(res, harness, a.scope, pr, a.format, a.provider)]
    snippets = [x for x in items if isinstance(x, Snippet)]
    omitted = [x for x in items if not isinstance(x, Snippet)]
    if a.check:
        problems = check(snippets, a.max_age_days)
        _out(a, {"problems": problems}, "\n".join(problems) or f"all {len(snippets)} snippet(s) use verified keys")
        return 1 if problems else 0
    text = "\n".join(s.text() for s in snippets)
    notes = [f"# omitted: {o.harness}: {o.reason}" for o in omitted]
    if a.json:
        imp = [{"harness": g["harness"], "model": g["model"], "role": g["role"], "days": g["days"],
                "active_days": g.get("active_days"),
                "profiles": {n: {"threshold": c["threshold"], "action": c["action"],
                                 "vs_default": c["vs_default"]["impact"],
                                 "vs_current": (c["vs_current"] or {}).get("impact")}
                             for n, c in g["comparison"].items()}}
               for g in res["groups"]]  # fmt: skip
        print(json.dumps({"snippets": [s.to_dict() for s in snippets], "omitted": [vars(o) for o in omitted],
                          "impact": imp}, indent=2, ensure_ascii=False))  # fmt: skip
    else:
        print(text + ("\n" + "\n".join(notes) if notes else ""))
        t = _term(a)
        if t:
            from ..term.views import recs_table

            print("\n" + t[0].lines(recs_table(res, min(t[1], 120), _lang1(a), getattr(a, "unit", "pct"))))
        else:
            table = impact_text(res, a.lang)
            if table:
                print(table)
    if a.copy:
        if not snippets:
            print("nothing to copy", file=sys.stderr)
            return 1
        if not shutil.which("pbcopy"):
            raise CLIError("--copy needs pbcopy (macOS); use --print and copy manually")
        subprocess.run(["pbcopy"], input=text, text=True, check=True)
        print(f"copied {len(snippets)} snippet(s) to the clipboard", file=sys.stderr)
    return 0


# ---------- service: serve / recalibrate / schedule / consent ----------


def _service_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("calibration")
    g.add_argument("--every", default="1d", help="recalibrate interval: 30m, 6h, 1d, 2w ... or off (default 1d)")
    g.add_argument("--state-dir", default=STATE, help="service state, logs, backups (default ~/.compactlab)")
    g.add_argument("--lens-out", default=f"{STATE}/lens.json", help="summary for the compactlab-lens mod ('' to skip)")
    g.add_argument(
        "--auto-apply", action="store_true", help="apply the consented profile after each run (needs consent)"
    )
    g.add_argument("--dry-run", action="store_true", help="with --auto-apply: decide and show the diff, write nothing")
    g.add_argument("--min-days", type=float, default=7.0, help="auto-apply: minimum history days (default 7)")
    g.add_argument("--min-turns", type=int, default=500, help="auto-apply: minimum turns (default 500)")
    g.add_argument("--max-step", type=_k, default=100_000, help="auto-apply: largest move per run (default 100k)")
    g.add_argument("--cooldown", default="7d", help="auto-apply: minimum time between changes (default 7d)")


def _calibrator(a: argparse.Namespace):
    from ..service.core import Calibrator
    from ..service.policy import Rules, load_consent
    from ..service.schedule import parse_every

    try:
        every = parse_every(a.every)
        cooldown = parse_every(a.cooldown) or 0.0
    except ValueError as e:
        raise CLIError(str(e)) from e
    a.mode = "lite"  # the service never runs deep mode
    a.service = True  # short progress line, scan counts for the summary log
    state = Path(a.state_dir).expanduser()
    consent = None
    if a.auto_apply:
        try:
            consent = load_consent(state / "consent.json")
        except ap.ApplyError as e:
            raise CLIError(f"--auto-apply: {e}") from e

    def analyze() -> dict[str, Any]:
        res = run_analysis(a)
        if res is None:
            raise RuntimeError("analysis returned nothing")
        res["scan"] = getattr(a, "scan_stats", None)  # popped by the calibrator for its log line
        return res

    rules = Rules(min_days=a.min_days, min_turns=a.min_turns, max_step=a.max_step, cooldown_s=cooldown)
    lens = Path(a.lens_out).expanduser() if a.lens_out else None
    return Calibrator(analyze, state, every, lens_out=lens, consent=consent, rules=rules, dry_run=a.dry_run)


def _service_logging(state: Path) -> None:
    import logging
    from logging.handlers import RotatingFileHandler

    (state / "service").mkdir(parents=True, exist_ok=True)
    lg = logging.getLogger("compactlab.service")
    lg.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    class Console(logging.StreamHandler):
        """Clears the in-place progress line before a log line, so the two never share a row."""

        def emit(self, record: logging.LogRecord) -> None:
            if self.stream.isatty():
                self.stream.write("\r\033[K")
            super().emit(record)

    for h in (RotatingFileHandler(state / "service" / "service.log", maxBytes=1_000_000, backupCount=3),
              Console(sys.stderr)):  # fmt: skip
        h.setFormatter(fmt)
        lg.addHandler(h)


def cmd_serve(a: argparse.Namespace) -> int:
    import logging
    import threading

    from ..service.http import load_token, make_server

    cal = _calibrator(a)
    state = Path(a.state_dir).expanduser()
    _service_logging(state)
    if a.nice and hasattr(os, "nice"):
        os.nice(a.nice)
    token = load_token(state / "service" / "token")
    try:
        srv = make_server(cal, a.host, a.port, token, unsafe_bind=a.unsafe_bind)
    except PermissionError as e:
        raise CLIError(str(e)) from e
    host, port = str(srv.server_address[0]), int(srv.server_address[1])
    t = _term(a, sys.stderr)
    if t:
        from ..term.views import serve_view

        print(t[0].lines(serve_view(f"http://{host}:{port}/", str(state / "service" / "token"), cal.next_text(),
                                    str(state), bool(a.auto_apply), t[1])), file=sys.stderr)  # fmt: skip
    else:
        print(f"compactlab serving http://{host}:{port}/  (token in {state / 'service' / 'token'}; Ctrl-C stops)",
              file=sys.stderr)  # fmt: skip
    logging.getLogger("compactlab.service").info("next calibration: %s", cal.next_text())
    # where `compactlab top` finds this service (loopback address only; removed on exit)
    endpoint = state / "service" / "endpoint.json"
    endpoint.write_text(json.dumps({"host": host, "port": port, "pid": os.getpid()}))
    stop = threading.Event()
    threading.Thread(target=cal.loop, args=(stop,), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()
        endpoint.unlink(missing_ok=True)
    return 0


def cmd_recalibrate(a: argparse.Namespace) -> int:
    """One calibration (what a scheduler runs), with the same auto-apply rules as the service."""
    cal = _calibrator(a)
    _service_logging(Path(a.state_dir).expanduser())
    out = cal.recalibrate("cli")
    text = [f"recalibration: {out['status']}" + (f" ({out.get('error')})" if out.get("error") else "")]
    for d in (out.get("auto_apply") or {}).get("decisions", []):
        text.append(f"  {d['model']}: {d['action']}: {d['reason']}")
    aa = out.get("auto_apply") or {}
    if aa.get("diff"):
        text.append(aa["diff"])
    if aa.get("manifest"):
        text.append(f"applied; undo with: compactlab revert --state-dir {a.state_dir}")
    elif aa.get("dry_run") and aa.get("diff"):
        text.append("dry-run: nothing written")
    _out(a, out, "\n".join(text))
    return 0 if out["status"] == "ok" else 1


def cmd_schedule(a: argparse.Namespace) -> int:
    from ..service import schedule as sc

    home = Path.home()
    plat = a.platform if a.platform != "auto" else ("launchd" if platform.system() == "Darwin" else "systemd")
    if a.action == "status":
        found = sc.installed(plat, home)
        print("\n".join(f"installed: {p}" for p in found) or f"not installed ({plat})")
        return 0
    if a.action == "uninstall":
        if not a.yes:
            print("refused: uninstall needs --yes", file=sys.stderr)
            return 2
        gone = sc.uninstall(plat, home)
        print("\n".join(f"removed: {p}" for p in gone) or "nothing to remove")
        if gone:
            print("unload it yourself if it is loaded (launchctl bootout / systemctl --user disable --now)")
        return 0
    try:
        every = sc.parse_every(a.every)
        units = sc.units(plat, a.mode, every, ["--state-dir", a.state_dir] + (["--auto-apply"] if a.auto_apply else []),
                         home)  # fmt: skip
    except ValueError as e:
        raise CLIError(str(e)) from e
    if a.action == "print":
        for u in units:
            print(f"# {u.path}\n{u.text}")
        return 0
    print(sc.diff(units))
    if not a.yes and (not sys.stdin.isatty() or input("write these files? [y/N] ").strip().lower() != "y"):
        print("not installed", file=sys.stderr)
        return 2
    try:
        for p in sc.install(units):
            print(f"wrote {p}")
    except PermissionError as e:
        raise CLIError(str(e)) from e
    print(sc.activation_hint(plat))
    return 0


def cmd_consent(a: argparse.Namespace) -> int:
    from dataclasses import asdict

    from ..service.policy import Consent, load_consent, save_consent

    path = Path(a.state_dir).expanduser() / "consent.json"
    if a.action == "show":
        try:
            print(json.dumps(asdict(load_consent(path)), indent=2))
        except ap.ApplyError as e:
            print(str(e), file=sys.stderr)
            return 1
        return 0
    if a.action == "clear":
        if path.exists():
            path.unlink()
        print("consent removed; auto-apply is off")
        return 0
    if not (a.file and a.models):
        raise CLIError("consent set needs --file and --models")
    c = Consent(a.harness, a.file, list(a.models), a.profile, expires=a.expires)
    print(json.dumps(asdict(c), indent=2))
    if not a.yes and (not sys.stdin.isatty() or input("allow compactlab to change these keys unattended? [y/N] ")
                      .strip().lower() != "y"):  # fmt: skip
        print("no consent stored", file=sys.stderr)
        return 2
    try:
        save_consent(path, c)
    except ap.ApplyError as e:
        raise CLIError(str(e)) from e
    print(f"stored {path}")
    return 0


def cmd_top(a: argparse.Namespace) -> int:
    """Live view of the latest result; one static frame when stdout is not a terminal (or with --once)."""
    import tempfile

    from ..term import top as tp
    from ..term.style import Style

    state = Path(a.state_dir).expanduser()
    tmp = None
    if a.demo:
        tmp = tempfile.mkdtemp(prefix="compactlab-top-")
        res: dict[str, Any] | None = _demo_result(Path(tmp), "en")
        src: Path | None = None
    else:
        cands = (
            [Path(a.result).expanduser()]
            if a.result
            else [state / "service" / "result.json", Path("compactlab-result.json"), state / "result.json"]
        )
        src = next((c for c in cands if c.exists()), None)
        if src is None:
            raise CLIError(
                "no result found; run `compactlab simulate` (or `compactlab serve`), or try `compactlab top --demo`"
            )
        res = _load_result(str(src))
    assert res is not None
    mtime = [src.stat().st_mtime if src else 0.0]

    def reload() -> dict[str, Any] | None:
        if src and src.exists() and src.stat().st_mtime != mtime[0]:
            mtime[0] = src.stat().st_mtime
            return _load_result(str(src))
        return None

    def recalibrate() -> dict[str, Any] | None:
        if a.demo:
            return _demo_result(Path(tempfile.mkdtemp(prefix="compactlab-top-")), "en")
        ns = build_parser().parse_args(["simulate", "--out", str(src or "compactlab-result.json"), "--lang", "en"])
        ns.lens_out = ""
        return run_analysis(ns)

    # --demo never looks at the real service; open on the first model, not its harness row
    st = tp.TopState(res=res, unit=a.unit, profile=a.profile, service={} if a.demo else tp.service_state(state), sel=1)
    if a.once or not (sys.stdout.isatty() and sys.stdin.isatty()):
        size = shutil.get_terminal_size((120, 36))
        cols, rows = a.width or size.columns, a.height or size.lines
        style = Style.detect(sys.stdout, a.color) if not a.plain else Style(0)
        print(tp.static(st, style, max(30, min(cols, 200)), max(10, rows)))
        return 0
    tp.run(st, state, reload, recalibrate, a.interval)
    return 0


def _demo_result(out: Path, lang: str = "both") -> dict[str, Any]:
    """Synthetic history under `out` analysed like `simulate` (never reads real data)."""
    synth.write_claude_code(out / "claude" / "projects", n_main=12, n_sub=6)
    synth.write_codex(out / "codex" / "sessions", n=6)
    demo_settings = out / "claude" / "settings.json"  # synthetic "current" config to show the comparison
    demo_settings.write_text(
        json.dumps({"modelSettings": {"claude-opus-5-5": {"autoCompactWindow": 600000}}}, indent=2)
    )
    ns = build_parser().parse_args(
        ["simulate", "--claude-root", str(out / "claude" / "projects"), "--codex-root", str(out / "codex" / "sessions"),
         "--since-days", "0", "--no-cache", "--offline-pricing", "--claude-settings", str(demo_settings),
         "--codex-config", str(out / "codex" / "config.toml"), "--lang", lang]
    )  # fmt: skip
    res = run_analysis(ns)
    assert res is not None
    res["demo"] = True
    return res


def cmd_demo(a: argparse.Namespace) -> int:
    from ..lens import summarize

    out = Path(a.out)
    res = _demo_result(out, a.lang)
    (out / "result.json").write_text(json.dumps(res, sort_keys=True))
    (out / "lens.json").write_text(json.dumps(summarize(res), sort_keys=True))
    (out / "report.html").write_text(render_html(res))
    print(_summary_out(a, res))
    print(f"\nSYNTHETIC DEMO DATA (not your history). Report: {out / 'report.html'}")
    print(f"Copy the settings / 复制推荐设置: compactlab recommend --result {out / 'result.json'} [--copy]")
    if not a.no_open:
        _open(out / "report.html")
    return 0


def build_parser() -> argparse.ArgumentParser:
    fmt = argparse.RawDescriptionHelpFormatter
    p = argparse.ArgumentParser(prog="compactlab", description=__doc__, epilog=EXAMPLES, formatter_class=fmt)
    p.add_argument("--version", action="version", version=f"compactlab {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")
    _commands(sub, fmt)
    for sp in sub.choices.values():  # every command: terminal look
        g = sp.add_argument_group("terminal output")
        g.add_argument("--plain", action="store_true", help="plain text tables, no colour or boxes")
        g.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                       help="colour: auto (TTY, honours NO_COLOR), always, never")  # fmt: skip
        g.add_argument("--unit", choices=["pct", "usd"], default="pct", help="lead with %% of the default or $ / day")
    return p


def _commands(sub: Any, fmt: Any) -> None:

    s = sub.add_parser("demo", help="build and open a report from bundled synthetic data", formatter_class=fmt)
    s.add_argument("--out", default="compactlab-demo")
    s.add_argument("--no-open", action="store_true", help="do not open the report")
    s.add_argument("--lang", choices=["both", "en", "zh"], default="both", help="summary language (default: both 中英)")
    s.set_defaults(f=cmd_demo)

    s = sub.add_parser("top", help="live terminal view of the latest result (htop-like; keys: ? for help)")
    s.add_argument("--result", help="result.json (default: the service's, ./compactlab-result.json, ~/.compactlab)")
    s.add_argument("--demo", action="store_true", help="synthetic data instead of your history")
    s.add_argument("--state-dir", default=STATE, help="service state, to show and reach a running service")
    s.add_argument("--profile", choices=list(PROFILE_NAMES), help="show this profile for every model")
    s.add_argument("--interval", type=float, default=5.0, help="seconds between reloads (default 5)")
    s.add_argument("--once", action="store_true", help="print one frame and exit")
    s.add_argument("--width", type=int, help="frame width for --once (default: the terminal's)")
    s.add_argument("--height", type=int, help="frame height for --once")
    s.set_defaults(f=cmd_top)

    s = sub.add_parser("doctor", help="detect harnesses, log locations, config paths, versions")
    s.add_argument("--json", action="store_true")
    s.set_defaults(f=cmd_doctor)

    s = sub.add_parser("scan", help="list sessions found, per harness/role/model")
    _source(s)
    s.set_defaults(f=cmd_scan)

    s = sub.add_parser("simulate", help="threshold sweep -> result.json + summary (lite or deep)")
    _sim(s)
    s.add_argument("--out", default="compactlab-result.json")
    s.add_argument(
        "--lens-out",
        default=f"{STATE}/lens.json",
        help="small per-model summary the compactlab-lens mod reads (default ~/.compactlab/lens.json; '' to skip)",
    )
    s.set_defaults(f=cmd_simulate)

    s = sub.add_parser("report", help="self-contained offline HTML report")
    _sim(s)
    s.add_argument("--result", help="render a saved result.json instead of re-running")
    s.add_argument("--out", default="compactlab-report.html")
    s.add_argument("--redact", action="store_true", help="replace session ids with short hashes")
    s.add_argument("--open", action="store_true", help="open the report when done")
    s.set_defaults(f=cmd_report)

    s = sub.add_parser("apply", help="write a profile to harness config (dry-run unless --write + authorization)")
    s.add_argument("--result", required=True)
    s.add_argument("--json", action="store_true")
    _profile_args(s)
    _config_args(s)
    _apply_opts(s)
    s.set_defaults(f=cmd_apply)

    s = sub.add_parser("retune", help="re-analyze and apply under an existing pre-authorization")
    _sim(s)
    _apply_opts(s)
    s.set_defaults(f=cmd_retune)

    s = sub.add_parser(
        "recommend", help="ready-to-paste settings per harness/scope/profile (prints or copies; never writes)"
    )
    _sim(s)
    s.add_argument("--result", help="use a saved result.json instead of re-running")
    s.add_argument(
        "--scope",
        choices=["global", "project", "agent"],
        default="global",
        help="user-level config, project config, or per-agent/subagent (default global)",
    )
    s.add_argument("--all-profiles", action="store_true", help="print cost, balanced and time side by side")
    s.add_argument(
        "--format",
        choices=["json", "toml", "yaml", "env", "shell"],
        help="default: the scope's native format (json for Claude Code, toml for Codex, yaml for subagents)",
    )
    s.add_argument(
        "--provider",
        default="default",
        help="provider: anthropic|bedrock|vertex|foundry|gateway (Claude Code); openai|amazon-bedrock|custom (Codex)",
    )
    s.add_argument("--copy", action="store_true", help="copy the snippet(s) to the macOS clipboard (pbcopy)")
    s.add_argument("--print", action="store_true", help="print the snippet(s) (default)")
    s.add_argument("--check", action="store_true", help="flag snippets whose config keys are unverified or stale")
    s.add_argument("--max-age-days", type=int, default=90, help="with --check: max age of a key's verification")
    s.set_defaults(f=cmd_recommend)

    s = sub.add_parser("serve", help="localhost service: live report + JSON API, recalibrates on a schedule (lite)")
    _sim(s)
    _service_args(s)
    s.add_argument("--host", default="127.0.0.1", help="bind address (loopback only unless --unsafe-bind)")
    s.add_argument("--port", type=int, default=0, help="port (default: a free one)")
    s.add_argument("--unsafe-bind", action="store_true", help="allow a non-loopback bind (not recommended)")
    s.add_argument("--nice", type=int, default=10, help="lower the process priority by this much (default 10)")
    s.set_defaults(f=cmd_serve)

    s = sub.add_parser("recalibrate", help="one lite recalibration (what a scheduler runs), optional auto-apply")
    _sim(s)
    _service_args(s)
    s.set_defaults(f=cmd_recalibrate)

    s = sub.add_parser("schedule", help="print / install a launchd or systemd user schedule (never auto-loaded)")
    s.add_argument("action", choices=["print", "install", "status", "uninstall"])
    s.add_argument("--platform", choices=["auto", "launchd", "systemd"], default="auto")
    s.add_argument("--mode", choices=["serve", "recalibrate"], default="recalibrate")
    s.add_argument("--every", default="1d")
    s.add_argument("--state-dir", default=STATE)
    s.add_argument("--auto-apply", action="store_true", help="scheduled runs auto-apply (needs consent)")
    s.add_argument("--yes", action="store_true", help="install/uninstall without the prompt (the diff still prints)")
    s.set_defaults(f=cmd_schedule)

    s = sub.add_parser("consent", help="store, show or clear the auto-apply consent (scope: file, models, profile)")
    s.add_argument("action", choices=["set", "show", "clear"])
    s.add_argument("--harness", choices=["claude-code", "codex"], default="claude-code")
    s.add_argument("--file", help="the one settings file auto-apply may change")
    s.add_argument("--models", nargs="*", help="models whose key may change")
    s.add_argument("--profile", choices=PROFILE_NAMES, default=DEFAULT_PROFILE)
    s.add_argument("--expires", help="YYYY-MM-DD after which the consent lapses")
    s.add_argument("--state-dir", default=STATE)
    s.add_argument("--yes", action="store_true")
    s.set_defaults(f=cmd_consent)

    s = sub.add_parser("revert", help="undo an apply using its manifest")
    s.add_argument("--state-dir", default=STATE)
    s.add_argument("--id", help="manifest id (default: latest)")
    s.add_argument("--force", action="store_true", help="also revert keys edited after apply")
    s.set_defaults(f=cmd_revert)

    s = sub.add_parser("pricing", help="show effective prices, or refresh from a public dataset")
    s.add_argument("action", choices=["show", "refresh"])
    s.add_argument("--model", action="append")
    s.add_argument("--source", choices=sorted(SOURCES), default="models.dev")
    s.add_argument("--url", help="override the dataset URL (https only)")
    s.add_argument("--pricing")
    s.add_argument("--offline-pricing", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(f=cmd_pricing)

    s = sub.add_parser("cache", help="inspect or clear the scan cache")
    s.add_argument("action", choices=["info", "clear"])
    s.add_argument("--cache-dir", default=f"{STATE}/cache")
    s.add_argument("--json", action="store_true")
    s.set_defaults(f=cmd_cache)


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        rc: int = a.f(a)
        return rc
    except CLIError as e:
        print(f"compactlab: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.exit(main())
