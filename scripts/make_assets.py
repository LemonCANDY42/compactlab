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
import os
import re
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


def run_cli(args: list[str], cols: int) -> str:
    """The real CLI in colour (truecolor) at a fixed width, as a terminal would show it."""
    env = {**os.environ, "COLUMNS": str(cols), "LINES": "60", "COLORTERM": "truecolor", "PYTHONUTF8": "1"}
    env.pop("NO_COLOR", None)
    out = subprocess.run([sys.executable, "-m", "compactlab.cli.main", *args, "--color", "always"],
                         check=True, capture_output=True, text=True, cwd=ROOT, env=env)  # fmt: skip
    return out.stdout


def demo(tmp: Path) -> tuple[str, dict]:
    text = run_cli(["demo", "--out", str(tmp / "demo"), "--no-open", "--lang", "en"], 100)
    text = text.replace(str(tmp / "demo"), "compactlab-demo")
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
    # one dominant model: five projects on the same four days (about five heavy sessions a day, several $100/day)
    for j, tag in enumerate("pqruv"):
        _extra_project(root, tag, ("claude-opus-5",), 4, 0, 61 + j)
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


SGR = re.compile(r"\x1b\[([0-9;]*)m")
BASIC = {30: "#1a1915", 31: "#d97757", 32: "#5fb37f", 33: "#e3a548", 37: "#ecebe4", 90: "#8f8c83", 91: "#f2685c",
         97: "#ffffff"}  # fmt: skip


def ansi_lines(text: str) -> list[list[tuple[str, str, bool]]]:
    """ANSI text -> per line [(text, colour, bold)] (SGR colour and bold only, which is all the renderer emits)."""
    out = []
    for raw in text.splitlines():
        segs, col, bold, pos = [], "#ecebe4", False, 0
        for m in SGR.finditer(raw):
            if m.start() > pos:
                segs.append((raw[pos : m.start()], col, bold))
            codes = [int(c) for c in m.group(1).split(";") if c] or [0]
            k = 0
            while k < len(codes):
                c = codes[k]
                if c == 0:
                    col, bold = "#ecebe4", False
                elif c == 1:
                    bold = True
                elif c == 38 and codes[k + 1 : k + 2] == [2]:
                    col = "#" + "".join(f"{v:02x}" for v in codes[k + 2 : k + 5])
                    k += 4
                elif c == 38 and codes[k + 1 : k + 2] == [5]:
                    k += 2
                elif c in BASIC:
                    col = BASIC[c]
                k += 1
            pos = m.end()
        if pos < len(raw):
            segs.append((raw[pos:], col, bold))
        out.append(segs)
    return out


def terminal_svg(text: str, title: str, cols: int, step: float = 0.035) -> str:
    """A terminal window drawn from real ANSI output, robust in any SVG renderer.

    Every run of non-blank text (words joined by single spaces) is placed at its own column, x = col * cell width,
    and stretched to its cell width, so spacing never depends on whitespace handling and box drawing, blocks and
    braille line up whatever the font. Text is visible by default: the fade-in only runs where CSS animations do
    (`both` fill from opacity 0), so rsvg-convert, Quick Look and previews show the whole frame."""
    from compactlab.term.style import cw

    lines = ansi_lines(text)
    cwid, lh, pad, top = 8.4, 19, 18, 40
    w, h = int(cols * cwid + pad * 2), int(top + lh * len(lines) + pad)
    rows = []
    for i, segs in enumerate(lines):
        x, spans = 0, []
        for t, col, bold in segs:
            for m in re.finditer(r"\S+(?: \S+)*", t):
                run = m.group(0)
                c0 = x + cw(t[: m.start()])
                weight = ' font-weight="700"' if bold else ""
                spans.append(
                    f'<tspan x="{pad + c0 * cwid:.1f}" textLength="{cw(run) * cwid:.1f}" '
                    f'lengthAdjust="spacingAndGlyphs" fill="{col}"{weight}>{html.escape(run)}</tspan>'
                )
            x += cw(t)
        if spans:
            rows.append(
                f'<text class="r" y="{top + i * lh + 14}" style="animation-delay:{0.2 + i * step:.2f}s">'
                f"{''.join(spans)}</text>"
            )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" xml:space="preserve" viewBox="0 0 {w} {h}"
 width="{w}" height="{h}" role="img" aria-label="{html.escape(title)} (synthetic data)">
<style>
text{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"DejaVu Sans Mono",monospace;font-size:13.5px;
 white-space:pre}}
.r{{animation:in .3s ease both}}
@keyframes in{{from{{opacity:0}}}}
@media (prefers-reduced-motion:reduce){{.r{{animation:none}}}}
</style>
<rect width="{w}" height="{h}" rx="12" fill="#1a1915"/>
<circle cx="22" cy="20" r="6" fill="#ff5f57"/><circle cx="42" cy="20" r="6" fill="#febc2e"/>
<circle cx="62" cy="20" r="6" fill="#28c840"/>
<text x="{w / 2:.0f}" y="24" text-anchor="middle" fill="#a8a59b" font-size="12">{html.escape(title)}</text>
{chr(10).join(rows)}
</svg>
"""


def hero(text: str) -> str:
    """The demo's header, its first group panel, the no-change line and the recommendations: a short hero."""
    plain = [SGR.sub("", ln) for ln in text.splitlines()]
    keep = list(range(0, next(i for i, ln in enumerate(plain) if ln.startswith("╰")) + 1))
    rest = next((i for i, ln in enumerate(plain) if ln.startswith("✓")), None)
    if rest is not None:
        keep += [-1, *range(rest, len(plain))]
    src = text.splitlines()
    return "\n".join("" if i == -1 else src[i] for i in keep)


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
        (ASSETS / "demo.svg").write_text(terminal_svg(hero(text), "compactlab demo", 100))
        top = run_cli(["top", "--demo", "--once", "--width", "112", "--height", "30"], 112)
        (ASSETS / "top.svg").write_text(terminal_svg(top, "compactlab top --demo", 112, step=0.012))
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
        dots = "d.querySelector('[data-ovst=dots]').click();d.getElementById('fOv').scrollIntoView()"
        h = tmp / "dots.html"
        h.write_text(f'<!doctype html><body style="margin:0"><iframe id="f" src="{page}#overview" '
                     'style="width:1440px;height:1400px;border:0;display:block"></iframe><script>f.onload=()=>'
                     f'setTimeout(()=>{{const d=f.contentDocument;{dots}}},300)</script>')  # fmt: skip
        shot(b, h.as_uri(), ASSETS / "report-overview-dots.png", 1440, 1100)
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
