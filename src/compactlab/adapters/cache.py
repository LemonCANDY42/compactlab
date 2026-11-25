"""Incremental on-disk scan cache.

One SQLite row per log file: size, mtime_ns, SHA-1 of the first 4 KiB, byte offset of the last complete line,
parser version and parser state. Unchanged file -> rebuild its session from the row. Appended file with the same
head -> resume the parser from the saved offset. Anything else -> full parse. A run reads and writes only the rows
of the files it scans, one row per transaction, so a change rewrites one row, not the whole cache.

The parser state is stored in two parts: the session, which is all an unchanged file needs, and the rest (seen
message ids, pending tool calls), which is read only to resume an appended file.

The same database keeps the last few lite analysis results, each under a key of the files it was made from and of
every other input (see `cli/main.py`), so a run whose files and inputs are unchanged reuses one without decoding a
session.

Sessions are stored lean (`model.lean`): token counts and timings, a flag for user messages that correct the agent,
hashed tool keys and summary lengths, but no prompt text, tool arguments, output snippets or summaries. The resume
part may still hold the text of a log's last user message while its turn is unfinished. The cache is kept in a 0700
directory (`~/.compactlab/cache`) with 0600 files. Rows of files that no longer exist are pruned after each scan, the
first scan with a new cache version deletes the files of older versions (never newer ones), `--no-cache` bypasses it
and `compactlab cache clear` removes every version.
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
from typing import TYPE_CHECKING, Any, BinaryIO, NamedTuple

from ..model import lean, lean_dict, lean_state
from .base import parse_lines

if TYPE_CHECKING:
    from ..model import Session
    from .base import Adapter, Parser

# 5: lean rows, no transcript text (the first scan deletes version 4's, which had it); 6: nor in the resume state (5's
# kept a user message, tool command and tool output that no turn had yet)
CACHE_VERSION = 6
HEAD = 4096
BUSY_TIMEOUT_S = 10.0  # wait this long for another run's write; then go on without caching that file
ERASE_TIMEOUT_S = 1.0  # wait this long for other runs' reads to end before erasing pruned rows from the files
RESULTS_KEPT = 4  # analysis results kept (one per set of inputs: the service's, a `simulate` with other options)
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
);
CREATE TABLE IF NOT EXISTS results (
    files TEXT NOT NULL,
    inputs TEXT NOT NULL,
    facts TEXT NOT NULL,
    result TEXT NOT NULL,
    made REAL NOT NULL,
    PRIMARY KEY (files, inputs)
);
"""
_META = "parser, head, size, mtime_ns, offset"
_JSON = json.JSONEncoder(separators=(",", ":"))
_UNUSABLE = {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}


class Pruned(NamedTuple):
    """What `prune()` did: rows of deleted logs found, whether they were deleted, and whether their bytes are gone
    from the cache files too (not while another run still reads a snapshot that holds them)."""

    stale: int
    deleted: bool
    erased: bool


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
                    session = _rebuild(adapter, state) if state is not None else None
                    if session is not None:
                        self.stats["hit"] += 1
                        return lean(session)
                elif adapter.resumable and st.st_size > offset and size <= st.st_size:
                    state = self._state(adapter, path, row, resume=True)
                    resumed = _resume(adapter, state, f, offset) if state is not None else None
                    if resumed is not None:
                        self.stats["resumed"] += 1
                        return self._store(adapter, path, head, st, resumed[1], resumed[0])
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
            session = json.loads(rows[0][0])
            rest = json.loads(rows[0][1]) if resume else {}
        except ValueError:
            return None  # unreadable row: parse the file again, which replaces it
        if not isinstance(session, dict) or not isinstance(rest, dict):
            return None  # valid JSON of the wrong shape (e.g. `null`): the same
        return {"path": str(path), "session": session, **rest}

    def _store(
        self, adapter: Adapter, path: Path, head: str, st: os.stat_result, offset: int, parser: Parser
    ) -> Session:
        # lean first: the finished session shares its turns with the parser's, so the dump below is lean already and
        # each user message is checked for a correction once
        finished = lean(parser.finish())
        db = self._open(create=True)
        if db is not None:
            state = parser.dump()
            session = lean_dict(state.pop("session"))  # no transcript text on disk (compactions; anything not shared)
            state.pop("path", None)
            state = lean_state(state) if adapter.resumable else {}  # only resuming reads it
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
        return finished

    def prune(self) -> Pruned:
        """Delete the rows of files that no longer exist, in one transaction, and erase them from the cache files.

        `secure_delete` zeroes the deleted rows in the pages SQLite writes, but those pages go to the write-ahead log
        first and the old ones stay in the database file until a checkpoint copies the new ones over them. A
        checkpoint cannot do that while another run is reading a snapshot that still holds the rows, so erasing waits
        up to `ERASE_TIMEOUT_S` for such reads to end; when they have not, the rows are erased by the checkpoint SQLite
        runs when the last run using the cache closes it."""
        rows = self._exec("SELECT adapter, path FROM entries", create=False) or []
        gone = [(a, p) for a, p in rows if not os.path.exists(p)]
        if not gone:
            return Pruned(0, True, True)
        db = self._open(create=False)
        if db is None:
            return Pruned(len(gone), False, False)
        try:
            db.execute("BEGIN IMMEDIATE")  # all or none: a half-done prune would look done
            db.executemany("DELETE FROM entries WHERE adapter=? AND path=?", gone)
            db.execute("COMMIT")
        except sqlite3.Error:
            if db.in_transaction:
                with contextlib.suppress(sqlite3.Error):
                    db.execute("ROLLBACK")
            return Pruned(len(gone), False, False)
        return Pruned(len(gone), True, self._erase())

    def _erase(self) -> bool:
        """Checkpoint the write-ahead log into the database and truncate it. False when another run's read kept
        part of it from being copied."""
        db = self._db
        if db is None:
            return False
        try:
            db.execute(f"PRAGMA busy_timeout={int(ERASE_TIMEOUT_S * 1000)}")
            try:
                busy = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
            finally:
                db.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_S * 1000)}")
        except sqlite3.Error:
            return False
        return busy == 0

    # ---------- analysis results ----------

    def facts(self, files: str) -> dict[str, Any] | None:
        """What the analysis saved about the sessions of these files (see `save_result`), or None."""
        rows = self._exec("SELECT facts FROM results WHERE files=? ORDER BY made DESC LIMIT 1", (files,), create=False)
        try:
            return json.loads(rows[0][0]) if rows else None
        except ValueError:
            return None

    def result(self, files: str, inputs: str) -> dict[str, Any] | None:
        """The analysis result saved for exactly these files and inputs, or None."""
        rows = self._exec("SELECT result FROM results WHERE files=? AND inputs=?", (files, inputs), create=False)
        try:
            res = json.loads(rows[0][0]) if rows else None
        except ValueError:
            return None
        return res if isinstance(res, dict) else None

    def save_result(self, files: str, inputs: str, facts: dict[str, Any], result: str, now: float) -> None:
        """Keep `result` (JSON text) for these keys; `facts` are the session-derived values the next run needs to
        rebuild `inputs` without decoding a session (models seen, harness and model pairs). Only the newest
        `RESULTS_KEPT` results are kept."""
        self._exec(
            "INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?, ?)", (files, inputs, _JSON.encode(facts), result, now)
        )
        self._exec(
            "DELETE FROM results WHERE rowid NOT IN (SELECT rowid FROM results ORDER BY made DESC LIMIT ?)",
            (RESULTS_KEPT,),
        )

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    # ---------- inspection and cleanup ----------

    def info(self) -> dict[str, Any]:
        """What is on disk: rows (and how many are of deleted logs), saved results, bytes of this version, of older
        versions (deleted by the next scan) and of newer ones (a newer compactlab's, left alone). Creates nothing."""
        paths = [p for (p,) in self._exec("SELECT path FROM entries", create=False) or []]
        results = self._exec("SELECT count(*) FROM results", create=False)
        sizes = [(_age(ver), p.stat().st_size) for p, ver in self._ours()]
        return {
            "path": str(self.file),
            "exists": self.file.exists(),
            "files": len(paths),
            "stale": sum(not os.path.exists(p) for p in paths),
            "results": results[0][0] if results else 0,
            "bytes": sum(n for age, n in sizes if age == 0),
            "old_bytes": sum(n for age, n in sizes if age < 0),
            "newer_bytes": sum(n for age, n in sizes if age > 0),
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
        # Older versions' files. Never a newer version's: a newer compactlab may share this directory. Leftover
        # temporary files of the JSON versions carry no version; they are left alone too while a newer one is here.
        newer = any(_age(ver) > 0 for _, ver in self._ours())
        self._remove(lambda ver: _age(ver) < 0 and not (ver is None and newer))
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


def _age(ver: int | None) -> int:
    """-1 for an older cache version's file (a JSON version's temporary file has no version), 0 for this one's,
    1 for a newer one's."""
    if ver is None or ver < CACHE_VERSION:
        return -1
    return 1 if ver > CACHE_VERSION else 0


def _rebuild(adapter: Adapter, state: dict[str, Any]) -> Session | None:
    """The session of an unchanged file from its row; None when the row does not fit the parser (wrong shape)."""
    try:
        return adapter.parser_cls.load(state).finish()
    except Exception:  # a damaged or foreign row: parse the file again, which replaces it
        return None


def _resume(adapter: Adapter, state: dict[str, Any], f: BinaryIO, offset: int) -> tuple[Parser, int] | None:
    """The parser resumed from its row and fed the appended lines, with the new offset; None when the row does not
    fit the parser."""
    try:
        parser = adapter.parser_cls.load(state)
        return parser, _consume(f, offset, parser)
    except Exception:  # a damaged or foreign row: parse the file again from the start, which replaces it
        return None


def _connect(file: Path) -> sqlite3.Connection:
    db = sqlite3.connect(file, timeout=BUSY_TIMEOUT_S, isolation_level=None)  # autocommit: one row, one transaction
    try:
        db.execute("PRAGMA journal_mode=WAL")  # concurrent runs read while another writes
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA secure_delete=ON")  # zero pruned and replaced rows: no excerpts of deleted logs linger
        db.executescript(_SCHEMA)
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
