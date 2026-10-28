"""compactlab: measure where to auto-compact from your own agent history.

Modes: lite (default; offline, zero cost) and deep (opt-in; adds LLM-driven checks, needs --yes and --budget).
Tiers: headroom (default; a bit larger than the optimum for fewer lossy compactions) and standard (optimum).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
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
from ..sim.engine import SimOptions, analyze

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
    g.add_argument(
        "--headroom",
        type=float,
        default=0.12,
        help="headroom tier: max combined-score excess over optimum (default 0.12)",
    )
    g.add_argument("--headroom-cap", type=_k, help="never recommend a headroom threshold above this")
    g.add_argument("--cache-ttl", type=int, help="prompt-cache TTL in seconds (default from pricing file: 300)")
    g.add_argument("--summary-tokens", type=_k, help="compaction summary size (default: observed median, else 12k)")
    g.add_argument("--base-tokens", type=_k, help="post-compaction base context (default: observed, else 35k)")
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


def _roots(a: argparse.Namespace) -> dict[str, list[str] | None]:
    return {"claude-code": a.claude_root, "codex": a.codex_root}


def load_sessions(a: argparse.Namespace) -> list[Session]:
    cache = None if a.no_cache else ScanCache(a.cache_dir)
    out: list[Session] = []
    since = a.since_days or None
    for h in a.harness or adapter_names():
        out += list(get_adapter(h)(_roots(a).get(h)).sessions(since, cache))
    if cache:
        cache.save()
    return out


def _opts(a: argparse.Namespace) -> SimOptions:
    try:
        lo, hi, st = (_k(x) for x in a.grid.split(":"))
    except (ValueError, argparse.ArgumentTypeError) as e:
        raise CLIError(f"--grid must look like 100k:1m:25k ({e})") from e
    return SimOptions(
        grid_start=lo,
        grid_stop=hi,
        grid_step=st,
        recall_floor=a.recall_floor,
        ttl_seconds=a.cache_ttl,
        summary_tokens=a.summary_tokens,
        base_tokens=a.base_tokens,
        headroom_tolerance=a.headroom,
        headroom_step=st,
        headroom_cap=a.headroom_cap,
    )


def _pricing(a: argparse.Namespace) -> PricingTable:
    return PricingTable.load(getattr(a, "pricing", None), offline=getattr(a, "offline_pricing", False))


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
    res = analyze(sessions, pricing, _opts(a))
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


def _summary(res: dict[str, Any]) -> str:
    lines = [
        f"mode: {res['mode']}" + (f"  (deep spent ${res['deep']['spent_usd']:.2f})" if res.get("deep") else ""),
        f"{'model':22} {'role':8} {'sess':>4} {'turns':>6}  {'standard':>8}  {'headroom':>8}  "
        f"{'+cost':>6}  {'-compact':>8}  {'recall':>6}",
    ]
    for g in res["groups"]:
        st, hd = g["tiers"]["standard"], g["tiers"]["headroom"]
        lines.append(
            f"{g['model'][:22]:22} {g['role']:8} {g['n_sessions']:>4} {g['n_turns']:>6}  "
            f"{st['threshold'] // 1000:>7}k  {hd['threshold'] // 1000:>7}k  {hd['extra_cost_pct'] * 100:>5.1f}%  "
            f"{hd['fewer_compactions']:>8}  {hd['recall']:>6.3f}"
            + ("" if g["recommendation"]["floor_met"] else "  [recall floor not met]")
        )
    lines.append(
        "headroom (default) trades a bounded extra cost for fewer compactions; recall is an assumed proxy in lite mode."
    )
    return "\n".join(lines)


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
    Path(a.out).write_text(json.dumps(res, sort_keys=True))
    _out(
        a,
        {
            "result": a.out,
            "mode": res["mode"],
            "groups": [{k: g[k] for k in ("model", "role", "tiers", "recommendation")} for g in res["groups"]],
        },
        _summary(res) + f"\n\nwrote {a.out}",
    )
    return 0


def cmd_report(a: argparse.Namespace) -> int:
    res = json.loads(Path(a.result).read_text()) if a.result else run_analysis(a)
    if res is None:
        return 2
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
        "--tier",
        choices=["headroom", "standard", "eco", "recall"],
        default="headroom",
        help="which result to write (default headroom); pre-authorization bounds still win",
    )
    g.add_argument("--claude-settings", help="Claude Code settings.json to modify")
    g.add_argument("--codex-config", help="Codex config.toml to modify (one global key, not per model)")
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


def _recs(res: dict[str, Any], tier: str) -> list[dict[str, Any]]:
    out = []
    for g in res["groups"]:
        if g["role"] != "main":
            continue
        v = g["tiers"][tier]["threshold"] if tier in ("headroom", "standard") else g["gears"][tier]
        out.append({"harness": g["harness"], "model": g["model"], "value": v})
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
        changes, rejected = ap.plan_changes(_recs(res, a.tier), pre)
        rendered = ap.render(changes)
    except ap.ApplyError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    plan = {
        "tier": a.tier,
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
    return _do_apply(a, json.loads(Path(a.result).read_text()))


def cmd_retune(a: argparse.Namespace) -> int:
    if not a.preauth:
        print('refused: retune needs --preauth (with "allow_retune": true)', file=sys.stderr)
        return 2
    res = run_analysis(a)
    return 2 if res is None else _do_apply(a, res, retune=True)


def cmd_revert(a: argparse.Namespace) -> int:
    sd = Path(a.state_dir).expanduser()
    try:
        m = sd / "manifests" / f"{a.id}.json" if a.id else ap.latest_manifest(sd)
        for line in ap.revert(m, a.force):
            print(line)
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
    bins = {"claude-code": "claude", "codex": "codex"}
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
                        "jsonl_files": sum(1 for _ in r.rglob("*.jsonl")) if r.exists() else 0,
                    }
                    for r in roots
                ],
                "config": [{"path": str(c), "exists": c.exists()} for c in ad.config_paths()],
            }
        )
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
    lines.append(
        f"\npricing: bundled snapshot {report['pricing']['bundled']}; "
        f"refreshed: {'yes' if report['pricing']['refreshed'] else 'no'}"
    )
    lines.append(f"scan cache: {cf} ({'present' if cf.exists() else 'none'})")
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


def cmd_demo(a: argparse.Namespace) -> int:
    out = Path(a.out)
    synth.write_claude_code(out / "claude" / "projects", n_main=12, n_sub=6)
    synth.write_codex(out / "codex" / "sessions", n=6)
    ns = argparse.Namespace(
        harness=None,
        claude_root=[str(out / "claude" / "projects")],
        codex_root=[str(out / "codex" / "sessions")],
        since_days=0,
        no_cache=True,
        cache_dir="",
        pricing=None,
        offline_pricing=True,
        grid="100k:1m:25k",
        recall_floor=0.90,
        headroom=0.12,
        headroom_cap=None,
        cache_ttl=None,
        summary_tokens=None,
        base_tokens=None,
        mode="lite",
    )
    res = run_analysis(ns)
    assert res is not None
    res["demo"] = True
    (out / "result.json").write_text(json.dumps(res, sort_keys=True))
    (out / "report.html").write_text(render_html(res))
    print(_summary(res))
    print(f"\nSYNTHETIC DEMO DATA (not your history). Report: {out / 'report.html'}")
    if not a.no_open:
        _open(out / "report.html")
    return 0


def build_parser() -> argparse.ArgumentParser:
    fmt = argparse.RawDescriptionHelpFormatter
    p = argparse.ArgumentParser(prog="compactlab", description=__doc__, epilog=EXAMPLES, formatter_class=fmt)
    p.add_argument("--version", action="version", version=f"compactlab {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    s = sub.add_parser("demo", help="build and open a report from bundled synthetic data", formatter_class=fmt)
    s.add_argument("--out", default="compactlab-demo")
    s.add_argument("--no-open", action="store_true", help="do not open the report")
    s.set_defaults(f=cmd_demo)

    s = sub.add_parser("doctor", help="detect harnesses, log locations, config paths, versions")
    s.add_argument("--json", action="store_true")
    s.set_defaults(f=cmd_doctor)

    s = sub.add_parser("scan", help="list sessions found, per harness/role/model")
    _source(s)
    s.set_defaults(f=cmd_scan)

    s = sub.add_parser("simulate", help="threshold sweep -> result.json + summary (lite or deep)")
    _sim(s)
    s.add_argument("--out", default="compactlab-result.json")
    s.set_defaults(f=cmd_simulate)

    s = sub.add_parser("report", help="self-contained offline HTML report")
    _sim(s)
    s.add_argument("--result", help="render a saved result.json instead of re-running")
    s.add_argument("--out", default="compactlab-report.html")
    s.add_argument("--redact", action="store_true", help="replace session ids with short hashes")
    s.add_argument("--open", action="store_true", help="open the report when done")
    s.set_defaults(f=cmd_report)

    s = sub.add_parser("apply", help="write a tier to harness config (dry-run unless --write + authorization)")
    s.add_argument("--result", required=True)
    s.add_argument("--json", action="store_true")
    _apply_opts(s)
    s.set_defaults(f=cmd_apply)

    s = sub.add_parser("retune", help="re-analyze and apply under an existing pre-authorization")
    _sim(s)
    _apply_opts(s)
    s.set_defaults(f=cmd_retune)

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
    return p


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
