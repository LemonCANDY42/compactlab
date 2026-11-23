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
    trigger: str = ""  # "auto" | "manual" (/compact) | "" when the log does not say


def is_auto(c: Compaction) -> bool:
    """A compaction the harness's threshold caused. Unknown counts as auto; only a logged manual one is excluded."""
    return c.trigger != "manual"


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


def drop_inherited(sessions: list[Session]) -> list[Session]:
    """Remove history a resumed or forked session copied from an earlier session's log.

    Claude Code writes the parent's messages into the new file with their original timestamps, so the same API call
    would be billed and counted once per copy. The earliest session keeps a turn (or compaction); later copies drop
    it and count it in `skipped_inherited`. Sessions left empty are dropped. Turns without a timestamp are kept.
    """
    seen_turns: set[tuple] = set()
    seen_comps: set[tuple] = set()
    out: list[Session] = []
    for s in sorted(sessions, key=lambda x: min((t.ts for t in x.turns if t.ts), default=float("inf"))):
        keep: list[Turn] = []
        new_index: list[int] = []  # original turn index -> index among the kept turns
        for t in s.turns:
            new_index.append(len(keep))
            key = (t.ts, t.ctx, t.cache_read, t.cache_write, t.output, t.model)
            if t.ts is not None and key in seen_turns:
                continue
            if t.ts is not None:
                seen_turns.add(key)
            keep.append(t)
        comps = []
        for c in s.compactions:
            ckey = (c.ts, c.pre_tokens, c.trigger)
            if c.ts is not None and ckey in seen_comps:
                continue
            if c.ts is not None:
                seen_comps.add(ckey)
            if c.turn_index < len(new_index):
                c.turn_index = new_index[c.turn_index]
            else:
                c.turn_index = len(keep)
            if c.turn_index < len(keep):
                comps.append(c)
        if len(keep) != len(s.turns):
            s.skipped_inherited += len(s.turns) - len(keep)
            s.turns, s.compactions = keep, comps
        if s.turns:
            out.append(s)
    return out
