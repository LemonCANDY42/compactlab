"""`compactlab top`: a live terminal view of the latest result (htop / btop style).

The screen is built by `frame(state, cols, rows)`, a pure function returning segment lines, and keys are handled by
`handle(state, key)`; the curses loop only draws lines and reads keys. That keeps the layout testable without a
terminal, and the same frame is printed once when stdout is not a TTY. Nothing here writes settings: `c` copies the
setting for the selected model to the clipboard, `r` asks the local service (or an in-process lite run) to
recalibrate.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .style import Line, Style, clip, cw, pad, width
from .views import PGLYPH, PROFILES, figs, keep_reason, label, status, totals, verdict
from .widgets import braille, chip, fmin, fpct, fusd, k, pack, was_now

KEYS = [
    ("↑ ↓  j k", "select"),
    ("← →  h l", "fold / expand a harness"),
    ("p", "cycle profile: cost, balanced, time, each model's own"),
    ("%", "lead with % of the default or $ per day"),
    ("r", "recalibrate now (the local service, else an in-process lite run)"),
    ("c", "copy the selected model's setting (never writes it)"),
    ("/", "filter models; Enter keeps it, Esc clears it"),
    ("?", "this help"),
    ("q", "quit"),
]


@dataclass
class TopState:
    res: dict[str, Any]
    sel: int = 0
    folded: set[str] = field(default_factory=set)
    profile: str | None = None  # None: each group's own selected profile
    unit: str = "pct"
    filt: str = ""
    mode: str = "normal"  # normal | filter | help
    msg: str = ""
    service: dict[str, Any] = field(default_factory=dict)  # {"up": bool, "next": str, "age": str}
    left_rows: int = 0  # set by frame(): where the tree pane starts, for mouse clicks
    left_top: int = 0


def prof_of(st: TopState, g: dict[str, Any]) -> str:
    return st.profile or g.get("profile") or "balanced"


def rows(st: TopState) -> list[tuple[str, Any]]:
    """The tree: ('h', harness) then ('g', group index) per model and role, filtered and folded."""
    out: list[tuple[str, Any]] = []
    q = st.filt.lower()
    gs = st.res["groups"]
    for h in sorted({g["harness"] for g in gs}):
        idx = [
            i
            for i, g in enumerate(gs)
            if g["harness"] == h and (not q or q in (g["model"] + label(g["model"])).lower())
        ]
        if not idx:
            continue
        out.append(("h", h))
        if h in st.folded:
            continue
        idx.sort(
            key=lambda i: (
                status(gs[i], prof_of(st, gs[i])) != "set",
                -abs((figs(gs[i], prof_of(st, gs[i])) or {"d": 0})["d"]),
            )
        )
        out += [("g", i) for i in idx]
    return out


def selected(st: TopState) -> tuple[str, Any] | None:
    r = rows(st)
    return r[min(st.sel, len(r) - 1)] if r else None


def handle(st: TopState, key: str) -> str | None:
    """Update the state for one key; returns an action for the loop ('quit', 'recal', 'copy') or None."""
    r = rows(st)
    if st.mode == "help":
        st.mode = "normal"
        return "quit" if key == "q" else None
    if st.mode == "filter":
        if key in ("ENTER", "ESC"):
            st.mode = "normal"
            if key == "ESC":
                st.filt = ""
        elif key == "BACKSPACE":
            st.filt = st.filt[:-1]
        elif len(key) == 1 and key.isprintable():
            st.filt += key
        st.sel = 0
        return None
    if key == "q":
        return "quit"
    if key == "ESC":  # closes the filter or help (above); never quits, so a stray escape sequence is harmless
        st.msg = ""
        return None
    if key in ("DOWN", "j", "WHEELDOWN"):
        st.sel = min(st.sel + 1, max(len(r) - 1, 0))
    elif key in ("UP", "k", "WHEELUP"):
        st.sel = max(st.sel - 1, 0)
    elif key in ("LEFT", "h", "RIGHT", "l", "ENTER"):
        cur = selected(st)
        if cur:
            h = cur[1] if cur[0] == "h" else st.res["groups"][cur[1]]["harness"]
            if key in ("LEFT", "h"):
                st.folded.add(h)
                st.sel = rows(st).index(("h", h))
            elif cur[0] == "h":
                st.folded.discard(h)
    elif key == "p":
        order: list[str | None] = [None, *PROFILES]
        st.profile = order[(order.index(st.profile) + 1) % len(order)]
        st.msg = "profile: " + (st.profile or "each model's own")
    elif key == "%":
        st.unit = "usd" if st.unit == "pct" else "pct"
    elif key == "/":
        st.mode = "filter"
    elif key == "?":
        st.mode = "help"
    elif key == "r":
        return "recal"
    elif key == "c":
        return "copy"
    elif key.startswith("CLICK:"):
        y = int(key.split(":")[1]) - st.left_top
        if 0 <= y < st.left_rows and y < len(r):
            st.sel = y
    return None


def snippet(g: dict[str, Any], threshold: int) -> str:
    """The setting for one model, in the harness's own syntax (the report's Copy button gives the same)."""
    if g["harness"] == "codex":
        return f"model_auto_compact_token_limit = {threshold}"
    return json.dumps({"modelSettings": {g["model"]: {"autoCompactWindow": threshold}}})


# ---------- the frame ----------


def _meters(st: TopState, cols: int) -> list[Line]:
    t = totals(st.res, st.profile)
    toks: list[Line] = []
    if t["n"]:
        toks.append([("Σ ", "dim"), *chip(t["pc"], fusd(t["d"]) + "/d", st.unit), (" cost", "dim")])
        toks.append([*chip(t["pt"], fmin(t["dm"]) + "/d", st.unit), (" wall-clock", "dim")])
        toks.append([(f"{t['n']} model{'s' if t['n'] != 1 else ''} to change", "dim")])
    sv = st.service
    if sv.get("age"):  # only once something has calibrated; no placeholders
        nxt = f" · next {sv['next']}" if sv.get("next") else ""
        toks.append([("◴ ", "dim"), ("calibrated " + sv["age"], "ink"), (nxt, "dim")])
    if sv.get("known"):  # only once a service was set up
        toks.append([("● ", "good" if sv.get("up") else "dim"), ("service " + ("up" if sv.get("up") else "not running"),
                     "ink" if sv.get("up") else "dim")])  # fmt: skip
    toks.append([(PGLYPH.get(st.profile or "", "◆") + " ", "acc"), (st.profile or "own profile", "ink")])
    v = verdict(st.res, prof=st.profile)
    return ([clip(v, cols)] if v else []) + pack(toks, cols, "   ")


def _tree(st: TopState, cols: int, height: int) -> list[Line]:
    gs = st.res["groups"]
    r = rows(st)
    start = max(0, min(st.sel - height + 1, len(r) - height)) if len(r) > height else 0
    out: list[Line] = []
    for n, (kind, v) in enumerate(r[start : start + height], start):
        mark = "▶" if n == st.sel else " "
        if kind == "h":
            hg = [g for g in gs if g["harness"] == v]
            setd = sum((figs(g, prof_of(st, g)) or {"d": 0})["d"] for g in hg if status(g, prof_of(st, g)) == "set")
            line: Line = [
                (mark, "acc_b"),
                ("▸ " if v in st.folded else "▾ ", "dim"),
                (v, "title"),
                ("  " + fusd(setd), "dim"),
            ]
        else:
            g = gs[v]
            p = prof_of(st, g)
            f = figs(g, p)
            s = status(g, p)
            name = f"{label(g['model'])} {'sub' if g['role'] == 'subagent' else g['role']}"
            line = [(mark, "acc_b"), ("  ", None), (name, "hl" if n == st.sel else "ink" if s != "noop" else "dim")]
            tail: Line = []
            if s == "noop":
                tail = [("✓ no change", "dim")]
            elif f:
                if s == "set":
                    _, b = was_now(f["was"], f["now"], 6)
                    tail = [*b, (" ", None), *chip(f["pc"], None, st.unit)]
                else:
                    tail = [("keep default", "dim")]
            room = cols - width(line) - width(tail) - 1
            line = [*line, (" " * max(room, 1), None), *tail] if room >= 1 else clip(line, cols)
        out.append(pad(line, cols))
    return out + [pad([], cols)] * (height - len(out))


def _detail(st: TopState, cols: int, height: int) -> list[Line]:
    cur = selected(st)
    if not cur:
        return [pad([("no models match the filter", "dim")], cols)] + [pad([], cols)] * (height - 1)
    if cur[0] == "h":
        hg = [g for g in st.res["groups"] if g["harness"] == cur[1]]
        sub = {"groups": hg}
        t = totals(sub, st.profile)
        body: list[Line] = [[(cur[1], "title"), (f"  {len(hg)} groups", "dim")]]
        if t["n"]:
            body += pack([[*chip(t["pc"], fusd(t["d"]) + "/day", st.unit), (" cost", "dim")],
                          [*chip(t["pt"], fmin(t["dm"]) + "/day", st.unit), (" time", "dim")]], cols)  # fmt: skip
        body.append([("→ expand with l / →; select a model for its curve", "dim")])
        return [pad(x, cols) for x in body[:height]] + [pad([], cols)] * max(height - len(body), 0)
    g = st.res["groups"][cur[1]]
    p = prof_of(st, g)
    pr, d = g["profiles"][p], g["default"]
    f = figs(g, p)
    s = status(g, p)
    ttl = g.get("cache_ttl")
    cache = (f" · cache {ttl // 3600} h" if ttl >= 3600 else f" · cache {ttl // 60} min") if ttl else ""
    cache = cache if g["harness"] == "claude-code" else ""  # only Anthropic bills by cache tier
    body = [[(label(g["model"]), "title"), (f" · {g['role']} · {g['harness']} · {g['n_sessions']} sessions · "
                                            f"{g['days']:.1f} d{cache}", "dim")]]  # fmt: skip
    if s == "noop":
        body += [
            [("✓ never reaches the default threshold: nothing to tune", "good")],
            [(f"peak {k(g.get('peak_max'))} · p50 {k(g.get('peak_p50'))} · default {k(d['threshold'])}", "dim")],
        ]
    elif f:
        ans = [[(k(pr["threshold"]), "big")], [*chip(f["pc"], None, st.unit), (" cost", "dim")],
               [*chip(f["pt"], None, st.unit), (" wall-clock", "dim")],
               [(f"{fusd(f['d'])} · {fmin(f['dm'])} per day vs default {k(d['threshold'])}", "dim")]]  # fmt: skip
        if st.unit != "pct":
            ans[1] = [(fusd(f["d"], True), ("good" if f["d"] < 0 else "warn") + "_b"), (" cost", "dim")]
            ans[2] = [(fmin(f["dm"], True), ("good" if f["dm"] < 0 else "warn") + "_b"), (" wall-clock", "dim")]
        body += pack(ans, cols)
        if s == "keep":
            body += pack([[("▪ keep default: " + keep_reason(g, p), "dim")]], cols)
        # cost and time vs threshold, both as % of the default, with default / current / profile marks
        grid = g["grid"]
        cc = [
            (c - d["combined_cost"]) / d["combined_cost"] * 100 if d["combined_cost"] else 0.0
            for c in g["combined_cost"]
        ]
        tt = [(x - d["time_s"]) / d["time_s"] * 100 if d["time_s"] else 0.0 for x in g["time_s"]]
        lo, hi = min(min(cc), min(tt), 0.0), max(max(cc), max(tt), 0.0)
        ax = 7
        cw_ = max(cols - ax, 8)
        col = lambda v: round((v - grid[0]) / ((grid[-1] - grid[0]) or 1) * (cw_ - 1))  # noqa: E731
        vl = [(col(d["threshold"]), "dim", "┊")]
        if g.get("current"):
            vl.append((col(g["current"]["threshold"]), "dim", "╎"))
        vl.append((col(pr["threshold"]), "acc_b", "┃"))
        ch = max(min(height - len(body) - 6, 14), 3)
        plot = braille([(tt, "dim"), (cc, "acc")], cw_, ch, lo, hi, vl)
        for i, ln in enumerate(plot):
            tick = f"{hi:+.0f}%" if i == 0 else f"{lo:+.0f}%" if i == len(plot) - 1 else ""
            body.append([(tick.rjust(ax - 1) + " ", "dim"), *ln])
        lo_t, hi_t, sel = k(grid[0]), k(grid[-1]), k(pr["threshold"])
        c0 = col(pr["threshold"]) - len(sel) // 2
        if len(lo_t) < c0 and c0 + len(sel) < cw_ - len(hi_t):  # selected threshold labelled under its marker
            axis: Line = [(lo_t + " " * (c0 - len(lo_t)), "dim"), (sel, "acc_b"),
                          (" " * (cw_ - len(hi_t) - c0 - len(sel)) + hi_t, "dim")]  # fmt: skip
        else:
            axis = [(lo_t + " " * max(cw_ - len(lo_t) - len(hi_t), 1) + hi_t, "dim")]
        body.append([(" " * ax, None), *axis])
        legend = "  ┊ default  ┃ profile" + ("  ╎ current" if g.get("current") else "")
        body.append([(" " * ax, None), ("⠿ cost", "acc"), ("  ⠿ wall-clock", "dim"), (legend, "dim")])
        a, b = was_now(f["was"], f["now"], max(min(cols - 26, 40), 6))
        body.append([("cost was ", "dim"), *a, (f" ${f['was']:,.1f}", "dim")])
        body.append(
            [
                ("     now ", "dim"),
                *b,
                (f" ${f['now']:,.1f} ", "ink"),
                (fpct(f["pc"]), "good" if f["d"] < 0 else "warn"),
            ]
        )
        if f["d"] < 0:
            body.append([("         ", None), ("░ = saved", "good")])
    toks = []
    for n in PROFILES:
        fn = figs(g, n)
        tok: Line = [(PGLYPH[n] + " ", "acc" if n == p else "dim"), (f"{n} {k(g['profiles'][n]['threshold'])} ", "ink")]
        if fn and not g["no_action_needed"]:
            tok.append((fpct(fn["pc"]), "good" if fn["d"] < 0 else "warn"))
        toks.append(tok)
    body += pack(toks, cols, "   ")
    return [pad(x, cols) for x in body[:height]] + [pad([], cols)] * max(height - len(body), 0)


def frame(st: TopState, cols: int, nrows: int) -> list[Line]:
    """The whole screen: exactly `nrows` lines, none wider than `cols`."""
    cols, nrows = max(cols, 30), max(nrows, 10)
    title: Line = [(" compactlab top ", "acc_b"), ("  " + ("% of default" if st.unit == "pct" else "$ / day"), "dim")]
    hint = "? help · q quit"
    top = [*title, (" " * max(cols - width(title) - cw(hint), 1), None), (hint, "dim")]
    out = [clip(top, cols), *_meters(st, cols), [("─" * cols, "frame")]]
    body_h = nrows - len(out) - 1
    if cols >= 80:
        lw = max(28, min(46, cols * 38 // 100))
        left, right = _tree(st, lw, body_h), _detail(st, cols - lw - 3, body_h)
        st.left_top, st.left_rows = len(out), body_h
        out += [[*left[i], (" │ ", "frame"), *right[i]] for i in range(body_h)]
    else:  # narrow: the tree above, the detail below
        th = max(min(len(rows(st)), body_h // 3), 3)
        st.left_top, st.left_rows = len(out), th
        out += [*_tree(st, cols, th), [("─" * cols, "frame")], *_detail(st, cols, body_h - th - 1)]
    if st.mode == "filter":
        status_line: Line = [("/ ", "acc_b"), (st.filt + "▏", "ink"), ("   Enter keeps · Esc clears", "dim")]
    else:
        status_line = (
            [(st.msg, "ink")]
            if st.msg
            else [("↑↓ select · ←→ fold · p profile · % unit · r recal · c copy · / filter", "dim")]
        )
    out.append(status_line)
    if st.mode == "help":
        out = _overlay(out, cols)
    return [pad(clip(x, cols), cols) for x in out[:nrows]]


def _overlay(screen: list[Line], cols: int) -> list[Line]:
    keyw = max(cw(a) for a, _ in KEYS)
    body: list[Line] = [[(a + " " * (keyw - cw(a)) + "  ", "acc"), (b, "ink")] for a, b in KEYS]
    body += [[], [("cost = list-price estimate · % = change vs the harness default · any key closes", "dim")]]
    w = min(cols - 4, max(width(x) for x in body) + 4)
    from .widgets import panel

    box = panel([("keys", "title")], body, w)
    y0, x0 = 3, max((cols - w) // 2, 0)
    for i, ln in enumerate(box):
        if y0 + i < len(screen):
            screen[y0 + i] = [(" " * x0, None), *ln]
    return screen


# ---------- data, service, clipboard ----------


def service_state(state_dir: Path) -> dict[str, Any]:
    """Whether the local service answers (its endpoint file + /api/health), and the calibration times."""
    st = state_dir / "service" / "state.json"
    ep = state_dir / "service" / "endpoint.json"
    out: dict[str, Any] = {"up": False, "known": st.exists() or ep.exists()}
    if st.exists():
        try:
            s = json.loads(st.read_text())
            if s.get("last_ok"):
                out["age"] = _ago(time.time() - s["last_ok"]) + " ago"
        except ValueError:
            pass
    if ep.exists():
        try:
            e = json.loads(ep.read_text())
            with urllib.request.urlopen(f"http://{e['host']}:{e['port']}/api/health", timeout=0.5) as r:
                h = json.loads(r.read())
            out["up"], out["endpoint"] = True, e
            if h.get("next_calibration"):
                out["next"] = time.strftime("%H:%M", time.localtime(h["next_calibration"]))
        except (OSError, ValueError, KeyError):
            pass
    return out


def _ago(sec: float) -> str:
    sec = max(sec, 0)
    return f"{sec / 60:.0f}m" if sec < 3600 else f"{sec / 3600:.1f}h" if sec < 86400 else f"{sec / 86400:.1f}d"


def recal_service(state_dir: Path, endpoint: dict[str, Any]) -> bool:
    token = (state_dir / "service" / "token").read_text().strip()
    req = urllib.request.Request(f"http://{endpoint['host']}:{endpoint['port']}/api/recalibrate", method="POST",
                                 headers={"Authorization": "Bearer " + token})  # fmt: skip
    try:
        with urllib.request.urlopen(req, timeout=2) as r:  # loopback only
            return r.status in (202, 409)
    except OSError:
        return False


def copy(text: str) -> bool:
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text, text=True, check=True, timeout=5)
                return True
            except (OSError, subprocess.SubprocessError):
                continue
    return False


def act(st: TopState, action: str, state_dir: Path, recalibrate: Callable[[], dict[str, Any] | None]) -> None:
    if action == "copy":
        cur = selected(st)
        if not cur or cur[0] != "g":
            st.msg = "select a model to copy its setting"
            return
        g = st.res["groups"][cur[1]]
        p = prof_of(st, g)
        if status(g, p) != "set":
            what = {"keep": "keep default", "noop": "no change"}[status(g, p)]
            st.msg = f"{label(g['model'])}: {what}: nothing to copy"
            return
        text = snippet(g, g["profiles"][p]["threshold"])
        st.msg = ("copied: " if copy(text) else "no clipboard tool; the setting: ") + text
    elif action == "recal":
        ep = st.service.get("endpoint")
        if ep and recal_service(state_dir, ep):
            st.msg = "service: recalibration started"
        else:
            st.msg = "recalibrating in-process (lite)…"
            res = recalibrate()
            if res:
                st.res = res
                st.msg = "recalibrated in-process (lite)"


# ---------- curses ----------


def run(st: TopState, state_dir: Path, reload: Callable[[], dict[str, Any] | None],
        recalibrate: Callable[[], dict[str, Any] | None], interval: float = 5.0) -> None:  # fmt: skip
    import curses
    import os

    os.environ.setdefault("ESCDELAY", "25")  # a bare Esc is a key, not the start of a slow escape sequence

    def main(scr: Any) -> None:
        curses.curs_set(0)
        curses.use_default_colors()
        pairs: dict[str, int] = {}
        if curses.has_colors():
            curses.start_color()
            from .style import PALETTE, ROLE_BASE, _cube

            for n, (role, (rgb, basic)) in enumerate(PALETTE.items(), 1):
                c = (
                    _cube(rgb)
                    if curses.COLORS >= 256
                    else {"31": 1, "32": 2, "33": 3, "91": 1, "90": 8, "97": 7}.get(basic, 7)
                )
                try:
                    curses.init_pair(n, c if c < curses.COLORS else -1, -1)
                    pairs[role] = curses.color_pair(n)
                except curses.error:
                    pass
            for alias, base in ROLE_BASE.items():
                if base in pairs:
                    pairs[alias] = pairs[base]
        curses.mousemask(curses.ALL_MOUSE_EVENTS | getattr(curses, "REPORT_MOUSE_POSITION", 0))
        scr.timeout(int(interval * 1000))
        last = time.time()
        while True:
            h, w = scr.getmaxyx()
            scr.erase()
            for y, ln in enumerate(frame(st, w - 1, h)):
                x = 0
                for text, role in ln:
                    attr = pairs.get(role or "", 0) | (
                        curses.A_BOLD if role and (role.endswith("_b") or role in ("title", "big", "hl")) else 0
                    )
                    if role == "hl":
                        attr |= curses.A_REVERSE
                    with contextlib.suppress(curses.error):  # the bottom-right cell raises after drawing
                        scr.addstr(y, x, text, attr)
                    x += cw(text)
            scr.refresh()
            try:  # get_wch raises on the refresh timeout instead of returning -1
                ch = scr.get_wch() if hasattr(scr, "get_wch") else scr.getch()
            except curses.error:
                ch = None
            key = _key(ch, curses)
            if key is None:
                if time.time() - last >= interval:
                    new = reload()
                    if new:
                        st.res = new
                    st.service = service_state(state_dir)
                    last = time.time()
                continue
            a = handle(st, key)
            if a == "quit":
                return
            if a:
                st.msg = "working…"
                scr.addstr(h - 1, 0, st.msg[: max(w - 2, 0)])
                scr.refresh()
                act(st, a, state_dir, recalibrate)

    with contextlib.suppress(curses.error):
        curses.wrapper(main)


def _key(ch: Any, curses: Any) -> str | None:
    if ch in (-1, None):
        return None
    if ch == curses.KEY_RESIZE:
        return "RESIZE"
    if ch == curses.KEY_MOUSE:
        try:
            _, x, y, _, b = curses.getmouse()
        except curses.error:
            return None
        if b & getattr(curses, "BUTTON4_PRESSED", 0):
            return "WHEELUP"
        if b & getattr(curses, "BUTTON5_PRESSED", 1 << 21):
            return "WHEELDOWN"
        return f"CLICK:{y}:{x}"
    named = {curses.KEY_UP: "UP", curses.KEY_DOWN: "DOWN", curses.KEY_LEFT: "LEFT", curses.KEY_RIGHT: "RIGHT",
             curses.KEY_BACKSPACE: "BACKSPACE", curses.KEY_ENTER: "ENTER"}  # fmt: skip
    if isinstance(ch, int):
        return named.get(ch)
    return {"\n": "ENTER", "\r": "ENTER", "\x1b": "ESC", "\x7f": "BACKSPACE", "\b": "BACKSPACE"}.get(ch, ch)


def static(st: TopState, style: Style, cols: int, nrows: int) -> str:
    return style.lines(frame(st, cols, nrows))
