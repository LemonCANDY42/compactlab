"""OpenCode adapter (the v1 message store: OpenCode 1.x, checked against 1.19.5 history and the 1.19.6 source).

Data: `$XDG_DATA_HOME/opencode` (default `~/.local/share/opencode`, macOS included). Every session of every project
lives in one SQLite database, `opencode.db` (`opencode-<channel>.db` on non-release channels; the `OPENCODE_DB`
variable may point elsewhere). It is opened read-only with SQLite's normal locking: a reader in WAL mode never
blocks OpenCode's writer, and nothing is written. If a read-only connection cannot be opened (no `-shm` file and
no write access to create one) it falls back to `immutable=1`, which skips changes still in the WAL file.

Tables used: `session` (id, parent_id: a subagent run started by the task tool, time_updated in ms), `message`
(id, session_id, data JSON: role, time.created/completed, providerID/modelID, summary, finish, error) and `part`
(id, message_id, time_created in ms, data JSON with `type`). One billed request is one step: a `step-start` part,
then text/reasoning/tool parts, then a `step-finish` part whose `tokens` are DISJOINT: `input` (uncached),
`cache.read`, `cache.write`, `output` (reasoning excluded) and `reasoning`. A compaction is a user message with a
`compaction` part (`auto`, `overflow`) answered by an assistant message with `summary: true`; that message's own
step is the summary call, which is not a turn. A summary that ended with an error or without `finish` is dropped.

Not read: sessions stored by the OpenCode 2.x pre-releases (`session_v2`, `session_message`, `session_part`);
`doctor` counts them. The pre-SQLite JSON store (`storage/message`, `storage/part`) is migrated into the database by
OpenCode itself on start.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..model import Compaction, Session, Turn
from .base import Adapter, BadRecord, Parser, _bump_error, count, register

KNOWN_PARTS = {
    "text",
    "reasoning",
    "file",
    "agent",
    "compaction",
    "subtask",
    "retry",
    "step-start",
    "step-finish",
    "tool",
    "snapshot",
    "patch",
}
_ARG_KEYS = ("filePath", "path", "pattern", "command", "url", "query", "file_path")


def _obj(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _ms(v: Any) -> float | None:
    return float(v) / 1000 if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def _tool_key(d: dict[str, Any]) -> str:
    inp = _obj(_obj(d.get("state")).get("input"))
    arg = next((inp[k] for k in _ARG_KEYS if inp.get(k)), "")
    return f"{d.get('tool', '?')}:{str(arg)[:200]}"


class OpenCodeParser(Parser):
    """Fed one session's rows in order: {"kind": "session" | "message" | "part" | "end", ...} (see `records`)."""

    version = 2  # 2: the provider is the session's platform (prices: direct vendor, OpenRouter, Bedrock, ...)

    def __init__(self, path: str):
        self.session = Session(id=Path(path).stem, harness="opencode", role="main", path=path)
        self.msg: dict[str, Any] = {}  # data of the message being read
        self.msg_turns = 0  # turns made from this message's steps
        self.step_start: float | None = None
        self.step_tools: list[tuple[str, str, int, float | None]] = []  # key, snippet, est. tokens, exec start
        self.summary_text = ""
        self.pending_user = ""
        self.last_input_ts: float | None = None

    def feed(self, d: dict[str, Any]) -> None:
        kind = d.get("kind")
        data = _obj(d.get("data"))
        if kind == "session":
            self.session.id = str(d.get("id") or self.session.id)
            if d.get("parent_id"):
                self.session.parent_id = str(d["parent_id"])
                self.session.role = "subagent"
        elif kind == "message":
            self._end_message()
            self.msg = data
            role = data.get("role")
            if role == "user":
                self.last_input_ts = _ms(_obj(data.get("time")).get("created")) or self.last_input_ts
            elif role != "assistant":
                self.note_unknown(self.session, f"message:{role}")
        elif kind == "part":
            try:
                self._part(data, _ms(d.get("time_created")))
            except BadRecord:
                self.session.parse_errors += 1
        elif kind == "end":
            self._end_message()
        else:
            self.note_unknown(self.session, f"record:{kind}")

    def _part(self, d: dict[str, Any], ts: float | None) -> None:
        typ = str(d.get("type"))
        if typ not in KNOWN_PARTS:
            self.note_unknown(self.session, f"part:{typ}")
            return
        role, summary = self.msg.get("role"), bool(self.msg.get("summary"))
        if typ == "text" and isinstance(d.get("text"), str):
            if role == "user" and not d.get("synthetic"):
                self.pending_user = (self.pending_user + " " + d["text"]).strip()
            elif summary:
                self.summary_text += d["text"]
        elif summary or role != "assistant":
            return  # the summary call's own steps and tools are not turns
        elif typ == "step-start":
            self.step_start, self.step_tools = ts, []
        elif typ == "tool":
            st = _obj(d.get("state"))
            body = str(st.get("output") or st.get("error") or "")
            start = _ms(_obj(st.get("time")).get("start"))
            self.step_tools.append((_tool_key(d), body[:400], len(body) // 4, start))
        elif typ == "step-finish":
            self._turn(d.get("tokens"), ts)

    def _turn(self, tokens: Any, ts: float | None) -> None:
        s = self.session
        tk = _obj(tokens)
        cache = _obj(tk.get("cache"))
        inp, cr, cw = count(tk.get("input")), count(cache.get("read")), count(cache.get("write"))
        out = count(tk.get("output")) + count(tk.get("reasoning"))
        tools, self.step_tools = self.step_tools, []
        start, self.step_start = self.step_start, None
        if inp + cr + cw == 0:
            return  # an aborted step, or a provider that reported no usage: nothing to replay
        # the response ends where its last tool call starts running; the step itself also spans the tools
        starts = [t for *_, t in tools if t]
        end = min(max(starts), ts) if starts and ts else (max(starts) if starts else ts)
        model = self.msg.get("modelID") or "unknown"
        provider = self.msg.get("providerID")
        if provider:
            s.platform = str(provider)  # OpenCode's provider ID: `anthropic` is the vendor's API, not OpenRouter
        s.turns.append(
            Turn(
                ts=end,
                ctx=inp + cr + cw,
                cache_read=cr,
                cache_write=cw,
                output=out,
                model=f"{provider}/{model}" if provider else str(model),
                tool_keys=[k for k, *_ in tools],
                tool_result_tokens=[n for _, _, n, _ in tools],
                snippets=[b for _, b, _, _ in tools if b],
                user_text=self.pending_user,
                start_ts=start or self.last_input_ts,
            )
        )
        self.msg_turns += 1
        self.pending_user = ""
        self.last_input_ts = ts or self.last_input_ts

    def _end_message(self) -> None:
        m, s = self.msg, self.session
        times = _obj(m.get("time"))
        created, done = _ms(times.get("created")), _ms(times.get("completed"))
        if m.get("role") == "assistant" and m.get("summary"):
            if m.get("finish") and not m.get("error"):
                dur = done - created if done and created else None
                s.compactions.append(
                    Compaction(
                        turn_index=len(s.turns),
                        pre_tokens=s.turns[-1].ctx if s.turns else None,
                        ts=done or created,
                        summary_text=self.summary_text[:2000],
                        duration_s=dur if dur is not None and 0 < dur <= 900 else None,
                    )
                )
            self.last_input_ts = done or self.last_input_ts
        elif m.get("role") == "assistant" and self.msg_turns == 0 and isinstance(m.get("tokens"), dict):
            try:  # no step parts (an older build or an interrupted reply): the message's own totals
                self.step_start = self.step_start or created
                self._turn(m["tokens"], done or created)
            except BadRecord:
                s.parse_errors += 1
        self.msg, self.msg_turns, self.summary_text = {}, 0, ""
        self.step_start, self.step_tools = None, []

    def finish(self) -> Session:
        from ..model import snapshot

        return snapshot(self.session)

    def dump(self) -> dict[str, Any]:
        from ..model import session_to_dict

        st = {k: v for k, v in self.__dict__.items() if k != "session"}
        return {"path": self.session.path, "session": session_to_dict(self.session), **st}

    @classmethod
    def load(cls, state: dict[str, Any]) -> OpenCodeParser:
        from ..model import session_from_dict

        p = cls(state["path"])
        if "session" in state:
            p.session = session_from_dict(state["session"])
            for k, v in state.items():
                if k not in ("path", "session") and hasattr(p, k):
                    setattr(p, k, [tuple(x) for x in v] if k == "step_tools" else v)
        return p


def connect(path: Path) -> tuple[sqlite3.Connection, str]:
    """A read-only connection and how it was opened ("ro", or "immutable" when read-only cannot open it)."""
    uri = "file:" + quote(str(path.resolve()))
    try:
        con = sqlite3.connect(f"{uri}?mode=ro", uri=True, timeout=2)
        con.execute("select 1 from sqlite_master limit 1").fetchall()
        return con, "ro"
    except sqlite3.OperationalError:
        return sqlite3.connect(f"{uri}?mode=ro&immutable=1", uri=True), "immutable"


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("select name from sqlite_master where type = 'table'")}


def _json(text: Any) -> dict[str, Any] | None:
    try:
        d = json.loads(text)
    except (TypeError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def records(con: sqlite3.Connection, since_ms: int = 0) -> Iterator[tuple[str, dict[str, Any] | None]]:
    """(session id, record) for every v1 session updated since `since_ms`, rows in order; None = unreadable row."""
    if not {"session", "message", "part"} <= _tables(con):
        return
    sessions = con.execute(
        "select id, parent_id from session where time_updated >= ? order by time_created, id", (since_ms,)
    ).fetchall()
    for sid, parent in sessions:
        yield sid, {"kind": "session", "id": sid, "parent_id": parent}
        parts: dict[str, list[tuple[Any, Any]]] = {}
        for mid, created, data in con.execute(
            "select message_id, time_created, data from part where session_id = ? order by id", (sid,)
        ):
            parts.setdefault(mid, []).append((created, data))
        for mid, data in con.execute("select id, data from message where session_id = ? order by id", (sid,)):
            d = _json(data)
            if d is None:
                yield sid, None
                continue
            yield sid, {"kind": "message", "id": mid, "data": d}
            for created, pdata in parts.get(mid, []):
                pd = _json(pdata)
                yield sid, ({"kind": "part", "time_created": created, "data": pd} if pd is not None else None)
        yield sid, {"kind": "end"}


@register
class OpenCodeAdapter(Adapter):
    name = "opencode"
    parser_cls = OpenCodeParser
    resumable = False

    @classmethod
    def default_roots(cls) -> list[Path]:
        import os

        base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
        return [Path(os.path.expanduser(base)) / "opencode"]

    @classmethod
    def config_paths(cls) -> list[Path]:
        import os

        base = Path(os.path.expanduser(os.environ.get("XDG_CONFIG_HOME") or "~/.config")) / "opencode"
        return [base / "opencode.json", base / "opencode.jsonc", base / "config.json"]

    def find_files(self) -> Iterator[Path]:
        for r in self.roots:
            if r.is_file() and r.suffix == ".db":
                yield r
            elif r.exists():
                yield from sorted(r.glob("opencode*.db"))

    def read_records(self, path: Path, parser: Parser | None = None) -> Iterator[dict[str, Any]]:
        """Every session's rows in one stream (tests and `parse_file`); `load` splits them per session."""
        con, _ = connect(path)
        try:
            for _, rec in records(con):
                if rec is None:
                    _bump_error(parser)
                    continue
                yield rec
        finally:
            con.close()

    def load(self, path: Path, cache: Any = None, since: float | None = None) -> list[Session]:
        """Every session in the database updated since `since`. The scan cache is not used: one indexed
        query per session is cheaper than hashing a database that changes on every reply."""
        out: list[Session] = []
        try:
            con, _ = connect(path)
        except sqlite3.DatabaseError as e:
            raise OSError(f"{path}: {e}") from e
        try:
            parser: OpenCodeParser | None = None
            for sid, rec in records(con, int(since * 1000) if since else 0):
                if parser is None or parser.session.id != sid:
                    if parser is not None:
                        out.append(parser.finish())
                    parser = OpenCodeParser(f"{path}#{sid}")
                if rec is None:
                    parser.session.parse_errors += 1
                else:
                    parser.feed(rec)
            if parser is not None:
                out.append(parser.finish())
        except sqlite3.DatabaseError as e:
            raise OSError(f"{path}: {e}") from e
        finally:
            con.close()
        return out

    def fingerprint(self, path: Path, since: float | None = None) -> list[Any] | None:
        """The database and its write-ahead log as files (any write changes one of them), and which sessions are
        recent enough to load: a session ages out of `since` without the file changing."""
        files = [[q.name, q.stat().st_size, q.stat().st_mtime_ns] for q in (path, Path(f"{path}-wal")) if q.exists()]
        try:
            con, _ = connect(path)
        except sqlite3.DatabaseError as e:
            raise OSError(f"{path}: {e}") from e
        try:
            ids = (
                con.execute(
                    "select id, time_updated from session where time_updated >= ? order by time_created, id",
                    (int(since * 1000) if since else 0,),
                ).fetchall()
                if "session" in _tables(con)
                else []
            )
        except sqlite3.DatabaseError as e:
            raise OSError(f"{path}: {e}") from e
        finally:
            con.close()
        return [str(path), self.parser_cls.version, files, ids]

    def doctor_checks(self) -> list[dict[str, Any]]:
        out = []
        for path in self.find_files():
            try:
                con, how = connect(path)
                try:
                    t = _tables(con)
                    v1 = con.execute("select count(*) from session").fetchone()[0] if "session" in t else 0
                    v2 = (
                        con.execute("select count(distinct session_id) from session_message").fetchone()[0]
                        if "session_message" in t
                        else 0
                    )
                finally:
                    con.close()
            except sqlite3.DatabaseError as e:
                out.append({"ok": False, "label": "sqlite", "text": f"{path.name}: {e}"})
                continue
            text = f"{path.name}: {v1} sessions readable" + (" (opened immutable: WAL tail skipped)" * (how != "ro"))
            if v2:
                text += f"; {v2} stored by OpenCode 2.x are not read yet"
            out.append({"ok": bool(v1) and how == "ro", "label": "sqlite", "text": text})
        return out
