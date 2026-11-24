"""One calibration or config write at a time per state dir, across processes (service/lock.py), and
`apply --expect-plan`. The contention tests use a real second process: a helper that holds the lock, or the engine
run the way the app runs it."""

import errno
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from compactlab.cli import main
from compactlab.cli.main import EX_BUSY
from compactlab.service import lock
from compactlab.service.core import Calibrator
from compactlab.service.lock import Busy, LockError, lock_path, single_flight

SRC = str(Path(__file__).parents[1] / "src")
HOLD = """
import sys
from pathlib import Path
from compactlab.service.lock import single_flight
with single_flight(Path(sys.argv[1]), sys.argv[2]):
    print("locked", flush=True)
    sys.stdin.readline()
"""
CLI = "import sys; from compactlab.cli import main; sys.exit(main())"


def _env():
    return {**os.environ, "PYTHONPATH": SRC}


@pytest.fixture
def holder():
    """start(state_dir, job) -> a second process that holds the state dir's lock until its stdin closes or it is
    killed."""
    procs: list[subprocess.Popen] = []

    def start(state: Path, job: str = "calibrate (schedule)") -> subprocess.Popen:
        p = subprocess.Popen([sys.executable, "-c", HOLD, str(state), job], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True, env=_env())  # fmt: skip
        assert p.stdout and p.stdout.readline().strip() == "locked"
        procs.append(p)
        return p

    yield start
    for p in procs:
        if p.poll() is None:
            p.kill()
            p.wait(5)


def _release(p: subprocess.Popen) -> None:
    assert p.stdin
    p.stdin.close()
    assert p.wait(5) == 0


def _result():
    return {"mode": "lite", "groups": [], "sessions": []}


def test_second_process_is_busy_until_the_holder_exits_or_crashes(tmp_path, holder):
    state = tmp_path / "state"
    p = holder(state, "calibrate (schedule)")
    with pytest.raises(Busy) as e, single_flight(state, "apply --write"):
        pass
    assert e.value.holder["pid"] == p.pid and e.value.holder["job"] == "calibrate (schedule)"
    assert "`calibrate (schedule)`" in str(e.value) and f"pid {p.pid}" in str(e.value)
    _release(p)
    with single_flight(state, "apply --write"):  # a normal exit frees it
        pass

    p = holder(state)
    os.kill(p.pid, signal.SIGKILL)  # a crash frees it too: no stale lock, nothing to clean up
    p.wait(5)
    with single_flight(state, "revert"):
        assert json.loads(lock_path(state).read_text())["job"] == "revert"
    assert lock_path(state).exists()  # never deleted (that would let two processes hold "the" lock)
    assert lock_path(state).stat().st_mode & 0o777 == 0o600


def test_the_lock_is_not_reentrant_and_is_per_state_dir(tmp_path):
    with single_flight(tmp_path / "a", "apply --write"):
        with pytest.raises(Busy), single_flight(tmp_path / "a", "revert"):
            pass
        with single_flight(tmp_path / "b", "revert"):  # another state dir is another lock
            pass


def test_engine_calibrate_run_by_the_app_is_busy_while_this_process_writes(tmp_path):
    state = tmp_path / "state"
    with single_flight(state, "apply --write"):
        r = subprocess.run([sys.executable, "-c", CLI, "engine", "calibrate", "--state-dir", str(state),
                            "--lens-out", "", "--reason", "app"], capture_output=True, text=True, env=_env(),
                           timeout=60)  # fmt: skip
    assert r.returncode == 1, r.stderr
    err = json.loads(r.stdout)
    assert err["kind"] == "error" and err["error"]["code"] == "busy" and err["engine_api"] == 1
    assert "`apply --write`" in err["error"]["message"] and f"pid {os.getpid()}" in err["error"]["message"]
    st = state / "service" / "state.json"
    assert not st.exists() or json.loads(st.read_text())["failures"] == 0  # busy is not a failure


def _simulate(tmp_path, claude_root, codex_root) -> Path:
    out = tmp_path / "r.json"
    assert main(["simulate", "--claude-root", str(claude_root), "--codex-root", str(codex_root), "--since-days", "0",
                 "--grid", "100k:600k:50k", "--out", str(out)]) == 0  # fmt: skip
    return out


def test_every_entry_point_is_busy_while_another_process_holds_the_lock(
    tmp_path, holder, claude_root, codex_root, capsys
):
    r = _simulate(tmp_path, claude_root, codex_root)
    settings, state = tmp_path / "settings.json", tmp_path / "state"
    orig = '{\n  "theme": "dark"\n}\n'
    settings.write_text(orig)
    apply = ["apply", "--result", str(r), "--claude-settings", str(settings), "--models", "claude-opus-5-5",
             "--state-dir", str(state)]  # fmt: skip
    pre = tmp_path / "pre.json"
    pre.write_text(json.dumps({"version": 1, "allow_retune": True, "targets": [{
        "harness": "claude-code", "file": str(settings), "keys": ["modelSettings.*.autoCompactWindow"],
        "models": ["claude-opus-5-5"], "min": 100000, "max": 800000}]}))  # fmt: skip
    source = ["--claude-root", str(claude_root), "--codex-root", str(codex_root), "--since-days", "0",
              "--grid", "100k:600k:50k", "--no-cache", "--offline-pricing", "--state-dir", str(state)]  # fmt: skip
    assert main([*apply, "--write", "--i-authorize"]) == 0  # something for revert to undo
    applied = settings.read_text()
    assert applied != orig
    capsys.readouterr()

    p = holder(state, "calibrate (schedule)")
    busy = "busy: another compactlab process is running `calibrate (schedule)`"

    assert main([*apply, "--write", "--i-authorize", "--profile", "cost"]) == EX_BUSY
    assert busy in capsys.readouterr().err and settings.read_text() == applied
    assert main([*apply, "--write", "--i-authorize", "--json"]) == EX_BUSY
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "busy"
    assert main(apply) == 0  # a dry-run only reads: it does not need the lock
    capsys.readouterr()

    assert main(["revert", "--state-dir", str(state)]) == EX_BUSY
    assert busy in capsys.readouterr().err and settings.read_text() == applied
    assert main(["retune", *source, "--preauth", str(pre), "--write"]) == EX_BUSY
    assert busy in capsys.readouterr().err and settings.read_text() == applied

    assert main(["recalibrate", *source, "--lens-out", ""]) == EX_BUSY
    assert "recalibration: busy: another compactlab process is running" in capsys.readouterr().out
    assert main(["engine", "calibrate", *source, "--lens-out", ""]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "busy"

    ran = []
    cal = Calibrator(lambda: ran.append(1) or _result(), state, 3600.0)
    out = cal.recalibrate("api")
    assert out["status"] == "busy" and "`calibrate (schedule)`" in out["message"] and not ran
    assert cal.failures == 0 and cal.last_attempt is None and not (state / "service" / "state.json").exists()

    _release(p)
    assert main(["revert", "--state-dir", str(state)]) == 0 and settings.read_text() == orig


def test_scheduled_runs_wait_for_the_lock_and_count_runs_made_elsewhere(tmp_path, holder):
    state = tmp_path / "state"
    ran = []
    cal = Calibrator(lambda: ran.append("service") or _result(), state, 3600.0)
    tries = []
    real = cal.recalibrate

    def counted(*args, **kw):
        tries.append(1)
        return real(*args, **kw)

    cal.recalibrate = counted  # type: ignore[method-assign]
    p = holder(state, "engine calibrate")
    stop = threading.Event()
    th = threading.Thread(target=cal.loop, args=(stop, 0.05))
    th.start()
    time.sleep(0.5)
    stop.set()
    th.join(5)
    assert not ran and 2 <= len(tries) <= 20  # retried every tick, no busy loop, nothing run
    due = cal.next_due()
    assert cal.failures == 0 and due is not None and due <= time.time()  # still due
    _release(p)

    other = Calibrator(lambda: _result(), state, 3600.0)  # e.g. the app's `engine calibrate`
    assert other.recalibrate("app")["status"] == "ok"
    assert real("schedule", if_due=True) == {"status": "skipped"} and not ran  # that run counts here
    assert cal.last_ok == other.last_ok and cal.result and cal.result["calibration"]["reason"] == "app"
    assert real("api")["status"] == "ok" and ran == ["service"]  # an explicit request still runs

    def broken():
        raise RuntimeError("disk on fire")

    failing = Calibrator(broken, state, 3600.0, clock=lambda: time.time() + 7200)  # an hour after it is due
    assert failing.recalibrate("cli")["status"] == "error"
    later = Calibrator(lambda: ran.append("later") or _result(), state, 3600.0, clock=lambda: time.time() + 7230)
    later.failures = 0  # stale in memory: the failure is read again under the lock
    assert later.recalibrate("schedule", if_due=True) == {"status": "skipped"} and later.failures == 1
    assert "later" not in ran  # backing off after a failure made elsewhere


# ---------- apply --expect-plan ----------


def test_expect_plan_writes_only_the_reviewed_plan(tmp_path, claude_root, codex_root, capsys):
    r = _simulate(tmp_path, claude_root, codex_root)
    settings, state = tmp_path / "settings.json", tmp_path / "state"
    settings.write_text("{}\n")
    base = ["apply", "--result", str(r), "--claude-settings", str(settings), "--models", "claude-opus-5-5",
            "--state-dir", str(state)]  # fmt: skip
    capsys.readouterr()
    assert main([*base, "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    h = plan["plan_hash"]
    assert plan["dry_run"] and plan["changes"] and len(h) == 16
    assert main(base) == 0 and f"plan {h}" in capsys.readouterr().out  # same plan, same hash, any run

    new = plan["changes"][0]["new"]
    settings.write_text(json.dumps({"modelSettings": {"claude-opus-5-5": {"autoCompactWindow": new + 50_000}}}))
    edited = settings.read_text()  # someone changed the key after the review
    assert main([*base, "--write", "--i-authorize", "--expect-plan", h, "--json"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == "plan_changed" and h in out["error"]["message"]
    assert out["plan_hash"] != h and out["changes"][0]["old"] == new + 50_000 and "manifest" not in out
    assert settings.read_text() == edited and not (state / "manifests").exists() and not (state / "backups").exists()
    assert main([*base, "--write", "--i-authorize", "--expect-plan", h]) == 2  # text: plan on stdout, why on stderr
    cap = capsys.readouterr()
    assert f"plan {out['plan_hash']}" in cap.out and "refused: the plan is not the reviewed one" in cap.err
    assert main([*base, "--expect-plan", h]) == 2  # a dry-run checks it too
    assert "refused: the plan is not the reviewed one" in capsys.readouterr().err
    assert main([*base, "--write", "--i-authorize", "--expect-plan", " ", "--json"]) == 2  # an empty hash never matches
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "plan_changed" and settings.read_text() == edited

    settings.write_text("{}\n")
    assert main([*base, "--write", "--i-authorize", "--expect-plan", h.upper(), "--json"]) == 0
    done = json.loads(capsys.readouterr().out)
    assert done["plan_hash"] == h and done["manifest"] and "error" not in done
    assert json.loads(settings.read_text())["modelSettings"]["claude-opus-5-5"]["autoCompactWindow"] == new


def test_refusals_are_json_with_json(tmp_path, claude_root, codex_root, capsys):
    r = _simulate(tmp_path, claude_root, codex_root)
    capsys.readouterr()
    args = ["apply", "--result", str(r), "--claude-settings", str(tmp_path / "s.json"), "--models", "claude-opus-5-5",
            "--state-dir", str(tmp_path / "state"), "--write", "--json"]  # fmt: skip
    assert main(args) == 2  # no authorization
    cap = capsys.readouterr()
    assert json.loads(cap.out)["error"]["code"] == "refused" and "refused: writing needs --preauth" in cap.err


def test_a_lock_that_cannot_be_taken_is_a_failure_not_busy_and_not_a_dead_scheduler(tmp_path, monkeypatch, capsys):
    def no_locks(fd, op):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(lock.fcntl, "flock", no_locks)  # e.g. a home directory on a file system without flock
    state = tmp_path / "state"
    with pytest.raises(LockError, match="cannot lock"), single_flight(state, "revert"):
        pass
    assert main(["revert", "--state-dir", str(state)]) == 2 and "refused: cannot lock" in capsys.readouterr().err

    ran = []
    cal = Calibrator(lambda: ran.append(1) or _result(), state, 3600.0)
    out = cal.recalibrate("schedule", if_due=True)
    assert out["status"] == "error" and "cannot lock" in out["error"] and cal.failures == 1 and not ran
    assert not (state / "service" / "state.json").exists()  # shared state is only written under the lock
    due = cal.next_due()
    assert due is not None and due > time.time() + 200  # backs off like a failed analysis
    stop = threading.Event()
    th = threading.Thread(target=cal.loop, args=(stop, 0.05))
    th.start()
    time.sleep(0.2)
    assert th.is_alive() and cal.failures == 1  # waiting out the backoff, not dead and not spinning
    stop.set()
    th.join(5)


def test_a_run_that_cannot_write_its_result_saves_the_failure_before_releasing_the_lock(tmp_path, monkeypatch):
    state = tmp_path / "state"
    cal = Calibrator(_result, state, 3600.0)
    write, save = cal._write, cal._save_state
    held = []

    def failing_write(name, obj):
        if name == "result.json":
            raise OSError(errno.ENOSPC, "No space left on device")
        write(name, obj)

    def probing_save():
        try:
            with single_flight(state, "probe"):
                held.append(False)
        except Busy:
            held.append(True)
        save()

    monkeypatch.setattr(cal, "_write", failing_write)
    monkeypatch.setattr(cal, "_save_state", probing_save)
    out = cal.recalibrate("manual")
    assert out["status"] == "error" and "No space left" in out["error"]
    assert held == [True]  # another process could not have run between the failure and its record
    st = json.loads((state / "service" / "state.json").read_text())
    assert st["failures"] == 1 and st["last_ok"] is None  # not recorded as calibrated
    assert cal.last_ok is None and cal.result is None
