"""Terminal widgets built from segment lines: header with a mark, boxed panels, threshold gauge, was/now bars,
sparkline, braille line chart, delta chips and chip packing. Everything takes a width and stays inside it."""

from __future__ import annotations

from collections.abc import Sequence

from .style import Line, clip, cw, pad, width

MINUS = "−"
MARK = ["▗▄▄▄▄▄▖", "▐█████▌", "▐███▌  ", "▐██▌   ", "▝▀▀▘   "]  # a context shrinking at a threshold
SPARK = "▁▂▃▄▅▆▇█"


# ---------- numbers: percent first, signed amounts, arrows on deltas only ----------


def k(n: float | None) -> str:
    if n is None:
        return "–"
    return f"{n / 1e6:g}M" if n >= 1_000_000 else f"{round(n / 1000)}k"


def pabs(v: float) -> str:
    a = abs(v * 100)
    return f"{a:.1f}" if a < 10 else f"{a:.0f}"


def fpct(v: float | None, arrow: bool = True) -> str:
    """'▼ −21%' / '▲ +3.0%' (a share, as a fraction); without the arrow: '−21%'."""
    if v is None:
        return "–"
    if abs(v * 100) < 0.05:
        return "0%"
    sign = MINUS if v < 0 else "+"
    return (("▼ " if v < 0 else "▲ ") if arrow else "") + f"{sign}{pabs(v)}%"


def fusd(v: float, arrow: bool = False) -> str:
    if abs(v) < 0.05:
        return "$0"
    a = abs(v)
    body = f"${a:.1f}" if a < 1000 else f"${a:,.0f}"
    return (("▼ " if v < 0 else "▲ ") if arrow else "") + (MINUS if v < 0 else "+") + body


def fmin(v: float, arrow: bool = False) -> str:
    if abs(v) < 0.05:
        return "0 min"
    a = abs(v)
    return (("▼ " if v < 0 else "▲ ") if arrow else "") + (MINUS if v < 0 else "+") + f"{a:.1f} min"


def money(v: float) -> str:
    """An absolute amount (no sign, no arrow): '$619', '$43.0'."""
    return f"${v:,.0f}" if v >= 100 else f"${v:.1f}"


def tone(v: float | None) -> str | None:
    if v is None or abs(v) < 1e-9:
        return None
    return "good" if v < 0 else "warn"


def chip(pct: float | None, amount: str | None, unit: str = "pct") -> Line:
    """A delta: the lead figure bold and coloured by sign, the other one dim. `unit` picks the lead."""
    t = tone(pct) if pct is not None else None
    if unit == "pct" and pct is not None:
        return [(fpct(pct), (t or "ink") + "_b")] + ([(" " + amount, "dim")] if amount else [])
    return [(amount or "–", (t or "ink") + "_b")] + ([(" " + fpct(pct, arrow=False), "dim")] if pct is not None else [])


# ---------- layout ----------


def pack(tokens: Sequence[Line], cols: int, sep: str = "  ") -> list[Line]:
    """Tokens on as few rows as fit `cols`, breaking only between tokens (an over-wide token is clipped)."""
    rows: list[Line] = []
    row: Line = []
    for tok in tokens:
        tok = clip(tok, cols)
        if row and width(row) + cw(sep) + width(tok) > cols:
            rows.append(row)
            row = []
        row = row + ([(sep, None)] if row else []) + tok
    if row:
        rows.append(row)
    return rows


def panel(title: Line, body: list[Line], cols: int) -> list[Line]:
    """A rounded box `cols` wide with the title in the top edge."""
    inner = max(cols - 4, 1)
    t = clip(title, max(inner - 2, 1))
    top: Line = [("╭─ ", "frame"), *t, (" " + "─" * max(inner - width(t) - 1, 0) + "╮", "frame")]
    out = [top]
    for ln in body:
        out.append([("│ ", "frame"), *pad(ln, inner), (" │", "frame")])
    out.append([("╰" + "─" * (cols - 2) + "╯", "frame")])
    return out


def header(facts: list[tuple[str, Line]], cols: int, title: str = "compactlab") -> list[Line]:
    """fastfetch-style: the mark on the left, key facts on the right (stacked under the mark when narrow)."""
    mark: list[Line] = [[(m, "acc")] for m in MARK]
    rows: list[Line] = [[(title, "acc_b")], [("─" * min(len(title) + 6, max(cols, 1)), "frame")]]
    keyw = max((cw(key) for key, _ in facts), default=0)
    for key, val in facts:
        rows.append([(key + " " * (keyw - cw(key)) + "  ", "dim"), *val])
    if cols < 46:
        return [clip(r, cols) for r in [*mark, [("", None)], *rows]]
    gap = 3
    side = cols - 7 - gap
    out = []
    for i in range(max(len(mark), len(rows))):
        left = mark[i] if i < len(mark) else [(" " * 7, None)]
        right = clip(rows[i], side) if i < len(rows) else []
        out.append([*pad(left, 7), (" " * gap, None), *right])
    return out


# ---------- charts ----------


def gauge(cols: int, lo: float, hi: float, marks: list[tuple[float, str, str]], unsafe: list[tuple[float, float]]
          ) -> list[Line]:  # fmt: skip
    """A threshold axis lo..hi: '░' red where recall is below the floor, marks drawn on the bar, labels under it.

    marks: (threshold, glyph, role); the label of an 'acc_b' mark (the selection) is placed first."""
    n = max(cols, 10)
    pos = lambda v: min(n - 1, max(0, round((v - lo) / ((hi - lo) or 1) * (n - 1))))  # noqa: E731
    cells: list[tuple[str, str | None]] = [("─", "frame")] * n
    for a, b in unsafe:
        for i in range(pos(a), pos(b) + 1):
            cells[i] = ("░", "bad")
    labels: list[tuple[int, str, str]] = []
    for v, glyph, role in marks:
        cells[pos(v)] = (glyph, role)
        labels.append((pos(v), k(v), role))
    bar: Line = list(cells)
    under: Line = [(" ", None)] * n
    taken = [False] * n
    for p, text, role in sorted(labels, key=lambda x: (x[2] != "acc_b", x[0])):  # the selection's label first
        s = max(0, min(p - len(text) // 2, n - len(text)))
        if any(taken[s : s + len(text) + 1]):
            continue
        for j, ch in enumerate(text):
            under[s + j] = (ch, role.replace("_b", ""))
            taken[s + j] = True
    # the axis ends go on the label row where they do not collide with a mark's label
    for text, start in ((k(lo), 0), (k(hi), n - len(k(hi)))):
        if not any(taken[max(start - 1, 0) : start + len(text) + 1]):
            for j, ch in enumerate(text):
                under[start + j] = (ch, "frame")
    return [_merge(bar), _merge(under)]


def _merge(cells: Sequence[tuple[str, str | None]]) -> Line:
    out: Line = []
    for t, r in cells:
        if out and out[-1][1] == r:
            out[-1] = (out[-1][0] + t, r)
        else:
            out.append((t, r))
    return out


def was_now(was: float, now: float, cols: int, scale: float | None = None) -> tuple[Line, Line]:
    """Two bars on one scale: grey 'was', accent 'now' with the saving hatched green (or the addition amber)."""
    top = max(scale or 0, was, now, 1e-9)
    n = max(cols, 4)
    wc, nc = max(round(was / top * n), 1 if was > 0 else 0), max(round(now / top * n), 1 if now > 0 else 0)
    a: Line = [("█" * wc, "was"), ("·" * (n - wc), "frame")]
    if nc <= wc:
        b: Line = [("█" * nc, "acc"), ("░" * (wc - nc), "good"), ("·" * (n - wc), "frame")]
    else:
        b = [("█" * wc, "acc"), ("█" * (nc - wc), "warn"), ("·" * (n - nc), "frame")]
    return _merge(a), _merge(b)


def spark(values: Sequence[float], cols: int, sel: int | None = None) -> Line:
    """Block sparkline resampled to `cols`; the cell holding index `sel` is drawn in the accent."""
    if not values:
        return []
    n = max(cols, 1)
    lo, hi = min(values), max(values)
    cells: list[tuple[str, str | None]] = []
    for c in range(n):
        i = min(len(values) - 1, int(c * len(values) / n))
        v = values[i]
        ch = SPARK[min(7, int((v - lo) / ((hi - lo) or 1) * 7.999))]
        hit = sel is not None and min(len(values) - 1, int((c + 1) * len(values) / n)) > sel >= i
        cells.append((ch, "acc_b" if hit else "dim"))
    return _merge(cells)


BRAILLE = 0x2800
DOTS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))  # [row][col] bits of a 2x4 braille cell


def braille(series: list[tuple[Sequence[float], str]], cols: int, rows: int, lo: float, hi: float,
            vlines: Sequence[tuple[int, str]] = ()) -> list[Line]:  # fmt: skip
    """Line chart in braille dots (2x4 per cell). Each series: (values over the x range, role). `vlines`: (column,
    role) markers drawn as '│' where no dot falls. Later series win a shared cell's colour."""
    w, h = max(cols, 2), max(rows, 1)
    bits = [[0] * w for _ in range(h)]
    role: list[list[str | None]] = [[None] * w for _ in range(h)]
    for vals, r in series:
        if not vals:
            continue
        pts = []
        for x in range(w * 2):
            fi = x / (w * 2 - 1 or 1) * (len(vals) - 1)  # linear between samples, so the curve reads as a line
            i = min(int(fi), len(vals) - 2) if len(vals) > 1 else 0
            v = vals[i] + (vals[i + 1] - vals[i]) * (fi - i) if len(vals) > 1 else vals[0]
            y = (v - lo) / ((hi - lo) or 1)
            pts.append(min(h * 4 - 1, max(0, round((1 - y) * (h * 4 - 1)))))
        for x in range(w * 2):
            y0, y1 = pts[x], pts[x - 1] if x else pts[x]
            for y in range(min(y0, y1), max(y0, y1) + 2):  # join steps; one extra dot row makes the line heavier
                y = min(y, h * 4 - 1)
                cy, cx = divmod(y, 4)[0], x // 2
                bits[cy][cx] |= DOTS[y % 4][x % 2]
                role[cy][cx] = r
    out: list[Line] = []
    vl = dict(vlines)
    for cy in range(h):
        cells = []
        for cx in range(w):
            if bits[cy][cx]:
                cells.append((chr(BRAILLE + bits[cy][cx]), role[cy][cx]))
            elif cx in vl:
                cells.append(("│", vl[cx]))
            else:
                cells.append((" ", None))
        out.append(_merge(cells))
    return out
