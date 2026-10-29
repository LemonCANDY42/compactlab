#!/usr/bin/env python3
"""Regenerate docs/assets/* from synthetic demo data (never from real history).

  python scripts/make_assets.py [--chrome PATH]

Produces:
  docs/assets/report.png          first screen of a synthetic showcase report at 1440 px (headless Chrome), plus
                                  report-dark.png, report-overview.png, report-noop.png, report-mobile.png and
                                  report-mobile-dark.png (390 px)
  docs/assets/social-preview.png  1280x640 card for the GitHub social preview (headless Chrome)
  docs/assets/demo.svg            animated terminal of `compactlab demo` (pure SVG/CSS, no Chrome needed)
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "docs" / "assets"
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
    "chromium-browser",
]


def chrome(path: str | None) -> str:
    for c in [path, *CHROME_CANDIDATES]:
        if c and (Path(c).exists() or shutil.which(c)):
            return c
    sys.exit("headless Chrome/Chromium not found; pass --chrome PATH")


def shot(browser: str, url: str, out: Path, w: int, h: int, scale: int = 2, dark: bool = False) -> None:
    with tempfile.TemporaryDirectory() as prof:
        subprocess.run(
            [browser, "--headless=new", "--use-mock-keychain", "--password-store=basic", "--disable-gpu",
             "--no-first-run", "--allow-file-access-from-files", f"--user-data-dir={prof}",
             f"--blink-settings=preferredColorScheme={0 if dark else 1}",
             "--hide-scrollbars", "--virtual-time-budget=3000", f"--force-device-scale-factor={scale}",
             f"--window-size={w},{h}",
             f"--screenshot={out}", url],
            check=True, capture_output=True, timeout=120,
        )  # fmt: skip


def demo(tmp: Path) -> tuple[str, dict]:
    out = subprocess.run(
        [sys.executable, "-m", "compactlab.cli.main", "demo", "--out", str(tmp / "demo"), "--no-open", "--lang", "en"],
        check=True, capture_output=True, text=True, cwd=ROOT,
    )  # fmt: skip
    text = out.stdout.replace(str(tmp / "demo"), "compactlab-demo")
    return text, json.loads((tmp / "demo" / "result.json").read_text())


def _extra_project(
    root: Path, tag: str, models: tuple[str, ...], n_main: int, n_sub: int, seed: int, main_lines: int = 0
) -> None:
    """One more synthetic project next to the demo one, with its own session and message ids. `main_lines` cuts
    main sessions short (short sessions never reach the default threshold)."""
    from compactlab import synth

    tmp = root.parent / f"x{tag}"
    synth.write_claude_code(tmp, n_main=n_main, n_sub=n_sub, seed=seed, models=models)
    src = tmp / "-proj-demo"
    for p in sorted(src.rglob("*.jsonl")):
        rel = Path(*[q.replace("s0", tag + "0", 1) if q.startswith("s0") else q for q in p.relative_to(src).parts])
        out = root / f"-proj-{tag}" / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = p.read_text().replace("msg_s", f"msg_{tag}").replace("tu_s", f"tu_{tag}").splitlines()
        if main_lines and "subagents" not in rel.parts:
            lines = lines[:main_lines]
        out.write_text("\n".join(lines) + "\n")
    shutil.rmtree(tmp)


def showcase(tmp: Path) -> dict:
    """Synthetic history with many models: heavy groups that need tuning, a model with a dated id whose main and
    subagent runs never compact, a subagent-only model, and a Codex group. Never reads real data."""
    from compactlab import synth
    from compactlab.adapters import get_adapter
    from compactlab.pricing import PricingTable
    from compactlab.sim.engine import analyze

    root = tmp / "claude"
    synth.write_claude_code(root, n_main=12, n_sub=6)
    _extra_project(root, "h", ("claude-haiku-5-5-20260915",), 4, 3, 21, main_lines=90)
    _extra_project(root, "o", ("claude-opus-4-7", "claude-fable-5-1"), 6, 2, 31)
    _extra_project(root, "f", ("claude-sonnet-5",), 0, 4, 41)
    synth.write_codex(tmp / "codex", n=6)
    ss = list(get_adapter("claude-code")(root).sessions()) + list(get_adapter("codex")(tmp / "codex").sessions())
    # synthetic "current" settings on two models, so the current-setting path is shown
    cur = {("claude-code", "claude-opus-5-5"): (525_000, "t"), ("claude-code", "claude-sonnet-5-5"): (625_000, "t")}
    res = analyze(ss, PricingTable.load(offline=True), current=cur)
    res["demo"] = True
    return res


CROWD = {
    "claude-code": [
        f"claude-{f}-{v}" for f in ("opus", "sonnet", "haiku") for v in ("5-5", "5", "4-7", "4-5", "4-1", "4")
    ]
    + ["claude-fable-5-1", "claude-fable-5"],
    "codex": [f"gpt-{v}" for v in ("5-codex", "5.3-codex", "5.5", "5.5-mini", "5.3", "5", "5-mini", "4.1", "o4", "o3")],
    "example-cli": ["qwen-3-coder", "kimi-k2", "glm-4.6", "deepseek-v3.2", "mistral-dev-2"],
}


def crowd(res: dict) -> dict:
    """35 models over three harnesses for the sidebar at scale: copies of the showcase groups under other names
    (a UI fixture only; the numbers are the copied groups' own)."""
    import copy

    out = copy.deepcopy(res)
    src = res["groups"]
    out["groups"], out["sessions"] = [], []
    k = 0
    for h, models in CROWD.items():
        for m in models:
            base = src[k % len(src)]
            for g in [x for x in src if x["model"] == base["model"] and x["harness"] == base["harness"]]:
                ng = copy.deepcopy(g)
                ng["harness"], ng["model"] = h, m
                out["groups"].append(ng)
                for s_ in res["sessions"]:
                    if s_["model"] == g["model"] and s_["role"] == g["role"]:
                        out["sessions"].append({**s_, "id": f"{m}-{s_['id']}", "model": m, "harness": h})
            k += 1
    return out


def terminal_svg(text: str) -> str:
    # keep the hero short: first model/role block plus the closing lines
    blocks = text.split("\n\n")
    first = [ln for ln in blocks[1].splitlines() if not ln.strip().startswith("· default")]
    tail = [ln for ln in text.splitlines() if ln.startswith(("Profile shown first", "SYNTHETIC"))]
    lines = ["$ uvx compactlab demo --lang en", blocks[0], *first, "  ...", *tail]
    lh, pad, w = 22, 20, 1000
    h = pad * 2 + 30 + lh * len(lines)
    rows = []
    for i, ln in enumerate(lines):
        delay = 0.4 if i == 0 else 1.4 + 0.18 * i
        color = "#8b93ff" if i == 0 else ("#5eead4" if "SYNTHETIC" in ln else "#e6e8eb")
        rows.append(
            f'<text x="{pad}" y="{pad + 44 + i * lh}" fill="{color}" style="animation-delay:{delay:.2f}s">'
            f"{html.escape(ln)}</text>"
        )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img"
 aria-label="Terminal running compactlab demo on synthetic data">
<style>
text{{font:13.5px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre;opacity:0;
 animation:in .25s ease forwards}}
@keyframes in{{to{{opacity:1}}}}
@media (prefers-reduced-motion:reduce){{text{{animation:none;opacity:1}}}}
</style>
<rect width="{w}" height="{h}" rx="12" fill="#0e0f11"/>
<circle cx="22" cy="20" r="6" fill="#ff5f57"/><circle cx="42" cy="20" r="6" fill="#febc2e"/>
<circle cx="62" cy="20" r="6" fill="#28c840"/>
{"".join(rows)}
</svg>
"""


def social_html(result: dict) -> str:
    g = next(x for x in result["groups"] if x["role"] == "main")
    p = g["profiles"]
    k = lambda v: f"{v // 1000}k"  # noqa: E731
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;width:1280px;height:640px;background:#0e0f11;color:#ececed;
 font-family:-apple-system,"SF Pro Display",system-ui,sans-serif}}
.wrap{{padding:72px 80px;box-sizing:border-box;height:100%;display:flex;flex-direction:column;
 justify-content:space-between}}
h1{{font-size:88px;margin:0;letter-spacing:-.03em;font-weight:700}}
p{{font-size:34px;line-height:1.3;color:#b9bcc2;margin:18px 0 0;max-width:1000px}}
.row{{display:flex;gap:28px;align-items:flex-end}}
.card{{border:1px solid #2a2d33;border-radius:18px;padding:22px 28px;min-width:230px}}
.l{{font-size:20px;color:#8b8e94}}.v{{font-size:52px;font-weight:650;margin-top:6px}}
.a{{color:#8b93ff}}.b{{color:#5eead4}}.n{{font-size:18px;color:#6f737a;margin-left:auto;text-align:right}}
</style></head><body><div class="wrap">
<div><h1>compactlab</h1><p>Your agent history says where to auto-compact. Cost vs context-rot trade-offs per model
and role, then tune Claude Code / Codex safely.</p></div>
<div class="row">
<div class="card"><div class="l">cost</div><div class="v a">{k(p["cost"]["threshold"])}</div></div>
<div class="card"><div class="l">balanced (default)</div><div class="v b">{k(p["balanced"]["threshold"])}</div></div>
<div class="card"><div class="l">time</div><div class="v a">{k(p["time"]["threshold"])}</div></div>
<div class="n">offline · open source · MIT<br>numbers: synthetic demo data</div>
</div></div></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chrome")
    a = ap.parse_args()
    ASSETS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        text, result = demo(tmp)
        (ASSETS / "demo.svg").write_text(terminal_svg(text))
        b = chrome(a.chrome)
        from compactlab.report.html import render_html

        res = showcase(tmp / "showcase")
        noop = next(g for g in res["groups"] if g["no_action_needed"] and g["role"] == "main")
        (tmp / "showcase.html").write_text(render_html(res))
        page = (tmp / "showcase.html").as_uri()
        shot(b, page, ASSETS / "report.png", 1440, 1240)
        shot(b, page, ASSETS / "report-dark.png", 1440, 1240, dark=True)
        shot(b, page + "#overview", ASSETS / "report-overview.png", 1440, 1240)
        key = urllib.parse.quote("|".join([noop["harness"], noop["model"], noop["role"]]))
        shot(b, page + "#g=" + key, ASSETS / "report-noop.png", 1440, 760)
        # the sidebar at 35 models: harnesses folded, then a search
        (tmp / "crowd.html").write_text(render_html(crowd(res)))
        fold = "d.querySelectorAll('.har>.fold[aria-expanded=true]').forEach(b=>b.click())"
        find = "const q=d.getElementById('tq');q.value='opus';q.dispatchEvent(new Event('input'))"
        for name, js in (("report-tree.png", fold), ("report-tree-search.png", find)):
            h = tmp / f"{name}.html"
            h.write_text(f'<!doctype html><body style="margin:0"><iframe id="f" src="{(tmp / "crowd.html").as_uri()}" '
                         'style="width:1440px;height:1000px;border:0;display:block"></iframe><script>f.onload=()=>'
                         f'setTimeout(()=>{{const d=f.contentDocument;{js}}},300)</script>')  # fmt: skip
            shot(b, h.as_uri(), ASSETS / name, 1440, 1000)
        # headless Chrome will not go below ~500px wide, so the phone view is an iframe in a host page
        host = tmp / "mobile.html"
        host.write_text(f'<!doctype html><body style="margin:0"><iframe src="{page}" '
                        'style="width:390px;height:1600px;border:0;display:block"></iframe>')  # fmt: skip
        shot(b, host.as_uri(), ASSETS / "report-mobile.png", 390, 1600)
        shot(b, host.as_uri(), ASSETS / "report-mobile-dark.png", 390, 1600, dark=True)
        card = tmp / "social.html"
        card.write_text(social_html(result))
        shot(b, card.as_uri(), ASSETS / "social-preview.png", 1280, 640, scale=1)
    print("wrote", ", ".join(p.name for p in sorted(ASSETS.iterdir())))


if __name__ == "__main__":
    main()
