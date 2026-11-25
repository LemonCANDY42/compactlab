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
from .base import Adapter, BadRecord, Parser, count, env_roots, parse_ts, register

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
TOOL_OUTPUTS = ("function_call_output", "custom_tool_call_output")
MODEL_OUTPUTS = ("reasoning", "function_call", "custom_tool_call")  # plus an assistant message


def _output_text(out: Any) -> str:
    """A tool output as text: a string as is; a list of content blocks as its text blocks (an image's base64 payload
    is not context text); anything else as JSON."""
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        return "".join(str(b.get("text", "")) for b in out if isinstance(b, dict) and isinstance(b.get("text"), str))
    return json.dumps(out)


def _dur(start: float | None, end: float | None) -> float | None:
    if start is None or end is None:
        return None
    d = end - start
    return d if 0 < d <= 900 else None


def _spawn_parent(src: Any) -> str | None:
    """A subagent's parent when only its spawn record names it (`source.subagent.thread_spawn.parent_thread_id`)."""
    sub = src.get("subagent") if isinstance(src, dict) else None
    spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
    pid = spawn.get("parent_thread_id") if isinstance(spawn, dict) else None
    return str(pid) if pid else None


def _is_subagent(src: Any) -> bool:
    if isinstance(src, str):
        return src.strip().lower() == "subagent"
    if isinstance(src, dict):
        return "subagent" in src
    return False


class CodexParser(Parser):
    version = 6  # 5: custom_tool_call(_output), list outputs, response timing; 6: model_provider
    head_records = 1  # session_meta: which session this file is (a fork's copied history names other ones)

    def __init__(self, path: str):
        self.session = Session(id=Path(path).stem, harness="codex", role="main", path=path)
        self.model = "unknown"
        self.leaf_id: str | None = None
        self.last_total: list[int] | None = None  # [input, cached, output, total]
        self.pending_tools: list[list[str]] = []
        self.pending_user = ""
        self.call_turn: dict[str, int] = {}
        self.replaying = False
        self.last_input_ts: float | None = None  # last user message / tool output
        # the response being generated: the input it answers and its last output item. Codex writes a response's
        # token_count after its tool already ran, so token_count minus the last input would time the tool, not the model
        self.resp_start: float | None = None
        self.resp_end: float | None = None
        self.pending_results: dict[str, list[Any]] = {}  # call_id -> [snippet, tokens] for a call not yet in a turn

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
                s.parent_id = next((str(p[k]) for k in PARENT_KEYS if p.get(k)), None) or _spawn_parent(p.get("source"))
                s.platform = str(p["model_provider"]) if p.get("model_provider") else None
                self.cwd = str(p["cwd"]) if p.get("cwd") else None
            elif sid and sid != self.leaf_id:
                self.replaying = True  # embedded ancestor metadata: what follows is copied history
        elif typ == "turn_context":
            self.model = str(p.get("model") or self.model)
            self.replaying = False
            self.cwd = str(p["cwd"]) if p.get("cwd") else self.cwd
        elif typ == "compacted" or (typ == "event_msg" and p.get("type") == "context_compacted"):
            if self.replaying:
                return
            msg = str(p.get("message") or "") if typ == "compacted" else ""
            if s.compactions and s.compactions[-1].turn_index == len(s.turns):
                if not s.compactions[-1].summary_len:  # a lean compaction keeps only the summary's length
                    s.compactions[-1].set_summary(msg)
            else:
                s.compactions.append(
                    Compaction(
                        turn_index=len(s.turns),
                        ts=ts,
                        summary_text=msg,
                        pre_tokens=s.turns[-1].ctx if s.turns else None,
                        duration_s=_dur(self.last_input_ts, ts),
                    )
                )
            self.last_input_ts = ts or self.last_input_ts
        elif typ == "response_item":
            self._item(p)
            pt, role = p.get("type"), p.get("role")
            if pt in MODEL_OUTPUTS or (pt == "message" and role == "assistant"):
                if self.resp_end is None:
                    self.resp_start = self.last_input_ts
                self.resp_end = ts or self.resp_end
            elif pt in TOOL_OUTPUTS or (pt == "message" and role == "user"):
                if pt == "message":  # a new prompt: a response without a token_count (aborted) is over
                    self.resp_start = self.resp_end = None
                self.last_input_ts = ts or self.last_input_ts
        elif typ == "event_msg":
            et = str(p.get("type"))
            if et not in KNOWN_EVENTS:
                self.note_unknown(s, f"event_msg:{et}")
            if et in ("user_message", "turn_aborted"):  # either way, a response in progress ended without usage
                self.resp_start = self.resp_end = None
            if et == "user_message":
                self.pending_user = str(p.get("message") or "")
                self.last_input_ts = ts or self.last_input_ts
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
            cmd = (a.get("command") or a.get("cmd")) if isinstance(a, dict) else None
            if isinstance(cmd, list):
                cmd = " ".join(map(str, cmd))
            self.pending_tools.append([str(p.get("call_id", "")), f"{p.get('name', '?')}:{str(cmd or args)[:200]}"])
        elif pt == "custom_tool_call":  # freeform tools (e.g. `exec`): the input is the raw text
            self.pending_tools.append(
                [str(p.get("call_id", "")), f"{p.get('name', '?')}:{str(p.get('input', ''))[:200]}"]
            )
        elif pt in TOOL_OUTPUTS:
            body = _output_text(p.get("output"))
            cid = str(p.get("call_id", ""))
            if cid not in self.call_turn and any(c == cid for c, _ in self.pending_tools):
                self.pending_results[cid] = [body[:400], len(body) // 4]  # its turn comes with the next token_count
            elif s.turns:
                idx = self.call_turn.get(cid, len(s.turns) - 1)
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
        try:
            tot = info.get("total_token_usage") or {}
            last = info.get("last_token_usage")
            if not isinstance(tot, dict) or not (last is None or isinstance(last, dict)):
                raise BadRecord("usage is not an object")
            cur = [count(tot.get(k)) for k in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")]
            window = count(info.get("model_context_window"))
            lastv = (
                [count(last.get(k)) for k in ("input_tokens", "cached_input_tokens", "output_tokens")]
                if last is not None
                else None
            )
        except BadRecord:
            s.parse_errors += 1
            return
        if tot and cur == self.last_total:
            return  # identical re-emission
        prev, self.last_total = self.last_total, (cur if tot else self.last_total)
        if window:
            s.context_window = window
        if lastv is not None:
            inp, cached, out = lastv
        elif tot and prev:
            inp, cached, out = cur[0] - prev[0], cur[1] - prev[1], cur[2] - prev[2]
        else:
            return
        start, end = (self.resp_start, self.resp_end) if self.resp_end is not None else (self.last_input_ts, ts)
        self.resp_start = self.resp_end = None
        if self.replaying or (inp == 0 and out == 0):
            s.skipped_inherited += 1  # copied parent prefix or zero-usage inherited opening
            self.pending_tools, self.pending_results = [], {}
            return
        t = Turn(
            ts=end,
            ctx=inp,
            cache_read=cached,
            cache_write=0,
            output=out,
            model=self.model,
            user_text=self.pending_user,
            start_ts=start,
        )
        self.pending_user = ""
        for cid, key in self.pending_tools:
            t.tool_keys.append(key)
            self.call_turn[cid] = len(s.turns)
            if cid in self.pending_results:
                snippet, toks = self.pending_results.pop(cid)
                t.snippets.append(snippet)
                t.tool_result_tokens.append(toks)
        self.pending_tools, self.pending_results = [], {}
        s.turns.append(t)

    def finish(self) -> Session:
        from ..model import snapshot

        return snapshot(self.session)

    def dump(self) -> dict[str, Any]:
        from ..model import session_to_dict

        st = {k: v for k, v in self.__dict__.items() if k not in ("session", "cwd")}  # paths stay out of the cache
        return {"path": self.session.path, "session": session_to_dict(self.session), **st}

    @classmethod
    def load(cls, state: dict[str, Any]) -> CodexParser:
        from ..model import session_from_dict

        p = cls(state["path"])
        if "session" in state:
            p.session = session_from_dict(state["session"])
            for k in (
                "model",
                "leaf_id",
                "last_total",
                "pending_tools",
                "pending_user",
                "call_turn",
                "replaying",
                "last_input_ts",
                "resp_start",
                "resp_end",
                "pending_results",
            ):
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
