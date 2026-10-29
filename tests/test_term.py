"""Terminal renderer: widths 30..200, plain vs colour, CJK width, golden widget snapshots, `top` on a fake screen.

Goldens live in tests/golden/; regenerate them with UPDATE_GOLDEN=1 after an intended change.
"""

import os
import re
from pathlib import Path

import pytest

from compactlab.adapters import get_adapter
from compactlab.cli import main as cli_main
from compactlab.pricing import PricingTable
from compactlab.sim.engine import analyze
from compactlab.term import top as tp
from compactlab.term.style import Style, clip, cw, pad, plain, width
from compactlab.term.views import doctor_view, recs_table, summary_view
from compactlab.term.widgets import braille, gauge, header, panel, spark, was_now

GOLDEN = Path(__file__).parent / "golden"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(scope="module")
def res(claude_root, codex_root):
    ss = list(get_adapter("claude-code")(claude_root).sessions()) + list(get_adapter("codex")(codex_root).sessions())
    return analyze(ss, PricingTable.load(offline=True), current={("claude-code", "claude-opus-5-5"): (600_000, "t")})


def golden(name: str, text: str) -> None:
    p = GOLDEN / f"{name}.txt"
    if os.environ.get("UPDATE_GOLDEN") or not p.exists():
        p.parent.mkdir(exist_ok=True)
        p.write_text(text)
    assert text == p.read_text(), f"{name} changed; UPDATE_GOLDEN=1 to accept"


def test_display_width_counts_cjk_and_never_splits_a_wide_glyph():
    assert cw("均衡") == 4 and cw("abc") == 3 and cw("▼ −21%") == 6 and cw("é") == 1
    line = [("均衡方案", "ink"), (" 450k", "dim")]
    assert width(clip(line, 5)) <= 5 and plain(clip(line, 5)).endswith("…")
    assert width(pad(line, 20)) == 20 and width(pad(line, 6)) == 6


@pytest.mark.parametrize("cols", [40, 80, 120])
def test_widgets_golden(cols):
    s = Style(0)
    out = []
    out += header([("history", [("2026-09-01 → 09-18", "ink")]), ("impact", [("▼ −24% −$185.3/day", "good_b")])], cols)
    out += gauge(cols, 100_000, 1_000_000, [(967_000, "┊", "dim"), (175_000, "$", "acc"), (450_000, "◆", "acc_b")],
                 [(700_000, 1_000_000)])  # fmt: skip
    out += list(was_now(619.3, 491.8, cols - 10)) + list(was_now(653, 673, cols - 10))
    out.append(spark([1, 2, 3, 5, 8, 13, 21, 34, 55], cols, 4))
    out += braille([([0, 1, 4, 9, 16], "acc"), ([16, 9, 4, 1, 0], "dim")], cols - 2, 4, 0, 16, [(3, "dim")])
    out += panel([("Opus 5 · main", "title")], [[("均衡 450k ▼ −21% cost", "ink")]], cols)
    assert all(width(x) <= cols for x in out), [width(x) for x in out]
    golden(f"widgets_{cols}", s.lines(out) + "\n")


@pytest.mark.parametrize("lang", ["en", "zh"])
@pytest.mark.parametrize("cols", [30, 40, 80, 120, 200])
def test_summary_fits_every_width_plain_and_colour(res, cols, lang):
    lines = summary_view(res, cols, lang)
    assert lines and all(width(x) <= cols for x in lines), max(width(x) for x in lines)
    plain_text = Style(0).lines(lines)
    colour = Style(24).lines(lines)
    assert "\x1b[" not in plain_text and "\x1b[38;2;" in colour
    assert [ln.rstrip() for ln in ANSI.sub("", colour).splitlines()] == [ln.rstrip() for ln in plain_text.splitlines()]
    assert "▼ −" in plain_text or "▲ +" in plain_text  # deltas carry arrow and sign
    for depth, marker in ((256, "\x1b[38;5;"), (16, "\x1b[3")):
        assert marker in Style(depth).lines(lines)


def test_recs_and_doctor_fit(res):
    for cols in (40, 80, 120):
        assert all(width(x) <= cols for x in recs_table(res, cols))
    rep = {"compactlab": "0", "python": "3.12", "pricing": {"bundled": "2026-09", "refreshed": False},
           "cache": {"exists": False, "bytes": 0},
           "harnesses": [{"name": "claude-code", "cli": None, "log_roots": [{"path": "/x/" + "y" * 90, "exists": True,
                          "jsonl_files": 3}], "config": [{"path": "/c", "exists": False}]}]}  # fmt: skip
    for cols in (40, 80):
        assert all(width(x) <= cols for x in doctor_view(rep, cols))


def test_colour_detection(monkeypatch):
    class Tty:
        def isatty(self):
            return True

    for k in ("NO_COLOR", "COLORTERM", "TERM"):
        monkeypatch.delenv(k, raising=False)
    assert Style.detect(None).depth == 0  # not a terminal
    monkeypatch.setenv("TERM", "xterm-256color")
    assert Style.detect(Tty()).depth == 256
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert Style.detect(Tty()).depth == 24
    monkeypatch.setenv("NO_COLOR", "1")
    assert Style.detect(Tty()).depth == 0 and Style.detect(Tty(), "always").depth == 24
    assert Style.detect(Tty(), "never").depth == 0


# ---------- top: the frame and the keys, without a terminal ----------


@pytest.mark.parametrize("size", [(30, 10), (60, 24), (80, 24), (120, 36), (200, 50)])
def test_top_frame_is_exactly_the_screen(res, size):
    st = tp.TopState(res=res, sel=1)
    for mode in ("normal", "help", "filter"):
        st.mode = mode
        f = tp.frame(st, *size)
        assert len(f) == size[1] and all(width(x) == size[0] for x in f), (mode, [width(x) for x in f])


def test_top_keys(res, monkeypatch):
    st = tp.TopState(res=res)
    rows = tp.rows(st)
    assert rows[0][0] == "h" and any(k == "g" for k, _ in rows)
    tp.handle(st, "j")
    assert st.sel == 1 and tp.selected(st)[0] == "g"
    tp.handle(st, "k")
    assert st.sel == 0
    h = rows[0][1]
    tp.handle(st, "h")  # fold the harness
    assert h in st.folded and len(tp.rows(st)) < len(rows)
    tp.handle(st, "l")
    assert h not in st.folded
    for want in ("cost", "balanced", "time", None):
        tp.handle(st, "p")
        assert st.profile == want
    tp.handle(st, "%")
    assert st.unit == "usd"
    tp.handle(st, "/")
    for ch in "zzz":
        tp.handle(st, ch)
    assert st.filt == "zzz" and not tp.rows(st)
    assert any("no models match" in plain(x) for x in tp.frame(st, 100, 24))
    tp.handle(st, "ESC")
    assert st.filt == "" and st.mode == "normal"
    tp.handle(st, "?")
    assert st.mode == "help" and any("keys" in plain(x) for x in tp.frame(st, 100, 30))
    tp.handle(st, "x")
    assert st.mode == "normal"
    tp.frame(st, 120, 30)
    tp.handle(st, f"CLICK:{st.left_top + 2}:3")
    assert st.sel == 2
    assert tp.handle(st, "q") == "quit" and tp.handle(st, "r") == "recal" and tp.handle(st, "c") == "copy"
    # copy without a clipboard tool shows the setting; it never writes settings
    monkeypatch.setattr(tp.shutil, "which", lambda _: None)
    st.sel = next(
        i
        for i, (k_, v) in enumerate(tp.rows(st))
        if k_ == "g" and tp.status(res["groups"][v], tp.prof_of(st, res["groups"][v])) == "set"
    )
    tp.act(st, "copy", Path("/nonexistent"), lambda: None)
    assert st.msg.startswith("no clipboard tool; the setting: ") and "autoCompactWindow" in st.msg
    # recalibrate without a reachable service runs the in-process callback
    called = []
    tp.act(st, "recal", Path("/nonexistent"), lambda: called.append(1) or res)
    assert called and st.msg == "recalibrated in-process (lite)"


def test_cli_terminal_paths(tmp_path, claude_root, codex_root, capsys, monkeypatch):
    out = tmp_path / "r.json"
    base = ["simulate", "--claude-root", str(claude_root), "--codex-root", str(codex_root), "--since-days", "0",
            "--no-cache", "--offline-pricing", "--out", str(out), "--lens-out", "", "--lang", "en"]  # fmt: skip
    assert cli_main(base) == 0
    assert "no action needed" in capsys.readouterr().out  # off a TTY: the plain tables
    monkeypatch.setenv("COLUMNS", "80")
    assert cli_main([*base, "--color", "always"]) == 0
    text = capsys.readouterr().out
    assert "\x1b[" in text and "╭─" in text and all(cw(x) <= 80 for x in ANSI.sub("", text).splitlines()[:-1])
    assert cli_main(["top", "--result", str(out), "--once", "--plain", "--width", "80", "--height", "24"]) == 0
    frame = capsys.readouterr().out.splitlines()
    assert len(frame) == 24 and all(cw(x) <= 80 for x in frame) and "\x1b[" not in "".join(frame)


@pytest.mark.parametrize("name", ["demo.svg", "top.svg"])
def test_terminal_svgs_render_without_animation_or_whitespace_handling(name):
    """Visible with no CSS animation (rsvg-convert, Quick Look, previews) and spacing fixed by x positions."""
    svg = (Path(__file__).parent.parent / "docs" / "assets" / name).read_text()
    assert 'xml:space="preserve"' in svg.split(">", 1)[0]
    without_keyframes = re.sub(r"@keyframes[^}]*\}\}", "", svg)
    assert "opacity:0" not in without_keyframes.replace(" ", "")  # the default state is fully visible
    assert "@keyframes in{from{opacity:0}}" in svg  # the fade-in is additive
    spans = re.findall(r"<tspan ([^>]*)>([^<]*)</tspan>", svg)
    assert spans and all('x="' in a and "textLength" in a for a, _ in spans)
    assert all(t == t.strip() and "  " not in t for _, t in spans), "runs of spaces must be split into positioned runs"


def test_docs_only_show_install_commands_that_work():
    """compactlab is installed from GitHub (not on PyPI): no bare `uvx compactlab` / `pip install compactlab`."""
    root = Path(__file__).parent.parent
    bad = re.compile(r"\b(?:uvx|pipx install|pip install|uv tool install|uv add)\s+compactlab\b")
    files = [*root.glob("*.md"), *(root / "docs").glob("*.md"), *(root / "mods").rglob("*.md"),
             *(root / "docs" / "assets").glob("*.svg"), *(root / "src").rglob("*.py")]  # fmt: skip
    hits = [f"{f.relative_to(root)}: {m.group(0)}" for f in files for m in bad.finditer(f.read_text(errors="ignore"))]
    assert not hits, hits
