"""One compactlab job at a time per state dir, across processes.

Every calibration that keeps the calibrator's state (`recalibrate`, `engine calibrate`, a scheduled or requested run
of `serve`) and every config write (`apply --write`, `retune`, `revert`) runs inside `single_flight()`. It takes an
exclusive `flock` on `<state-dir>/service/lock` without waiting: whoever gets it runs, everyone else gets `Busy` at
once and does nothing. The menu bar app, the CLI and a launchd/systemd schedule can therefore start jobs whenever they
like.

There are no stale locks: a `flock` belongs to the open file, so the kernel drops it when the holder closes it or
exits, however it exits (a crash, `kill -9`). The descriptor is not inherited by child processes (PEP 446), so a
subprocess cannot keep it alive. The file stays behind on purpose and must never be deleted: a process still holding
the old file and one that created a new one would both think they hold the lock. What the holder writes into it
(pid, job, start time) only feeds the `Busy` message.

The lock is not reentrant: a second `single_flight()` on the same state dir in the same process is `Busy` too. Take it
once, at the entry point.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any


class Busy(Exception):
    """Another process holds the lock. `holder` is what it recorded ({pid, job, since}); {} when it is unreadable."""

    def __init__(self, path: Path, holder: dict[str, Any]) -> None:
        self.path, self.holder = path, holder
        super().__init__(describe(holder))


class LockError(OSError):
    """The lock file cannot be created, opened or locked (permissions, a file system without `flock`). Not
    contention: nothing else is running because of it."""


def lock_path(state_dir: Path) -> Path:
    return state_dir / "service" / "lock"


def describe(holder: dict[str, Any]) -> str:
    job, pid, since = holder.get("job"), holder.get("pid"), holder.get("since")
    who = f"`{job}`" if isinstance(job, str) and job else "a calibration or config write"
    when = []
    if isinstance(pid, int):
        when.append(f"pid {pid}")
    if isinstance(since, (int, float)):
        when.append("since " + time.strftime("%H:%M:%S", time.localtime(since)))
    extra = f" ({', '.join(when)})" if when else ""
    return f"another compactlab process is running {who}{extra}; try again when it finishes"


def _holder(fd: int) -> dict[str, Any]:
    try:
        d = json.loads(os.pread(fd, 4096, 0) or b"{}")
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


@contextlib.contextmanager
def single_flight(state_dir: Path, job: str) -> Generator[None, None, None]:
    """Hold `<state-dir>/service/lock` for the duration of the block, or raise `Busy` at once (`LockError` when the
    lock cannot be taken at all)."""
    path = lock_path(state_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        raise LockError(f"cannot open the lock {path}: {e.strerror or e}") from e
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise LockError(f"cannot lock {path}: {e.strerror or e}") from e
            raise Busy(path, _holder(fd)) from None
        rec = json.dumps({"pid": os.getpid(), "job": job, "since": time.time()}).encode()
        with contextlib.suppress(OSError):  # only for the busy message; never a reason not to run
            os.ftruncate(fd, 0)
            os.pwrite(fd, rec, 0)
        yield
    finally:
        os.close(fd)  # releases the lock
