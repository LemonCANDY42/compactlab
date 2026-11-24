"""Turn a recommended token threshold into a harness's own unit.

compactlab picks thresholds in tokens. Several harnesses take a fraction of the window instead (dsh, Gemini CLI,
OpenCode 2.x as a share kept free) or a token count kept free below a limit (OpenCode 1.x). A fraction is rounded so
the trigger never lands above the recommended threshold, and the result says how many tokens one step of the last
decimal is on that window, so a snippet can say when the window makes the fraction coarse. A global fraction shared
by models with different windows lands at different token counts; `shared_fraction` says where it lands for each.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

COARSE_TOKENS = 5_000  # a rounding step wider than this is worth a note


@dataclass
class Fraction:
    value: float  # what to write
    tokens: int  # where it triggers on this window
    step_tokens: int  # tokens per unit of the last decimal on this window
    note: str = ""  # set when the step is coarse


def fraction_for(threshold: int, window: int, places: int = 3) -> Fraction:
    """The largest fraction with `places` decimals whose trigger (fraction x window) is at or below `threshold`."""
    scale = 10**places
    units = math.floor(threshold / window * scale + 1e-9)
    value = units / scale
    step = math.ceil(window / scale)
    tokens = int(value * window)
    note = ""
    if step > COARSE_TOKENS:
        note = (f"one step of {1 / scale:g} is {step:,} tokens on a {window:,}-token window, so {value:g} triggers at "
                f"{tokens:,} instead of {threshold:,}")  # fmt: skip
    return Fraction(value, tokens, step, note)


def free_share(threshold: int, window: int, places: int = 1) -> Fraction:
    """The share of the window to keep free (e.g. OpenCode 2.x `compaction.buffer`), in percent with `places`
    decimals, rounded up so the trigger window x (1 - share) is at or below `threshold`."""
    scale = 10**places
    units = math.ceil((1 - threshold / window) * 100 * scale - 1e-9)
    pct = units / scale
    step = math.ceil(window / (100 * scale))
    tokens = int(window * (1 - pct / 100))
    note = ""
    if step > COARSE_TOKENS:
        note = f"one step of {1 / scale:g}% is {step:,} tokens on a {window:,}-token window"
    return Fraction(pct, tokens, step, note)


def shared_fraction(targets: list[tuple[str, int, int]], places: int = 3) -> tuple[Fraction, list[str]]:
    """One global fraction for several (model, threshold, window) targets, most important first: the first target's
    fraction, and a line for every other target saying where that fraction lands on its window."""
    _, threshold, window = targets[0]
    f = fraction_for(threshold, window, places)
    lines = []
    for m, t, w in targets[1:]:
        at = int(f.value * w)
        lines.append(f"{m}: {f.value:g} of {w:,} triggers at {at:,} (its own pick: {t:,})")
    return f, lines
