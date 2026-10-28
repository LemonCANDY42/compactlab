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
    """Stateful, resumable line parser. `dump()` must return JSON-serializable state."""

    version: ClassVar[int] = 1

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

    def parse_file(self, path: Path) -> Session:
        p = self.new_parser(path)
        for rec in iter_jsonl(path, p):
            p.feed(rec)
        return p.finish()

    def sessions(self, since_days: float | None = None, cache: Any = None) -> Iterator[Session]:
        cut = time.time() - since_days * 86400 if since_days else None
        for path in self.find_files():
            try:
                if cut and path.stat().st_mtime < cut:
                    continue
                s = cache.parse(self, path) if cache is not None else self.parse_file(path)
            except OSError:
                continue
            if s.turns:
                yield s


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
