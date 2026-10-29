"""DeepSeek Harness (`dsh`) adapter. Experimental: built from the published types and the project's own
snapshot logs, not yet checked against a real user's history.

Logs: `$DSH_HOME/sessions` (default `~/.dsh/sessions`), laid out as
`--<cwd>--/<session>/session[.vN].jsonl[.zstd]`. Only the newest generation (highest `vN`, none = 0) of a
session is read. `.jsonl.zstd` is a chain of independent zstd frames (header frame, then one per append
batch); it is decoded with the standard library on Python 3.14+ (`compression.zstd`), else with the `zstd`
command, else skipped and reported by `doctor`. A torn last frame is dropped, as dsh itself does.

Rows: a `session` header (`id`, `parentSession`, `origin: "subagent"`, `isSeeded`), then events
`{type, seq, time, data}`. Usage is `assistant/message` → `data.usage` with DISJOINT counts:
`inputTokens` (uncached), `cacheReadTokens`, `cacheWriteTokens`, `outputTokens`. Compactions are
`compaction/start|summary|end`; a summary followed by an `end` with `error` is dropped. A seeded fork copies
its parent's events up to `session/end-seed`; usage before that marker is not counted.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..model import Compaction, Session, Turn
from .base import Adapter, Parser, env_roots, parse_lines, parse_ts, register

KNOWN_TYPES = {
    "session",
    "assistant/message",
    "assistant/attempt",
    "user/message",
    "system/message",
    "developer/message",
    "tool/call",
    "tool/result",
    "compaction/start",
    "compaction/summary",
    "compaction/end",
    "session/end-seed",
    "request/header",
    "request/context",
    "turn/start",
    "turn/end",
    "step/start",
    "step/end",
    "agent/inbox/spliced",
    "session/title",
    "session/title-llm-request",
    "sandbox/mode",
    "approval/policy",
    "permission/preset",
    "model/selection",
    "working-directory/change",
    "goal/change",
    "image/offload",
    "job/registered",
    "job/settled",
    "subagent/catalog",
    "subagent/descriptor",
}
_GEN = re.compile(r"^session(?:\.v(\d+))?\.jsonl(\.zstd)?$")


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _stream_time(data: dict[str, Any]) -> float | None:
    """Latest chunk time in an embedded provider stream (rows may lack their own `time`)."""
    best = None
    for c in data.get("stream") or []:
        if isinstance(c, dict):
            for k in ("time", "time0"):
                v = c.get(k)
                if isinstance(v, (int, float)) and v > 0 and (best is None or v > best):
                    best = v
    return parse_ts(best) if best else None


def zstd_decode(raw: bytes) -> bytes | None:
    """Decode concatenated zstd frames, keeping every complete frame. None when no decoder is available."""
    try:
        from compression import zstd  # type: ignore[import-not-found]  # Python 3.14+
    except ImportError:
        exe = shutil.which("zstd")
        if not exe:
            return None
        r = subprocess.run([exe, "-dcq"], input=raw, capture_output=True, check=False)
        return r.stdout  # on a torn tail zstd exits non-zero after writing what it could decode
    out = bytearray()
    while raw:
        d = zstd.ZstdDecompressor()
        try:
            chunk = d.decompress(raw)
        except zstd.ZstdError:
            break
        if not d.eof:
            break  # torn last frame: dsh truncates it too
        out += chunk
        raw = d.unused_data
    return bytes(out)


def zstd_available() -> bool:
    stdlib = sys.version_info >= (3, 14) and importlib.util.find_spec("compression.zstd") is not None
    return stdlib or shutil.which("zstd") is not None


class DshParser(Parser):
    version = 1

    def __init__(self, path: str):
        self.session = Session(id=Path(path).parent.name, harness="dsh", role="main", path=path)
        self.model = "unknown"
        self.seeded = False
        self.pending_user = ""
        self.call_turn: dict[str, int] = {}
        self.last_input_ts: float | None = None
        self.compact_start: float | None = None

    def feed(self, d: dict[str, Any]) -> None:
        s = self.session
        typ = str(d.get("type"))
        raw = d.get("data")
        data: dict[str, Any] = raw if isinstance(raw, dict) else {}
        if typ not in KNOWN_TYPES:
            self.note_unknown(s, typ)
            return
        ts = parse_ts(d.get("time"))
        if typ == "session":
            s.id = str(d.get("id") or s.id)
            s.parent_id = str(d["parentSession"]) if d.get("parentSession") else None
            s.role = "subagent" if d.get("origin") == "subagent" else "main"
            self.seeded = bool(d.get("isSeeded"))
        elif typ == "session/end-seed":
            self.seeded = False
        elif typ == "request/context":
            self.model = str(data.get("model") or self.model)
            if data.get("contextWindow"):
                s.context_window = int(data["contextWindow"])
        elif typ in ("user/message", "tool/result"):
            if typ == "user/message":
                self.pending_user = _text_of(data.get("content"))
            else:
                self._tool_result(data)
            self.last_input_ts = ts or self.last_input_ts
        elif typ == "tool/call" and s.turns and not self.seeded:
            key = f"{data.get('name', '?')}:{self._arg(data.get('arguments'))}"
            s.turns[-1].tool_keys.append(key)
            self.call_turn[str(data.get("callId", ""))] = len(s.turns) - 1
        elif typ == "assistant/message":
            self._assistant(data, ts)
        elif typ == "compaction/start":
            self.compact_start = ts
        elif typ == "compaction/summary" and not self.seeded:
            s.compactions.append(
                Compaction(
                    turn_index=len(s.turns),
                    pre_tokens=s.turns[-1].ctx if s.turns else None,
                    ts=ts,
                    summary_text=str(data.get("summary") or _text_of(data.get("rawOutput")))[:2000],
                )
            )
        elif typ == "compaction/end" and not self.seeded:
            c = s.compactions[-1] if s.compactions else None
            if c is not None and c.turn_index == len(s.turns):
                if data.get("error"):
                    s.compactions.pop()
                elif ts and self.compact_start and 0 < ts - self.compact_start <= 900:
                    c.duration_s = ts - self.compact_start
            self.compact_start = None
            self.last_input_ts = ts or self.last_input_ts

    @staticmethod
    def _arg(args: Any) -> str:
        try:
            a = json.loads(args) if isinstance(args, str) else (args or {})
        except ValueError:
            return str(args)[:200]
        if isinstance(a, dict):
            v = a.get("file_path") or a.get("path") or a.get("pattern") or a.get("command") or a.get("query") or ""
            return str(v)[:200]
        return str(a)[:200]

    def _tool_result(self, data: dict[str, Any]) -> None:
        s = self.session
        m = data.get("message") or {}
        if self.seeded or not s.turns or not isinstance(m, dict):
            return
        body = _text_of(m.get("content"))
        idx = self.call_turn.get(str(m.get("toolCallId", "")), len(s.turns) - 1)
        s.turns[idx].snippets.append(body[:400])
        s.turns[idx].tool_result_tokens.append(len(body) // 4)

    def _assistant(self, data: dict[str, Any], ts: float | None) -> None:
        s = self.session
        u = data.get("usage")
        if not isinstance(u, dict):
            return
        if self.seeded:
            s.skipped_inherited += 1  # copied from the parent session
            return
        inp, cr, cw, out = (
            int(u.get(k) or 0) for k in ("inputTokens", "cacheReadTokens", "cacheWriteTokens", "outputTokens")
        )
        src = (data.get("message") or {}).get("source") or {}
        model = str(src.get("model") or self.model)
        s.turns.append(
            Turn(
                ts=ts or _stream_time(data),
                ctx=inp + cr + cw,
                cache_read=cr,
                cache_write=cw,
                output=out,
                model=model,
                user_text=self.pending_user,
                start_ts=self.last_input_ts,
            )
        )
        self.pending_user = ""

    def finish(self) -> Session:
        from ..model import snapshot

        return snapshot(self.session)

    def dump(self) -> dict[str, Any]:
        from ..model import session_to_dict

        st = {k: v for k, v in self.__dict__.items() if k != "session"}
        return {"path": self.session.path, "session": session_to_dict(self.session), **st}

    @classmethod
    def load(cls, state: dict[str, Any]) -> DshParser:
        from ..model import session_from_dict

        p = cls(state["path"])
        if "session" in state:
            p.session = session_from_dict(state["session"])
            for k in ("model", "seeded", "pending_user", "call_turn", "last_input_ts", "compact_start"):
                setattr(p, k, state.get(k, getattr(p, k)))
        return p


@register
class DshAdapter(Adapter):
    name = "dsh"
    parser_cls = DshParser
    resumable = False  # zstd frames: re-parse a changed file from the start

    @classmethod
    def default_roots(cls) -> list[Path]:
        return env_roots("DSH_HOME", "sessions", ["~/.dsh/sessions"])

    def find_files(self) -> Iterator[Path]:
        """The newest generation of each session directory."""
        best: dict[Path, tuple[int, Path]] = {}
        for r in self.roots:
            if not r.exists():
                continue
            for f in r.rglob("session*.jsonl*"):
                m = _GEN.match(f.name)
                if not m:
                    continue
                gen = int(m.group(1) or 0)
                if f.parent not in best or gen > best[f.parent][0]:
                    best[f.parent] = (gen, f)
        for _, f in sorted(best.values(), key=lambda x: str(x[1])):
            yield f

    def read_records(self, path: Path, parser: Parser | None = None) -> Iterator[dict[str, Any]]:
        raw = path.read_bytes()
        if path.suffix == ".zstd":
            dec = zstd_decode(raw)
            if dec is None:
                return  # no decoder; doctor says so
            raw = dec
        yield from parse_lines(iter(raw.splitlines()), parser)  # a torn last line counts as a parse error
