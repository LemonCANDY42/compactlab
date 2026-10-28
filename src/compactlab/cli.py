"""compactlab CLI: scan, simulate, report, apply, revert, retune, eval, demo."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from . import __version__, apply as ap, synth
from .adapters import adapter_names, get_adapter
from .aieval import CONFIRM_FLAG, EvalError, estimate_cost, load_client, run_eval
from .model import Session
from .pricing import PricingTable
from .report import render_html
from .simulate import SimOptions, analyze

DEFAULT_STATE = "~/.compactlab"


def _k(s: str) -> int:
    s = s.strip().lower()
    return int(float(s[:-1]) * 1000) if s.endswith("k") else int(float(s[:-1]) * 1_000_000) if s.endswith("m") else int(s)


def _add_source(p: argparse.ArgumentParser) -> None:
    p.add_argument("--harness", action="append", choices=adapter_names(), help="default: all")
    p.add_argument("--claude-root", help="default ~/.claude/projects")
    p.add_argument("--codex-root", help="default ~/.codex/sessions")
    p.add_argument("--since-days", type=float, default=30)


def _add_sim(p: argparse.ArgumentParser) -> None:
    _add_source(p)
    p.add_argument("--pricing", help="pricing override JSON (see src/compactlab/data/pricing.json)")
    p.add_argument("--grid", default="100k:1m:25k", help="start:stop:step (stop capped at each model's window)")
    p.add_argument("--recall-floor", type=float, default=0.90)
    p.add_argument("--cache-ttl", type=int, help="seconds; default from pricing file (300)")
    p.add_argument("--summary-tokens", type=_k)
    p.add_argument("--base-tokens", type=_k)


def load_sessions(a) -> list[Session]:
    roots = {"claude-code": a.claude_root, "codex": a.codex_root}
    out = []
    for h in a.harness or adapter_names():
        out += list(get_adapter(h)(roots.get(h)).sessions(a.since_days))
    return out


def run_sim(a) -> dict:
    lo, hi, st = (_k(x) for x in a.grid.split(":"))
    opts = SimOptions(grid_start=lo, grid_stop=hi, grid_step=st, recall_floor=a.recall_floor,
                      ttl_seconds=a.cache_ttl, summary_tokens=a.summary_tokens, base_tokens=a.base_tokens)
    return analyze(load_sessions(a), PricingTable.load(a.pricing), opts)


def _print_summary(res: dict) -> None:
    print(f"{'model':28} {'role':9} {'sess':>5} {'turns':>7} {'pick':>7} {'range':>13} {'recall':>7}  penalty")
    for g in res["groups"]:
        r = g["recommendation"]
        print(f"{g['model'][:28]:28} {g['role']:9} {g['n_sessions']:>5} {g['n_turns']:>7} {r['threshold']//1000:>6}k "
              f"{r['range'][0]//1000:>5}k-{r['range'][1]//1000:<5}k {r['recall_at_best']:>7.3f}  {g['penalty_params']['source']}"
              + ("" if r["floor_met"] else "  [recall floor NOT met]"))


def cmd_scan(a) -> int:
    ss = load_sessions(a)
    print(f"{len(ss)} sessions")
    by: dict = {}
    for s in ss:
        d = by.setdefault((s.harness, s.role, s.model), [0, 0, 0, 0])
        d[0] += 1; d[1] += len(s.turns); d[2] += len(s.compactions); d[3] = max(d[3], s.peak)
    print(f"{'harness':12} {'role':9} {'model':28} {'sess':>5} {'turns':>7} {'compact':>8} {'peak':>8}")
    for (h, r, m), d in sorted(by.items()):
        print(f"{h:12} {r:9} {m[:28]:28} {d[0]:>5} {d[1]:>7} {d[2]:>8} {d[3]//1000:>7}k")
    return 0


def cmd_simulate(a) -> int:
    res = run_sim(a)
    Path(a.out).write_text(json.dumps(res))
    _print_summary(res)
    print(f"\nwrote {a.out}")
    return 0


def cmd_report(a) -> int:
    res = json.loads(Path(a.result).read_text()) if a.result else run_sim(a)
    Path(a.out).write_text(render_html(res))
    print(f"wrote {a.out}")
    return 0


def _preauth(a) -> ap.Preauth:
    if a.preauth:
        return ap.load_preauth(a.preauth)
    if not a.i_authorize:
        raise ap.ApplyError("writing needs --preauth FILE or --i-authorize (with explicit --claude-settings/--codex-config, --models, --min, --max)")
    files = {}
    if a.claude_settings:
        files["claude-code"] = Path(a.claude_settings)
    if a.codex_config:
        files["codex"] = Path(a.codex_config)
    if not files or not a.models:
        raise ap.ApplyError("--i-authorize requires explicit config file paths and --models")
    return ap.explicit_preauth(files, a.models, a.min, a.max)


def _recs(res: dict, gear: str) -> list[dict]:
    return [{"harness": g["harness"], "model": g["model"], "value": g["gears"][gear] if gear != "recommended" else g["recommendation"]["threshold"]}
            for g in res["groups"] if g["role"] == "main"]


def _do_apply(a, res: dict, retune: bool = False) -> int:
    if a.write or a.preauth or retune:
        try:
            pre = _preauth(a) if (a.write or retune) else (ap.load_preauth(a.preauth) if a.preauth else None)
        except ap.ApplyError as e:
            print(f"refused: {e}", file=sys.stderr)
            return 2
    else:
        pre = None
    if retune and not (pre and pre.allow_retune):
        print("refused: retune needs a --preauth file with \"allow_retune\": true", file=sys.stderr)
        return 2
    if pre is None:  # dry-run without any authorization: show what an explicit flag would do
        files = {}
        if a.claude_settings:
            files["claude-code"] = Path(a.claude_settings)
        if a.codex_config:
            files["codex"] = Path(a.codex_config)
        pre = ap.explicit_preauth(files, a.models or [g["model"] for g in res["groups"]], a.min, a.max)
    try:
        changes, rejected = ap.plan_changes(_recs(res, a.gear), pre)
        rendered = ap.render(changes)
    except ap.ApplyError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    for r in rejected:
        print(f"skip: {r}")
    for ch in changes:
        print(f"{ch.harness}: {ch.key}: {ch.old!r} -> {ch.new}  ({ch.file})")
    if not changes:
        print("nothing to change")
        return 0
    print(ap.diff_text(rendered))
    for n in ap.formatting_note(rendered, changes):
        print("note:", n)
    if not a.write:
        print("dry-run: nothing written (add --write with authorization to apply)")
        return 0
    m = ap.write_changes(changes, Path(a.state_dir).expanduser(), pre)
    print(f"applied; manifest {m}\nrevert with: compactlab revert --state-dir {a.state_dir}")
    return 0


def _add_apply(p: argparse.ArgumentParser) -> None:
    p.add_argument("--gear", choices=["recommended", "eco", "balanced", "recall"], default="recommended")
    p.add_argument("--claude-settings", help="Claude Code settings.json to modify")
    p.add_argument("--codex-config", help="Codex config.toml to modify (global key, not per model)")
    p.add_argument("--preauth", help="pre-authorization JSON (allowlisted files/keys/models/bounds)")
    p.add_argument("--i-authorize", action="store_true", help="explicit authorization instead of a preauth file")
    p.add_argument("--models", nargs="*")
    p.add_argument("--min", type=_k, default=100_000)
    p.add_argument("--max", type=_k, default=1_000_000)
    p.add_argument("--write", action="store_true", help="actually write (default is dry-run)")
    p.add_argument("--state-dir", default=DEFAULT_STATE)


def cmd_apply(a) -> int:
    return _do_apply(a, json.loads(Path(a.result).read_text()))


def cmd_retune(a) -> int:
    return _do_apply(a, run_sim(a), retune=True)


def cmd_revert(a) -> int:
    sd = Path(a.state_dir).expanduser()
    try:
        m = sd / "manifests" / f"{a.id}.json" if a.id else ap.latest_manifest(sd)
        for line in ap.revert(m, a.force):
            print(line)
    except ap.ApplyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_eval(a) -> int:
    pricing = PricingTable.load(a.pricing)
    ss = [s for s in load_sessions(a) if s.compactions and any(c.summary_text for c in s.compactions)]
    targets = [(s, c) for s in ss for c in s.compactions if c.summary_text][: a.max_compactions]
    est = sum(estimate_cost(5, len(c.summary_text), a.answer_model, a.judge_model, a.repeats, pricing) for _, c in targets)
    print(f"{len(targets)} summaries; estimated cost ~${est:.4f}; hard budget cap ${a.budget_usd:.2f}")
    if not a.i_understand_this_calls_an_llm:
        print(f"not running: pass {CONFIRM_FLAG} to call the LLM client", file=sys.stderr)
        return 2
    try:
        client = load_client(a.client)
        for s, c in targets:
            rep = run_eval(s, c, client, answer_model=a.answer_model, judge_model=a.judge_model, pricing=pricing,
                           budget_usd=a.budget_usd, repeats=a.repeats, confirm=True)
            print(f"{s.id[:20]} @turn {c.turn_index}: exact {rep.exact_rate:.2f} judged {rep.judged_rate:.2f} "
                  f"flags {len(rep.flags)} spent ${rep.spent_usd:.4f}")
    except EvalError as e:
        print(f"stopped: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_demo(a) -> int:
    out = Path(a.out)
    synth.write_claude_code(out / "claude" / "projects", n_main=12, n_sub=6)
    synth.write_codex(out / "codex" / "sessions", n=6)
    ns = argparse.Namespace(harness=None, claude_root=str(out / "claude" / "projects"), codex_root=str(out / "codex" / "sessions"),
                            since_days=None, pricing=None, grid="100k:1m:25k", recall_floor=0.90, cache_ttl=None,
                            summary_tokens=None, base_tokens=None)
    res = run_sim(ns)
    (out / "result.json").write_text(json.dumps(res))
    (out / "report.html").write_text(render_html(res))
    _print_summary(res)
    print(f"\nsynthetic data only. wrote {out}/report.html")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap_ = argparse.ArgumentParser(prog="compactlab", description=__doc__)
    ap_.add_argument("--version", action="version", version=__version__)
    sub = ap_.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan", help="list sessions found"); _add_source(p); p.set_defaults(f=cmd_scan)
    p = sub.add_parser("simulate", help="threshold sweep -> result.json"); _add_sim(p)
    p.add_argument("--out", default="compactlab-result.json"); p.set_defaults(f=cmd_simulate)
    p = sub.add_parser("report", help="self-contained HTML report"); _add_sim(p)
    p.add_argument("--result"); p.add_argument("--out", default="compactlab-report.html"); p.set_defaults(f=cmd_report)
    p = sub.add_parser("apply", help="dry-run by default; --write needs authorization")
    p.add_argument("--result", required=True); _add_apply(p); p.set_defaults(f=cmd_apply)
    p = sub.add_parser("retune", help="re-simulate then apply, under the same pre-authorization")
    _add_sim(p); _add_apply(p); p.set_defaults(f=cmd_retune)
    p = sub.add_parser("revert", help="undo an apply from its manifest")
    p.add_argument("--state-dir", default=DEFAULT_STATE); p.add_argument("--id"); p.add_argument("--force", action="store_true")
    p.set_defaults(f=cmd_revert)
    p = sub.add_parser("eval", help="OPT-IN LLM evaluation of summaries (costs money)"); _add_sim(p)
    p.add_argument("--client", default="mock"); p.add_argument("--answer-model", default="claude-sonnet-5-5")
    p.add_argument("--judge-model", default="claude-haiku-5-5"); p.add_argument("--budget-usd", type=float, required=True)
    p.add_argument("--repeats", type=int, default=3); p.add_argument("--max-compactions", type=int, default=3)
    p.add_argument("--i-understand-this-calls-an-llm", action="store_true"); p.set_defaults(f=cmd_eval)
    p = sub.add_parser("demo", help="generate synthetic data + report"); p.add_argument("--out", default="compactlab-demo")
    p.set_defaults(f=cmd_demo)
    a = ap_.parse_args(argv)
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
