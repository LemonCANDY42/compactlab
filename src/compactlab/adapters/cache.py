"""Incremental on-disk scan cache.

Per file we keep (size, mtime_ns, sha1 of the first 4 KiB, byte offset of the last complete line,
parser state). Unchanged file -> reuse. Appended file with the same head -> resume the parser from
the saved offset. Anything else -> full parse. The cache holds parsed excerpts of your transcripts
(tool commands, 400-char output snippets), so it is written with 0600 permissions under
`~/.compactlab/cache` and can be disabled with `--no-cache` or wiped with `compactlab cache clear`.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .base import parse_lines

if TYPE_CHECKING:
    from ..model import Session
    from .base import Adapter

CACHE_VERSION = 1
HEAD = 4096


class ScanCache:
    def __init__(self, directory: str | os.PathLike[str]):
        self.dir = Path(os.path.expanduser(str(directory)))
        self.file = self.dir / f"scan-v{CACHE_VERSION}.json"
        self.entries: dict[str, Any] = {}
        self.stats = {"hit": 0, "resumed": 0, "parsed": 0}
        self._dirty = False
        if self.file.exists():
            try:
                self.entries = json.loads(self.file.read_text()).get("entries", {})
            except ValueError:
                self.entries = {}

    def parse(self, adapter: Adapter, path: Path) -> Session:
        st = path.stat()
        key = f"{adapter.name}:{path}"
        e = self.entries.get(key)
        ver = adapter.parser_cls.version
        with open(path, "rb") as f:
            head = hashlib.sha1(f.read(HEAD)).hexdigest()
            if e and e["parser"] == ver and e["head"] == head:
                if e["size"] == st.st_size and e["mtime_ns"] == st.st_mtime_ns:
                    self.stats["hit"] += 1
                    return adapter.parser_cls.load(e["state"]).finish()
                if adapter.resumable and st.st_size > e["offset"] and len(head) and e["size"] <= st.st_size:
                    parser = adapter.parser_cls.load(e["state"])
                    offset = self._consume(f, e["offset"], parser)
                    self.stats["resumed"] += 1
                    return self._store(key, ver, head, st, offset, parser)
            parser = adapter.new_parser(path)
            if adapter.resumable:
                offset = self._consume(f, 0, parser)
            else:
                for rec in adapter.read_records(path, parser):
                    parser.feed(rec)
                offset = st.st_size
            self.stats["parsed"] += 1
            return self._store(key, ver, head, st, offset, parser)

    @staticmethod
    def _consume(f: Any, start: int, parser: Any) -> int:
        f.seek(start)
        data = f.read()
        end = data.rfind(b"\n") + 1  # never consume a partially written last line
        for rec in parse_lines(iter(data[:end].splitlines()), parser):
            parser.feed(rec)
        return start + end

    def _store(self, key: str, ver: int, head: str, st: os.stat_result, offset: int, parser: Any) -> Session:
        self.entries[key] = {
            "parser": ver,
            "head": head,
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "offset": offset,
            "state": parser.dump(),
        }
        self._dirty = True
        return parser.finish()

    def save(self) -> None:
        if not self._dirty:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".scan.")
        with os.fdopen(fd, "w") as fh:
            json.dump({"version": CACHE_VERSION, "entries": self.entries}, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.file)
        self._dirty = False

    def clear(self) -> None:
        if self.file.exists():
            self.file.unlink()
        self.entries = {}
