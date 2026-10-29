"""Colour capability, the palette, display width, and styled text as segments.

A line is a list of `(text, role)` segments. Widgets build lines; `Style.line` turns one into a string for the
terminal (truecolor, 256 or 16 colours, or none), so layout never depends on escape codes and width is measured on
the text alone. The roles carry the report's and the lens mod's colour language: clay accent, green saves, amber
adds, red broken guardrails, grey for the default and secondary text.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from typing import IO, Any

Seg = tuple[str, Any]  # (text, role); role is a palette key or None
Line = list[Seg]

# role -> truecolor (works on dark and light backgrounds), 16-colour SGR fallback
PALETTE: dict[str, tuple[tuple[int, int, int], str]] = {
    "acc": ((217, 119, 87), "31"),
    "good": ((95, 179, 127), "32"),
    "warn": ((227, 165, 72), "33"),
    "bad": ((242, 104, 92), "91"),
    "dim": ((143, 140, 131), "90"),
    "was": ((120, 116, 108), "90"),
    "ink": ((236, 235, 228), "97"),
    "frame": ((96, 93, 86), "90"),
    "hl": ((255, 255, 255), "97"),
}
BOLD = {"title", "big", "hl"}  # roles drawn bold (with the colour of their base role, see ROLE_BASE)
ROLE_BASE = {"title": "ink", "big": "ink", "good_b": "good", "warn_b": "warn", "acc_b": "acc", "bad_b": "bad"}
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def cw(text: str) -> int:
    """Display width: East Asian wide / fullwidth glyphs take two columns, combining marks none."""
    w = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in "WF" else 1
    return w


def width(line: Line) -> int:
    return sum(cw(t) for t, _ in line)


def clip(line: Line, n: int) -> Line:
    """The line cut to `n` columns, never splitting a wide glyph (an ellipsis marks the cut)."""
    if width(line) <= n:
        return line
    out: Line = []
    left = max(n - 1, 0)
    for text, role in line:
        if left <= 0:
            break
        part = ""
        for ch in text:
            c = cw(ch)
            if c > left:
                left = 0
                break
            part += ch
            left -= c
        if part:
            out.append((part, role))
    out.append(("…", "dim"))
    return out


def pad(line: Line, n: int) -> Line:
    """Clip or right-pad with spaces to exactly `n` columns."""
    line = clip(line, n)
    return [*line, (" " * (n - width(line)), None)] if width(line) < n else line


def plain(line: Line) -> str:
    return "".join(t for t, _ in line)


def _cube(rgb: tuple[int, int, int]) -> int:
    """Nearest xterm-256 colour cube index."""
    r, g, b = (round(v / 255 * 5) for v in rgb)
    return 16 + 36 * r + 6 * g + b


@dataclass
class Style:
    depth: int = 0  # 0 none, 16, 256, 24 (truecolor)

    @classmethod
    def detect(cls, stream: IO[str] | None, mode: str = "auto") -> Style:
        """`mode`: always / never / auto. Auto: no colour off a TTY, with NO_COLOR, or on a dumb terminal."""
        if mode == "never":
            return cls(0)
        env = os.environ
        if mode == "auto":
            if "NO_COLOR" in env or env.get("TERM") == "dumb":
                return cls(0)
            if stream is None or not getattr(stream, "isatty", lambda: False)():
                return cls(0)
        ct, term = env.get("COLORTERM", "").lower(), env.get("TERM", "")
        if ct in ("truecolor", "24bit"):
            return cls(24)
        if "256" in term:
            return cls(256)
        return cls(16 if mode == "auto" or term else 24)

    def sgr(self, role: str | None) -> str:
        if not self.depth or not role:
            return ""
        base = ROLE_BASE.get(role, role)
        rgb, basic = PALETTE.get(base, PALETTE["ink"])
        bold = "1;" if role in BOLD or role.endswith("_b") else ""
        if self.depth == 24:
            return f"\x1b[{bold}38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
        if self.depth == 256:
            return f"\x1b[{bold}38;5;{_cube(rgb)}m"
        return f"\x1b[{bold}{basic}m"

    def line(self, line: Line) -> str:
        if not self.depth:
            return plain(line)
        out = []
        for text, role in line:
            s = self.sgr(role)
            out.append(f"{s}{text}\x1b[0m" if s else text)
        return "".join(out)

    def lines(self, lines: list[Line]) -> str:
        return "\n".join(self.line(x).rstrip() if not self.depth else self.line(x) for x in lines)
