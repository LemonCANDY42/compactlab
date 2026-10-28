"""Normalized event model shared by all harness adapters."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Turn:
    """One model response (one billed API call)."""
    ts: float | None            # epoch seconds, None if unknown
    ctx: int                    # total input tokens seen by the model (input+cache_read+cache_write)
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    model: str = "unknown"
    tool_keys: list[str] = field(default_factory=list)   # e.g. "Read:/a/b.py", normalized
    tool_result_tokens: list[int] = field(default_factory=list)  # parallel to tool_keys (estimated)
    user_text: str = ""         # user text that preceded this turn (may be empty)
    snippets: list[str] = field(default_factory=list)    # truncated tool commands/results, for fact checks


@dataclass
class Compaction:
    turn_index: int             # index of the first turn *after* the boundary
    pre_tokens: int | None = None
    ts: float | None = None
    summary_text: str = ""


@dataclass
class Session:
    id: str
    harness: str                # "claude-code" | "codex" | ...
    role: str                   # "main" | "subagent"
    path: str
    turns: list[Turn] = field(default_factory=list)
    compactions: list[Compaction] = field(default_factory=list)
    context_window: int | None = None

    @property
    def model(self) -> str:
        counts: dict[str, int] = {}
        for t in self.turns:
            counts[t.model] = counts.get(t.model, 0) + 1
        return max(counts, key=counts.get) if counts else "unknown"

    @property
    def peak(self) -> int:
        return max((t.ctx for t in self.turns), default=0)
