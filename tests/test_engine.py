"""`compactlab engine`: the JSON contract the app reads, the live session reader and the watch loop.

tests/contract/ holds one example of every message kind; the app's tests decode the same files. A field that is
removed or changes type fails here as a breaking change (bump ENGINE_API); a new field fails until the examples are
regenerated with COMPACTLAB_UPDATE_CONTRACT=1.
"""

import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from compactlab.adapters import get_adapter
from compactlab.cli import main
from compactlab.engine import ENGINE_API, live, watch
from compactlab.engine.live import Files, LiveSessions, read_tail
from compactlab.pricing import PricingTable

CONTRACT = Path(__file__).parent / "contract"
NOW = time.time()


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t * 1000) % 1000:03d}Z"


def _claude_lines(sid: str, n: int, ctx0: int, t0: float, one_hour: bool = True, compact_after: bool = False,
                  cwd: str = "/work/shop") -> list[dict]:  # fmt: skip
    out = []
    for i in range(n):
        t = t0 + i * 30
        out.append({"type": "user", "timestamp": _iso(t), "sessionId": sid, "cwd": cwd,
                    "message": {"role": "user", "content": f"step {i} " + "x" * 400}})  # fmt: skip
        cw = 2_000
        usage = {"input_tokens": 5, "cache_read_input_tokens": ctx0 + i * 1_000 - cw - 5,
                 "cache_creation_input_tokens": cw, "output_tokens": 300,
                 "cache_creation": {"ephemeral_1h_input_tokens": cw if one_hour else 0,
                                    "ephemeral_5m_input_tokens": 0 if one_hour else cw}}  # fmt: skip
        out.append({"type": "assistant", "timestamp": _iso(t + 10), "sessionId": sid, "cwd": cwd, "requestId": f"r{i}",
                    "message": {"id": f"m{i}", "role": "assistant", "model": "claude-opus-5-5", "usage": usage,
                                "content": [{"type": "text", "text": "ok " + "y" * 400}]}})  # fmt: skip
    if compact_after:
        out.append({"type": "system", "subtype": "compact_boundary", "timestamp": _iso(t0 + n * 30), "uuid": "b1",
                    "compactMetadata": {"trigger": "auto", "preTokens": ctx0 + n * 1_000}})  # fmt: skip
    return out


def _codex_lines(sid: str, n: int, ctx0: int, t0: float, cwd: str = "/work/api") -> list[dict]:
    out = [
        {"timestamp": _iso(t0), "type": "session_meta",
         "payload": {"id": sid, "source": "cli", "cwd": cwd, "model_provider": "openai",
                     "base_instructions": {"text": "z" * 5_000}}},
    ]  # fmt: skip
    tot = [0, 0, 0]
    for i in range(n):
        t = t0 + i * 30
        out.append({"timestamp": _iso(t), "type": "turn_context", "payload": {"model": "gpt-6.2-sol", "cwd": cwd}})
        out.append({"timestamp": _iso(t), "type": "response_item",
                    "payload": {"type": "message", "role": "user",
                                "content": [{"type": "input_text", "text": "go " + "x" * 400}]}})  # fmt: skip
        last = [ctx0 + i * 1_000, ctx0 + i * 1_000 - 500, 200]
        tot = [a + b for a, b in zip(tot, last, strict=True)]
        out.append({"timestamp": _iso(t + 9), "type": "response_item",
                    "payload": {"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": "done"}]}})  # fmt: skip
        out.append({"timestamp": _iso(t + 10), "type": "event_msg",
                    "payload": {"type": "token_count",
                                "info": {"total_token_usage": {"input_tokens": tot[0], "cached_input_tokens": tot[1],
                                                               "output_tokens": tot[2], "total_tokens": sum(tot)},
                                         "last_token_usage": {"input_tokens": last[0], "cached_input_tokens": last[1],
                                                              "output_tokens": last[2]},
                                         "model_context_window": 475_000}}})  # fmt: skip
    return out


def _write(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(d) + "\n" for d in lines))
    return path


@pytest.fixture
def logs(isolated_home):
    """A home with two Claude Code sessions (one with a subagent, one just compacted), a Codex session, an old file
    and settings that configure one threshold."""
    tmp_path = isolated_home
    claude = tmp_path / ".claude" / "projects"
    codex = tmp_path / ".codex" / "sessions"
    main_id, done_id = "11111111-aaaa", "22222222-bbbb"
    _write(claude / "-Users-me-github-shop" / f"{main_id}.jsonl", _claude_lines(main_id, 40, 300_000, NOW - 3_600))
    _write(claude / "-Users-me-github-shop" / main_id / "subagents" / "agent-a1.jsonl",
           _claude_lines(main_id, 5, 60_000, NOW - 1_800, one_hour=False))  # fmt: skip
    _write(claude / "-Users-me-github-blog" / f"{done_id}.jsonl",
           _claude_lines(done_id, 10, 500_000, NOW - 600, compact_after=True, cwd="/work/blog"))  # fmt: skip
    old = _write(claude / "-Users-me-github-old" / "33333333-cccc.jsonl", _claude_lines("33333333-cccc", 3, 9_000, 0))
    os.utime(old, (NOW - 30 * 86_400, NOW - 30 * 86_400))
    _write(codex / "2026" / "11" / "23" / "rollout-cx1.jsonl", _codex_lines("cx-1", 30, 100_000, NOW - 900))
    settings = tmp_path / ".claude" / "settings.json"
    settings.write_text(json.dumps({"modelSettings": {"claude-opus-5-5": {"autoCompactWindow": 650_000}}}))
    (tmp_path / ".codex" / "config.toml").write_text('model = "gpt-6.2-sol"\n')
    return {"claude": claude, "codex": codex, "settings": settings, "codex_config": tmp_path / ".codex" / "config.toml",
            "home": tmp_path}  # fmt: skip


def _live(logs, **kw):
    return LiveSessions(
        {"claude-code": [str(logs["claude"])], "codex": [str(logs["codex"])]},
        {"claude-code": logs["settings"], "codex": logs["codex_config"]},
        PricingTable.load(offline=True),
        time.time,
        **kw,
    )


def _by_id(snap):
    return {r["id"]: r for r in snap["sessions"]}


# ---------- live sessions ----------


def test_live_sessions_rows(logs):
    snap = _live(logs).snapshot()
    assert snap["engine_api"] == ENGINE_API and snap["kind"] == "sessions"
    rows = _by_id(snap)
    assert set(rows) == {"11111111-aaaa", "agent-a1", "22222222-bbbb", "cx-1"}  # the 30-day-old file is not active
    m = rows["11111111-aaaa"]
    assert m["ctx"] == 300_000 + 39 * 1_000 and m["model"] == "claude-opus-5-5" and m["project"] == "shop"
    assert m["threshold"] == 650_000 and m["threshold_source"] == "configured"
    assert "settings.json" in m["threshold_note"]
    assert m["cache_ttl_s"] == 3600 and m["cache_expires_at"] == pytest.approx(m["last_request_at"] + 3600)
    assert m["compacted_at"] is None and m["role"] == "main" and m["parent_id"] is None
    sub = rows["agent-a1"]
    assert sub["role"] == "subagent" and sub["parent_id"] == "11111111-aaaa" and sub["cache_ttl_s"] == 300
    done = rows["22222222-bbbb"]
    # compacted after its last request: the size is unknown until the next one
    assert done["ctx"] is None and done["compacted_at"] and done["project"] == "blog"
    cx = rows["cx-1"]
    assert cx["ctx"] == 100_000 + 29 * 1_000 and cx["model"] == "gpt-6.2-sol" and cx["platform"] == "openai"
    assert cx["window"] == 475_000 and cx["threshold"] == int(475_000 * 0.9) and cx["threshold_source"] == "default"
    assert cx["cache_ttl_s"] is None and cx["project"] == "api"
    assert snap["idle_compaction"] == {"enabled": None, "min_tokens": 200_000}
    times = [r["last_activity_at"] for r in snap["sessions"]]
    assert times == sorted(times, reverse=True)


def test_live_project_names_off_and_harness_filter(logs):
    snap = _live(logs, project_names=False, harnesses=("codex",)).snapshot()
    assert [(r["id"], r["project"]) for r in snap["sessions"]] == [("cx-1", None)]


@pytest.mark.parametrize("harness,name", [("claude-code", "11111111-aaaa.jsonl"), ("codex", "rollout-cx1.jsonl")])
def test_tail_read_matches_full_parse(logs, monkeypatch, harness, name):
    monkeypatch.setattr(live, "TAIL", 3_000)  # forces mid-file starts, the head record and a retry with more tail
    ad = get_adapter(harness)()
    path = next(Path(logs["home"]).rglob(name))
    s, p, newest = read_tail(ad, str(path), path.stat().st_size)
    full = ad.parse_file(path)
    assert s.turns and (s.turns[-1].ctx, s.turns[-1].model) == (full.turns[-1].ctx, full.turns[-1].model)
    assert (s.id, s.role, s.platform, s.context_window) == (full.id, full.role, full.platform, full.context_window)
    assert p.cwd and len(s.turns) < len(full.turns) and newest == pytest.approx(full.turns[-1].ts, abs=1)


def test_codex_fork_tail_ignores_copied_history(tmp_path, monkeypatch):
    """A fork copies its parent's history under the parent's session_meta; only the fork's own requests count."""
    parent = _codex_lines("parent", 5, 300_000, NOW - 900)
    fork = _codex_lines("fork", 1, 50_000, NOW - 60)
    lines = [fork[0], *parent, *fork[1:]]  # the fork's own meta, then the copied parent, then its own turn
    path = _write(tmp_path / "rollout-fork.jsonl", lines)
    monkeypatch.setattr(live, "TAIL", 1_500)
    s, _, _ = read_tail(get_adapter("codex")(), str(path), path.stat().st_size)
    assert s.id == "fork" and s.turns[-1].ctx == 50_000


def test_claude_rewritten_history_is_not_the_latest_request(tmp_path, monkeypatch):
    """Claude Code can append copies of earlier messages (same ids, original times) and then compact. The full parse
    drops the copies as seen; a reader of the end must not take the last copy for the latest request."""
    real = _claude_lines("s", 40, 300_000, NOW - 4 * 3600)
    copies = [d for d in real if d["type"] == "assistant"][:30]  # history re-written up to an older point
    boundary = {"type": "system", "subtype": "compact_boundary", "timestamp": _iso(NOW - 600), "uuid": "b9",
                "compactMetadata": {"trigger": "manual", "preTokens": 339_000}}  # fmt: skip
    path = _write(tmp_path / "s.jsonl", [*real, *copies, boundary])
    ad = get_adapter("claude-code")()
    full = ad.parse_file(path)
    for tail in (200_000, 3_000):  # copies inside the tail; copies larger than the tail (read further back)
        monkeypatch.setattr(live, "TAIL", tail)
        s, _, _ = read_tail(ad, str(path), path.stat().st_size)
        i, last = live.latest(s)
        assert (last.ts, last.ctx) == (full.turns[-1].ts, full.turns[-1].ctx) and last.ctx == 339_000
        after = live._after(s, i, last)
        assert after is not None and after.ts == pytest.approx(NOW - 600, abs=1)


def test_touched_file_with_old_records_is_not_active(logs):
    old = _write(
        logs["claude"] / "-p" / "44444444-dddd.jsonl", _claude_lines("44444444-dddd", 3, 9_000, NOW - 9 * 86_400)
    )
    os.utime(old)  # e.g. copied or synced: mtime now, last record nine days ago
    assert "44444444-dddd" not in _by_id(_live(logs).snapshot())


def test_config_created_after_start_is_read(logs):
    logs["settings"].unlink()
    ls = _live(logs)
    assert _by_id(ls.snapshot())["11111111-aaaa"]["threshold_source"] == "default"
    logs["settings"].write_text(json.dumps({"autoCompactWindow": 400_000,
                                            "env": {"CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS": "50000"}}))  # fmt: skip
    snap = ls.snapshot()
    assert _by_id(snap)["11111111-aaaa"]["threshold"] == 400_000
    assert snap["idle_compaction"]["min_tokens"] == 100_000  # Claude Code raises a smaller minimum to 100k


def test_unreadable_files_are_counted_every_scan(logs):
    p = next(Path(logs["codex"]).rglob("*.jsonl"))
    p.chmod(0)
    try:
        ls = _live(logs)
        assert [ls.snapshot()["unreadable"] for _ in range(2)] == [1, 1]
    finally:
        p.chmod(0o600)


def test_codex_subagent_parent_from_spawn_record_and_cwd_not_cached(tmp_path):
    lines = _codex_lines("child", 2, 50_000, NOW - 60)
    lines[0]["payload"]["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": "mom", "depth": 1}}}
    path = _write(tmp_path / "rollout-child.jsonl", lines)
    ad = get_adapter("codex")()
    p = ad.new_parser(path)
    for d in lines:
        p.feed(d)
    assert p.session.role == "subagent" and p.session.parent_id == "mom" and p.cwd == "/work/api"
    assert "/work/api" not in json.dumps(p.dump())  # the scan cache never holds paths


def test_active_window_longer_than_two_days(tmp_path):
    f = _write(tmp_path / "r" / "a.jsonl", [{}])
    os.utime(f, (NOW - 60 * 3600, NOW - 60 * 3600))
    assert [p for p, _ in Files([tmp_path / "r"], time.time).recent(72 * 3600)] == [str(f)]


def test_files_incremental(tmp_path):
    clock = [1000.0]
    root = tmp_path / "r"
    a = _write(root / "p" / "a.jsonl", [{}])
    os.utime(a, (clock[0], clock[0]))
    f = Files([root], lambda: clock[0])
    assert [p for p, _ in f.recent(3600)] == [str(a)]
    b = _write(root / "p" / "b.jsonl", [{}])  # added between full walks: the directory's mtime moved
    os.utime(b, (clock[0], clock[0]))
    os.utime(root / "p", ns=(int(clock[0] * 1e9) + 1, int(clock[0] * 1e9) + 1))
    c = _write(root / "q" / "new" / "c.jsonl", [{}])  # a new directory under a new directory
    os.utime(c, (clock[0], clock[0]))
    os.utime(root, ns=(int(clock[0] * 1e9) + 2, int(clock[0] * 1e9) + 2))
    clock[0] += 10
    assert sorted(p for p, _ in f.recent(3600)) == sorted(map(str, (a, b, c)))
    a.unlink()
    os.utime(root / "p", ns=(int(clock[0] * 1e9) + 3, int(clock[0] * 1e9) + 3))
    clock[0] += 10
    assert sorted(p for p, _ in f.recent(3600)) == sorted(map(str, (b, c)))
    clock[0] += 7200  # nothing written for two hours: not active, still known
    assert f.recent(3600) == [] and str(b) in f.files


def test_unchanged_files_are_not_read_again(logs, monkeypatch):
    ls = _live(logs)
    ls.snapshot()
    calls = []
    real = live.read_tail
    monkeypatch.setattr(live, "read_tail", lambda *a: calls.append(a[1]) or real(*a))
    ls.snapshot()
    assert calls == []
    p = next(Path(logs["codex"]).rglob("*.jsonl"))
    with p.open("a") as fh:
        fh.write(json.dumps(_codex_lines("cx-1", 31, 100_000, NOW)[-1]) + "\n")
    ls.snapshot()
    assert calls == [str(p)]


# ---------- watch loop ----------


def _scanner(snaps):
    it = iter(snaps)
    last = {}

    def scan():
        nonlocal last
        last = next(it, last)
        return last

    return scan


def _run(inp, snaps, **kw):
    out = io.StringIO()
    rc = watch.run(_scanner(snaps), {"engine_api": ENGINE_API, "kind": "hello"}, inp, out, **kw)
    return rc, [json.loads(x) for x in out.getvalue().splitlines()]


def _snap(n):
    return {"engine_api": ENGINE_API, "kind": "sessions", "at": n, "sessions": [{"id": "s", "ctx": n}]}


def test_watch_commands():
    rc, msgs = _run(io.StringIO("ping\nsnapshot\nrefresh\nbogus\nquit\nping\n"),
                    [_snap(1), _snap(1), _snap(1)], min_interval=0, interval=0)  # fmt: skip
    kinds = [m["kind"] for m in msgs]
    # the first scan always prints; snapshot prints though unchanged; refresh prints only on change
    assert rc == 0 and kinds == ["hello", "sessions", "pong", "sessions", "error"]
    assert msgs[-1]["error"]["code"] == "bad_command" and all(m["engine_api"] == ENGINE_API for m in msgs)


def test_watch_refresh_prints_changes_and_merges_requests():
    r, w = os.pipe()
    out = io.StringIO()
    snaps = [_snap(1), _snap(2), _snap(3)]
    t = threading.Thread(target=lambda: watch.run(_scanner(snaps), {"kind": "hello"}, os.fdopen(r), out, 0.2, 0))
    t.start()
    time.sleep(0.05)
    os.write(w, b"refresh\nrefresh\nrefresh\n")  # within min_interval of the first scan: merged into one
    time.sleep(0.5)
    os.write(w, b"refresh\n")
    time.sleep(0.4)
    os.close(w)
    t.join(5)
    assert [json.loads(x).get("at") for x in out.getvalue().splitlines()[1:]] == [1, 2, 3]


def test_watch_interval_scan_and_eof():
    r, w = os.pipe()
    inp = os.fdopen(r)
    out = io.StringIO()
    snaps = [_snap(i) for i in range(1, 50)]
    t = threading.Thread(target=lambda: watch.run(_scanner(snaps), {"kind": "hello"}, inp, out, 0, 0.05))
    t.start()
    time.sleep(0.4)
    os.close(w)  # the app went away
    t.join(5)
    assert not t.is_alive()
    assert sum('"sessions"' in x for x in out.getvalue().splitlines()) >= 3


def test_watch_scan_error_keeps_running():
    def boom():
        raise OSError("disk")

    out = io.StringIO()
    rc = watch.run(boom, {"kind": "hello"}, io.StringIO("ping\n"), out, 0, 0)
    msgs = [json.loads(x) for x in out.getvalue().splitlines()]
    assert rc == 0 and [m["kind"] for m in msgs] == ["hello", "error", "pong"]
    assert msgs[1]["error"]["code"] == "scan_failed"


# ---------- CLI and contract fixtures ----------


def _cli(args, capsys):
    rc = main(["engine", *args])
    return rc, json.loads(capsys.readouterr().out)


def _shape(v):
    if isinstance(v, dict):
        return {k: _shape(x) for k, x in v.items()}
    if isinstance(v, list):
        out = None
        for x in v:
            out = _merge(out, _shape(x))
        return [out]
    if v is None or isinstance(v, (bool, str)):
        return None if v is None else type(v).__name__
    return "number"


def _merge(a, b):
    if a is None or b is None:
        return b if a is None else a
    if isinstance(a, dict) and isinstance(b, dict):
        return {k: _merge(a.get(k), b.get(k)) for k in a.keys() | b.keys()}
    if isinstance(a, list) and isinstance(b, list):
        return [_merge(a[0], b[0])]
    return a


def _diff(want, got, path="$"):
    """(breaking, added): fields the example has that the output lost or retyped; fields only the output has."""
    if want is None or got is None:
        return [], []
    if isinstance(want, dict) and isinstance(got, dict):
        br, ad = [], [f"{path}.{k}" for k in got.keys() - want.keys()]
        br += [f"{path}.{k} removed" for k in want.keys() - got.keys()]
        for k in want.keys() & got.keys():
            b, a = _diff(want[k], got[k], f"{path}.{k}")
            br, ad = br + b, ad + a
        return br, ad
    if isinstance(want, list) and isinstance(got, list):
        return _diff(want[0], got[0], path + "[]")
    return ([f"{path}: {want} -> {got}"] if want != got else []), []


def _scrub(v, home):
    if isinstance(v, dict):
        return {k: _scrub(x, home) for k, x in v.items()}
    if isinstance(v, list):
        return [_scrub(x, home) for x in v]
    return v.replace(str(home), "~") if isinstance(v, str) else v


def _contract(kind, msg, home):
    path = CONTRACT / f"{kind}.json"
    if os.environ.get("COMPACTLAB_UPDATE_CONTRACT"):
        path.write_text(json.dumps(_scrub(msg, home), indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    want = json.loads(path.read_text())
    assert want["engine_api"] == ENGINE_API and want["kind"] == msg["kind"]
    breaking, added = _diff(_shape(want), _shape(msg))
    assert not breaking, f"breaking change to `{kind}`, bump ENGINE_API: {breaking}"
    assert not added, f"new fields in `{kind}`: regenerate with COMPACTLAB_UPDATE_CONTRACT=1 pytest {__file__}: {added}"


def test_cli_hello_sessions_error_contract(logs, capsys):
    state = logs["home"] / ".compactlab"
    rc, hello = _cli(["hello", "--state-dir", str(state)], capsys)
    assert rc == 0 and hello["kind"] == "hello" and hello["live_harnesses"] == ["claude-code", "codex"]
    cc = next(h for h in hello["harnesses"] if h["name"] == "claude-code")
    assert cc["roots"][0] == {"path": str(logs["claude"]), "exists": True} and cc["config"]["exists"]
    assert hello["result"]["exists"] is False and hello["service"]["failures"] == 0
    _contract("hello", hello, logs["home"])

    rc, snap = _cli(["sessions", "--state-dir", str(state)], capsys)
    assert rc == 0 and len(snap["sessions"]) == 4
    _contract("sessions", snap, logs["home"])

    rc, err = _cli(["result", "--state-dir", str(state)], capsys)
    assert rc == 1 and err["kind"] == "error" and err["error"]["code"] == "no_result"
    _contract("error", err, logs["home"])

    out = io.StringIO()
    watch.run(lambda: snap, hello, io.StringIO("ping\n"), out, 0, 0)
    _contract("pong", json.loads(out.getvalue().splitlines()[-1]), logs["home"])


def test_cli_calibrate_and_result_contract(tmp_path, claude_root, codex_root, capsys):
    state = tmp_path / "state"
    args = ["calibrate", "--state-dir", str(state), "--lens-out", "", "--claude-root", str(claude_root),
            "--codex-root", str(codex_root), "--harness", "claude-code", "--harness", "codex", "--since-days", "0",
            "--no-cache", "--offline-pricing", "--no-current", "--reason", "app"]  # fmt: skip
    rc, res = _cli(args, capsys)
    assert rc == 0 and res["kind"] == "result" and res["calibration"]["reason"] == "app"
    assert res["service"]["last_calibrated"] == res["calibration"]["at"] and res["auto_apply"] is None
    g = res["groups"][0]
    assert set(g["profiles"]) == {"cost", "balanced", "time"} and "action" in g["profiles"]["balanced"]
    assert "codex" in res["global_keys"]
    _contract("result", res, tmp_path)

    rc, again = _cli(["result", "--state-dir", str(state), "--profile", "time"], capsys)
    assert rc == 0 and again["profile"] == "time" and all(g["selected"] == "time" for g in again["groups"])
    assert again["calibration"] == res["calibration"]


def test_watch_subprocess(logs):
    """The installed entry point, as the app runs it: JSON Lines out, commands in, exits at end of input."""
    env = {**os.environ, "HOME": str(logs["home"]), "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    code = "import sys; from compactlab.cli import main; sys.exit(main())"
    p = subprocess.Popen([sys.executable, "-c", code, "engine", "watch", "--min-interval", "0"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)  # fmt: skip
    assert p.stdin and p.stdout
    first = [json.loads(p.stdout.readline()) for _ in range(2)]
    assert [m["kind"] for m in first] == ["hello", "sessions"] and len(first[1]["sessions"]) == 4
    p.stdin.write("ping\n")
    p.stdin.flush()
    assert json.loads(p.stdout.readline())["kind"] == "pong"
    p.stdin.close()
    assert p.wait(10) == 0


def test_watch_exits_quietly_when_the_reader_goes_away(logs):
    env = {**os.environ, "HOME": str(logs["home"]), "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    code = "import sys; from compactlab.cli import main; sys.exit(main())"
    p = subprocess.Popen([sys.executable, "-c", code, "engine", "watch", "--min-interval", "0"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         env=env)  # fmt: skip
    assert p.stdin and p.stdout
    p.stdout.readline()
    p.stdout.close()
    p.stdin.write("snapshot\nsnapshot\n")
    p.stdin.flush()
    assert p.wait(10) == 0


def test_watch_startup_error_is_json(logs, capsys):
    bad = logs["home"] / "bad-pricing.json"
    bad.write_text("{not json")
    assert main(["engine", "watch", "--pricing", str(bad)]) == 1
    err = json.loads(capsys.readouterr().out)
    assert err["kind"] == "error" and err["error"]["code"] in ("internal", "bad_args")


def test_demo_reads_no_real_logs_of_other_harnesses(tmp_path, monkeypatch):
    def real_logs(self):
        raise AssertionError(f"demo looked for {self.name} logs")

    for name in ("dsh", "opencode", "gemini-cli"):
        monkeypatch.setattr(get_adapter(name), "find_files", real_logs)
    assert main(["demo", "--out", str(tmp_path / "d"), "--no-open", "--lang", "en"]) == 0
    res = json.loads((tmp_path / "d" / "result.json").read_text())
    assert {g["harness"] for g in res["groups"]} == {"claude-code", "codex"}
