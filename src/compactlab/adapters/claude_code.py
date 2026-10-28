"""Claude Code adapter: ~/.claude/projects/**/*.jsonl (including subagents/)."""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from ..model import Compaction, Session, Turn
from .base import Adapter, iter_jsonl, parse_ts, register


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _tool_key(block: dict) -> str:
    inp = block.get("input") or {}
    arg = inp.get("file_path") or inp.get("path") or inp.get("pattern") or inp.get("command") or inp.get("query") or ""
    return f"{block.get('name', '?')}:{str(arg)[:200]}"


@register
class ClaudeCodeAdapter(Adapter):
    name = "claude-code"
    default_root = "~/.claude/projects"

    def find_files(self) -> Iterator[Path]:
        if self.root.exists():
            yield from sorted(self.root.rglob("*.jsonl"))

    def parse_file(self, path: Path) -> Session | None:
        role = "subagent" if "subagents" in path.parts else "main"
        s = Session(id=path.stem, harness=self.name, role=role, path=str(path))
        seen: set[str] = set()
        pending_user = ""
        pending_summary: Compaction | None = None
        tool_ids: dict[str, int] = {}   # tool_use id -> (turn idx)
        for d in iter_jsonl(path):
            if d.get("isSidechain") and role == "main" and "subagents" not in path.parts:
                pass  # sidechain lines inside a main file are still that file's own turns
            if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
                meta = d.get("compactMetadata") or {}
                pending_summary = Compaction(turn_index=len(s.turns), pre_tokens=meta.get("preTokens"),
                                             ts=parse_ts(d.get("timestamp")))
                s.compactions.append(pending_summary)
                continue
            m = d.get("message")
            if not isinstance(m, dict):
                continue
            if m.get("role") == "user":
                content = m.get("content")
                text = _text_of(content)
                if d.get("isCompactSummary") and s.compactions:
                    s.compactions[-1].summary_text = text
                    continue
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            idx = tool_ids.get(b.get("tool_use_id"))
                            if idx is not None and idx < len(s.turns):
                                body = _text_of(b.get("content")) if not isinstance(b.get("content"), str) else b["content"]
                                t = s.turns[idx]
                                t.snippets.append(body[:400])
                                t.tool_result_tokens.append(len(body) // 4)
                if text and not d.get("isMeta"):
                    pending_user = text
                continue
            if m.get("role") != "assistant":
                continue
            mid = m.get("id")
            if mid:
                if mid in seen:
                    continue
                seen.add(mid)
            u = m.get("usage") or {}
            cr = u.get("cache_read_input_tokens") or 0
            cw = u.get("cache_creation_input_tokens") or 0
            ctx = (u.get("input_tokens") or 0) + cr + cw
            if ctx == 0:
                continue
            t = Turn(ts=parse_ts(d.get("timestamp")), ctx=ctx, cache_read=cr, cache_write=cw,
                     output=u.get("output_tokens") or 0, model=m.get("model") or "unknown",
                     user_text=pending_user)
            pending_user = ""
            content = m.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        t.tool_keys.append(_tool_key(b))
                        if b.get("id"):
                            tool_ids[b["id"]] = len(s.turns)
                        cmd = (b.get("input") or {}).get("command")
                        if cmd:
                            t.snippets.append(f"$ {str(cmd)[:200]}")
            s.turns.append(t)
        # drop compactions pointing past the end
        s.compactions = [c for c in s.compactions if c.turn_index < len(s.turns)]
        return s
