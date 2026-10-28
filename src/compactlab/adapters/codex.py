"""Codex adapter: ~/.codex/sessions/**/rollout-*.jsonl.

Format knowledge (tolerantly parsed; unknown shapes are skipped):
  {"type":"session_meta","payload":{"id":...,"source":...}}
  {"type":"turn_context","payload":{"model":...}}
  {"type":"event_msg","payload":{"type":"token_count","info":{
       "total_token_usage":{...},"last_token_usage":{"input_tokens","cached_input_tokens",
       "output_tokens","reasoning_output_tokens"},"model_context_window":N}}}
  {"type":"response_item","payload":{"type":"function_call"|"function_call_output"|"message",...}}
  {"type":"compacted","payload":{"message":...}}  /  event_msg payload type "context_compacted"
OpenAI semantics: input_tokens already includes cached_input_tokens.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from ..model import Compaction, Session, Turn
from .base import Adapter, iter_jsonl, parse_ts, register


@register
class CodexAdapter(Adapter):
    name = "codex"
    default_root = "~/.codex/sessions"

    def find_files(self) -> Iterator[Path]:
        if self.root.exists():
            yield from sorted(self.root.rglob("*.jsonl"))

    def parse_file(self, path: Path) -> Session | None:
        s = Session(id=path.stem, harness=self.name, role="main", path=str(path))
        model = "unknown"
        last_total = None
        pending_tools: list[tuple[str, str]] = []
        pending_user = ""
        last_call_idx: dict[str, int] = {}
        for d in iter_jsonl(path):
            p = d.get("payload")
            typ = d.get("type")
            if not isinstance(p, dict):
                continue
            ts = parse_ts(d.get("timestamp"))
            if typ == "session_meta":
                s.id = str(p.get("id") or s.id)
                src = p.get("source")
                if (isinstance(src, dict) and "subagent" in json.dumps(src).lower()) or \
                        (isinstance(src, str) and "subagent" in src.lower()):
                    s.role = "subagent"
            elif typ == "turn_context":
                model = p.get("model") or model
            elif typ == "compacted" or (typ == "event_msg" and p.get("type") == "context_compacted"):
                if s.compactions and s.compactions[-1].turn_index == len(s.turns):
                    if typ == "compacted" and not s.compactions[-1].summary_text:
                        s.compactions[-1].summary_text = str(p.get("message") or "")
                else:
                    s.compactions.append(Compaction(turn_index=len(s.turns), ts=ts,
                                                    pre_tokens=s.turns[-1].ctx if s.turns else None,
                                                    summary_text=str(p.get("message") or "") if typ == "compacted" else ""))
            elif typ == "response_item":
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
                    pending_tools.append((p.get("call_id", ""), f"{p.get('name', '?')}:{str(cmd or args)[:200]}"))
                elif pt == "function_call_output" and s.turns:
                    out = p.get("output")
                    body = out if isinstance(out, str) else json.dumps(out)
                    idx = last_call_idx.get(p.get("call_id", ""), len(s.turns) - 1)
                    t = s.turns[idx]
                    t.snippets.append(body[:400])
                    t.tool_result_tokens.append(len(body) // 4)
                elif pt == "message" and p.get("role") == "user":
                    c = p.get("content")
                    if isinstance(c, list):
                        pending_user = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
            elif typ == "event_msg":
                pt = p.get("type")
                if pt == "user_message":
                    pending_user = str(p.get("message") or "")
                elif pt == "token_count":
                    info = p.get("info")
                    if not isinstance(info, dict):
                        continue
                    tot = (info.get("total_token_usage") or {}).get("total_tokens")
                    if tot is not None and tot == last_total:
                        continue  # duplicate emission
                    last_total = tot
                    last = info.get("last_token_usage") or {}
                    inp = last.get("input_tokens") or 0
                    if not inp:
                        continue
                    if info.get("model_context_window"):
                        s.context_window = int(info["model_context_window"])
                    t = Turn(ts=ts, ctx=inp, cache_read=last.get("cached_input_tokens") or 0, cache_write=0,
                             output=(last.get("output_tokens") or 0), model=model, user_text=pending_user)
                    pending_user = ""
                    for cid, key in pending_tools:
                        t.tool_keys.append(key)
                        last_call_idx[cid] = len(s.turns)
                    pending_tools = []
                    s.turns.append(t)
        s.compactions = [c for c in s.compactions if c.turn_index < len(s.turns)]
        return s
