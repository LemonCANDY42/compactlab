"""The calibrator behind `compactlab serve`, `compactlab recalibrate` and `compactlab engine calibrate`: one lite
analysis at a time (in this process and across processes, see lock.py), on a schedule, with backoff after failures;
the last result is kept in memory and on disk (the report, the API and the lens mod read it)."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import apply as ap
from ..lens import summarize
from .lock import Busy, LockError, single_flight
from .policy import Consent, Rules, load_applied, load_consent, run_auto_apply
from .schedule import next_due

log = logging.getLogger("compactlab.service")


class Calibrator:
    """Runs `analyze()` (a lite analysis over the incremental scan cache) at most once at a time.

    `recalibrate()` returns at once with `{"status": "busy"}` while a run is in flight here or another process holds
    the state dir's lock (a calibration or a config write); the schedule loop never overlaps runs either. State (last
    success, failures, next due) is persisted so a restart keeps the cadence, and is re-read under the lock, so runs
    by other processes (the app, the CLI, a scheduler) count too.
    """

    def __init__(self, analyze: Callable[[], dict[str, Any]], state_dir: Path, every: float | None,
                 lens_out: Path | None = None, consent: Consent | None = None, rules: Rules | None = None,
                 dry_run: bool = False, clock: Callable[[], float] = time.time,
                 consent_path: Path | None = None) -> None:  # fmt: skip
        self.analyze, self.every, self.clock = analyze, every, clock
        self.dir = state_dir / "service"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lens_out, self.consent, self.rules, self.dry_run = lens_out, consent, rules or Rules(), dry_run
        # with a consent file, the consent is re-read before every auto-apply: clearing or expiring it takes effect
        self.consent_path = consent_path
        self.auto_apply = consent is not None or consent_path is not None
        self._lock = threading.Lock()
        self.result: dict[str, Any] | None = None
        self.last_ok: float | None = None
        self.last_attempt: float | None = None
        self.failures = 0
        self.last_error: str | None = None
        self.last_apply: dict[str, Any] | None = None
        self._load()

    def _load(self) -> None:
        """Read the persisted state; the result too when it is newer than the one in memory."""
        st = self._read("state.json") or {}
        last_ok = st.get("last_ok")
        if self.result is None or last_ok != self.last_ok:
            self.result = self._read("result.json")
        self.last_ok = last_ok
        self.last_attempt = st.get("last_attempt")
        self.failures = int(st.get("failures", 0))
        self.last_error = st.get("last_error")
        self.last_apply = st.get("last_apply")

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

    def next_text(self) -> str:
        """The next scheduled calibration as local time, 'now', or 'manual only'."""
        due = self.next_due()
        if due is None:
            return "manual only"
        if due <= self.clock():
            return "now"
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(due))

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "every_s": self.every,
            "last_calibrated": self.last_ok,
            "last_attempt": self.last_attempt,
            "next_calibration": self.next_due(),
            "failures": self.failures,
            "last_error": self.last_error,
            "auto_apply": self.auto_apply and self.consent is not None,
            "last_apply": self.last_apply,
            "applied": {k.split("::", 1)[1]: v for k, v in load_applied(self.dir / "applied.json").items()},
        }

    def recalibrate(self, reason: str = "manual", if_due: bool = False) -> dict[str, Any]:
        """One run now. Busy (nothing runs, not a failure) while another run holds this calibrator or the state
        dir's lock. With `if_due` (the schedule), a run that another process made since is enough: `skipped`."""
        if not self._lock.acquire(blocking=False):
            log.info("recalibrate (%s) skipped: a run is already in flight", reason)
            return {"status": "busy"}
        try:
            with single_flight(self.dir.parent, f"calibrate ({reason})"):
                self._load()  # another process may have calibrated, or failed, since we last looked
                due = self.next_due()
                if if_due and due is not None and due > self.clock():
                    log.info("recalibrate (%s) skipped: calibrated elsewhere; next calibration: %s", reason,
                             self.next_text())  # fmt: skip
                    return {"status": "skipped"}
                result, last_ok = self.result, self.last_ok
                try:
                    return self._run(reason)
                except OSError as e:  # a result, lens or state file cannot be written: a failed run
                    self.result, self.last_ok = result, last_ok  # nothing new was recorded as calibrated
                    return self._fail(f"{type(e).__name__}: {e}")  # saved while still holding the lock
        except Busy as e:
            log.info("recalibrate (%s) skipped: %s", reason, e)
            return {"status": "busy", "message": str(e)}
        except LockError as e:  # the lock cannot be taken: back off here; the shared state is not written without it
            self.last_attempt = self.clock()
            return self._fail(str(e), save=False)
        finally:
            self._lock.release()

    def _fail(self, error: str, save: bool = True) -> dict[str, Any]:
        self.failures += 1
        self.last_error = error[:500]
        log.error("recalibration failed (%d in a row): %s", self.failures, self.last_error)
        if save:
            try:
                self._save_state()
            except OSError as e:
                log.error("cannot save the calibrator state: %s", e)
        return {"status": "error", "error": self.last_error}

    def _run(self, reason: str) -> dict[str, Any]:
        start = self.clock()
        self.last_attempt = start
        log.info("recalibrating (%s)", reason)
        try:
            res = self.analyze()
        except Exception as e:
            return self._fail(f"{type(e).__name__}: {e}")
        now = self.clock()
        scan = res.pop("scan", None) or {}
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
        if self.auto_apply:
            try:
                applied = self._auto_apply(res, now)
            except Exception as e:  # e.g. a malformed settings file: back off like a failed analysis
                self.failures += 1
                self.last_error = f"auto-apply: {type(e).__name__}: {e}"[:500]
                log.error("auto-apply failed (%d in a row): %s", self.failures, self.last_error)
                self.last_apply = {"at": now, "error": self.last_error}
                self._save_state()
                return {"status": "error", "error": self.last_error}
            self.last_apply = {"at": now, **applied}
            out["auto_apply"] = applied
        self._save_state()
        log.info("recalibrated in %.1fs: %s files, %s cached, %s parsed, %d groups%s; next calibration: %s",
                 res["calibration"]["took_s"], scan.get("files", "?"), scan.get("cached", "?"), scan.get("parsed", "?"),
                 len(res.get("groups", [])), "; analysis reused (nothing changed)" if scan.get("reused") else "",
                 self.next_text())  # fmt: skip
        return out

    def _auto_apply(self, res: dict[str, Any], now: float) -> dict[str, Any]:
        if self.consent_path is not None:
            try:
                self.consent = load_consent(self.consent_path)
            except ap.ApplyError as e:  # cleared, expired or unreadable: stop applying, keep calibrating
                self.consent = None
                log.warning("auto-apply is off: %s", e)
                return {"decisions": [], "dry_run": self.dry_run, "diff": "", "manifest": None, "error": str(e)}
        if self.consent is None:
            return {"decisions": [], "dry_run": self.dry_run, "diff": "", "manifest": None,
                    "error": "no consent"}  # fmt: skip
        applied = run_auto_apply(res, self.consent, self.dir.parent, self.rules, now, dry_run=self.dry_run)
        for d in applied["decisions"]:
            log.info("auto-apply %s: %s (%s)", d["model"], d["action"], d["reason"])
        if applied.get("manifest"):
            log.info("auto-apply wrote %s; undo with compactlab revert", applied["manifest"])
        return applied

    def _save_state(self) -> None:
        keys = ("last_ok", "last_attempt", "failures", "last_error", "last_apply")
        self._write("state.json", {k: getattr(self, k) for k in keys})

    def loop(self, stop: threading.Event, tick: float = 60.0) -> None:
        """Runs due calibrations until `stop` is set; wakes at least every `tick` seconds. While another run holds
        the lock it tries again every `tick`."""
        while not stop.is_set():
            due = self.next_due()
            if due is not None and self.clock() >= due:
                if self.recalibrate("schedule", if_due=True)["status"] == "busy":
                    stop.wait(tick)
                continue
            stop.wait(tick if due is None else max(1.0, min(tick, due - self.clock())))
