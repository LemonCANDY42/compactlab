"""Estimate the re-discovery penalty from compactions that already happened in the history."""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, asdict

from ..model import Session

WINDOW = 20  # turns inspected before/after a compaction
MIN_OBSERVED = 5

CORRECTION_RE = re.compile(
    r"(\bi (already )?(told|said|mentioned)\b|\byou forgot\b|\bas i said\b|\bnot what i\b|"
    r"\bwe (already )?(did|discussed|decided)\b|\balready (told|said|did)\b|"
    r"我说过|我刚才说|你忘了|不是这个|已经说过|之前说过|刚才不是)", re.I)

REDISCOVERY_TOOLS = ("Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch", "read_file", "shell", "exec_command")


@dataclass
class PenaltyParams:
    rho: float = 0.04              # re-read tokens per dropped context token
    fixed_tokens: int = 6000       # extra tokens re-added per compaction regardless of size
    correction_turns: float = 0.3  # extra user-correction turns per compaction
    source: str = "prior"          # "prior" | "observed"
    n_observed: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _is_rediscovery(key: str) -> bool:
    return key.split(":", 1)[0] in REDISCOVERY_TOOLS or key.split(":", 1)[0].startswith("mcp__")


def repeat_stats(s: Session, start: int, end: int) -> tuple[int, int, int]:
    """(repeated tool calls, estimated tokens of repeats, correction phrases) in turns [start,end),
    where 'repeated' means the same tool+argument appeared in turns before `start` or earlier in the window."""
    seen: set[str] = set()
    for t in s.turns[:start]:
        seen.update(t.tool_keys)
    reps = toks = corr = 0
    for t in s.turns[start:end]:
        for i, k in enumerate(t.tool_keys):
            if _is_rediscovery(k):
                if k in seen:
                    reps += 1
                    toks += t.tool_result_tokens[i] if i < len(t.tool_result_tokens) else 2000
            seen.add(k)
        if t.user_text and CORRECTION_RE.search(t.user_text):
            corr += 1
    return reps, toks, corr


def calibrate(sessions: list[Session], base_tokens: int, summary_tokens: int) -> PenaltyParams:
    excess_tok: list[float] = []
    dropped: list[float] = []
    excess_corr: list[float] = []
    for s in sessions:
        for c in s.compactions:
            a = c.turn_index
            if a < WINDOW // 2 or a + WINDOW // 2 > len(s.turns):
                continue
            _, after_t, after_c = repeat_stats(s, a, a + WINDOW)
            _, before_t, before_c = repeat_stats(s, a - WINDOW, a)
            pre = c.pre_tokens or s.turns[a - 1].ctx
            excess_tok.append(max(after_t - before_t, 0))
            excess_corr.append(max(after_c - before_c, 0))
            dropped.append(max(pre - base_tokens - summary_tokens, 1))
    n = len(dropped)
    if n < MIN_OBSERVED:
        return PenaltyParams(n_observed=n)
    rho = min(max(sum(excess_tok) / sum(dropped), 0.0), 0.5)
    return PenaltyParams(rho=rho, fixed_tokens=0, correction_turns=statistics.fmean(excess_corr),
                         source="observed", n_observed=n)
