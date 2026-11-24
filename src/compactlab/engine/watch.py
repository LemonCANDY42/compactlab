"""`compactlab engine watch`: a resident process that prints the active sessions as JSON Lines.

stdout: a `hello` line, then a `sessions` line at start and whenever the sessions change. stdin, one command per line:
`refresh` (files changed: rescan, print only if something changed), `snapshot` (rescan and print regardless),
`ping` (answers `pong`), `quit`. End of input also ends it, so the process never outlives the app that started it.
Scans are at least `min_interval` seconds apart (requests in between are merged into one), and every `interval`
seconds without a request (0: never) a scan runs anyway, so sessions that went quiet drop out. A closed stdout
raises BrokenPipeError to the caller.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from typing import Any, TextIO

from . import dumps, error, message

COMMANDS = ("refresh", "snapshot", "ping", "quit")


def run(
    scan: Callable[[], dict[str, Any]],
    hello: dict[str, Any],
    inp: TextIO,
    out: TextIO,
    min_interval: float = 5.0,
    interval: float = 60.0,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    q: queue.Queue[str | None] = queue.Queue()

    def read() -> None:
        try:
            for line in inp:
                q.put(line.strip())
        finally:
            q.put(None)

    threading.Thread(target=read, name="engine-watch-stdin", daemon=True).start()

    def emit(msg: dict[str, Any]) -> None:
        out.write(dumps(msg) + "\n")
        out.flush()

    last_rows: str | None = None
    last_scan = -float("inf")
    due: float | None = clock()  # the first scan runs at once and is always printed
    force = True
    emit(hello)
    while True:
        now = clock()
        if due is None and interval > 0:
            due = last_scan + interval
        if due is not None and now >= due:
            try:
                snap = scan()
            except Exception as e:  # keep running; the next request or interval retries
                emit(error("scan_failed", f"{type(e).__name__}: {e}"[:500]))
            else:
                rows = dumps({k: v for k, v in snap.items() if k != "at"})
                if force or rows != last_rows:
                    emit(snap)
                last_rows = rows
            last_scan, due, force = clock(), None, False
            continue
        try:
            cmd = q.get(timeout=None if due is None else max(due - now, 0.0))
        except queue.Empty:
            continue
        if cmd is None or cmd == "quit":
            return 0
        if cmd == "ping":
            emit(message("pong"))
        elif cmd in ("refresh", "snapshot"):
            at = max(clock(), last_scan + min_interval)
            due = at if due is None else min(due, at)
            force = force or cmd == "snapshot"
        elif cmd:
            emit(error("bad_command", f"unknown command {cmd[:80]!r}; expected one of {', '.join(COMMANDS)}"))
