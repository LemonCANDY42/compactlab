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
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import __version__, synth
from .. import apply as ap
from ..adapters import adapter_names, get_adapter
from ..adapters.cache import ScanCache
from ..eval.deep import EvalError, MockClient, deep_analyze, load_client, plan_deep
from ..model import Session, drop_inherited
from ..pricing import DEFAULT_REFRESHED, PricingTable
from ..pricing.refresh import SOURCES, recent_changes, refresh
from ..report.html import render_html
from ..sim.baseline import MAX_CONTEXT_ENV, read_current, read_env_int, read_idle
from ..sim.engine import SimOptions, analyze, price_drift, reselect
from ..sim.select import DEFAULT_PROFILE, PROFILE_NAMES, PROFILES, WAIT_CAP_MIN_PER_DAY

STATE = "~/.compactlab"
EX_BUSY = 75  # sysexits EX_TEMPFAIL: another calibration or config write holds the state dir's lock; try again later
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
        "--opencode-root",
        action="append",
        help="OpenCode data dir or opencode.db (default $XDG_DATA_HOME/opencode or ~/.local/share/opencode)",
    )
    g.add_argument(
        "--gemini-root",
        action="append",
        help="Gemini CLI tmp dir (default $GEMINI_CLI_HOME/.gemini/tmp or ~/.gemini/tmp)",
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
    g.add_argument("--opencode-config", help="OpenCode opencode.json, read only (default: ~/.config/opencode/)")
    g.add_argument("--gemini-settings", help="Gemini CLI settings.json, read only (default: ~/.gemini/settings.json)")


def _roots(a: argparse.Namespace) -> dict[str, list[str] | None]:
    return {
        "claude-code": a.claude_root,
        "codex": a.codex_root,
        "dsh": a.dsh_root,
        "opencode": a.opencode_root,
        "gemini-cli": a.gemini_root,
    }


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
            first = len(out)
            for i, path in enumerate(files, 1):
                if i % 20 == 0 or i == len(files):
                    stats = "" if quiet or not cache else f" · cached {cache.stats['hit'] + cache.stats['resumed']}"
                    _progress(f"scanning {h} {i}/{len(files)} files{stats}")
                try:
                    found = ad.load(path, cache, cut)
                except OSError:
                    continue
                out += [s for s in found if s.turns]
            if h == "claude-code":  # a resumed or forked log repeats its parent's history
                out[first:] = drop_inherited(out[first:])
        if cache:
            cache.prune()  # rows of deleted logs
    finally:
        if cache:
            cache.close()
        _progress("")
    st = cache.stats if cache else {"hit": 0, "resumed": 0, "parsed": total}
    a.scan_stats = {"files": total, "cached": st["hit"] + st["resumed"], "parsed": st["parsed"]}
    return out


def _opts(a: argparse.Namespace) -> SimOptions:
    try:
        lo, hi, st = (_k(x) for x in a.grid.split(":"))
    except (ValueError, argparse.ArgumentTypeError) as e:
        raise CLIError(f"--grid must look like 100k:1m:25k ({e})") from e
    if a.max_threshold is not None and a.max_threshold < lo:
        raise CLIError(
            f"--max-threshold {a.max_threshold:,} is below the grid start {lo:,}: lower the grid start (--grid) or "
            "raise the cap"
        )
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


def _claude_settings_path(a: argparse.Namespace) -> Path | None:
    if getattr(a, "claude_settings", None):
        return Path(a.claude_settings).expanduser()
    return next((c for c in get_adapter("claude-code").config_paths() if c.exists()), None)


def _idle_opts(a: argparse.Namespace, opts: SimOptions) -> SimOptions:
    """Claude Code settings the replay needs, read-only: idle compaction (on with a 200k minimum when nothing says
    otherwise) and CLAUDE_CODE_MAX_CONTEXT_TOKENS (the window for model IDs Claude Code does not recognize)."""
    if getattr(a, "no_current", False):
        return opts
    path = _claude_settings_path(a)
    enabled, min_tokens, _ = read_idle(path)
    if enabled is not None:
        opts.idle_compaction = enabled
    if min_tokens:
        opts.idle_min_tokens = min_tokens
    opts.claude_max_context = read_env_int(path, MAX_CONTEXT_ENV)
    return opts


def _config_paths(a: argparse.Namespace) -> dict[str, Path | None]:
    """Each harness's config file: the one named on the command line, else the first default that exists."""
    paths: dict[str, Path | None] = {}
    for h, arg in (
        ("claude-code", a.claude_settings),
        ("codex", a.codex_config),
        ("opencode", getattr(a, "opencode_config", None)),
        ("gemini-cli", getattr(a, "gemini_settings", None)),
    ):
        if arg:
            paths[h] = Path(arg).expanduser()
        else:
            cands = get_adapter(h).config_paths() if h in adapter_names() else []
            paths[h] = next((c for c in cands if c.exists()), None)
    return paths


def _current(a: argparse.Namespace, sessions: list[Session]) -> dict[tuple[str, str], tuple[int | None, str]]:
    """Read the configured threshold per (harness, model) from config files, read-only."""
    if getattr(a, "no_current", False):
        return {}
    paths = _config_paths(a)
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
    drift = price_drift(res, PricingTable.load())
    if drift:
        what = "; ".join(f"{d['model']} {d['field']} {d['old']:g} -> {d['new']:g}" for d in drift[:6])
        more = f" (+{len(drift) - 6} more)" if len(drift) > 6 else ""
        print(f"note: prices or windows changed since {p.name} was made: {what}{more}. Its picks use the old figures; "
              "re-run `compactlab simulate` for today's.", file=sys.stderr)  # fmt: skip
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
    if quiet and not getattr(a, "offline_pricing", False) and not getattr(a, "pricing", None):
        from ..pricing.auto import maybe_refresh

        seen = {(t.model, s.platform) for s in sessions for t in s.turns}
        pricing, info = maybe_refresh(pricing, seen, time.time(), lambda: _pricing(a))
        if info:
            import logging

            moved = "; ".join(f"{c['model']} {c['field']} {c['old']} -> {c['new']}" for c in info["changes"])
            logging.getLogger("compactlab.service").info(
                "price refresh (%s%s): %s", info["reason"],
                f": {', '.join(info['missing'])}" if info["missing"] else "",
                f"from {info['source']}; changed for your models: {moved or 'nothing'}; "
                f"still unpriced: {', '.join(info['still_missing']) or 'none'}"
                if info["refreshed"] else f"failed ({info['error']})")  # fmt: skip

    def step(i: int, n: int, name: str) -> None:
        _progress(f"simulating {i}/{n}" + ("" if quiet else f": {name}"))

    res = analyze(sessions, pricing, _idle_opts(a, _opts(a)), current=_current(a, sessions), progress=step)
    _progress("")
    from ..pricing.auto import load_state

    res["model_notes"]["auto_refresh"] = bool(load_state().get("enabled"))
    # price and window changes the refreshes brought in the last 30 days, for the rows your models use
    used = {g["price_key"] for g in res["groups"]}
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
    res["model_notes"]["price_changes"] = [
        c for c in recent_changes(DEFAULT_REFRESHED, used, limit=50) if str(c.get("date", "")) >= since
    ]
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
ACTION_ZH = {"set": "设置", "keep default": "保持默认", "keep current": "保持当前", "no action needed": "无需调整",
             "-": "-"}  # fmt: skip


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
        sel_cmp = g["comparison"][g.get("profile") or "balanced"]
        if sel_cmp.get("global") and sel_cmp["action"] != "set":
            from ..term.views import keep_reason

            notes += [keep_reason(g, g.get("profile") or "balanced", x) for x in langs]
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
    from ..term.views import idle_lines, model_lines

    for x in langs:
        il = idle_lines(res, x)
        if il:
            lines += ["", *il]
        ml = model_lines(res, x)
        if ml:
            lines += ["", *ml]
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
        "keep_cur": "{p}: picks {t}, within the tolerance of your current {c}: recommend keeps your setting",
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
        "keep_cur": "{p}:选出 {t},与你当前的 {c} 相差在容差内,recommend 会保持你的设置",
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
            act_n = g["comparison"][n]["action"]
            if act_n in ("keep default", "keep current") and g["comparison"][n].get("global"):
                from ..term.views import keep_reason

                lines += [f"  · {T[x][n]}: {keep_reason(g, n, x)}" for x in langs]
            elif act_n in ("keep default", "keep current"):
                key = "keep" if act_n == "keep default" else "keep_cur"
                lines += ["  · " + IMPACT[x][key].format(p=T[x][n], t=_k_(g["profiles"][n]["threshold"]),
                                                         c=_k_((g.get("current") or {}).get("configured")))
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
    g.add_argument(
        "--expect-plan",
        metavar="HASH",
        help="refuse (exit 2, write nothing) unless the plan is the one a dry-run printed with this hash",
    )
    g.add_argument("--state-dir", default=STATE, help="backups, manifests and the lock (default ~/.compactlab)")


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
    """The selected profile's threshold per main-session model; groups where the default is fine are left alone. A
    harness with one setting for every model (Codex) gets one entry: the value chosen across its models."""
    out = []
    seen: set[str] = set()
    for g in res["groups"]:
        if g["role"] != "main" or g["comparison"][g["profile"]]["action"] in (
            "no action needed", "keep default", "keep current"):  # fmt: skip
            continue
        glob = (res.get("global_keys") or {}).get(g["harness"])
        if glob:
            if g["harness"] in seen:
                continue
            seen.add(g["harness"])
            out.append({"harness": g["harness"], "model": g["model"],
                        "value": glob["profiles"][g["profile"]]["threshold"]})  # fmt: skip
            continue
        out.append({"harness": g["harness"], "model": ap.model_key(g["harness"], g["model"]),
                    "value": g["profiles"][g["profile"]]["threshold"]})  # fmt: skip
    return out


def _apply_error(a: argparse.Namespace, code: str, text: str, plan: dict[str, Any] | None = None) -> None:
    """A refusal: the reason on stderr and, with --json, an `error` object ({code, message}) on stdout, next to the
    plan when there is one, so a caller never has to parse stderr. Codes: refused, plan_changed, busy."""
    print(f"{'busy' if code == 'busy' else 'refused'}: {text}", file=sys.stderr)
    if getattr(a, "json", False):
        _out(a, {**(plan or {}), "error": {"code": code, "message": text}}, "")


def _single_flight(a: argparse.Namespace, job: str, fn: Callable[[], int]) -> int:
    """Run `fn` holding the state dir's lock (service/lock.py). While another calibration or config write holds it,
    run nothing and exit EX_BUSY."""
    from ..service.lock import Busy, LockError, single_flight

    try:
        with single_flight(Path(a.state_dir).expanduser(), job):
            return fn()
    except Busy as e:
        _apply_error(a, "busy", str(e))
        return EX_BUSY
    except LockError as e:
        _apply_error(a, "refused", str(e))
        return 2


def _do_apply(a: argparse.Namespace, res: dict[str, Any], retune: bool = False) -> int:
    """Plan, show and (with --write) write. A write runs under the state dir's lock (the caller holds it), so the
    plan it checks against --expect-plan is the one it writes."""
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
        _apply_error(a, "refused", str(e))
        return 2
    h = ap.plan_hash(changes)
    plan = {
        "profile": res["groups"][0]["profile"] if res["groups"] else (a.profile or DEFAULT_PROFILE),
        "dry_run": not a.write,
        "changes": [
            {"harness": c.harness, "file": str(c.file), "key": c.key, "old": c.old, "new": c.new} for c in changes
        ],
        "skipped": rejected,
        "plan_hash": h,
    }
    text = [f"skip: {r}" for r in rejected]
    text += [f"{c.harness}: {c.key}: {c.old!r} -> {c.new}  ({c.file})" for c in changes]
    if changes:
        text.append(ap.diff_text(rendered))
        text += [f"note: {n}" for n in ap.formatting_note(rendered, changes)]
    else:
        text.append("nothing to change")
    text.append(f"plan {h}")
    want = None if a.expect_plan is None else a.expect_plan.strip().lower()
    if want is not None and want != h:  # an empty hash never matches
        if not a.json:
            print("\n".join(text))
        why = f"the plan is not the reviewed one (expected {want or '(empty)'}, now {h})"
        _apply_error(a, "plan_changed", f"{why}; nothing written: review this plan, then pass its hash", plan)
        return 2
    if changes and not a.write:
        text.append("dry-run: nothing written (add --write with authorization to apply)")
    if changes and a.write:
        m = ap.write_changes(changes, Path(a.state_dir).expanduser(), pre)
        plan["manifest"] = str(m)
        text.append(f"applied; manifest {m}\nundo with: compactlab revert --state-dir {a.state_dir}")
    _out(a, plan, "\n".join(text))
    return 0


def cmd_apply(a: argparse.Namespace) -> int:
    res = _reselect(a, _load_result(a.result))
    if not a.write:
        return _do_apply(a, res)  # a dry-run only reads: no lock
    return _single_flight(a, "apply --write", lambda: _do_apply(a, res))


def cmd_retune(a: argparse.Namespace) -> int:
    if not a.preauth:
        _apply_error(a, "refused", 'retune needs --preauth (with "allow_retune": true)')
        return 2

    def run() -> int:  # a calibration, then maybe a write: one job under the lock
        res = run_analysis(a)
        return 2 if res is None else _do_apply(a, res, retune=True)

    return _single_flight(a, "retune", run)


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

    def run() -> int:
        try:
            m = sd / "manifests" / f"{a.id}.json" if a.id else ap.latest_manifest(sd)
            for line in ap.revert(m, a.force):
                print(line)
            _forget_applied(sd, m.stem)
        except (ap.ApplyError, FileNotFoundError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        return 0

    return _single_flight(a, "revert", run)


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
    bins = {"claude-code": "claude", "codex": "codex", "dsh": "dsh", "opencode": "opencode", "gemini-cli": "gemini"}
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
                "checks": ad().doctor_checks(),
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
    sc = ScanCache(f"{STATE}/cache")
    report["cache"] = sc.info()
    sc.close()
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
        lines += [f"  {c['label']:6} {'ok ' if c['ok'] else '-- '} {c['text']}" for c in h["checks"]]
    lines.append(
        f"\npricing: bundled snapshot {report['pricing']['bundled']}; "
        f"refreshed: {'yes' if report['pricing']['refreshed'] else 'no'}"
    )
    lines.append(f"scan cache {_cache_line(report['cache'])}")
    t = _term(a)
    if t and not a.json:
        from ..term.views import doctor_view

        print(t[0].lines(doctor_view(report, t[1])))
        return 0
    _out(a, report, "\n".join(lines))
    return 0


def cmd_pricing(a: argparse.Namespace) -> int:
    if a.action == "auto":
        from ..pricing.auto import AUTO_FILE, load_state, set_enabled

        st = set_enabled(a.state == "on") if a.state in ("on", "off") else load_state()

        def when(k: str) -> str:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(st[k])) if st.get(k) else "never"

        text = [
            f"automatic price refresh: {'on' if st.get('enabled') else 'off'} ({AUTO_FILE})",
            "  when on, `serve` and scheduled runs download models.dev (then LiteLLM) when the logs show a model "
            "with no price row of its own; at most once a day; nothing about you is sent",
            f"  last attempt: {when('last_attempt')}; last success: {when('last_ok')}"
            + (f" from {st['last_source']}" if st.get("last_source") else ""),
        ]
        if st.get("still_missing"):
            text.append("  still not in the public data: " + ", ".join(st["still_missing"]))
        for c in recent_changes(DEFAULT_REFRESHED, limit=10):
            text.append(f"  changed {c['date']}: {c['model']} {c['field']} {c['old']} -> {c['new']}")
        if st.get("last_error"):
            text.append(f"  last error: {st['last_error']}")
        _out(a, st, "\n".join(text))
        return 0
    if a.action == "refresh":
        try:
            n = refresh(a.source, DEFAULT_REFRESHED, a.url)
        except Exception as e:  # network/parse errors: keep bundled pricing
            print(f"refresh failed, bundled pricing still in use: {e}", file=sys.stderr)
            return 2
        print(f"stored {n} model rows from {a.source} in {DEFAULT_REFRESHED}")
        return 0
    t = PricingTable.load(a.pricing, offline=a.offline_pricing)
    keys = a.model or sorted(k for k in t.data["models"] if ":" not in k)  # platform rows: ask with --platform
    rows = []
    for k in keys:
        p = t.lookup(k, a.platform)
        rows.append(
            {
                "model": k,
                "row": p.key,
                "match": p.kind,
                "platform": p.platform,
                "input": p.input,
                "output": p.output,
                "cache_write": p.cache_write,
                "cache_read": p.cache_read,
                "window": p.max_window,
                "long_above": p.long_above,
                "source": p.source,
            }
        )
    text = [f"{'model':24} {'row':24} {'match':8} {'in':>6} {'out':>6} {'cw':>6} {'cr':>6} {'window':>8}  source"]
    text += [
        f"{r['model'][:24]:24} {r['row'][:24]:24} {r['match']:8} {r['input']:>6g} {r['output']:>6g} "
        f"{r['cache_write']:>6g} {r['cache_read']:>6g} {r['window'] // 1000:>7}k  {r['source']}"
        for r in rows
    ]
    legend = (
        "USD per million tokens. match: exact = the model's own row; vendor = the vendor's list price for a platform "
        "with no row of its own; family = borrowed from the closest model of the same family; fallback = unknown."
    )
    _out(a, rows, "\n".join(text) + "\n" + legend)
    return 0


def _cache_line(info: dict[str, Any]) -> str:
    text = f"{info['path']}: " + (f"{info['files']} files, {info['bytes'] / 1e6:.1f} MB" if info["exists"] else "none")
    if info["old_bytes"]:
        text += f"; older cache versions {info['old_bytes'] / 1e6:.1f} MB (deleted by the next scan)"
    return text


def cmd_cache(a: argparse.Namespace) -> int:
    c = ScanCache(a.cache_dir)
    if a.action == "clear":
        gone = c.clear()
        text = f"removed {len(gone)} cache files from {c.dir}" if gone else f"no cache files in {c.dir}"
        _out(a, {"removed": [str(p) for p in gone]}, text)
    else:
        info = c.info()
        c.close()
        _out(a, info, f"scan cache {_cache_line(info)}")
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
    return Calibrator(
        analyze,
        state,
        every,
        lens_out=lens,
        consent=consent,
        rules=rules,
        dry_run=a.dry_run,
        consent_path=state / "consent.json" if a.auto_apply else None,
    )


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
            self.stream = sys.stderr  # stderr as it is now, not when logging was set up
            if self.stream.isatty():
                self.stream.write("\r\033[K")
            super().emit(record)

    for old in [h for h in lg.handlers if h.get_name() == "compactlab"]:  # set up once per process
        lg.removeHandler(old)
        old.close()
    for h in (RotatingFileHandler(state / "service" / "service.log", maxBytes=1_000_000, backupCount=3),
              Console(sys.stderr)):  # fmt: skip
        h.setFormatter(fmt)
        h.set_name("compactlab")
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
    if out["status"] == "busy":
        text = [f"recalibration: busy: {out.get('message') or 'a run is already in flight'}"]
    else:
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
    return 0 if out["status"] == "ok" else EX_BUSY if out["status"] == "busy" else 1


# ---------- engine: the JSON interface the app talks to (docs/ENGINE_API.md) ----------


def _engine(fn: Any) -> Any:
    """Every engine command prints one JSON object; failures are an `error` object on stdout too, so a caller never
    has to parse stderr (which keeps the log and tracebacks)."""

    def run(a: argparse.Namespace) -> int:
        from .. import engine as en

        try:
            msg, rc = fn(a)
        except Exception as e:
            msg, rc = _engine_failure(e)
        print(en.dumps(msg), flush=True)
        return rc

    return run


def _engine_failure(e: Exception) -> tuple[dict[str, Any], int]:
    from .. import engine as en

    if isinstance(e, EngineError):
        return en.error(e.code, str(e)), 1
    if isinstance(e, CLIError):  # a bad option
        return en.error("bad_args", str(e)), 2
    import traceback

    traceback.print_exc(file=sys.stderr)
    return en.error("internal", f"{type(e).__name__}: {e}"[:500]), 1


class EngineError(CLIError):
    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.code = code


def _engine_hello(a: argparse.Namespace) -> dict[str, Any]:
    from ..engine import LIVE_HARNESSES, hello

    roots, cfg = _roots(a), _config_paths(a)
    harnesses = []
    for name in adapter_names():
        ad = get_adapter(name)(roots.get(name))
        c = cfg.get(name) or next(iter(ad.config_paths()), None)
        harnesses.append({
            "name": name,
            "live": name in LIVE_HARNESSES,
            "roots": [{"path": str(r), "exists": r.exists()} for r in ad.roots],
            "config": {"path": str(c), "exists": c.exists()} if c else None,
        })  # fmt: skip
    return hello(harnesses, Path(a.state_dir).expanduser())


def _live(a: argparse.Namespace) -> Any:
    from ..engine import LIVE_HARNESSES
    from ..engine.live import LiveSessions

    # a config file that does not exist yet is still watched: `apply` may create it while this runs
    cfg = {h: p or next(iter(get_adapter(h).config_paths()), None) for h, p in _config_paths(a).items()}
    return LiveSessions(
        _roots(a),
        cfg,
        _pricing(a),
        time.time,
        active_s=a.active_hours * 3600,
        project_names=not a.no_project_names,
        harnesses=tuple(h for h in LIVE_HARNESSES if not a.harness or h in a.harness),
    )


@_engine
def cmd_engine_hello(a: argparse.Namespace) -> tuple[dict[str, Any], int]:
    return _engine_hello(a), 0


@_engine
def cmd_engine_sessions(a: argparse.Namespace) -> tuple[dict[str, Any], int]:
    return _live(a).snapshot(), 0


def cmd_engine_watch(a: argparse.Namespace) -> int:
    from ..engine import dumps
    from ..engine.watch import run

    try:
        live, hello = _live(a), _engine_hello(a)
    except Exception as e:  # e.g. an unreadable --pricing file: say so in JSON like the one-shot commands
        msg, rc = _engine_failure(e)
        print(dumps(msg), flush=True)
        return rc
    try:
        return run(live.snapshot, hello, sys.stdin, sys.stdout, a.min_interval, a.interval)
    except BrokenPipeError:  # the app went away; keep the interpreter's final flush from failing too
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


@_engine
def cmd_engine_result(a: argparse.Namespace) -> tuple[dict[str, Any], int]:
    from ..engine import result_message, result_path

    state = Path(a.state_dir).expanduser()
    path = Path(a.result).expanduser() if a.result else result_path(state)
    if not path.exists():
        raise EngineError("no_result", f"no calibration yet ({path}); run: compactlab engine calibrate")
    try:
        res = _load_result(str(path))
    except (CLIError, ValueError, KeyError) as e:
        raise EngineError("bad_result", str(e)) from e
    if a.profile:
        reselect(res, a.profile)
    return result_message(res, state), 0


@_engine
def cmd_engine_calibrate(a: argparse.Namespace) -> tuple[dict[str, Any], int]:
    from ..engine import result_message

    cal = _calibrator(a)
    state = Path(a.state_dir).expanduser()
    _service_logging(state)
    out = cal.recalibrate(a.reason)
    if out["status"] == "busy":
        raise EngineError("busy", str(out.get("message") or "a calibration is already running"))
    if out["status"] != "ok" or cal.result is None:
        raise EngineError("calibration_failed", str(out.get("error") or "no result"))
    return result_message(cal.result, state, out.get("auto_apply")), 0


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
    c = Consent(a.harness, str(ap._p(a.file)), list(a.models), a.profile, expires=a.expires)
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


def cmd_share(a: argparse.Namespace) -> int:
    """Write an anonymous record of the estimated savings for people to post; never sends anything itself."""
    from .. import share

    if a.check:
        try:
            rec = share.parse_issue_body(Path(a.check).expanduser().read_text())
        except (OSError, share.ShareError) as e:
            raise CLIError(str(e)) from e
        bad = share.validate(rec)
        if bad:
            print("not acceptable:\n  " + "\n  ".join(bad), file=sys.stderr)
            return 1
        print("ok: " + share.preview(rec))
        return 0
    res = _load_result(a.result) if a.result else run_analysis(a)
    if res is None:
        return 2
    verification = None
    if a.verify:
        from .. import verify as vf

        sessions = load_sessions(a)
        try:
            m = (
                Path(a.state_dir).expanduser() / "manifests" / f"{a.id}.json"
                if a.id
                else ap.latest_manifest(Path(a.state_dir).expanduser())
            )
            manifest = json.loads(m.read_text())
        except (ap.ApplyError, OSError, ValueError) as e:
            raise CLIError(f"--verify needs a change made with compactlab apply: {e}") from e
        verification = vf.verify(sessions, manifest, _pricing(a), opts=_idle_opts(a, SimOptions()))
        if not any(g["verdict"] != "no_data" for g in verification["groups"]):
            raise CLIError("--verify: no sessions on both sides of the change yet; share without --verify")
    try:
        rec = share.build(res, verification=verification)
    except share.ShareError as e:
        raise CLIError(str(e)) from e
    if verification and not any(g["verification"] for g in rec["groups"]):
        raise CLIError("--verify: the verified models are not among the shared groups; share without --verify")
    text = json.dumps(rec, indent=2, sort_keys=True)
    out = Path(a.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n")
    if a.json:
        print(text)
    else:
        print(share.preview(rec))
        print(f"\nwrote {out}: numbers and model names only; read it before you post it.")
        print(f"It holds no session ids, paths, project names, timestamps or text. Post it at:\n  {share.ISSUE_URL}")
    if a.copy:
        if not shutil.which("pbcopy"):
            raise CLIError("--copy needs pbcopy (macOS); open the file and copy it by hand")
        subprocess.run(["pbcopy"], input=text, text=True, check=True)
        print("copied the record to the clipboard", file=sys.stderr)
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    """Compare what a change was predicted to do with what the logs show since it was applied."""
    from .. import verify as vf

    note = None
    if a.applied_at:
        if a.id or a.new is None:
            raise CLIError(
                "--applied-at describes a change made by hand: give --new (and --model for Claude Code), not --id"
            )
        harness = a.harness[0] if a.harness and len(a.harness) == 1 else "claude-code"
        if harness in ("claude-code", "opencode", "gemini-cli") and not a.model:
            raise CLIError(
                "--model is needed for a Claude Code, OpenCode or Gemini CLI change (the threshold is per model; "
                "OpenCode models are named provider/model, as `compactlab scan` lists them)"
            )
        try:
            manifest = vf.manual_manifest(vf.parse_when(a.applied_at), harness, a.model or "", a.new, a.old)
        except ValueError as e:
            raise CLIError(str(e)) from e
        m = Path("manual")
        note = (
            "Change made by hand: the time and the old value are as given"
            + (
                f" (old {a.old // 1000}k)"
                if a.old
                else " (old = the harness default; pass --old if it was something else)"
            )
            + "."
        )
    else:
        sd = Path(a.state_dir).expanduser()
        try:
            m = sd / "manifests" / f"{a.id}.json" if a.id else ap.latest_manifest(sd)
            manifest = json.loads(m.read_text())
        except (ap.ApplyError, OSError, ValueError) as e:
            raise CLIError(
                f"no usable apply manifest: {e}. Verify needs a change made with compactlab apply, "
                "or describe a manual one with --applied-at, --model and --new."
            ) from e
    sessions = load_sessions(a)
    if not sessions:
        raise CLIError("no sessions found; check `compactlab doctor`, or widen --since-days (0 = all)")
    res = vf.verify(sessions, manifest, _pricing(a), min_sessions=a.min_sessions, min_days=a.min_days,
                    opts=_idle_opts(a, SimOptions()))  # fmt: skip
    res["manifest"] = m.stem
    if note:
        res["note"] = note
    _out(a, res, vf.render(res))
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
         "--harness", "claude-code", "--harness", "codex",  # the other harnesses' default roots are real logs
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

    s = sub.add_parser(
        "verify", help="after apply: compare the predicted cost and compactions with what the logs show since"
    )
    _source(s)
    s.set_defaults(since_days=60)
    s.add_argument("--id", help="manifest id (default: the latest apply)")
    s.add_argument("--state-dir", default=STATE, help="backups and manifests (default ~/.compactlab)")
    h = s.add_argument_group("a change made by hand (instead of an apply manifest)")
    h.add_argument(
        "--applied-at", metavar="WHEN", help="when it took effect: 2026-10-29 or 2026-10-29T21:05 (local time)"
    )
    h.add_argument(
        "--model",
        help="the model the change is checked for (Claude Code: its modelSettings key; OpenCode: provider/model; "
        "Gemini CLI: --new is that model's token threshold)",
    )
    h.add_argument("--new", type=_k, help="the threshold you set, e.g. 625k")
    h.add_argument("--old", type=_k, help="the threshold before (default: the harness default)")
    s.add_argument("--pricing", help="pricing override JSON")
    s.add_argument("--offline-pricing", action="store_true", help="ignore refreshed prices")
    s.add_argument("--min-sessions", type=int, default=3, help="sessions needed on each side (default 3)")
    s.add_argument("--min-days", type=int, default=2, help="active days needed on each side (default 2)")
    s.set_defaults(f=cmd_verify)

    s = sub.add_parser(
        "share", help="anonymous record of your estimated savings to post for research (writes a file; sends nothing)"
    )
    _sim(s)
    s.add_argument("--result", help="use a saved result.json instead of re-running")
    s.add_argument("--out", default="compactlab-share.json", help="where to write the record")
    s.add_argument("--copy", action="store_true", help="also copy the record to the macOS clipboard (pbcopy)")
    s.add_argument("--check", metavar="FILE", help="validate a record (or an issue body) instead of building one")
    s.add_argument("--verify", action="store_true",
                   help="add what `compactlab verify` found since the last apply (predicted vs logged)")  # fmt: skip
    s.add_argument("--id", help="with --verify: manifest id (default: the latest apply)")
    s.add_argument("--state-dir", default=STATE, help="with --verify: backups and manifests (default ~/.compactlab)")
    s.set_defaults(f=cmd_share)

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

    s = sub.add_parser("engine", help="JSON interface for the compactlab app and other tools (docs/ENGINE_API.md)")
    es = s.add_subparsers(dest="engine_cmd", required=True, metavar="engine-command")
    e = es.add_parser("hello", help="engine and contract version, harness log and config locations")
    _source(e)
    _config_args(e)
    e.add_argument("--state-dir", default=STATE)
    e.set_defaults(f=cmd_engine_hello)
    for name, text in (
        ("sessions", "the active sessions now: context, model, threshold in force, cache expiry"),
        ("watch", "active sessions as JSON Lines on stdout while running; commands on stdin"),
    ):
        e = es.add_parser(name, help=text)
        _source(e)
        _config_args(e)
        e.add_argument("--state-dir", default=STATE)
        e.add_argument("--active-hours", type=float, default=6.0, help="sessions written in the last N hours (6)")
        e.add_argument("--no-project-names", action="store_true", help="leave out the working directory's name")
        e.add_argument("--pricing", help="pricing override JSON (windows of models the logs do not size)")
        e.add_argument("--offline-pricing", action="store_true", help="ignore refreshed prices and windows")
    e.set_defaults(f=cmd_engine_watch)
    e.add_argument("--min-interval", type=float, default=5.0, help="seconds between scans at most (default 5)")
    e.add_argument("--interval", type=float, default=60.0, help="scan anyway after N quiet seconds (0 = never)")
    es.choices["sessions"].set_defaults(f=cmd_engine_sessions)
    e = es.add_parser("result", help="the last calibration, summarized for the threshold table")
    e.add_argument("--state-dir", default=STATE)
    e.add_argument("--result", help="a result.json (default: the calibrator's, <state-dir>/service/result.json)")
    e.add_argument("--profile", choices=PROFILE_NAMES, help="select this profile (no replay)")
    e.set_defaults(f=cmd_engine_result)
    e = es.add_parser("calibrate", help="one lite calibration (like recalibrate), then its summary")
    _sim(e)
    _service_args(e)
    e.add_argument("--reason", default="engine", help="recorded with the calibration (e.g. app, schedule)")
    e.set_defaults(f=cmd_engine_calibrate)

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

    s = sub.add_parser(
        "pricing",
        help="show effective prices, refresh from a public dataset, or turn on automatic refresh for new models",
    )
    s.add_argument("action", choices=["show", "refresh", "auto"])
    s.add_argument("state", nargs="?", choices=["on", "off", "status"], help="for `auto`: on, off or status (default)")
    s.add_argument("--model", action="append")
    s.add_argument(
        "--platform", help="price on this platform: amazon-bedrock, google-vertex-anthropic, azure, openrouter"
    )
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
