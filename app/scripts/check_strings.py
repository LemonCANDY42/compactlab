#!/usr/bin/env python3
"""Check that every interface string in the app has a translation.

The English text is the key (Text("…"), Button("…"), L("…") and the other SwiftUI initializers that localize a
literal). Interpolations become `%@`, as Swift generates them for strings (the app interpolates only strings into
localized text). Compares the keys with Resources/<lang>.lproj/Localizable.strings and exits 1 when a key has no
translation or a translation no key uses.

    app/scripts/check_strings.py            # check
    app/scripts/check_strings.py --missing  # print the missing keys as .strings lines to fill in
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
CALLS = re.compile(
    r"(?<![\w.])(?:L|Text|Button|Toggle|Label|Section|DisclosureGroup|TextField|LabeledContent|Picker|Window|\.help)"
    r"\(\s*\""
)
# keys with nothing to translate (only placeholders, numbers and symbols)
NOTHING = re.compile(r"^[\s%@·→()+\-−:,./\d]*$")


def literal(src: str, i: int) -> tuple[str, int]:
    """The Swift string literal starting after the quote at src[i-1]; interpolations become %@."""
    out, depth = [], 0
    while i < len(src):
        c = src[i]
        if c == "\\" and src[i + 1] == "(":
            j, depth = i + 2, 1
            while depth:  # skip the interpolated expression, nested strings and parentheses included
                if src[j] == '"':
                    j += 1
                    while src[j] != '"':
                        j += 2 if src[j] == "\\" else 1
                elif src[j] == "(":
                    depth += 1
                elif src[j] == ")":
                    depth -= 1
                j += 1
            out.append("%@")
            i = j
            continue
        if c == "\\":
            esc = src[i + 1]
            out.append({"n": "\\n", '"': '\\"', "\\": "\\\\", "t": "\\t"}.get(esc, esc))
            i += 2
            continue
        if c == '"':
            return "".join(out), i + 1
        out.append(c)
        i += 1
    raise ValueError("unterminated string")


def keys() -> dict[str, str]:
    found: dict[str, str] = {}
    for f in sorted((APP / "Sources").rglob("*.swift")):
        src = f.read_text()
        for m in CALLS.finditer(src):
            k, _ = literal(src, m.end())
            if k and not NOTHING.match(k):
                found.setdefault(k, f"{f.relative_to(APP)}:{src.count(chr(10), 0, m.start()) + 1}")
    return found


def table(lang: str) -> dict[str, str]:
    p = APP / "Resources" / f"{lang}.lproj" / "Localizable.strings"
    pairs = re.findall(r'^"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)";', p.read_text(), re.M)
    return dict(pairs)


def main() -> int:
    want = keys()
    bad = 0
    langs = sorted(d.name.removesuffix(".lproj") for d in (APP / "Resources").glob("*.lproj"))
    for lang in [x for x in langs if x != "en"]:  # English is the keys
        have = table(lang)
        missing = [k for k in want if k not in have]
        unused = [k for k in have if k not in want]
        if "--missing" in sys.argv:
            for k in missing:
                print(f'"{k}" = "";  // {want[k]}')
            continue
        for k in missing:
            print(f"{lang}: no translation for {k!r} ({want[k]})")
        for k in unused:
            print(f"{lang}: translation of {k!r} is not used")
        for k, v in have.items():
            if k in want and k.count("%@") != v.count("%@"):
                print(f"{lang}: {k!r} has {k.count('%@')} placeholders, its translation {v.count('%@')}")
                bad += 1
        bad += len(missing) + len(unused)
        print(f"{lang}: {len(want) - len(missing)}/{len(want)} strings")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
