"""Codex adapter: `$CODEX_HOME/sessions` (default `~/.codex/sessions`) and `archived_sessions`.

Rollout lines are `{"timestamp", "type", "payload"}`. Token usage comes from
`event_msg/token_count` with `info.total_token_usage` (cumulative) and `info.last_token_usage`
(this request). We use `last_token_usage` as the per-request context size and only fall back to
the delta of cumulative totals when `last` is missing. Identical re-emitted snapshots are skipped.
OpenAI semantics: `input_tokens` already includes `cached_input_tokens`.

Forks and subagents can start with a copied parent prefix. Signals handled here (ideas from
CodexBar's rollout-shape analysis, re-implemented): a `forked_from_id` on session_meta, a
zero-usage opening snapshot (inherited context, not new usage), and an embedded ancestor
`session_meta` with a different id, after which replayed usage is dropped until the next
`turn_context`. These are heuristics; see data/harness_formats.json for verification status.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..model import Compaction, Session, Turn
from .base import Adapter, Parser, env_roots, parse_ts, register

KNOWN_TYPES = {"session_meta", "turn_context", "event_msg", "response_item", "compacted"}
KNOWN_EVENTS = {
    "token_count",
    "user_message",
    "agent_message",
    "agent_reasoning",
    "context_compacted",
    "task_started",
    "task_complete",
    "turn_aborted",
    "exec_command_begin",
    "exec_command_end",
}
PARENT_KEYS = ("forked_from_id", "forkedFromId", "parent_session_id", "parentSessionId")


def _is_subagent(src: Any) -> bool:
    if isinstance(src, str):
        return src.strip().lower() == "subagent"
    if isinstance(src, dict):
        return "subagent" in src
    return False


class CodexParser(Parser):
    version = 2

    def __init__(self, path: str):
        self.session = Session(id=Path(path).stem, harness="codex", role="main", path=path)
        self.model = "unknown"
        self.leaf_id: str | None = None
        self.last_total: list[int] | None = None  # [input, cached, output, total]
        self.pending_tools: list[list[str]] = []
        self.pending_user = ""
        self.call_turn: dict[str, int] = {}
        self.replaying = False

    def feed(self, d: dict[str, Any]) -> None:
        s = self.session
        p = d.get("payload")
        typ = str(d.get("type"))
        if typ not in KNOWN_TYPES:
            self.note_unknown(s, typ)
            return
        if not isinstance(p, dict):
            return
        ts = parse_ts(d.get("timestamp"))
        if typ == "session_meta":
            sid = str(p.get("id") or "")
            if self.leaf_id is None:
                self.leaf_id = sid or s.id
                s.id = self.leaf_id
                s.role = "subagent" if _is_subagent(p.get("source")) else s.role
                s.parent_id = next((str(p[k]) for k in PARENT_KEYS if p.get(k)), None)
            elif sid and sid != self.leaf_id:
                self.replaying = True  # embedded ancestor metadata: what follows is copied history
        elif typ == "turn_context":
            self.model = str(p.get("model") or self.model)
            self.replaying = False
        elif typ == "compacted" or (typ == "event_msg" and p.get("type") == "context_compacted"):
            if self.replaying:
                return
            msg = str(p.get("message") or "") if typ == "compacted" else ""
            if s.compactions and s.compactions[-1].turn_index == len(s.turns):
                s.compactions[-1].summary_text = s.compactions[-1].summary_text or msg
            else:
                s.compactions.append(
                    Compaction(
                        turn_index=len(s.turns),
                        ts=ts,
                        summary_text=msg,
                        pre_tokens=s.turns[-1].ctx if s.turns else None,
                    )
                )
        elif typ == "response_item":
            self._item(p)
        elif typ == "event_msg":
            et = str(p.get("type"))
            if et not in KNOWN_EVENTS:
                self.note_unknown(s, f"event_msg:{et}")
            if et == "user_message":
                self.pending_user = str(p.get("message") or "")
            elif et == "token_count":
                self._tokens(p, ts)

    def _item(self, p: dict[str, Any]) -> None:
        s = self.session
        pt = p.get("type")
        if pt == "function_call":
            args = p.get("arguments")
            try:
                a = json.loads(args) if isinstance(args, str) else (args or {})
            except ValueError:
                a = {}
            cmd = a.get("command") if isinstance(a, dict) else None
            if isinstance(cmd, list):
                cmd = " ".join(map(str, cmd))
            self.pending_tools.append([str(p.get("call_id", "")), f"{p.get('name', '?')}:{str(cmd or args)[:200]}"])
        elif pt == "function_call_output" and s.turns:
            out = p.get("output")
            body = out if isinstance(out, str) else json.dumps(out)
            idx = self.call_turn.get(str(p.get("call_id", "")), len(s.turns) - 1)
            s.turns[idx].snippets.append(body[:400])
            s.turns[idx].tool_result_tokens.append(len(body) // 4)
        elif pt == "message" and p.get("role") == "user":
            c = p.get("content")
            if isinstance(c, list):
                self.pending_user = " ".join(str(b.get("text", "")) for b in c if isinstance(b, dict))

    def _tokens(self, p: dict[str, Any], ts: float | None) -> None:
        s = self.session
        info = p.get("info")
        if not isinstance(info, dict):
            return
        tot = info.get("total_token_usage") or {}
        cur = [int(tot.get(k) or 0) for k in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")]
        if tot and cur == self.last_total:
            return  # identical re-emission
        prev, self.last_total = self.last_total, (cur if tot else self.last_total)
        if info.get("model_context_window"):
            s.context_window = int(info["model_context_window"])
        last = info.get("last_token_usage")
        if isinstance(last, dict):
            inp, cached, out = (int(last.get(k) or 0) for k in ("input_tokens", "cached_input_tokens", "output_tokens"))
        elif tot and prev:
            inp, cached, out = cur[0] - prev[0], cur[1] - prev[1], cur[2] - prev[2]
        else:
            return
        if self.replaying or (inp == 0 and out == 0):
            s.skipped_inherited += 1  # copied parent prefix or zero-usage inherited opening
            self.pending_tools = []
            return
        t = Turn(
            ts=ts, ctx=inp, cache_read=cached, cache_write=0, output=out, model=self.model, user_text=self.pending_user
        )
        self.pending_user = ""
        for cid, key in self.pending_tools:
            t.tool_keys.append(key)
            self.call_turn[cid] = len(s.turns)
        self.pending_tools = []
        s.turns.append(t)

    def finish(self) -> Session:
        from ..model import session_from_dict, session_to_dict

        out = session_from_dict(session_to_dict(self.session))
        out.compactions = [c for c in out.compactions if c.turn_index < len(out.turns)]
        return out

    def dump(self) -> dict[str, Any]:
        from ..model import session_to_dict

        st = {k: v for k, v in self.__dict__.items() if k != "session"}
        return {"path": self.session.path, "session": session_to_dict(self.session), **st}

    @classmethod
    def load(cls, state: dict[str, Any]) -> CodexParser:
        from ..model import session_from_dict

        p = cls(state["path"])
        if "session" in state:
            p.session = session_from_dict(state["session"])
            for k in ("model", "leaf_id", "last_total", "pending_tools", "pending_user", "call_turn", "replaying"):
                setattr(p, k, state.get(k, getattr(p, k)))
        return p


@register
class CodexAdapter(Adapter):
    name = "codex"
    parser_cls = CodexParser

    @classmethod
    def default_roots(cls) -> list[Path]:
        base = env_roots("CODEX_HOME", "", ["~/.codex"])
        return [b / sub for b in base for sub in ("sessions", "archived_sessions")]

    @classmethod
    def config_paths(cls) -> list[Path]:
        return [b / "config.toml" for b in env_roots("CODEX_HOME", "", ["~/.codex"])]
