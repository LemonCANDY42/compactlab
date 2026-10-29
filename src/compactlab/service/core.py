"""The calibrator behind `compactlab serve` and `compactlab recalibrate`: one lite analysis at a time, on a schedule,
with backoff after failures; the last result is kept in memory and on disk (the report, the API and the lens mod
read it)."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..lens import summarize
from .policy import Consent, Rules, load_applied, run_auto_apply
from .schedule import next_due

log = logging.getLogger("compactlab.service")


class Calibrator:
    """Runs `analyze()` (a lite analysis over the incremental scan cache) at most once at a time.

    `recalibrate()` returns at once with `{"status": "busy"}` while a run is in flight; the schedule loop never
    overlaps runs either. State (last success, failures, next due) is persisted so a restart keeps the cadence.
    """

    def __init__(self, analyze: Callable[[], dict[str, Any]], state_dir: Path, every: float | None,
                 lens_out: Path | None = None, consent: Consent | None = None, rules: Rules | None = None,
                 dry_run: bool = False, clock: Callable[[], float] = time.time) -> None:  # fmt: skip
        self.analyze, self.every, self.clock = analyze, every, clock
        self.dir = state_dir / "service"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lens_out, self.consent, self.rules, self.dry_run = lens_out, consent, rules or Rules(), dry_run
        self._lock = threading.Lock()
        self.result: dict[str, Any] | None = None
        st = self._read("state.json") or {}
        self.last_ok: float | None = st.get("last_ok")
        self.last_attempt: float | None = st.get("last_attempt")
        self.failures: int = int(st.get("failures", 0))
        self.last_error: str | None = st.get("last_error")
        self.last_apply: dict[str, Any] | None = st.get("last_apply")
        self.result = self._read("result.json")

    def _read(self, name: str) -> Any:
        p = self.dir / name
        try:
            return json.loads(p.read_text()) if p.exists() else None
        except (OSError, ValueError):
            log.warning("ignoring unreadable %s", p)
            return None

    def _write(self, name: str, obj: Any) -> None:
        p = self.dir / name
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, sort_keys=True))
        tmp.replace(p)

    @property
    def running(self) -> bool:
        return self._lock.locked()

    def next_due(self) -> float | None:
        return next_due(self.every, self.last_ok, self.last_attempt, self.failures, self.clock())

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "every_s": self.every,
            "last_calibrated": self.last_ok,
            "last_attempt": self.last_attempt,
            "next_calibration": self.next_due(),
            "failures": self.failures,
            "last_error": self.last_error,
            "auto_apply": bool(self.consent),
            "last_apply": self.last_apply,
            "applied": {k.split("::", 1)[1]: v for k, v in load_applied(self.dir / "applied.json").items()},
        }

    def recalibrate(self, reason: str = "manual") -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            log.info("recalibrate (%s) skipped: a run is already in flight", reason)
            return {"status": "busy"}
        try:
            return self._run(reason)
        finally:
            self._lock.release()

    def _run(self, reason: str) -> dict[str, Any]:
        start = self.clock()
        self.last_attempt = start
        log.info("recalibrating (%s)", reason)
        try:
            res = self.analyze()
        except Exception as e:
            self.failures += 1
            self.last_error = f"{type(e).__name__}: {e}"[:500]
            log.error("recalibration failed (%d in a row): %s", self.failures, self.last_error)
            self._save_state()
            return {"status": "error", "error": self.last_error}
        now = self.clock()
        res["calibration"] = {"at": now, "every_s": self.every, "reason": reason, "took_s": round(now - start, 1)}
        self.result = res
        self.last_ok, self.failures, self.last_error = now, 0, None
        self._write("result.json", res)
        lens = summarize(res)
        lens["calibration"] = res["calibration"]
        if self.lens_out:
            self.lens_out.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.lens_out.with_suffix(".tmp")
            tmp.write_text(json.dumps(lens, sort_keys=True))
            tmp.replace(self.lens_out)
        out: dict[str, Any] = {"status": "ok", "took_s": res["calibration"]["took_s"]}
        if self.consent:
            applied = run_auto_apply(res, self.consent, self.dir.parent, self.rules, now, dry_run=self.dry_run)
            for d in applied["decisions"]:
                log.info("auto-apply %s: %s (%s)", d["model"], d["action"], d["reason"])
            if applied.get("manifest"):
                log.info("auto-apply wrote %s; undo with compactlab revert", applied["manifest"])
            self.last_apply = {"at": now, **applied}
            out["auto_apply"] = applied
        self._save_state()
        log.info("recalibrated in %.1fs", res["calibration"]["took_s"])
        return out

    def _save_state(self) -> None:
        keys = ("last_ok", "last_attempt", "failures", "last_error", "last_apply")
        self._write("state.json", {k: getattr(self, k) for k in keys})

    def loop(self, stop: threading.Event, tick: float = 60.0) -> None:
        """Runs due calibrations until `stop` is set; wakes at least every `tick` seconds."""
        while not stop.is_set():
            due = self.next_due()
            if due is not None and self.clock() >= due:
                self.recalibrate("schedule")
                continue
            stop.wait(tick if due is None else max(1.0, min(tick, due - self.clock())))
