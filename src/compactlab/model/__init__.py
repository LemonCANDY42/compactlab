"""Normalized event model shared by all harness adapters."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Turn:
    """One model response (one billed API call)."""

    ts: float | None  # epoch seconds, None if unknown
    ctx: int  # total input tokens seen by the model (input+cache_read+cache_write)
    cache_read: int = 0
    cache_write: int = 0
    cache_write_1h: int = 0  # part of cache_write billed at the 1-hour tier (Anthropic usage.cache_creation)
    output: int = 0
    model: str = "unknown"
    tool_keys: list[str] = field(default_factory=list)  # e.g. "Read:/a/b.py", normalized
    tool_result_tokens: list[int] = field(default_factory=list)  # parallel to tool_keys (estimated)
    user_text: str = ""  # user text that preceded this turn (may be empty)
    snippets: list[str] = field(default_factory=list)  # truncated tool commands/results, for fact checks
    start_ts: float | None = None  # when the request was sent (last user/tool-result line before it)


@dataclass
class Compaction:
    turn_index: int  # index of the first turn *after* the boundary
    pre_tokens: int | None = None
    ts: float | None = None
    summary_text: str = ""
    duration_s: float | None = None  # wall-clock spent compacting, if it can be inferred


@dataclass
class Session:
    id: str
    harness: str  # "claude-code" | "codex" | ...
    role: str  # "main" | "subagent"
    path: str
    turns: list[Turn] = field(default_factory=list)
    compactions: list[Compaction] = field(default_factory=list)
    context_window: int | None = None
    parent_id: str | None = None  # fork/resume lineage, when the harness records it
    unknown_events: dict[str, int] = field(default_factory=dict)  # event types the parser did not use
    parse_errors: int = 0  # malformed lines skipped
    skipped_inherited: int = 0  # usage events judged to be copied from a parent session

    @property
    def model(self) -> str:
        counts: dict[str, int] = {}
        for t in self.turns:
            counts[t.model] = counts.get(t.model, 0) + 1
        return max(counts, key=lambda k: counts[k]) if counts else "unknown"

    @property
    def peak(self) -> int:
        return max((t.ctx for t in self.turns), default=0)


def session_to_dict(s: Session) -> dict:
    """Fast JSON-ready dict (dataclasses.asdict deep-copies and is ~20x slower on large sessions)."""
    d = dict(vars(s))
    d["turns"] = [dict(vars(t)) for t in s.turns]
    d["compactions"] = [dict(vars(c)) for c in s.compactions]
    d["unknown_events"] = dict(s.unknown_events)
    return d


def snapshot(s: Session) -> Session:
    """Copy of a session that a parser can keep appending to without affecting the copy's lists."""
    out = Session(**{**vars(s), "turns": list(s.turns), "unknown_events": dict(s.unknown_events)})
    out.compactions = [Compaction(**vars(c)) for c in s.compactions if c.turn_index < len(s.turns)]
    return out


def session_from_dict(d: dict) -> Session:
    d = dict(d)
    d["turns"] = [Turn(**t) for t in d.get("turns", [])]
    d["compactions"] = [Compaction(**c) for c in d.get("compactions", [])]
    return Session(**d)
