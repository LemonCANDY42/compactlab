"""Active sessions, read from the end of their log files: context size, model, threshold in force, cache expiry.

The same parsers as the full analysis read the records (format knowledge stays in one place), but only the last
256 KiB of each recently written file, plus the records a parser needs from the top of the file. More is read when
that holds no complete request, or when the harness re-writes earlier history at the end of a log (Claude Code
appends copies of old messages with their original times; the full parse drops them as already seen) and the
newest record is well after the latest request found. The latest request is the newest by time, not the last line.
Finding the files is incremental: a full walk every 10 minutes; in between, only directories whose mtime moved (a
file was added or removed) are listed again, and only files written in the last two days are stat'ed. A file whose
size and mtime did not change is not read again.

Project names (the last part of the logged working directory) are only ever printed, never written to a file.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..adapters import get_adapter
from ..adapters.base import Adapter, Parser, parse_lines, parse_ts
from ..model import Compaction, Session, Turn
from ..pricing import PricingTable
from ..sim.baseline import (
    MAX_CONTEXT_ENV,
    default_threshold,
    effective_max,
    read_current,
    read_env_int,
    read_idle,
)
from ..sim.engine import SimOptions, group_window
from . import LIVE_HARNESSES, message

TAIL = 256 << 10
TAIL_MAX = 32 << 20
REWRITE_GAP_S = 600  # newest record this much after the latest request: maybe a re-written copy, read further back
HEAD_MAX = 4 << 20  # a Codex session_meta line carries the base instructions
HOT_S = 2 * 86400  # files written this recently are stat'ed on every update; older ones only on the full walk
RESCAN_S = 600.0
ACTIVE_S = 6 * 3600
CACHE_1H, CACHE_5M = 3600, 300


class Files:
    """`*.jsonl` files under some roots, kept current without walking the whole tree every time."""

    def __init__(self, roots: list[Path], clock: Callable[[], float]) -> None:
        self.roots = roots
        self.clock = clock
        self.dirs: dict[str, int] = {}  # directory -> mtime_ns when last listed
        self.files: dict[str, float] = {}  # file -> mtime when last seen
        self.walked = -math.inf

    def _list(self, d: str, deep: bool) -> None:
        try:
            mtime = os.stat(d).st_mtime_ns
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            self._drop(d)
            return
        self.dirs[d] = mtime
        here = set()
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if deep or e.path not in self.dirs:
                        self._list(e.path, True)
                elif e.name.endswith(".jsonl"):
                    here.add(e.path)
                    if e.path not in self.files:
                        self.files[e.path] = e.stat().st_mtime
            except OSError:
                continue
        pre = d + os.sep
        for p in [p for p in self.files if p.startswith(pre) and os.sep not in p[len(pre) :] and p not in here]:
            del self.files[p]

    def _drop(self, d: str) -> None:
        pre = d + os.sep
        for k in [k for k in self.dirs if k == d or k.startswith(pre)]:
            del self.dirs[k]
        for p in [p for p in self.files if p.startswith(pre)]:
            del self.files[p]

    def recent(self, within: float) -> list[tuple[str, os.stat_result]]:
        """Files written in the last `within` seconds, with their stat."""
        now = self.clock()
        if now - self.walked >= RESCAN_S:
            self.dirs, self.files = {}, {}
            for r in self.roots:
                if r.is_dir():
                    self._list(str(r), True)
            self.walked = now
        else:
            for d, listed in list(self.dirs.items()):
                if d not in self.dirs:  # dropped with its parent in this loop
                    continue
                try:
                    changed = os.stat(d).st_mtime_ns != listed
                except OSError:
                    self._drop(d)
                    continue
                if changed:
                    self._list(d, False)
        out = []
        hot = max(HOT_S, within)
        for p, m in list(self.files.items()):
            if now - m > hot:
                continue
            try:
                st = os.stat(p)
            except OSError:
                del self.files[p]
                continue
            self.files[p] = st.st_mtime
            if now - st.st_mtime <= within:
                out.append((p, st))
        return out


def read_tail(ad: Adapter, path: str, size: int) -> tuple[Session, Parser, float | None]:
    """The session as far as the end of the file shows it: its last requests, any compaction after them, what the
    parser takes from the file's first records, and the newest record time (None when no record is dated)."""
    tail = TAIL
    while True:
        p = ad.new_parser(Path(path))
        start = max(0, size - tail)
        newest = 0.0
        with open(path, "rb") as f:  # line by line: a deep read must not hold the whole region at once
            if start and p.head_records:
                head = [f.readline(HEAD_MAX) for _ in range(p.head_records)]
                for d in parse_lines(iter(head), p):
                    p.feed(d)
            f.seek(start)
            if start:
                f.readline()  # a tail starts mid-line
            for d in parse_lines(iter(f), p):
                newest = max(newest, parse_ts(d.get("timestamp")) or 0.0)
                p.feed(d)
        s: Session = p.session  # type: ignore[attr-defined]  # live state, with compactions after the last turn
        if not start or tail >= TAIL_MAX:
            return s, p, newest or None
        if s.turns:
            last = latest(s)[1]
            copied = p.rewrites_history and last.ts is not None and newest - last.ts > REWRITE_GAP_S
            if last.model != "unknown" and not copied:
                return s, p, newest or None
        tail *= 4


def latest(s: Session) -> tuple[int, Turn]:
    """The newest request by time (a later line can be a copy of an older one), with its index."""
    i = max(range(len(s.turns)), key=lambda k: (s.turns[k].ts or 0.0, k))
    return i, s.turns[i]


def _after(s: Session, i: int, t: Turn) -> Compaction | None:
    """A compaction after request `i`: by time when both are dated, else by position."""
    for c in reversed(s.compactions):
        if (c.ts > t.ts) if (c.ts and t.ts) else c.turn_index > i:
            return c
    return None


@dataclass
class _Entry:
    sig: tuple[int, int]
    session: Session | None
    cwd: str | None
    newest: float | None = None  # the newest record time: last activity (mtime also moves when a file is touched)
    failed: bool = False


def _slim(s: Session) -> Session:
    for t in s.turns:  # the live view needs sizes and times, not text
        t.snippets, t.tool_keys, t.tool_result_tokens, t.user_text = [], [], [], ""
    for c in s.compactions:
        c.summary_text = ""
    return s


def _cache_ttl(s: Session) -> int | None:
    """The prompt-cache tier of the latest request that wrote cache (Claude Code logs it per request)."""
    if s.harness != "claude-code":
        return None
    for t in sorted(s.turns, key=lambda t: t.ts or 0.0, reverse=True):
        if t.cache_write:
            return CACHE_1H if t.cache_write_1h else CACHE_5M
    return None


def _parent(s: Session) -> str | None:
    # a Claude Code subagent's file lives under <parent session id>/subagents/
    if s.parent_id or s.harness != "claude-code" or s.role != "subagent":
        return s.parent_id
    return Path(s.path).parent.parent.name or None


class LiveSessions:
    """Snapshots of the sessions written to in the last `active_s` seconds."""

    def __init__(
        self,
        roots: dict[str, list[str] | None],
        configs: dict[str, Path | None],
        pricing: PricingTable,
        clock: Callable[[], float],
        active_s: float = ACTIVE_S,
        project_names: bool = True,
        harnesses: tuple[str, ...] = LIVE_HARNESSES,
    ) -> None:
        self.adapters = {h: get_adapter(h)(roots.get(h)) for h in harnesses}
        self.files = {h: Files(ad.roots, clock) for h, ad in self.adapters.items()}
        self.configs, self.pricing, self.clock = configs, pricing, clock
        self.active_s, self.project_names = active_s, project_names
        self.entries: dict[str, _Entry] = {}

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        claude_cfg = self.configs.get("claude-code")
        opts = SimOptions(claude_max_context=read_env_int(claude_cfg, MAX_CONTEXT_ENV))
        idle_on, idle_min, _ = read_idle(claude_cfg)
        current: dict[tuple[str, str], tuple[int | None, str]] = {}
        rows, seen = [], set()
        for h, ad in self.adapters.items():
            for path, st in self.files[h].recent(self.active_s):
                seen.add(path)
                sig = (st.st_size, st.st_mtime_ns)
                e = self.entries.get(path)
                if e is None or e.sig != sig:
                    try:
                        s, p, newest = read_tail(ad, path, st.st_size)
                        e = _Entry(sig, _slim(s) if s.turns else None, p.cwd, newest)
                    except (OSError, ValueError):
                        e = _Entry(sig, None, None, failed=True)
                    self.entries[path] = e
                active = e.newest or st.st_mtime
                if e.session is not None and now - active <= self.active_s:
                    rows.append(self._row(e, active, opts, current))
        for gone in set(self.entries) - seen:
            del self.entries[gone]
        unreadable = sum(e.failed for e in self.entries.values())
        rows.sort(key=lambda r: (-r["last_activity_at"], r["harness"], r["id"]))
        return message(
            "sessions",
            at=now,
            active_s=self.active_s,
            sessions=rows,
            unreadable=unreadable,
            # Claude Code raises a smaller minimum to 100k, as the analysis does
            idle_compaction={"enabled": idle_on, "min_tokens": max(idle_min or SimOptions().idle_min_tokens, 100_000)},
        )

    def _row(
        self, e: _Entry, active: float, opts: SimOptions, current: dict[tuple[str, str], tuple[int | None, str]]
    ) -> dict[str, Any]:
        s = e.session
        assert s is not None
        i, last = latest(s)
        h, model = s.harness, last.model
        # only this model's requests: another model's larger contexts must not raise its window
        mine = replace(s, turns=[x for x in s.turns if x.model == model], compactions=[])
        table_window = self.pricing.lookup(model, s.platform).max_window
        window, window_source, _ = group_window(h, model, [mine], table_window, opts)
        if s.context_window and window == s.context_window:
            window_source = "logged by the session"
        if (h, model) not in current:
            try:
                current[(h, model)] = read_current(h, model, self.configs.get(h))
            except (OSError, ValueError) as err:
                current[(h, model)] = (None, f"unreadable: {err}")
        configured, note = current[(h, model)]
        if configured is not None:
            threshold, source = min(configured, effective_max(h, window)), "configured"
        else:
            threshold, note = default_threshold(h, model, window)
            source = "default"
        after = _after(s, i, last)
        ttl = _cache_ttl(s)
        return {
            "harness": h,
            "id": s.id,
            "role": s.role,
            "parent_id": _parent(s),
            "project": (Path(e.cwd).name or None) if e.cwd and self.project_names else None,
            "model": model,
            "platform": s.platform,
            # null right after a compaction: the next request shows the compacted size
            "ctx": None if after else last.ctx,
            "compacted_at": (after.ts or active) if after else None,
            "window": window,
            "window_source": window_source,
            "threshold": threshold,
            "threshold_source": source,
            "threshold_note": note,
            "last_request_at": last.ts,
            "last_activity_at": active,
            "cache_ttl_s": ttl,
            "cache_expires_at": last.ts + ttl if ttl and last.ts else None,
        }
