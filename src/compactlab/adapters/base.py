"""Adapter interface. Add a harness by subclassing Adapter and calling register()."""
from __future__ import annotations

import abc
import os
import time
from pathlib import Path
from typing import Iterator

from ..model import Session

_REGISTRY: dict[str, type["Adapter"]] = {}


class Adapter(abc.ABC):
    name: str = ""
    default_root: str = ""

    def __init__(self, root: str | os.PathLike | None = None):
        self.root = Path(os.path.expanduser(str(root or self.default_root)))

    @abc.abstractmethod
    def find_files(self) -> Iterator[Path]:
        """Yield transcript files under self.root."""

    @abc.abstractmethod
    def parse_file(self, path: Path) -> Session | None:
        """Parse one transcript. Must tolerate unknown/malformed lines."""

    def sessions(self, since_days: float | None = None) -> Iterator[Session]:
        cut = time.time() - since_days * 86400 if since_days else None
        for p in self.find_files():
            try:
                if cut and p.stat().st_mtime < cut:
                    continue
                s = self.parse_file(p)
            except OSError:
                continue
            if s and s.turns:
                yield s


def register(cls: type[Adapter]) -> type[Adapter]:
    _REGISTRY[cls.name] = cls
    return cls


def get_adapter(name: str) -> type[Adapter]:
    return _REGISTRY[name]


def adapter_names() -> list[str]:
    return sorted(_REGISTRY)


def iter_jsonl(path: Path) -> Iterator[dict]:
    with open(path, errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = _loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                yield d


def _loads(s: str):
    import json
    return json.loads(s)


def parse_ts(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) / 1000 if v > 1e11 else float(v)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
