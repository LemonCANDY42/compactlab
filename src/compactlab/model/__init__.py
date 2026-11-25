"""Normalized event model shared by all harness adapters."""

from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass, field
from typing import Any

# A user message that corrects the agent ("I already told you", "你忘了"): counted by the re-discovery penalty.
CORRECTION_RE = re.compile(
    r"(\bi (already )?(told|said|mentioned)\b|\byou forgot\b|\bas i said\b|\bnot what i\b|"
    r"\bwe (already )?(did|discussed|decided)\b|\balready (told|said|did)\b|"
    r"我说过|我刚才说|你忘了|不是这个|已经说过|之前说过|刚才不是)",
    re.I,
)
# Every match of CORRECTION_RE contains one of these (lowercase), starting at most _BEFORE characters after the
# match starts and ending at most _AFTER characters before it ends ("i already told", "we already discussed"). So
# the regex need only run on short windows around them, which is many times faster on long messages. `str.lower`
# keeps positions and differs from the regex's case-insensitive matching only for the four letters below; a text
# with one of them goes straight to the regex.
_CORRECTION_WORDS = ("told", "said", "mentioned", "you forgot", "not what i", "we did", "we discussed", "we decided",
                     "we already ", "already did", "我说过", "我刚才说", "你忘了", "不是这个", "已经说过", "之前说过",
                     "刚才不是")  # fmt: skip
_BEFORE, _AFTER = 10, 9
_FOLDS_TO_ASCII = ("\u0130", "\u0131", "\u017f", "\u212a")  # İ ı ſ K


def corrects(text: str) -> bool:
    """Whether a user message corrects the agent: exactly `CORRECTION_RE.search(text)`, run only where it can match.

    A window starts _BEFORE characters before a word (the regex sees the real text before it, so `\b` there is
    exact) and ends _AFTER + 1 after it, merged with overlapping ones: a match ends before the window's end, so the
    end of the window never stands in for the end of the text."""
    if not text:
        return False
    if any(c in text for c in _FOLDS_TO_ASCII):
        return CORRECTION_RE.search(text) is not None
    low = text.lower()
    spans = []
    for w in _CORRECTION_WORDS:
        i = low.find(w)
        while i >= 0:
            spans.append((max(i - _BEFORE, 0), i + len(w) + _AFTER + 1))
            i = low.find(w, i + 1)
    if not spans:
        return False
    spans.sort()
    start, end = spans[0]
    for a, b in spans[1:]:
        if a > end:
            if CORRECTION_RE.search(text, start, end):
                return True
            start = a
        end = max(end, b)
    return CORRECTION_RE.search(text, start, end) is not None


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
    correction: bool = False  # set by `lean()`: user_text matched CORRECTION_RE before it was dropped

    def corrects(self) -> bool:
        """Whether the user text before this turn corrects the agent (from the text, or from a lean turn's flag)."""
        return self.correction or corrects(self.user_text)


@dataclass
class Compaction:
    turn_index: int  # index of the first turn *after* the boundary
    pre_tokens: int | None = None
    ts: float | None = None
    summary_text: str = ""
    duration_s: float | None = None  # wall-clock spent compacting, if it can be inferred
    trigger: str = ""  # "auto" | "manual" (/compact) | "" when the log does not say
    idle: bool = False  # Claude Code idle compaction: precomputed while the cache was warm, applied on return
    summary_chars: int = 0  # set by `lean()`: the length of summary_text before it was dropped

    @property
    def summary_len(self) -> int:
        """Characters of the summary: of its text, or of a lean compaction's recorded length."""
        return len(self.summary_text) if self.summary_text else self.summary_chars

    def set_summary(self, text: str) -> None:
        """Replace the summary (a parser that learns it after the boundary, possibly on a lean compaction)."""
        self.summary_text, self.summary_chars = text, 0


def is_auto(c: Compaction) -> bool:
    """A compaction the harness made by itself (threshold or idle). Unknown counts as auto; only a logged manual one
    is excluded."""
    return c.trigger != "manual"


def is_threshold(c: Compaction) -> bool:
    """A compaction the auto-compact threshold caused: automatic and not an idle compaction."""
    return is_auto(c) and not c.idle


@dataclass
class Session:
    id: str
    harness: str  # "claude-code" | "codex" | ...
    role: str  # "main" | "subagent"
    path: str
    turns: list[Turn] = field(default_factory=list)
    compactions: list[Compaction] = field(default_factory=list)
    context_window: int | None = None
    platform: str | None = None  # where the model ran, when the log says (Codex `model_provider`)
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


# ---------- lean sessions: what the lite analysis needs, without the transcript's text ----------
#
# The lite analysis reads text in three places only: whether a user message corrects the agent (CORRECTION_RE), which
# tool calls repeat (identity and tool name), and how long a compaction summary is. A lean session keeps those as a
# flag, a hashed key and a length, and drops the prompts, output snippets and summaries. The scan cache stores lean
# sessions and the lite analysis runs on them (a fraction of the memory); deep mode, which reads text, parses the
# logs itself. `lean` is idempotent, and a parser resumed from a lean session keeps working: it only appends to
# earlier turns, never reads their text.

_HASHED = "\x00"  # never in a tool key a harness logged


def tool_id(key: str) -> str:
    """A tool key without its argument's text: the tool name (what `_is_rediscovery` reads) and a 96-bit hash of the
    whole key, so equal keys stay equal. `key.split(":", 1)[0]` is the same before and after."""
    if _HASHED in key:
        return sys.intern(key)
    digest = hashlib.blake2b(key.encode(), digest_size=12).hexdigest()
    return sys.intern(f"{key.partition(':')[0]}:{_HASHED}{digest}")


def lean(s: Session) -> Session:
    """Drop the text of a session in place (see above) and share repeated strings; returns it."""
    for t in s.turns:
        if t.user_text:
            t.correction = t.corrects()
            t.user_text = ""
        if t.snippets:
            t.snippets = []
        if t.tool_keys:
            t.tool_keys = [tool_id(k) for k in t.tool_keys]
        t.model = sys.intern(t.model)
    for c in s.compactions:
        if c.summary_text:
            c.summary_chars, c.summary_text = len(c.summary_text), ""
    return s


def lean_dict(d: dict[str, Any]) -> dict[str, Any]:
    """`lean` for a session as `session_to_dict` gives it; returns a new dict and leaves `d` (and its lists) alone."""
    out = dict(d)
    turns = []
    for t in d.get("turns", []):
        t = dict(t)
        if t.get("user_text"):
            t["correction"] = bool(t.get("correction")) or corrects(t["user_text"])
            t["user_text"] = ""
        if t.get("snippets"):
            t["snippets"] = []
        if t.get("tool_keys"):
            t["tool_keys"] = [tool_id(k) for k in t["tool_keys"]]
        turns.append(t)
    out["turns"] = turns
    comps = []
    for c in d.get("compactions", []):
        if c.get("summary_text"):
            c = {**c, "summary_chars": len(c["summary_text"]), "summary_text": ""}
        comps.append(c)
    out["compactions"] = comps
    return out


# what a pending user message that corrects the agent is kept as in the scan cache: `lean` keeps only that it does
CORRECTION_MARK = "as I said"


def lean_state(st: dict[str, Any]) -> dict[str, Any]:
    """A parser's resume state (`Parser.dump`, without its session) with no text, for the scan cache; resuming from
    it gives the same lean session as a full parse. What the log has not yet attached to a turn keeps only what
    `lean` keeps of it: of a user message, whether it corrects the agent; of a tool call, its key's tool name and
    hash; of its output, the token count. Returns a new dict."""
    out = dict(st)
    if out.get("pending_user"):
        out["pending_user"] = CORRECTION_MARK if corrects(out["pending_user"]) else ""
    if out.get("pending_tools"):
        out["pending_tools"] = [[cid, tool_id(key)] for cid, key in out["pending_tools"]]
    if out.get("pending_results"):
        out["pending_results"] = {cid: ["", toks] for cid, (_, toks) in out["pending_results"].items()}
    return out


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
