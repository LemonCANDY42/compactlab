"""Claude Code adapter.

Transcripts: `$CLAUDE_CONFIG_DIR/projects` (comma-separated dirs allowed), else `~/.claude/projects`
and the legacy `~/.config/claude/projects`. Files under a `subagents/` directory are subagent runs.
See `data/harness_formats.json` for the format facts this relies on and their verification status.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..model import Compaction, Session, Turn
from .base import Adapter, Parser, env_roots, parse_ts, register

KNOWN_TYPES = {"user", "assistant", "system", "summary", "file-history-snapshot", "attachment", "progress"}


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _tool_key(block: dict[str, Any]) -> str:
    inp = block.get("input") or {}
    arg = inp.get("file_path") or inp.get("path") or inp.get("pattern") or inp.get("command") or inp.get("query") or ""
    return f"{block.get('name', '?')}:{str(arg)[:200]}"


class ClaudeParser(Parser):
    version = 3

    def __init__(self, path: str):
        role = "subagent" if "subagents" in Path(path).parts else "main"
        self.session = Session(id=Path(path).stem, harness="claude-code", role=role, path=path)
        self.seen: set[str] = set()
        self.pending_user = ""
        self.tool_turn: dict[str, int] = {}
        self.last_input_ts: float | None = None  # last user / tool-result / system line

    def feed(self, d: dict[str, Any]) -> None:
        s = self.session
        typ = d.get("type")
        if typ not in KNOWN_TYPES:
            self.note_unknown(s, str(typ))
        if s.parent_id is None and d.get("parentSessionId"):
            s.parent_id = str(d["parentSessionId"])
        ts = parse_ts(d.get("timestamp"))
        if typ == "system" and d.get("subtype") == "compact_boundary":
            meta = d.get("compactMetadata") or {}
            dur = (ts - self.last_input_ts) if (ts and self.last_input_ts) else None
            s.compactions.append(
                Compaction(
                    turn_index=len(s.turns),
                    pre_tokens=meta.get("preTokens"),
                    ts=ts,
                    duration_s=dur if dur is not None and 0 < dur <= 900 else None,
                )
            )
            self.last_input_ts = ts
            return
        m = d.get("message")
        if not isinstance(m, dict):
            return
        if m.get("role") == "user":
            self._user(d, m)
            if ts:
                self.last_input_ts = ts
        elif m.get("role") == "assistant":
            self._assistant(d, m)

    def _user(self, d: dict[str, Any], m: dict[str, Any]) -> None:
        s = self.session
        content = m.get("content")
        text = _text_of(content)
        if d.get("isCompactSummary"):
            if s.compactions:
                s.compactions[-1].summary_text = text
            return
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    idx = self.tool_turn.get(str(b.get("tool_use_id")))
                    if idx is not None and idx < len(s.turns):
                        c = b.get("content")
                        body = c if isinstance(c, str) else _text_of(c)
                        s.turns[idx].snippets.append(body[:400])
                        s.turns[idx].tool_result_tokens.append(len(body) // 4)
        if text and not d.get("isMeta"):
            self.pending_user = text

    def _assistant(self, d: dict[str, Any], m: dict[str, Any]) -> None:
        s = self.session
        mid = m.get("id")
        if mid:
            # Streaming writes the same API response several times; message id (+ request id) identifies it.
            key = f"{mid}:{d.get('requestId', '')}"
            if key in self.seen:
                return
            self.seen.add(key)
        u = m.get("usage") or {}
        cr = int(u.get("cache_read_input_tokens") or 0)
        cw = int(u.get("cache_creation_input_tokens") or 0)
        ctx = int(u.get("input_tokens") or 0) + cr + cw
        if ctx == 0:
            return
        t = Turn(
            ts=parse_ts(d.get("timestamp")),
            ctx=ctx,
            cache_read=cr,
            cache_write=cw,
            output=int(u.get("output_tokens") or 0),
            start_ts=self.last_input_ts,
            model=str(m.get("model") or "unknown"),
            user_text=self.pending_user,
        )
        self.pending_user = ""
        content = m.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    t.tool_keys.append(_tool_key(b))
                    if b.get("id"):
                        self.tool_turn[str(b["id"])] = len(s.turns)
                    cmd = (b.get("input") or {}).get("command")
                    if cmd:
                        t.snippets.append(f"$ {str(cmd)[:200]}")
        s.turns.append(t)

    def finish(self) -> Session:
        from ..model import snapshot

        return snapshot(self.session)

    def dump(self) -> dict[str, Any]:
        from ..model import session_to_dict

        return {
            "path": self.session.path,
            "session": session_to_dict(self.session),
            "seen": sorted(self.seen),
            "pending_user": self.pending_user,
            "tool_turn": self.tool_turn,
            "last_input_ts": self.last_input_ts,
        }

    @classmethod
    def load(cls, state: dict[str, Any]) -> ClaudeParser:
        from ..model import session_from_dict

        p = cls(state["path"])
        if "session" in state:
            p.session = session_from_dict(state["session"])
            p.seen = set(state.get("seen", []))
            p.pending_user = state.get("pending_user", "")
            p.tool_turn = dict(state.get("tool_turn", {}))
            p.last_input_ts = state.get("last_input_ts")
        return p


@register
class ClaudeCodeAdapter(Adapter):
    name = "claude-code"
    parser_cls = ClaudeParser

    @classmethod
    def default_roots(cls) -> list[Path]:
        return env_roots("CLAUDE_CONFIG_DIR", "projects", ["~/.claude/projects", "~/.config/claude/projects"])

    @classmethod
    def config_paths(cls) -> list[Path]:
        base = env_roots("CLAUDE_CONFIG_DIR", "", ["~/.claude"])
        return [b / "settings.json" for b in base]
