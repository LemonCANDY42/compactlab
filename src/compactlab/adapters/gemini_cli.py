"""Gemini CLI adapter. Experimental: built from the public source (v0.46.0 and v0.70.0); no real chat log was
available to check it against.

Logs: `$GEMINI_CLI_HOME/.gemini/tmp` (default `~/.gemini/tmp`), as
`<project>/chats/session-<YYYY-MM-DDTHH-MM>-<id8>.jsonl`; a subagent run is `<project>/chats/<parent session>/
<session>.jsonl` with `kind: "subagent"` in its header. `<project>` is a slug from `~/.gemini/projects.json` (older
versions used a SHA-256 of the project root). Older versions wrote one `session-*.json` document instead; it is read
the same way unless a `.jsonl` of the same name replaced it.

Lines: a header `{sessionId, projectHash, startTime, lastUpdated, kind?}`, message lines `{id, timestamp, type, ...}`
with type `user | gemini | info | error | warning`, and updates `{"$set": ...}`, `{"$rewindTo": id}`, `{"$patch": ...}`
(0.70+). A message is written again with the same id when its tokens or tool calls are attached; the last line wins,
so turns are keyed by id. Each `gemini` message is one API call with `tokens {input, output, cached, thoughts, tool,
total}`, where `input` is the whole prompt INCLUDING `cached` (Gemini's promptTokenCount). Tool results come back as
`user` messages made of functionResponse parts.

Compression leaves no explicit record. Its summary is a synthetic user message whose text holds `<state_snapshot>`:
appended as a line (0.70+, followed by a `$patch` that removes the old ids) or inside a `{"$set": {"messages": [...]}}`
checkpoint (0.46). The summary call's own usage is not written, and `/compress` looks the same as an automatic one.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..model import Compaction, Session, Turn
from .base import Adapter, BadRecord, Parser, _bump_error, count, env_roots, iter_jsonl, parse_ts, register

MESSAGE_TYPES = {"user", "gemini", "info", "error", "warning"}
SNAPSHOT = "<state_snapshot>"
_ARG_KEYS = ("file_path", "absolute_path", "path", "dir_path", "pattern", "command", "query", "url")


def token_limit(model: str) -> int:
    """Gemini CLI's own window for a model (core/tokenLimits.ts): 256k for Gemma 4, else 1,048,576."""
    return 256_000 if "gemma-4" in (model or "").lower() else 1_048_576


def _obj(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _parts(content: Any) -> list[Any]:
    if isinstance(content, list):
        return content
    return [content] if content else []


def _text(content: Any) -> str:
    out = []
    for p in _parts(content):
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict) and isinstance(p.get("text"), str):
            out.append(p["text"])
    return " ".join(out)


def _is_tool_result(content: Any) -> bool:
    return any(isinstance(p, dict) and "functionResponse" in p for p in _parts(content))


def _tool_key(call: dict[str, Any]) -> str:
    args = _obj(call.get("args"))
    arg = next((args[k] for k in _ARG_KEYS if args.get(k)), "")
    return f"{call.get('name', '?')}:{str(arg)[:200]}"


class GeminiParser(Parser):
    version = 1

    def __init__(self, path: str):
        p = Path(path)
        sub = p.parent.name != "chats" and p.parent.parent.name == "chats"
        self.session = Session(id=p.stem, harness="gemini-cli", role="subagent" if sub else "main", path=path)
        if sub:
            self.session.parent_id = p.parent.name
        self.turn_of: dict[str, int] = {}  # gemini message id -> turn index
        self.users: list[str] = []  # user message ids already read
        self.snapshots: list[str] = []  # summary message ids already counted as a compaction
        self.pending_user = ""
        self.last_input_ts: float | None = None

    def feed(self, d: dict[str, Any]) -> None:
        s = self.session
        try:
            if "$set" in d:
                msgs = _obj(d["$set"]).get("messages")
                for m in msgs if isinstance(msgs, list) else []:
                    if isinstance(m, dict) and m.get("type") == "user" and SNAPSHOT in _text(m.get("content")):
                        self._compaction(m)  # a 0.46 checkpoint that replaced the history
            elif "$rewindTo" in d or "$patch" in d:
                pass  # rewinds and patches change the saved history, not what was billed
            elif "id" in d and "type" in d:
                self._message(d)
            elif "sessionId" in d:
                s.id = str(d["sessionId"])
                if d.get("kind") == "subagent":
                    s.role = "subagent"
            else:
                self.note_unknown(s, "record")
        except BadRecord:
            s.parse_errors += 1

    def _message(self, d: dict[str, Any]) -> None:
        typ, mid, ts = str(d.get("type")), str(d.get("id")), parse_ts(d.get("timestamp"))
        if typ not in MESSAGE_TYPES:
            self.note_unknown(self.session, f"message:{typ}")
        elif typ == "user":
            text = _text(d.get("content"))
            if SNAPSHOT in text:
                self._compaction(d)
            elif mid not in self.users:
                self.users.append(mid)
                if _is_tool_result(d.get("content")):
                    self.last_input_ts = ts or self.last_input_ts
                else:
                    self.pending_user = (self.pending_user + " " + text).strip()
                    self.last_input_ts = ts or self.last_input_ts
        elif typ == "gemini":
            self._gemini(d, mid, ts)

    def _gemini(self, d: dict[str, Any], mid: str, ts: float | None) -> None:
        s = self.session
        tk = d.get("tokens")
        idx = self.turn_of.get(mid)
        if not isinstance(tk, dict):
            if idx is not None and d.get("toolCalls"):
                self._replace(idx, tool_calls=d["toolCalls"])
            return
        inp, cached, tool = count(tk.get("input")), count(tk.get("cached")), count(tk.get("tool"))
        out = count(tk.get("output")) + count(tk.get("thoughts"))
        if inp + out == 0:
            return
        calls = [c for c in d.get("toolCalls") or [] if isinstance(c, dict)]
        model = str(d.get("model") or (s.turns[-1].model if s.turns else "unknown"))
        turn = Turn(
            ts=ts,
            ctx=inp + tool,
            cache_read=min(cached, inp),
            output=out,
            model=model,
            tool_keys=[_tool_key(c) for c in calls],
            tool_result_tokens=[len(json.dumps(c.get("result"), default=str)) // 4 for c in calls],
            snippets=[json.dumps(c.get("result"), default=str)[:400] for c in calls if c.get("result")],
            user_text=self.pending_user,
            start_ts=self.last_input_ts,
        )
        if idx is None:
            self.turn_of[mid] = len(s.turns)
            s.turns.append(turn)
            self.pending_user = ""
        else:  # the same call written again: keep where it started, take the newer counts and tools
            old = s.turns[idx]
            turn.user_text, turn.start_ts, turn.ts = old.user_text, old.start_ts, old.ts or ts
            s.turns[idx] = turn

    def _replace(self, idx: int, tool_calls: Any) -> None:
        old = self.session.turns[idx]
        calls = [c for c in tool_calls if isinstance(c, dict)] if isinstance(tool_calls, list) else []
        self.session.turns[idx] = Turn(
            **{
                **vars(old),
                "tool_keys": [_tool_key(c) for c in calls],
                "tool_result_tokens": [len(json.dumps(c.get("result"), default=str)) // 4 for c in calls],
                "snippets": [json.dumps(c.get("result"), default=str)[:400] for c in calls if c.get("result")],
            }
        )

    def _compaction(self, m: dict[str, Any]) -> None:
        mid = str(m.get("id"))
        if mid in self.snapshots:
            return
        self.snapshots.append(mid)
        s = self.session
        ts = parse_ts(m.get("timestamp"))
        if s.compactions and s.compactions[-1].turn_index == len(s.turns):
            return  # the 0.46 checkpoint after an appended summary: the same compaction
        s.compactions.append(
            Compaction(
                turn_index=len(s.turns),
                pre_tokens=s.turns[-1].ctx if s.turns else None,
                ts=ts,
                summary_text=_text(m.get("content"))[:2000],
            )
        )
        self.last_input_ts = ts or self.last_input_ts

    def finish(self) -> Session:
        from ..model import snapshot

        out = snapshot(self.session)
        if out.turns:  # the window of the model the session is grouped under (its most-used one), not the largest
            out.context_window = token_limit(out.model)
        return out

    def dump(self) -> dict[str, Any]:
        from ..model import session_to_dict

        st = {k: v for k, v in self.__dict__.items() if k != "session"}
        return {"path": self.session.path, "session": session_to_dict(self.session), **st}

    @classmethod
    def load(cls, state: dict[str, Any]) -> GeminiParser:
        from ..model import session_from_dict

        p = cls(state["path"])
        if "session" in state:
            p.session = session_from_dict(state["session"])
            for k in ("turn_of", "users", "snapshots", "pending_user", "last_input_ts"):
                setattr(p, k, state.get(k, getattr(p, k)))
        return p


@register
class GeminiAdapter(Adapter):
    name = "gemini-cli"
    parser_cls = GeminiParser
    resumable = False  # a legacy .json session is rewritten in place; re-parse a changed file from the start

    @classmethod
    def default_roots(cls) -> list[Path]:
        return env_roots("GEMINI_CLI_HOME", ".gemini/tmp", ["~/.gemini/tmp"])

    @classmethod
    def config_paths(cls) -> list[Path]:
        return [r.parent / "settings.json" for r in cls.default_roots()]

    def find_files(self) -> Iterator[Path]:
        for r in self.roots:
            if not r.exists():
                continue
            files = [*r.glob("*/chats/*.jsonl"), *r.glob("*/chats/*/*.jsonl")]
            stems = {(f.parent, f.stem) for f in files}
            files += [f for f in r.glob("*/chats/session-*.json") if (f.parent, f.stem) not in stems]
            yield from sorted(files)

    def read_records(self, path: Path, parser: Parser | None = None) -> Iterator[dict[str, Any]]:
        if path.suffix != ".json":
            yield from iter_jsonl(path, parser)
            return
        try:
            doc = json.loads(path.read_bytes())
        except (ValueError, UnicodeDecodeError):
            _bump_error(parser)
            return
        if not isinstance(doc, dict):
            _bump_error(parser)
            return
        yield {k: v for k, v in doc.items() if k != "messages"}
        for m in doc.get("messages") or []:
            if isinstance(m, dict):
                yield m
            else:
                _bump_error(parser)
