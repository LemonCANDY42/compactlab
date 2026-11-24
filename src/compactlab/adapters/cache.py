"""Incremental on-disk scan cache.

One SQLite row per log file: size, mtime_ns, SHA-1 of the first 4 KiB, byte offset of the last complete line,
parser version and parser state. Unchanged file -> rebuild its session from the row. Appended file with the same
head -> resume the parser from the saved offset. Anything else -> full parse. A run reads and writes only the rows
of the files it scans, one row per transaction, so a change rewrites one row, not the whole cache.

The parser state is stored in two parts: the session, which is all an unchanged file needs, and the rest (seen
message ids, pending tool calls), which is read only to resume an appended file.

The cache holds parsed excerpts of your transcripts (your prompts' text, tool commands, 400-char output snippets),
so it is kept in a 0700 directory (`~/.compactlab/cache`) with 0600 files. Rows of files that no longer exist are
pruned after each scan, the first scan with a new cache version deletes the older versions' files, `--no-cache`
bypasses it and `compactlab cache clear` removes every version.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from .base import parse_lines

if TYPE_CHECKING:
    from ..model import Session
    from .base import Adapter, Parser

CACHE_VERSION = 4
HEAD = 4096
BUSY_TIMEOUT_S = 10.0  # wait this long for another run's write; then go on without caching that file
# Files this cache (any version) may leave in its directory: the database and its journals, the JSON files of
# versions 1-3, and their temporary files. Nothing else in the directory is touched.
_OURS = re.compile(r"scan-v(\d+)\.(?:json|sqlite3(?:-wal|-shm|-journal)?)|\.scan\.[a-z0-9_]{8}")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    adapter TEXT NOT NULL,
    path TEXT NOT NULL,
    parser INTEGER NOT NULL,
    head TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    offset INTEGER NOT NULL,
    session TEXT NOT NULL,
    resume TEXT NOT NULL,
    PRIMARY KEY (adapter, path)
)
"""
_META = "parser, head, size, mtime_ns, offset"
_JSON = json.JSONEncoder(separators=(",", ":"))
_UNUSABLE = {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}


class ScanCache:
    def __init__(self, directory: str | os.PathLike[str]):
        self.dir = Path(os.path.expanduser(str(directory)))
        self.file = self.dir / f"scan-v{CACHE_VERSION}.sqlite3"
        self.stats = {"hit": 0, "resumed": 0, "parsed": 0}
        self._db: sqlite3.Connection | None = None
        self._broken = False  # the database could not be opened: scan without it

    # ---------- scanning ----------

    def parse(self, adapter: Adapter, path: Path) -> Session:
        st = path.stat()
        ver = adapter.parser_cls.version
        with open(path, "rb") as f:
            head = hashlib.sha1(f.read(HEAD)).hexdigest()
            rows = self._exec(f"SELECT {_META} FROM entries WHERE adapter=? AND path=?", (adapter.name, str(path)))
            row = rows[0] if rows else None
            if row and row[0] == ver and row[1] == head:
                _, _, size, mtime_ns, offset = row
                if size == st.st_size and mtime_ns == st.st_mtime_ns:
                    state = self._state(adapter, path, row, resume=False)
                    if state is not None:
                        self.stats["hit"] += 1
                        return adapter.parser_cls.load(state).finish()
                elif adapter.resumable and st.st_size > offset and size <= st.st_size:
                    state = self._state(adapter, path, row, resume=True)
                    if state is not None:
                        parser = adapter.parser_cls.load(state)
                        offset = _consume(f, offset, parser)
                        self.stats["resumed"] += 1
                        return self._store(adapter, path, head, st, offset, parser)
            parser = adapter.new_parser(path)
            if adapter.resumable:
                offset = _consume(f, 0, parser)
            else:
                for rec in adapter.read_records(path, parser):
                    parser.feed(rec)
                offset = st.st_size
            self.stats["parsed"] += 1
            return self._store(adapter, path, head, st, offset, parser)

    def _state(self, adapter: Adapter, path: Path, meta: tuple[Any, ...], resume: bool) -> dict[str, Any] | None:
        """Parser state of the row `meta` was read from; None if another run has replaced that row since."""
        cols = "session, resume" if resume else "session"
        same = " AND ".join(f"{c}=?" for c in _META.split(", "))
        rows = self._exec(
            f"SELECT {cols} FROM entries WHERE adapter=? AND path=? AND {same}", (adapter.name, str(path), *meta)
        )
        if not rows:
            return None
        try:
            state = {"path": str(path), "session": json.loads(rows[0][0])}
            if resume:
                state.update(json.loads(rows[0][1]))
        except ValueError:
            return None  # unreadable row: parse the file again, which replaces it
        return state

    def _store(
        self, adapter: Adapter, path: Path, head: str, st: os.stat_result, offset: int, parser: Parser
    ) -> Session:
        db = self._open(create=True)
        if db is not None:
            state = parser.dump()
            session = state.pop("session")
            state.pop("path", None)
            self._exec(
                "INSERT OR REPLACE INTO entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    adapter.name,
                    str(path),
                    adapter.parser_cls.version,
                    head,
                    st.st_size,
                    st.st_mtime_ns,
                    offset,
                    _JSON.encode(session),
                    _JSON.encode(state),
                ),
            )
        return parser.finish()

    def prune(self) -> int:
        """Delete the rows of files that no longer exist. Returns how many were deleted."""
        rows = self._exec("SELECT adapter, path FROM entries", create=False) or []
        gone = [(a, p) for a, p in rows if not os.path.exists(p)]
        if gone and self._exec("DELETE FROM entries WHERE adapter=? AND path=?", gone, create=False, many=True) is None:
            return 0
        return len(gone)

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    # ---------- inspection and cleanup ----------

    def info(self) -> dict[str, Any]:
        """What is on disk: rows, bytes of this version and bytes of older versions. Creates nothing."""
        rows = self._exec("SELECT count(*) FROM entries", create=False)
        sizes = [(ver == CACHE_VERSION, p.stat().st_size) for p, ver in self._ours()]
        return {
            "path": str(self.file),
            "exists": self.file.exists(),
            "files": rows[0][0] if rows else 0,
            "bytes": sum(n for cur, n in sizes if cur),
            "old_bytes": sum(n for cur, n in sizes if not cur),
        }

    def clear(self) -> list[Path]:
        """Delete the cache files of every version. Returns the paths removed."""
        self.close()
        return self._remove(lambda _ver: True)

    def _ours(self) -> Iterator[tuple[Path, int | None]]:
        if not self.dir.is_dir():
            return
        for p in sorted(self.dir.iterdir()):
            m = _OURS.fullmatch(p.name)
            if m and p.is_file():
                yield p, int(m.group(1)) if m.group(1) else None

    def _remove(self, which: Callable[[int | None], bool]) -> list[Path]:
        removed = []
        for p, ver in self._ours():
            if which(ver):
                with contextlib.suppress(FileNotFoundError):
                    p.unlink()
                removed.append(p)
        return removed

    # ---------- database ----------

    def _open(self, create: bool) -> sqlite3.Connection | None:
        if self._db is not None or self._broken:
            return self._db
        if not create and not self.file.exists():
            return None
        try:
            if create:
                self._prepare()
            try:
                self._db = _connect(self.file)
            except sqlite3.DatabaseError as e:
                if not create or getattr(e, "sqlite_errorcode", None) not in _UNUSABLE:
                    raise
                self._remove(lambda ver: ver == CACHE_VERSION)  # corrupt or not a database: start over
                self._prepare()
                self._db = _connect(self.file)
        except (OSError, sqlite3.Error):
            self._broken = True
        return self._db

    def _prepare(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self._remove(lambda ver: ver != CACHE_VERSION)  # older versions and their leftover temporary files
        os.close(os.open(self.file, os.O_RDWR | os.O_CREAT, 0o600))
        os.chmod(self.file, 0o600)  # the WAL and shared-memory files take the database's mode

    def _exec(self, sql: str, args: Any = (), create: bool = True, many: bool = False) -> list[Any] | None:
        """Rows of one statement, or None if the cache is unavailable or the statement failed. A failure never stops
        a scan: a lock held by another run skips this statement, and a database found corrupt is deleted (with
        `create`) so that this run's next write starts a new one."""
        db = self._open(create)
        if db is None:
            return None
        try:
            return (db.executemany if many else db.execute)(sql, args).fetchall()
        except sqlite3.Error as e:
            if create and getattr(e, "sqlite_errorcode", None) in _UNUSABLE:
                self.close()
                self._remove(lambda ver: ver == CACHE_VERSION)
            return None


def _connect(file: Path) -> sqlite3.Connection:
    db = sqlite3.connect(file, timeout=BUSY_TIMEOUT_S, isolation_level=None)  # autocommit: one row, one transaction
    try:
        db.execute("PRAGMA journal_mode=WAL")  # concurrent runs read while another writes
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA secure_delete=ON")  # zero pruned and replaced rows: no excerpts of deleted logs linger
        db.execute(_SCHEMA)
    except BaseException:
        db.close()
        raise
    return db


def _consume(f: BinaryIO, start: int, parser: Parser) -> int:
    """Feed complete lines from `start`; return the offset after the last one. A torn last line is left unread."""
    f.seek(start)
    end = start

    def lines() -> Iterator[bytes]:
        nonlocal end
        for raw in f:
            if not raw.endswith(b"\n"):
                return  # partially written; read it next time
            end += len(raw)
            yield raw

    for rec in parse_lines(lines(), parser):
        parser.feed(rec)
    return end
