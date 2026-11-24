"""Adapter interface and registry.

A harness adapter knows where its transcripts live and how to turn one JSONL file into a
normalized `Session`. Parsing is done by an incremental `Parser` so the scan cache can resume
an append-only file from the last byte offset instead of re-reading it.
"""

from __future__ import annotations

import abc
import json
import os
import time
from collections.abc import Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from ..model import Session

_REGISTRY: dict[str, type[Adapter]] = {}


class Parser(abc.ABC):
    """Stateful, resumable line parser.

    `dump()` must return JSON-serializable state with the session under "session" (as `session_to_dict`). The scan
    cache stores that session apart from the rest of the state and rebuilds an unchanged file with
    `load({"path": ..., "session": ...}).finish()`, so `finish()` must depend only on the session.
    """

    version: ClassVar[int] = 1
    # records at the top of a file a reader of only the file's end must feed first (e.g. Codex's session_meta)
    head_records: ClassVar[int] = 0
    # the session's working directory as last logged; shown only by the live view, never stored or cached
    cwd: str | None = None
    # whether the harness appends copies of earlier messages (original times) that the full parse drops as seen, so
    # a reader of only the file's end has to look further back when the newest record is well after the last request
    rewrites_history: ClassVar[bool] = False

    @abc.abstractmethod
    def feed(self, record: dict[str, Any]) -> None: ...

    @abc.abstractmethod
    def finish(self) -> Session:
        """Return the session parsed so far without consuming parser state."""

    @abc.abstractmethod
    def dump(self) -> dict[str, Any]: ...

    @classmethod
    @abc.abstractmethod
    def load(cls, state: dict[str, Any]) -> Parser: ...

    def note_unknown(self, session: Session, kind: str) -> None:
        session.unknown_events[kind] = session.unknown_events.get(kind, 0) + 1


class Adapter(abc.ABC):
    name: ClassVar[str] = ""
    parser_cls: ClassVar[type[Parser]]
    # False when a file cannot be resumed from a byte offset (e.g. compressed logs): the scan cache then
    # reuses an unchanged file and re-parses a changed one from the start.
    resumable: ClassVar[bool] = True

    def __init__(self, roots: Sequence[str | os.PathLike[str]] | str | os.PathLike[str] | None = None):
        if roots is None:
            self.roots = self.default_roots()
        elif isinstance(roots, (str, os.PathLike)):
            self.roots = [Path(os.path.expanduser(str(roots)))]
        else:
            self.roots = [Path(os.path.expanduser(str(r))) for r in roots]

    @classmethod
    @abc.abstractmethod
    def default_roots(cls) -> list[Path]:
        """Where this harness keeps transcripts on this machine (may not exist)."""

    @classmethod
    def config_paths(cls) -> list[Path]:
        """Config files `apply` may target (for `doctor`)."""
        return []

    def find_files(self) -> Iterator[Path]:
        for r in self.roots:
            if r.exists():
                yield from sorted(r.rglob("*.jsonl"))

    def new_parser(self, path: Path) -> Parser:
        return self.parser_cls.load({"path": str(path)})

    def read_records(self, path: Path, parser: Parser | None = None) -> Iterator[dict[str, Any]]:
        """Records of one log file; override for logs that are not plain JSONL."""
        return iter_jsonl(path, parser)

    def parse_file(self, path: Path) -> Session:
        p = self.new_parser(path)
        for rec in self.read_records(path, p):
            p.feed(rec)
        return p.finish()

    def load(self, path: Path, cache: Any = None, since: float | None = None) -> list[Session]:
        """Sessions in one file found by `find_files()`, skipping a file not modified since `since` (epoch s).
        Most harnesses keep one session per file; a harness with one database for every session (OpenCode)
        overrides this, filters by each session's own update time and returns many."""
        if since and path.stat().st_mtime < since:
            return []
        return [cache.parse(self, path) if cache is not None else self.parse_file(path)]

    def fingerprint(self, path: Path, since: float | None = None) -> list[Any] | None:
        """Identity of what `load(path, cache, since)` would read, taken before reading it: part of the key a saved
        analysis result is reused under, so it must change whenever the sessions `load` returns could. None when
        `load` would return nothing. A harness that overrides `load` overrides this too."""
        import hashlib

        st = path.stat()
        if since and st.st_mtime < since:
            return None
        with open(path, "rb") as f:
            head = hashlib.sha1(f.read(4096)).hexdigest()
        return [str(path), self.parser_cls.version, st.st_size, st.st_mtime_ns, head]

    def doctor_checks(self) -> list[dict[str, Any]]:
        """Extra `doctor` lines for this harness: [{"ok": bool, "label": str, "text": str}]."""
        return []

    def sessions(self, since_days: float | None = None, cache: Any = None) -> Iterator[Session]:
        cut = time.time() - since_days * 86400 if since_days else None
        for path in self.find_files():
            try:
                found = self.load(path, cache, cut)
            except OSError:
                continue
            yield from (s for s in found if s.turns)


def register(cls: type[Adapter]) -> type[Adapter]:
    if not cls.name:
        raise ValueError("adapter needs a name")
    _REGISTRY[cls.name] = cls
    return cls


def get_adapter(name: str) -> type[Adapter]:
    return _REGISTRY[name]


def adapter_names() -> list[str]:
    return sorted(_REGISTRY)


def parse_lines(lines: Iterator[bytes], parser: Parser | None = None) -> Iterator[dict[str, Any]]:
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            _bump_error(parser)
            continue
        if isinstance(d, dict):
            yield d
        else:
            _bump_error(parser)


def _bump_error(parser: Parser | None) -> None:
    s = getattr(parser, "session", None)
    if s is not None:
        s.parse_errors += 1


class BadRecord(ValueError):
    """A recognized record whose fields are unusable; the parser skips it and counts a parse error."""


def count(v: Any) -> int:
    """A non-negative integer token count; absent/null is 0. Strings, bools, NaN and negatives are bad."""
    if v is None:
        return 0
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise BadRecord(f"not a number: {v!r}")
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        raise BadRecord(f"not finite: {v!r}")
    if v < 0:
        raise BadRecord(f"negative: {v!r}")
    return int(v)


def iter_jsonl(path: Path, parser: Parser | None = None) -> Iterator[dict[str, Any]]:
    with open(path, "rb") as f:
        yield from parse_lines(iter(f), parser)


def parse_ts(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) / 1000 if v > 1e11 else float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def env_roots(var: str, sub: str, fallbacks: list[str]) -> list[Path]:
    """Roots from an env var (comma-separated base dirs) or the given fallbacks."""
    val = os.environ.get(var)
    if val:
        return [Path(os.path.expanduser(x.strip())) / sub for x in val.split(",") if x.strip()]
    return [Path(os.path.expanduser(x)) for x in fallbacks]
