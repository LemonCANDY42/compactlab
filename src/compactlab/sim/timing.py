"""Wall-clock model fitted from the user's own timestamps.

turn latency(ctx)      median latency per 50k-token context bin, made monotone non-decreasing, interpolated.
compaction latency(n)  least-squares a + b·pre_tokens over observed compactions (a, b >= 0).
re-discovery step      median turn latency.

Latency of a turn = timestamp of the response minus the last user/tool-result line before it. That includes
generation time and, for Claude Code, only the first content block of a response. It is a proxy, so every fit
carries a confidence label. A fit that is not "ok" never drives a recommendation: the group falls back to the
pooled fit of all sessions of the same harness when that one is "ok", else to a documented prior that is
deliberately pessimistic about compaction latency, so thin data cannot push thresholds down.
"""

from __future__ import annotations

import statistics
from bisect import bisect_right
from dataclasses import asdict, dataclass, field

from ..model import Session

BIN = 50_000
MAX_TURN_S = 600.0
MIN_BIN_N = 5
# Priors (assumed): used only when the user's data is too thin to fit.
PRIOR_TURN = (8.0, 4.0 / 100_000)  # seconds = a + b·ctx
PRIOR_COMPACT = (30.0, 0.3 / 1000)  # seconds = a + b·pre_tokens (pessimistic: ~120 s at 300k)
MIN_COMPACT_OK = 20


@dataclass
class TimeModel:
    xs: list[float] = field(default_factory=list)  # bin centers
    ys: list[float] = field(default_factory=list)  # monotone medians (seconds)
    turn_prior: tuple[float, float] = PRIOR_TURN
    compact_ab: tuple[float, float] = PRIOR_COMPACT
    median_turn_s: float = 10.0
    n_turn_samples: int = 0
    n_bins: int = 0
    n_compact_samples: int = 0
    turn_confidence: str = "prior"  # "ok" | "low" | "prior" (of this group's own data)
    compact_confidence: str = "prior"
    turn_source: str = "prior"  # what the model actually uses: "own" | "pooled" | "prior"
    compact_source: str = "prior"
    slope_s_per_100k: float = 0.0

    def turn(self, ctx: float) -> float:
        if not self.xs:
            a, b = self.turn_prior
            return a + b * ctx
        if ctx <= self.xs[0]:
            return self.ys[0]
        if ctx >= self.xs[-1]:
            return self.ys[-1]
        i = bisect_right(self.xs, ctx)
        w = (ctx - self.xs[i - 1]) / (self.xs[i] - self.xs[i - 1])
        return self.ys[i - 1] + w * (self.ys[i] - self.ys[i - 1])

    def compact(self, pre: float) -> float:
        a, b = self.compact_ab
        return a + b * pre

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("xs")
        d.pop("ys")
        d["curve"] = [[int(x), round(y, 2)] for x, y in zip(self.xs, self.ys, strict=True)]
        return d


def turn_samples(sessions: list[Session]) -> list[tuple[int, float]]:
    out = []
    for s in sessions:
        for t in s.turns:
            if t.ts and t.start_ts:
                d = t.ts - t.start_ts
                if 0 < d <= MAX_TURN_S:
                    out.append((t.ctx, d))
    return out


def _fit_line(pts: list[tuple[float, float]]) -> tuple[float, float]:
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    vx = sum((x - mx) ** 2 for x, _ in pts)
    b = max(sum((x - mx) * (y - my) for x, y in pts) / vx, 0.0) if vx else 0.0
    a = max(my - b * mx, 0.0)
    return a, b


def fit(sessions: list[Session], fallback: TimeModel | None = None) -> TimeModel:
    """Fit from `sessions`; parts whose confidence is not "ok" use `fallback` (if it is "ok") or the prior."""
    m = TimeModel()
    samples = turn_samples(sessions)
    m.n_turn_samples = len(samples)
    if samples:
        m.median_turn_s = statistics.median(d for _, d in samples)
    bins: dict[int, list[float]] = {}
    for ctx, d in samples:
        bins.setdefault(ctx // BIN, []).append(d)
    good = sorted((k, statistics.median(v)) for k, v in bins.items() if len(v) >= MIN_BIN_N)
    m.n_bins = len(good)
    if good:
        run = 0.0
        for k, med in good:
            run = max(run, med)  # monotone non-decreasing
            m.xs.append(k * BIN + BIN / 2)
            m.ys.append(run)
        if len(good) >= 2:
            m.slope_s_per_100k = _fit_line([(k * BIN + BIN / 2, med) for k, med in good])[1] * 100_000
    m.turn_confidence = "ok" if (m.n_turn_samples >= 200 and m.n_bins >= 3) else ("low" if good else "prior")
    comp = [
        (float(c.pre_tokens), float(c.duration_s))
        for s in sessions
        for c in s.compactions
        if c.duration_s and c.pre_tokens
    ]
    m.n_compact_samples = len(comp)
    if len(comp) >= 5:
        m.compact_confidence = "ok" if len(comp) >= MIN_COMPACT_OK else "low"
    if m.compact_confidence == "ok":
        m.compact_ab, m.compact_source = _fit_line(comp), "own"
    elif fallback is not None and fallback.compact_confidence == "ok":
        m.compact_ab, m.compact_source = fallback.compact_ab, "pooled"
    if m.turn_confidence == "ok":
        m.turn_source = "own"
    elif fallback is not None and fallback.turn_confidence == "ok":
        m.xs, m.ys, m.turn_source = list(fallback.xs), list(fallback.ys), "pooled"
        m.median_turn_s = fallback.median_turn_s
    else:
        m.xs, m.ys = [], []  # a thin curve must not drive the choice either
        m.median_turn_s = TimeModel.median_turn_s  # nor may a handful of samples set the re-discovery step
    return m
