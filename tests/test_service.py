"""The background service: scheduling math, single-flight, backoff, auto-apply policy, consent scope, the HTTP guard,
and the scheduler files. Every path is under the temp HOME the autouse fixture sets."""

import http.client
import json
import threading
import time
from pathlib import Path

import pytest

from compactlab import apply as ap
from compactlab.cli import main
from compactlab.service import schedule as sc
from compactlab.service.core import Calibrator
from compactlab.service.http import check_bind, load_token, make_server
from compactlab.service.policy import Consent, Rules, decide, load_consent, run_auto_apply, save_consent

GRID = [100_000 + 25_000 * i for i in range(37)]  # 100k .. 1M


# ---------- scheduling math ----------


def test_parse_and_format_intervals():
    assert sc.parse_every("6h") == 6 * 3600 and sc.parse_every("1d") == 86400 and sc.parse_every("2w") == 14 * 86400
    assert sc.parse_every("90m") == 5400 and sc.parse_every("off") is None
    for bad in ("5s", "1y", "abc", "-1d"):
        with pytest.raises(ValueError):
            sc.parse_every(bad)
    assert sc.fmt_every(86400) == "1d" and sc.fmt_every(5400) == "90m" and sc.fmt_every(None) == "off"


def test_next_due_backoff_and_staleness():
    day = 86400.0
    assert sc.next_due(day, None, None, 0, now=1000) == 1000  # never calibrated: now
    assert sc.next_due(day, 5000, 5000, 0, now=6000) == 5000 + day
    assert sc.next_due(None, 5000, 5000, 0, now=6000) is None  # manual only
    assert sc.next_due(day, 5000, 9000, 1, now=9100) == 9000 + 300  # first failure: 5 min
    assert sc.next_due(day, 5000, 9000, 3, now=9100) == 9000 + 1200  # doubling
    assert sc.backoff(3600, 10) == 3600  # never longer than the interval
    assert not sc.is_stale(1000, day, 1000 + 1.5 * day) and sc.is_stale(1000, day, 1000 + 2.5 * day)
    assert sc.is_stale(None, day, 0)


# ---------- calibrator ----------


def _result(model="claude-opus-5-5"):
    return {"mode": "lite", "groups": [], "sessions": [], "model": model}


def test_single_flight_and_persisted_state(tmp_path):
    gate, started = threading.Event(), threading.Event()

    def slow():
        started.set()
        gate.wait(5)
        return _result()

    cal = Calibrator(slow, tmp_path, 3600.0, lens_out=tmp_path / "lens.json")
    t = threading.Thread(target=cal.recalibrate, args=("first",))
    t.start()
    assert started.wait(5)
    assert cal.running and cal.recalibrate("second") == {"status": "busy"}
    gate.set()
    t.join(5)
    assert not cal.running and cal.result is not None and cal.result["calibration"]["reason"] == "first"
    lens = json.loads((tmp_path / "lens.json").read_text())
    assert lens["calibration"]["every_s"] == 3600.0
    again = Calibrator(lambda: _result(), tmp_path, 3600.0)  # a restart keeps the cadence and the last result
    assert again.last_ok == cal.last_ok and again.next_due() == cal.last_ok + 3600 and again.result is not None


def test_failures_back_off_and_keep_the_last_result(tmp_path):
    clock = [1000.0]
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("disk on fire")
        return _result()

    cal = Calibrator(flaky, tmp_path, 86400.0, clock=lambda: clock[0])
    assert cal.recalibrate()["status"] == "ok"
    clock[0] += 86400
    out = cal.recalibrate()
    assert out["status"] == "error" and "disk on fire" in out["error"] and cal.failures == 1
    assert cal.result is not None  # last good result kept
    assert cal.next_due() == clock[0] + 300


def test_loop_runs_when_due_and_stops(tmp_path):
    runs = []
    cal = Calibrator(lambda: runs.append(1) or _result(), tmp_path, 3600.0)
    stop = threading.Event()
    th = threading.Thread(target=cal.loop, args=(stop, 0.05))
    th.start()
    deadline = time.time() + 5
    while not runs and time.time() < deadline:
        time.sleep(0.02)
    stop.set()
    th.join(5)
    assert runs == [1]  # due once at start, then not again for an hour


# ---------- auto-apply policy ----------


def _group(make_group, days=30.0, turns=5000):
    cost = [500 + 15 * i for i in range(len(GRID))]  # cheapest at the bottom, rises with the window
    g = make_group(GRID, cost, emax=1_000_000, days=days, default_thr=967_000)
    g.update(model="claude-opus-5-5", role="main", harness="claude-code", n_turns=turns,
             time_model={"turn_source": "own", "compact_source": "own"})  # fmt: skip
    return g


def test_hysteresis_max_step_cooldown_and_history(make_group):
    g = _group(make_group)
    target = g["profiles"]["balanced"]["threshold"]
    r = Rules()
    d = decide(g, "balanced", None, None, None, now=0, rules=r)
    assert d.action == "apply" and d.value in GRID
    assert abs((d.value or 0) - 967_000) <= r.max_step  # one bounded step toward the target
    assert target < (d.value or 0)
    # within the dead-band: no change
    near = decide(g, "balanced", target + 25_000, None, None, now=0, rules=r)
    assert near.action == "skip" and "dead-band" in near.reason
    # cooldown after our own change
    cool = decide(g, "balanced", 875_000, 875_000, last_applied_at=0, now=86400, rules=r)
    assert cool.action == "skip" and "cooldown" in cool.reason
    # too little history
    thin = decide(_group(make_group, days=2, turns=100), "balanced", None, None, None, now=0, rules=r)
    assert thin.action == "skip" and "too little history" in thin.reason
    # benefit below both thresholds
    picky = decide(g, "balanced", None, None, None, now=0, rules=Rules(min_benefit_pct=99, min_benefit_min=999))
    assert picky.action == "skip" and "benefit too small" in picky.reason


def test_low_confidence_and_unsafe_flags_skip(make_group):
    g = _group(make_group)
    g["time_model"] = {"turn_source": "prior", "compact_source": "prior"}
    assert "low confidence" in decide(g, "balanced", None, None, None, 0, Rules()).reason
    g = _group(make_group)
    g["profiles"]["balanced"]["flags"] = ["recall_floor_not_met"]
    assert decide(g, "balanced", None, None, None, 0, Rules()).action == "skip"


def test_manual_edit_is_respected_unless_forced(make_group):
    g = _group(make_group)
    d = decide(g, "balanced", 700_000, last_written=875_000, last_applied_at=0, now=30 * 86400, rules=Rules())
    assert d.action == "notify" and "edited by someone else" in d.reason
    forced = decide(g, "balanced", 700_000, 875_000, 0, 30 * 86400, Rules(), force=True)
    assert forced.action in ("apply", "skip") and forced.action != "notify"


def test_consent_scope_backup_journal_and_revert(tmp_path, make_group):
    settings = tmp_path / "settings.json"
    original = {"theme": "dark", "modelSettings": {"claude-sonnet-5-5": {"autoCompactWindow": 300000}}}
    settings.write_text(json.dumps(original, indent=2))
    consent = Consent("claude-code", str(settings), ["claude-opus-5-5"], "balanced")
    save_consent(tmp_path / "consent.json", consent)
    assert load_consent(tmp_path / "consent.json").models == ["claude-opus-5-5"]
    assert oct((tmp_path / "consent.json").stat().st_mode & 0o777) == "0o600"
    opus, sonnet = _group(make_group), _group(make_group)
    sonnet["model"] = "claude-sonnet-5-5"
    result = {"groups": [opus, sonnet]}

    dry = run_auto_apply(result, consent, tmp_path, Rules(), now=0, dry_run=True)
    assert dry["diff"] and dry["manifest"] is None and json.loads(settings.read_text()) == original

    out = run_auto_apply(result, consent, tmp_path, Rules(), now=0)
    after = json.loads(settings.read_text())
    assert [d["model"] for d in out["decisions"]] == ["claude-opus-5-5"]  # sonnet is outside the consent
    assert after["theme"] == "dark" and after["modelSettings"]["claude-sonnet-5-5"] == {"autoCompactWindow": 300000}
    new = after["modelSettings"]["claude-opus-5-5"]["autoCompactWindow"]
    assert new in GRID and 867_000 <= new < 967_000
    entry = json.loads((tmp_path / "service" / "journal.jsonl").read_text().splitlines()[-1])
    assert entry["new"] == new and entry["old"] is None and entry["manifest"]

    # a second run inside the cooldown leaves it alone; a manual edit is not overwritten
    assert run_auto_apply(result, consent, tmp_path, Rules(), now=3600)["manifest"] is None
    after["modelSettings"]["claude-opus-5-5"]["autoCompactWindow"] = 600_000
    settings.write_text(json.dumps(after))
    edited = run_auto_apply(result, consent, tmp_path, Rules(), now=30 * 86400)
    assert edited["decisions"][0]["action"] == "notify" and json.loads(settings.read_text()) == after

    # revert understands the auto-apply manifest (key edited since: skipped without --force)
    settings.write_text(json.dumps({**after, "modelSettings": {**after["modelSettings"],
                                    "claude-opus-5-5": {"autoCompactWindow": new}}}))  # fmt: skip
    assert main(["revert", "--state-dir", str(tmp_path)]) == 0
    assert "claude-opus-5-5" not in json.loads(settings.read_text())["modelSettings"]
    assert json.loads((tmp_path / "service" / "applied.json").read_text()) == {}


def test_consent_validation(tmp_path):
    with pytest.raises(ap.ApplyError):
        save_consent(tmp_path / "c.json", Consent("claude-code", "x", [], "balanced"))
    with pytest.raises(ap.ApplyError):
        load_consent(tmp_path / "missing.json")
    (tmp_path / "old.json").write_text(
        json.dumps({**Consent("claude-code", "x", ["m"]).__dict__, "expires": "2001-01-01"})
    )
    with pytest.raises(ap.ApplyError, match="expired"):
        load_consent(tmp_path / "old.json")


# ---------- HTTP ----------


def test_bind_refusal():
    check_bind("127.0.0.1", False)
    check_bind("::1", False)
    for host in ("0.0.0.0", "192.168.1.5", "example.com"):
        with pytest.raises(PermissionError):
            check_bind(host, False)
    check_bind("0.0.0.0", True)  # explicit opt-in


def _req(port, method, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


def test_api_token_guard_and_host_check(tmp_path):
    cal = Calibrator(lambda: _result(), tmp_path, None)
    token = load_token(tmp_path / "service" / "token")
    assert load_token(tmp_path / "service" / "token") == token  # reused
    srv = make_server(cal, "127.0.0.1", 0, token)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        status, body = _req(port, "GET", "/api/health")
        assert status == 200 and json.loads(body)["ok"] is True
        assert _req(port, "GET", "/api/result")[0] == 503  # nothing calibrated yet
        assert _req(port, "POST", "/api/recalibrate")[0] == 401
        assert _req(port, "POST", "/api/recalibrate", {"Authorization": "Bearer wrong"})[0] == 401
        assert _req(port, "GET", "/api/health", {"Host": "evil.example:80"})[0] == 403  # DNS rebinding
        assert _req(port, "POST", "/api/recalibrate", {"Authorization": f"Bearer {token}"})[0] == 202
        deadline = time.time() + 5
        while cal.result is None and time.time() < deadline:
            time.sleep(0.02)
        assert _req(port, "GET", "/api/result")[0] == 200
        assert _req(port, "GET", "/api/profile/nope")[0] == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_served_report_has_recalibrate_button(tmp_path, claude_root, codex_root):
    from test_cli import sim

    res = json.loads(sim(tmp_path, claude_root, codex_root).read_text())
    cal = Calibrator(lambda: res, tmp_path, 86400.0)
    cal.recalibrate()
    srv = make_server(cal, "127.0.0.1", 0, "tok")
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        status, body = _req(srv.server_address[1], "GET", "/")
        page = body.decode()
        assert status == 200 and '"service"' in page and "recalibrate" in page
        status, body = _req(srv.server_address[1], "GET", "/api/profile/time")
        assert status == 200 and json.loads(body)["profile"] == "time"
    finally:
        srv.shutdown()
        srv.server_close()


# ---------- scheduler files ----------


def test_schedule_units_install_status_uninstall(tmp_path):
    home = tmp_path / "home"
    for plat in ("launchd", "systemd"):
        units = sc.units(plat, "recalibrate", 6 * 3600, ["--state-dir", "/s"], home)
        assert all(str(u.path).startswith(str(home)) and sc.MARKER in u.text for u in units)
        assert "recalibrate" in units[0].text
        assert "+" in sc.diff(units)
        assert sc.install(units) == [u.path for u in units]
        assert sc.installed(plat, home) and sc.diff(units) == "(no change)\n"
        assert sc.uninstall(plat, home) and not sc.installed(plat, home)
    launchd = sc.units("launchd", "recalibrate", 6 * 3600, [], home)[0].text
    assert "<integer>21600</integer>" in launchd and "<key>Nice</key><integer>10</integer>" in launchd
    timer = sc.units("systemd", "recalibrate", 86400, [], home)[1].text
    assert "OnUnitActiveSec=86400s" in timer
    serve = sc.units("systemd", "serve", 86400, [], home)
    assert len(serve) == 1 and "Restart=on-failure" in serve[0].text and "--every" in serve[0].text
    with pytest.raises(ValueError):
        sc.units("launchd", "recalibrate", None, [], home)


def test_schedule_never_overwrites_a_foreign_file(tmp_path):
    home = tmp_path / "home"
    u = sc.units("launchd", "recalibrate", 86400, [], home)
    u[0].path.parent.mkdir(parents=True)
    u[0].path.write_text("<plist>someone else's</plist>")
    with pytest.raises(PermissionError):
        sc.install(u)
    assert sc.uninstall("launchd", home) == []  # and uninstall leaves it alone too


def test_schedule_cli_uses_home_and_needs_yes(capsys):
    home = Path.home()  # the autouse fixture's temp HOME
    assert main(["schedule", "print", "--platform", "systemd", "--every", "12h"]) == 0
    assert "OnUnitActiveSec=43200s" in capsys.readouterr().out
    assert main(["schedule", "install", "--platform", "systemd", "--every", "12h"]) == 2  # no tty, no --yes
    assert not (home / ".config").exists()
    assert main(["schedule", "install", "--platform", "systemd", "--every", "12h", "--yes"]) == 0
    assert (home / ".config" / "systemd" / "user" / "compactlab.timer").exists()
    assert main(["schedule", "uninstall", "--platform", "systemd"]) == 2
    assert main(["schedule", "uninstall", "--platform", "systemd", "--yes"]) == 0
    assert not (home / ".config" / "systemd" / "user" / "compactlab.timer").exists()


def test_recalibrate_cli_writes_result_and_lens(tmp_path, claude_root, codex_root):
    state = tmp_path / "state"
    args = [
        "recalibrate",
        "--claude-root",
        str(claude_root),
        "--codex-root",
        str(codex_root),
        "--since-days",
        "0",
        "--grid",
        "100k:600k:50k",
        "--state-dir",
        str(state),
        "--lens-out",
        str(state / "lens.json"),
        "--json",
    ]
    assert main(args) == 0
    lens = json.loads((state / "lens.json").read_text())
    assert lens["calibration"]["every_s"] == 86400 and lens["groups"]
    assert json.loads((state / "service" / "result.json").read_text())["mode"] == "lite"
    assert (state / "service" / "service.log").exists()
    assert main([*args, "--auto-apply"]) == 2  # no consent stored


def test_steps_from_an_out_of_guardrail_default_move_toward_the_floor(make_group):
    """Recall falls as the window grows: the default sits below the floor, and the first bounded step down is still
    below it. The step is taken because it improves recall; a step that made it worse would be refused."""
    recall = [0.95 - 0.003 * i for i in range(len(GRID))]  # 0.95 at 100k ... 0.842 at 1M
    g = _group(make_group)
    g["recall"] = recall
    j = GRID.index(950_000)
    g["default"]["recall"] = recall[j]
    d = decide(g, "balanced", None, None, None, now=0, rules=Rules())
    assert d.action == "apply" and recall[GRID.index(d.value)] < 0.90  # still below the floor, but better
    assert recall[GRID.index(d.value)] > recall[j]


class _Tty:
    def __init__(self, tty: bool):
        self.tty, self.out = tty, ""

    def isatty(self) -> bool:
        return self.tty

    def write(self, s: str) -> int:
        self.out += s
        return len(s)

    def flush(self) -> None:
        pass


def test_progress_is_one_in_place_line_that_fits_and_silent_off_a_tty(monkeypatch):
    import importlib
    import os
    import shutil

    cli = importlib.import_module("compactlab.cli.main")
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((40, 24)))
    tty = _Tty(True)
    monkeypatch.setattr(cli.sys, "stderr", tty)
    msgs = [f"scanning claude-code {i}/880 files · cached {i - 3} and some more words" for i in range(20, 900, 20)]
    for m in msgs:
        cli._progress(m)
    assert "\n" not in tty.out
    rows = [r.removeprefix("\033[K") for r in tty.out.split("\r") if r]
    assert len(rows) == len(msgs) and all(len(r) <= 39 for r in rows)
    assert all(m.startswith(r) and m[len(r)] == " " for m, r in zip(msgs, rows, strict=True))  # cut at a space
    pipe = _Tty(False)
    monkeypatch.setattr(cli.sys, "stderr", pipe)
    cli._progress("scanning claude-code 20/880 files")
    assert pipe.out == ""


def test_service_logs_one_summary_line_with_scan_counts(tmp_path, caplog):
    def analyze():
        res = _result()
        res["scan"] = {"files": 840, "cached": 830, "parsed": 10}
        return res

    cal = Calibrator(analyze, tmp_path, 3600.0)
    with caplog.at_level("INFO", logger="compactlab.service"):
        cal.recalibrate("schedule")
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs[0] == "recalibrating (schedule)"
    assert msgs[-1].startswith("recalibrated in ") and "840 files, 830 cached, 10 parsed, " in msgs[-1]
    assert "next calibration: 2" in msgs[-1]  # a local date, an hour from now
    assert "scan" not in json.loads((tmp_path / "service" / "result.json").read_text())
    assert Calibrator(analyze, tmp_path / "m", None).next_text() == "manual only"


# ---------- review fixes: consent reload, absolute path, step cap, scheduler survival ----------


def _consented_calibrator(tmp_path, make_group, **kw):
    settings = tmp_path / "settings.json"
    settings.write_text("{}\n")
    cpath = tmp_path / "consent.json"
    save_consent(cpath, Consent("claude-code", str(settings), ["claude-opus-5-5"], "balanced"))
    res = {"groups": [_group(make_group)], "sessions": []}
    cal = Calibrator(lambda: json.loads(json.dumps(res)), tmp_path, 3600.0, consent=load_consent(cpath),
                     consent_path=cpath, rules=Rules(cooldown_s=0), **kw)  # fmt: skip
    return cal, settings, cpath


def test_running_service_honours_a_cleared_or_expired_consent(tmp_path, make_group):
    cal, settings, cpath = _consented_calibrator(tmp_path, make_group)
    cpath.unlink()  # `compactlab consent clear` while the service runs
    out = cal.recalibrate()
    assert out["status"] == "ok" and json.loads(settings.read_text()) == {}
    assert "consent" in out["auto_apply"]["error"] and cal.status()["auto_apply"] is False
    # an expired consent is refused too
    save_consent(cpath, Consent("claude-code", str(settings), ["claude-opus-5-5"], "balanced", expires="2001-01-01"))
    assert cal.recalibrate()["auto_apply"]["error"] and json.loads(settings.read_text()) == {}
    # a fresh valid consent switches it back on without a restart
    save_consent(cpath, Consent("claude-code", str(settings), ["claude-opus-5-5"], "balanced"))
    out = cal.recalibrate()
    assert out["auto_apply"]["manifest"] and "autoCompactWindow" in json.loads(settings.read_text())["modelSettings"][
        "claude-opus-5-5"]  # fmt: skip


def test_consent_stores_an_absolute_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cpath = tmp_path / "state" / "consent.json"
    save_consent(cpath, Consent("claude-code", "settings.json", ["claude-opus-5-5"]))
    assert load_consent(cpath).file == str(tmp_path.resolve() / "settings.json")
    monkeypatch.chdir("/")
    assert load_consent(cpath).preauth().targets[0].file == tmp_path.resolve() / "settings.json"
    # a consent file written by an older version with a relative path is refused, not resolved against the cwd
    d = json.loads(cpath.read_text())
    d["file"] = "settings.json"
    cpath.write_text(json.dumps(d))
    with pytest.raises(ap.ApplyError):
        load_consent(cpath)
    # and the CLI stores it absolute
    monkeypatch.chdir(tmp_path)
    args = ["consent", "--state-dir", str(tmp_path / "s2"), "set", "--file", "rel.json", "--models", "m", "--yes"]
    assert main(args) == 0
    assert load_consent(tmp_path / "s2" / "consent.json").file == str(tmp_path.resolve() / "rel.json")


def test_auto_apply_never_exceeds_max_step_after_rounding(make_group):
    g = _group(make_group)
    r = Rules()
    for cur in (960_000, 962_000, 987_000, 931_000):  # off-grid values: nearest-grid rounding used to overshoot
        d = decide(g, "balanced", cur, None, None, now=0, rules=r)
        assert d.action == "apply" and abs((d.value or 0) - cur) <= r.max_step and d.value in GRID
    tiny = decide(g, "balanced", 960_000, None, None, now=0, rules=Rules(max_step=5_000))
    assert tiny.action == "skip" and "no grid point" in tiny.reason


def test_malformed_consented_config_backs_off_instead_of_killing_the_scheduler(tmp_path, make_group):
    clock = [1000.0]
    cal, settings, _ = _consented_calibrator(tmp_path, make_group, clock=lambda: clock[0])
    settings.write_text('{"broken": ')
    out = cal.recalibrate()
    assert out["status"] == "error" and "settings.json" in out["error"]
    assert cal.failures == 1 and cal.next_due() == clock[0] + 300
    assert cal.status()["last_error"] and cal.status()["last_apply"]["error"]
    stop = threading.Event()
    th = threading.Thread(target=cal.loop, args=(stop, 0.05))
    th.start()
    time.sleep(0.2)
    alive = th.is_alive()
    stop.set()
    th.join(5)
    assert alive  # the loop is still waiting out the backoff
    settings.write_text("{}\n")  # fixed: the next due run applies and clears the error
    clock[0] += 400
    assert cal.recalibrate()["status"] == "ok" and cal.failures == 0 and cal.last_error is None
