#!/usr/bin/env python3
"""Regenerate docs/assets/* from synthetic demo data (never from real history).

  python scripts/make_assets.py [--chrome PATH]

Produces:
  docs/assets/report.png          first screen of the demo report (headless Chrome), plus report-dark.png,
                                  report-mobile.png and report-mobile-dark.png
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
            [browser, f"--blink-settings=preferredColorScheme={0 if dark else 1}", "--allow-file-access-from-files", "--headless=new", "--use-mock-keychain", "--password-store=basic", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={prof}",
             "--hide-scrollbars", f"--force-device-scale-factor={scale}", f"--window-size={w},{h}",
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
        page = (tmp / "demo" / "report.html").as_uri()
        shot(b, page, ASSETS / "report.png", 1280, 1180)
        shot(b, page, ASSETS / "report-dark.png", 1280, 1180, dark=True)
        # headless Chrome will not go below ~500px wide, so the phone view is an iframe in a host page
        host = tmp / "mobile.html"
        host.write_text(f'<!doctype html><body style="margin:0"><iframe src="{page}" '
                        'style="width:390px;height:1500px;border:0;display:block"></iframe>')  # fmt: skip
        shot(b, host.as_uri(), ASSETS / "report-mobile.png", 390, 1500)
        shot(b, host.as_uri(), ASSETS / "report-mobile-dark.png", 390, 1500, dark=True)
        card = tmp / "social.html"
        card.write_text(social_html(result))
        shot(b, card.as_uri(), ASSETS / "social-preview.png", 1280, 640, scale=1)
    print("wrote", ", ".join(p.name for p in sorted(ASSETS.iterdir())))


if __name__ == "__main__":
    main()
