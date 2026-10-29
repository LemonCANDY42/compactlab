"""DeepSeek Harness adapter. Row shapes follow dsh's own snapshot logs (snapshots/session/*/session.v4.jsonl in
deepseek-ai/deepseek-harness); every log here is hand-written or synthetic."""

import copy
import json
import shutil
import subprocess

import pytest

from compactlab import synth
from compactlab.adapters.cache import ScanCache
from compactlab.adapters.dsh import DshAdapter, zstd_decode
from compactlab.pricing import PricingTable
from compactlab.recommend import Snippet, generate
from compactlab.sim.baseline import default_threshold, effective_max


def ev(typ, data, seq, time=None):
    d = {"type": typ, "seq": seq, "data": data}
    if time is not None:
        d["time"] = time
    return d


def usage(inp, cr=0, cw=0, out=10):
    return {"inputTokens": inp, "cacheReadTokens": cr, "cacheWriteTokens": cw, "outputTokens": out}


def assistant(seq, u, time=None, model="deepseek-v4-flash"):
    msg = {"role": "assistant", "source": {"kind": "model", "provider": "deepseek-official", "model": model}}
    return ev("assistant/message", {"turn": 1, "step": seq, "message": msg, "usage": u}, seq, time)


T0 = 1_790_000_000_000  # epoch ms
HEADER = {"type": "session", "version": 4, "id": "s-main", "createdAt": 0, "cwd": "/p", "isSeeded": False,
          "delegationDepth": 0}  # fmt: skip

MAIN = [
    HEADER,
    ev(
        "request/context",
        {"provider": "deepseek-official", "model": "deepseek-v4-flash", "contextWindow": 1_048_576},
        0,
    ),
    ev("user/message", {"content": [{"type": "text", "text": "fix the bug"}]}, 1, T0),
    assistant(2, usage(1000, 9000, 0, 50), T0 + 2_000),
    ev("tool/call", {"callId": "c1", "name": "read", "arguments": json.dumps({"file_path": "/p/a.py"})}, 3),
    ev(
        "tool/result",
        {"message": {"role": "tool", "toolCallId": "c1", "content": [{"type": "text", "text": "x" * 800}]}},
        4,
        T0 + 3_000,
    ),
    assistant(5, usage(500, 11000, 0, 40), T0 + 5_000),
    ev("compaction/start", {"compactionId": "k1", "turn": 1}, 6, T0 + 6_000),
    ev(
        "compaction/summary",
        {"compactionId": "k1", "summary": "done so far", "shadowedTokenCount": 11000},
        7,
        T0 + 36_000,
    ),
    ev("compaction/end", {"compactionId": "k1", "turn": 1}, 8, T0 + 36_000),
    ev("compaction/start", {"compactionId": "k2", "turn": 1}, 9, T0 + 37_000),
    ev("compaction/summary", {"compactionId": "k2", "summary": "again"}, 10, T0 + 38_000),
    ev("compaction/end", {"compactionId": "k2", "turn": 1, "error": {"message": "overflow"}}, 11, T0 + 38_000),
    ev("plugin:acme/thing", {}, 12),
    assistant(13, usage(300, 2000), T0 + 40_000),
]


def write(path, rows, tail=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n" + tail)
    return path


def parse(path):
    return DshAdapter(path.parents[2]).parse_file(path)


def test_main_session_turns_compactions_and_unknowns(tmp_path):
    p = write(tmp_path / "sessions" / "--p--" / "s-main" / "session.v4.jsonl", MAIN, tail="{not json\n")
    s = parse(p)
    assert (s.harness, s.role, s.id, s.context_window) == ("dsh", "main", "s-main", 1_048_576)
    assert [t.ctx for t in s.turns] == [10_000, 11_500, 2_300]  # disjoint counts are summed
    assert (s.turns[0].cache_read, s.turns[0].output, s.turns[0].model) == (9000, 50, "deepseek-v4-flash")
    assert s.turns[0].ts == T0 / 1000 + 2 and s.turns[0].start_ts == T0 / 1000
    assert s.turns[0].tool_keys == ["read:/p/a.py"] and s.turns[0].tool_result_tokens == [200]
    assert s.turns[0].user_text == "fix the bug"
    assert len(s.compactions) == 1  # the one ending in an error is dropped
    c = s.compactions[0]
    assert (c.turn_index, c.pre_tokens, c.duration_s, c.summary_text) == (2, 11_500, 30, "done so far")
    assert s.unknown_events == {"plugin:acme/thing": 1} and s.parse_errors == 1


def test_subagent_and_seeded_fork(tmp_path):
    sub = {**HEADER, "id": "child", "parentSession": "s-main", "origin": "subagent", "delegationDepth": 1}
    s = parse(write(tmp_path / "sessions" / "--p--" / "child" / "session.v4.jsonl", [sub, assistant(1, usage(10))]))
    assert (s.role, s.parent_id, len(s.turns)) == ("subagent", "s-main", 1)

    fork = {**HEADER, "id": "fork", "isSeeded": True}
    rows = [fork, assistant(1, usage(5000)), assistant(2, usage(6000)), ev("session/end-seed", {}, 3),
            assistant(4, usage(100, 6000))]  # fmt: skip
    s = parse(write(tmp_path / "sessions" / "--p--" / "fork" / "session.v4.jsonl", rows))
    assert [t.ctx for t in s.turns] == [6100] and s.skipped_inherited == 2


def test_turn_time_falls_back_to_stream_chunks(tmp_path):
    a = assistant(1, usage(10))
    a["data"]["stream"] = [
        {"type": "chunk", "time": 1_791_427_805_327},
        {"type": "text-chunks", "time0": 1_791_427_806_000},
    ]
    s = parse(write(tmp_path / "sessions" / "--p--" / "s" / "session.v4.jsonl", [HEADER, a]))
    assert s.turns[0].ts == pytest.approx(1_791_427_806.0)


def test_newest_generation_only(tmp_path):
    root = tmp_path / "sessions"
    write(root / "--p--" / "a" / "session.jsonl", [HEADER, assistant(1, usage(1))])
    write(root / "--p--" / "a" / "session.v4.jsonl", [HEADER, assistant(1, usage(2))])
    write(root / "--p--" / "a" / "notes.jsonl", [HEADER])
    write(root / "--q--" / "b" / "session.v3.jsonl", [HEADER, assistant(1, usage(3))])
    files = list(DshAdapter(root).find_files())
    assert [f.name for f in files] == ["session.v4.jsonl", "session.v3.jsonl"]


def _zstd_frames(chunks):
    try:
        from compression import zstd

        return [zstd.compress(c) for c in chunks]
    except ImportError:
        exe = shutil.which("zstd")
        if not exe:
            pytest.skip("no zstd encoder (Python 3.14+ or the zstd command)")
        return [subprocess.run([str(exe), "-qc"], input=c, capture_output=True, check=True).stdout for c in chunks]


def test_zstd_frames_and_torn_tail(tmp_path):
    lines = [(json.dumps(r) + "\n").encode() for r in MAIN]
    frames = _zstd_frames([b"".join(lines[i : i + 4]) for i in range(0, len(lines), 4)])
    p = tmp_path / "sessions" / "--p--" / "s-main" / "session.v4.jsonl.zstd"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"".join(frames) + frames[-1][: len(frames[-1]) // 2])  # torn last frame
    assert (zstd_decode(p.read_bytes()) or b"").startswith(b"".join(lines))
    s = parse(p)
    assert [t.ctx for t in s.turns] == [10_000, 11_500, 2_300] and len(s.compactions) == 1


def test_cache_reuses_unchanged_and_reparses_changed(tmp_path):
    root = tmp_path / "sessions"
    p = write(root / "--p--" / "s-main" / "session.v4.jsonl", MAIN[:5])
    ad = DshAdapter(root)
    cache = ScanCache(tmp_path / "c")
    assert len(cache.parse(ad, p).turns) == 1
    assert len(cache.parse(ad, p).turns) == 1 and cache.stats["hit"] == 1
    write(p, MAIN)
    assert len(cache.parse(ad, p).turns) == 3
    assert cache.stats == {"hit": 1, "resumed": 0, "parsed": 2}  # never resumed from a byte offset


def test_defaults_and_effective_max():
    assert default_threshold("dsh", "deepseek-flash", 1_000_000)[0] == 678_464
    assert default_threshold("dsh", "deepseek-flash", 1_048_576)[0] == 727_040
    assert effective_max("dsh", 1_048_576) == 1_048_576 - 256_000
    small = default_threshold("dsh", "x", 128_000)[0]
    assert 0 < small < 128_000


def test_pricing_rows():
    t = PricingTable.load(offline=True)
    assert t.lookup("deepseek-v4-flash").key == "deepseek-flash"
    assert t.lookup("deepseek-v4-pro").cache_read == 0.022


@pytest.fixture(scope="module")
def dsh_result(tmp_path_factory):
    d = tmp_path_factory.mktemp("dsh")
    synth.write_dsh(d / "sessions", n=4)
    out = d / "res.json"
    from compactlab.cli.main import main

    assert main(["simulate", "--harness", "dsh", "--dsh-root", str(d / "sessions"), "--since-days", "0",
                 "--no-cache", "--out", str(out), "--plain"]) == 0  # fmt: skip
    return json.loads(out.read_text())


def test_simulate_end_to_end(dsh_result):
    (g,) = dsh_result["groups"]
    assert (g["harness"], g["model"], g["max_window"]) == ("dsh", "deepseek-v4-flash", 1_048_576)
    assert g["default"]["threshold"] == 727_040 and g["effective_max"] == 792_576
    assert g["observed_compactions"] > 0


def test_recommend_snippet(dsh_result):
    r = copy.deepcopy(dsh_result)
    g = r["groups"][0]
    g["comparison"]["balanced"]["action"] = "set"
    g["profiles"]["balanced"]["threshold"] = 550_000
    (sn,) = [x for x in generate(r, harness="dsh", profile="balanced") if isinstance(x, Snippet)]
    assert "thresholdRatio: 0.524" in sn.content and "headroomTokens: 0" in sn.content
    assert "maxTokens: 65536" in sn.content  # headroomTokens 0 would otherwise make the summary cap 0
    assert sn.knobs == ["dsh.plugin.compaction-basic.modelPolicies"] and sn.warnings
    g["profiles"]["balanced"]["threshold"] = 100_000  # ratio below retainRatio 0.16
    assert not [x for x in generate(r, harness="dsh", profile="balanced") if isinstance(x, Snippet)]
    assert not [x for x in generate(r, harness="dsh", scope="project") if isinstance(x, Snippet)]
