"""A lite run reuses a saved analysis result only when the files it would read and every other input are unchanged,
and its output is then byte-identical to a full run's."""

import importlib
import json
import os
import sqlite3
import time

import pytest

from compactlab import synth
from compactlab.adapters.cache import ScanCache
from compactlab.adapters.claude_code import ClaudeParser
from compactlab.adapters.opencode import OpenCodeAdapter
from compactlab.service.policy import Consent, save_consent

cli = importlib.import_module("compactlab.cli.main")


@pytest.fixture
def logs(tmp_path):
    root = tmp_path / "claude" / "projects"
    synth.write_claude_code(root, n_main=4, n_sub=2)
    return root


@pytest.fixture
def scans(monkeypatch):
    """How many times the logs were scanned (a reused result scans none)."""
    n = [0]
    real = cli.load_sessions

    def counted(*a, **k):
        n[0] += 1
        return real(*a, **k)

    monkeypatch.setattr(cli, "load_sessions", counted)
    return n


def _settings(tmp_path, window=None):
    p = tmp_path / "settings.json"
    d = {"modelSettings": {"claude-opus-5-5": {"autoCompactWindow": window}}} if window else {}
    p.write_text(json.dumps(d))
    return p


def _simulate(tmp_path, logs, capsys, *extra, cache=True):
    out = tmp_path / "r.json"
    args = ["simulate", "--claude-root", str(logs), "--harness", "claude-code", "--since-days", "30",
            "--offline-pricing", "--claude-settings", str(tmp_path / "settings.json"), "--lens-out", "",
            "--lang", "en", "--plain", "--out", str(out), "--cache-dir", str(tmp_path / "c"), *extra]  # fmt: skip
    capsys.readouterr()
    assert cli.main([*args] if cache else [*args, "--no-cache"]) == 0
    return out.read_bytes(), capsys.readouterr().out


def test_reused_result_is_byte_identical_to_a_full_run(tmp_path, logs, capsys, scans):
    _settings(tmp_path, 600_000)
    full = _simulate(tmp_path, logs, capsys, cache=False)
    first = _simulate(tmp_path, logs, capsys)
    assert scans[0] == 2
    again = _simulate(tmp_path, logs, capsys)
    assert scans[0] == 2  # nothing scanned
    assert full == first == again
    assert ScanCache(tmp_path / "c").info()["results"] == 1


def test_any_change_to_the_files_read_means_a_new_analysis(tmp_path, logs, capsys, scans, monkeypatch):
    _settings(tmp_path)
    _simulate(tmp_path, logs, capsys)
    files = sorted(logs.rglob("*.jsonl"))

    def check(n):
        got = _simulate(tmp_path, logs, capsys)
        assert scans[0] == n
        assert got == _simulate(tmp_path, logs, capsys, cache=False)
        assert _simulate(tmp_path, logs, capsys) == got and scans[0] == n + 1  # and the new result is reused

    lines = files[0].read_text().splitlines(True)
    with open(files[0], "a") as f:  # appended
        f.write(lines[-1])
    check(2)
    (logs / "-proj-demo" / "copy.jsonl").write_text(files[1].read_text())  # a new log
    check(4)
    files[2].unlink()  # a deleted log
    check(6)
    old = time.time() - 60 * 86400
    os.utime(files[3], (old, old))  # a log that aged out of the 30-day window
    check(8)
    monkeypatch.setattr(ClaudeParser, "version", ClaudeParser.version + 1)  # a parser change
    check(10)


def test_a_log_aging_out_of_the_window_means_a_new_analysis(tmp_path, logs, capsys, scans):
    """The window moves with the clock: a log can leave it without changing."""
    _settings(tmp_path)
    old = time.time() - 25 * 86400
    os.utime(sorted(logs.rglob("*.jsonl"))[0], (old, old))
    _simulate(tmp_path, logs, capsys)  # --since-days 30: the log is in
    got = _simulate(tmp_path, logs, capsys, "--since-days", "20")  # as if five days passed: it is out
    assert scans[0] == 2 and got == _simulate(tmp_path, logs, capsys, "--since-days", "20", cache=False)


def test_any_change_to_the_other_inputs_means_a_new_analysis(tmp_path, logs, capsys, scans, monkeypatch):
    settings = _settings(tmp_path)
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"version": 2, "models": {}}))
    _simulate(tmp_path, logs, capsys, "--pricing", str(prices))
    n = 1

    def check(*extra):
        nonlocal n
        got = _simulate(tmp_path, logs, capsys, "--pricing", str(prices), *extra)
        n += 1
        assert scans[0] == n
        assert got == _simulate(tmp_path, logs, capsys, "--pricing", str(prices), *extra, cache=False)
        n += 1
        _simulate(tmp_path, logs, capsys, "--pricing", str(prices), *extra)
        assert scans[0] == n  # reused

    row = {"input": 9.0, "output": 30.0, "cache_read": 0.9, "cache_write": 11.0, "max_input": 1_000_000}
    prices.write_text(json.dumps({"version": 2, "models": {"claude-opus-5-5": row}}))  # a price
    check()
    check("--grid", "100k:800k:50k")  # an option
    settings.write_text(json.dumps({"modelSettings": {"claude-opus-5-5": {"autoCompactWindow": 500_000}}}))
    check("--grid", "100k:800k:50k")  # the configured threshold
    settings.write_text(json.dumps({"idleCompaction": False, **json.loads(settings.read_text())}))
    check("--grid", "100k:800k:50k")  # a harness setting the replay reads
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")  # the local time rules
    time.tzset()
    try:
        check("--grid", "100k:800k:50k")
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    monkeypatch.setattr(cli, "_CODE", "another compactlab")  # compactlab itself
    check("--grid", "100k:800k:50k")


def test_a_scan_cache_refreshed_by_another_command_is_not_enough(tmp_path, logs, capsys, scans):
    """Zero newly parsed files does not mean an unchanged analysis: another command can bring the scan cache up to
    date after the result was saved."""
    _settings(tmp_path)
    _simulate(tmp_path, logs, capsys)
    f = sorted(logs.rglob("*.jsonl"))[0]
    with open(f, "a") as fh:
        fh.write(f.read_text().splitlines(True)[-1])
    from compactlab.adapters import get_adapter

    ad, sc = get_adapter("claude-code")(logs), ScanCache(tmp_path / "c")
    for p in ad.find_files():
        sc.parse(ad, p)  # e.g. `compactlab sessions` ran meanwhile
    sc.close()
    got = _simulate(tmp_path, logs, capsys)
    assert scans[0] == 2 and got == _simulate(tmp_path, logs, capsys, cache=False)


def test_no_cache_neither_reuses_nor_saves(tmp_path, logs, capsys, scans):
    _settings(tmp_path)
    _simulate(tmp_path, logs, capsys, cache=False)
    _simulate(tmp_path, logs, capsys, cache=False)
    assert scans[0] == 2 and not (tmp_path / "c").exists()


def test_saved_result_and_its_keys_hold_no_prompt_text(tmp_path, capsys):
    _settings(tmp_path)
    logs = tmp_path / "claude" / "projects"
    synth.write_claude_code(logs, n_main=2, n_sub=0)
    f = sorted(logs.rglob("*.jsonl"))[0]
    recs = [json.loads(x) for x in f.read_text().splitlines()]
    for r in recs:
        if r.get("type") == "user" and isinstance(r.get("message", {}).get("content"), str):
            r["message"]["content"] = "SECRET-PROMPT " + r["message"]["content"]
    f.write_text("".join(json.dumps(r) + "\n" for r in recs))
    _simulate(tmp_path, logs, capsys)
    sc = ScanCache(tmp_path / "c")
    rows = sc._exec("SELECT files, inputs, facts, result FROM results")
    assert len(rows) == 1 and not any("SECRET-PROMPT" in col for col in rows[0])
    assert len(rows[0][0]) == len(rows[0][1]) == 64  # keys are hashes: no paths either
    sc.close()
    assert b"SECRET-PROMPT" not in b"".join(p.read_bytes() for p in (tmp_path / "c").iterdir())  # nor the rows


def test_reused_calibration_is_fresh_and_auto_apply_still_runs(tmp_path, logs, capsys, scans):
    settings = _settings(tmp_path)
    state = tmp_path / "state"
    save_consent(state / "consent.json", Consent("claude-code", str(settings), ["claude-opus-5-5"], "balanced"))
    args = ["recalibrate", "--claude-root", str(logs), "--harness", "claude-code", "--since-days", "30",
            "--offline-pricing", "--claude-settings", str(settings), "--state-dir", str(state),
            "--cache-dir", str(tmp_path / "c"), "--lens-out", str(state / "lens.json"), "--auto-apply", "--dry-run",
            "--min-days", "0", "--min-turns", "0", "--json"]  # fmt: skip
    outs, results, lenses = [], [], []
    for _ in range(2):
        capsys.readouterr()
        assert cli.main(args) == 0
        outs.append(json.loads(capsys.readouterr().out))
        results.append(json.loads((state / "service" / "result.json").read_text()))
        lenses.append(json.loads((state / "lens.json").read_text()))
    assert scans[0] == 1  # the second calibration reused the first one's analysis
    first, second = (r.pop("calibration") for r in results)
    assert second["at"] > first["at"] and second["reason"] == "cli"
    for f, c in zip([*results, *lenses], [first, second, first, second], strict=True):
        assert [h.pop("calibration") for h in f["harnesses"].values()] == [c]  # the run its groups come from
    assert results[0] == results[1]
    assert [ln.pop("calibration") for ln in lenses] == [first, second] and lenses[0] == lenses[1]
    assert outs[0]["auto_apply"]["decisions"] and outs[0]["auto_apply"] == outs[1]["auto_apply"]
    log = (state / "service" / "service.log").read_text()
    assert "analysis reused" in log.splitlines()[-1]


def test_opencode_fingerprint_follows_writes_and_the_window(tmp_path):
    synth.write_opencode(tmp_path / "oc", n=2)
    db = tmp_path / "oc" / "opencode.db"
    ad = OpenCodeAdapter(tmp_path / "oc")
    fp = ad.fingerprint(db)
    assert fp == ad.fingerprint(db)
    con = sqlite3.connect(db)
    con.execute("pragma journal_mode=wal")
    sid, upd = con.execute("select id, time_updated from session order by time_updated limit 1").fetchone()
    con.execute("update session set project_id = 'moved' where id = ?", (sid,))
    con.commit()  # in the WAL: the database file itself is unchanged
    assert ad.fingerprint(db) != fp
    fp = ad.fingerprint(db)
    assert ad.fingerprint(db, since=(upd + 2) / 1000) != fp  # one session aged out, the files did not change
    con.close()


def test_a_damaged_saved_result_is_analysed_again(tmp_path, logs, capsys, scans):
    _settings(tmp_path)
    full = _simulate(tmp_path, logs, capsys)
    sc = ScanCache(tmp_path / "c")
    for col, bad in (("facts", '{"seen": 5}'), ("facts", "[]"), ("result", '{"groups": 1}'), ("result", "[1]")):
        sc._exec(f"UPDATE results SET {col} = ?", (bad,))
        assert _simulate(tmp_path, logs, capsys) == full  # analysed again, and the row replaced
    sc.close()
    assert scans[0] == 5
    _simulate(tmp_path, logs, capsys)
    assert scans[0] == 5
