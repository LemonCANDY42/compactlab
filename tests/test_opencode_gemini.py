"""OpenCode and Gemini CLI adapters, defaults, recommenders and the shared unit step. Every database and log here is
synthetic or hand-written; the row shapes follow OpenCode 1.19.6 and Gemini CLI 0.46/0.70 source."""

import copy
import json
import sqlite3

import pytest

from compactlab import synth
from compactlab.adapters.cache import ScanCache
from compactlab.adapters.gemini_cli import GeminiAdapter, token_limit
from compactlab.adapters.opencode import OpenCodeAdapter
from compactlab.recommend import Snippet, check, generate
from compactlab.recommend.units import fraction_for, free_share, shared_fraction
from compactlab.sim.baseline import default_threshold, effective_max, read_current
from compactlab.verify import changes_from_manifest, manual_manifest

# ---------- OpenCode ----------


def _db(path, sessions):
    """sessions: {sid: (parent, [(message data, [part data, ...]), ...])}; malformed rows as raw strings."""
    con = sqlite3.connect(path)
    con.executescript(synth.OPENCODE_SCHEMA)
    m = p = 0
    for sid, (parent, msgs) in sessions.items():
        con.execute("insert into session values (?,?,?,?,?)", (sid, "proj", parent, 1, 2_000_000_000_000))
        for data, parts in msgs:
            m += 1
            raw = data if isinstance(data, str) else json.dumps(data)
            con.execute("insert into message values (?,?,?,?,?)", (f"msg_{m:04d}", sid, m, m, raw))
            for pd in parts:
                p += 1
                raw = pd if isinstance(pd, str) else json.dumps(pd)
                con.execute("insert into part values (?,?,?,?,?,?)",
                            (f"prt_{p:04d}", f"msg_{m:04d}", sid, 1_790_000_000_000 + p * 1000, 0, raw))  # fmt: skip
    con.commit()
    con.close()


def tok(inp, cr=0, cw=0, out=10, rsn=0):
    return {"input": inp, "output": out, "reasoning": rsn, "cache": {"read": cr, "write": cw}}


def asst(tokens=None, **kw):
    d = {
        "role": "assistant",
        "providerID": "anthropic",
        "modelID": "claude-sonnet-5-5",
        "finish": "stop",
        "time": {"created": 1_790_000_000_000, "completed": 1_790_000_005_000},
        "tokens": tokens or tok(0),
    }
    return d | kw


USER = {"role": "user", "time": {"created": 1_790_000_000_000}}


def test_opencode_steps_tools_compaction_and_unknowns(tmp_path):
    db = tmp_path / "opencode.db"
    _db(db, {"ses_a": (None, [
        (USER, [{"type": "text", "text": "fix it"}, {"type": "text", "text": "ctx", "synthetic": True}]),
        (asst(tok(1000, 9000, 500, 40, 20)), [
            {"type": "step-start"}, {"type": "reasoning", "text": "..."},
            {"type": "tool", "tool": "read", "state": {"input": {"filePath": "/a.py"}, "output": "x" * 400,
                                                         "time": {"start": 1_790_000_005_500}}},
            {"type": "step-finish", "tokens": tok(1000, 9000, 500, 40, 20)},
            {"type": "step-start"}, {"type": "step-finish", "tokens": tok(200, 10_500, 0, 30)},
            {"type": "step-start"}, {"type": "step-finish", "tokens": tok(0)},  # aborted step: not billed
            {"type": "brand-new"}, "{not json",
        ]),
        (USER, [{"type": "compaction", "auto": True}]),
        (asst(tok(11_000, out=900), summary=True, mode="compaction"), [
            {"type": "step-start"}, {"type": "text", "text": "## Objective\nkeep going"},
            {"type": "step-finish", "tokens": tok(11_000, out=900)},
        ]),
        (asst(tok(3000, 2000)), []),  # no step parts: the message's own totals
        ({"role": "system"}, []),
    ])})  # fmt: skip
    (s,) = OpenCodeAdapter(tmp_path).load(db)
    assert (s.id, s.role, s.harness) == ("ses_a", "main", "opencode")
    assert [t.ctx for t in s.turns] == [10_500, 10_700, 5000]
    t0 = s.turns[0]
    assert (t0.cache_read, t0.cache_write, t0.output, t0.model) == (9000, 500, 60, "anthropic/claude-sonnet-5-5")
    assert t0.tool_keys == ["read:/a.py"] and t0.tool_result_tokens == [100] and t0.user_text == "fix it"
    assert t0.ts == 1_790_000_005.5 and t0.start_ts == 1_790_000_003.0  # response ends where its tool starts
    (c,) = s.compactions
    assert (c.turn_index, c.pre_tokens, c.duration_s) == (2, 10_700, 5.0) and "Objective" in c.summary_text
    assert s.unknown_events == {"part:brand-new": 1, "message:system": 1} and s.parse_errors == 1


def test_opencode_failed_summary_subagent_and_since(tmp_path):
    db = tmp_path / "opencode.db"
    _db(db, {
        "ses_main": (None, [(asst(tok(500)), [{"type": "step-finish", "tokens": tok(500)}]),
                            (USER, [{"type": "compaction", "auto": False}]),
                            (asst(summary=True, error={"name": "ContextOverflowError"}, finish="error"), [])]),
        "ses_child": ("ses_main", [(asst(tok(800)), [{"type": "step-finish", "tokens": tok(800)}])]),
    })  # fmt: skip
    a = OpenCodeAdapter(db)  # a root may be the database itself
    main, child = sorted(a.load(db), key=lambda s: s.id, reverse=True)
    assert not main.compactions  # an errored summary is not a compaction
    assert (child.role, child.parent_id) == ("subagent", "ses_main")
    assert a.load(db, since=2_100_000_000) == []  # sessions not updated since the cut are skipped
    assert {s.id for s in a.sessions()} == {"ses_main", "ses_child"}


def test_opencode_synthetic_history_end_to_end(tmp_path):
    synth.write_opencode(tmp_path / "oc", n=2)
    ss = OpenCodeAdapter(tmp_path / "oc").load(tmp_path / "oc" / "opencode.db")
    assert {s.role for s in ss} == {"main", "subagent"}
    main = [s for s in ss if s.role == "main"]
    assert all(s.compactions for s in main) and not any(s.unknown_events or s.parse_errors for s in ss)
    out = tmp_path / "res.json"
    from compactlab.cli.main import main as cli

    assert cli(["simulate", "--harness", "opencode", "--opencode-root", str(tmp_path / "oc"), "--since-days", "0",
                "--no-cache", "--out", str(out), "--plain"]) == 0  # fmt: skip
    res = json.loads(out.read_text())
    g = next(g for g in res["groups"] if g["role"] == "main")
    assert g["model"] == "anthropic/claude-sonnet-5-5" and g["observed_compactions"] > 0
    assert g["default"]["threshold"] == g["max_window"] - 32_000 and g["effective_max"] == g["max_window"] - 20_000


def test_opencode_wal_changes_are_read_without_writing(tmp_path):
    db = tmp_path / "opencode.db"
    _db(db, {"ses_a": (None, [(asst(tok(100)), [{"type": "step-finish", "tokens": tok(100)}])])})
    writer = sqlite3.connect(db)
    writer.execute("pragma journal_mode=wal")
    writer.execute("insert into part values ('prt_9999', 'msg_0001', 'ses_a', 1790000100000, 0, ?)",
                   (json.dumps({"type": "step-finish", "tokens": tok(400)}),))  # fmt: skip
    writer.commit()  # still in the WAL while the writer is open
    before = db.stat().st_mtime_ns
    (s,) = OpenCodeAdapter(tmp_path).load(db)
    assert [t.ctx for t in s.turns] == [100, 400] and db.stat().st_mtime_ns == before
    writer.close()


def test_opencode_defaults_and_current(tmp_path):
    assert default_threshold("opencode", "x/y", 200_000)[0] == 168_000
    assert effective_max("opencode", 1_000_000) == 980_000
    cfg = tmp_path / "opencode.json"
    models = {"m1": {"limit": {"context": 1_000_000, "input": 520_000, "output": 64_000}},
              "m2": {"limit": {"context": 128_000, "output": 8_000}}}  # fmt: skip
    cfg.write_text(json.dumps({"provider": {"bailian": {"models": models}}}))
    assert read_current("opencode", "bailian/m1", cfg)[0] == 500_000  # input - reserved min(20000, 32000)
    assert read_current("opencode", "bailian/m2", cfg)[0] == 120_000  # context - output
    assert read_current("opencode", "bailian/m3", cfg)[0] is None
    cfg.write_text(json.dumps({"compaction": {"auto": False}}))
    assert "auto is false" in read_current("opencode", "bailian/m1", cfg)[1]


def _set(result, harness, threshold):
    r = copy.deepcopy(result)
    for g in r["groups"]:
        if g["harness"] == harness and g["role"] == "main":
            g["comparison"]["balanced"]["action"] = "set"
            g["profiles"]["balanced"]["threshold"] = threshold
    return r


@pytest.fixture(scope="module")
def oc_result(tmp_path_factory):
    d = tmp_path_factory.mktemp("oc")
    synth.write_opencode(d, n=2, model="bailian-cli/deepseek-v4-pro")
    from compactlab.cli.main import main as cli

    out = d / "res.json"
    assert cli(["simulate", "--harness", "opencode", "--opencode-root", str(d), "--since-days", "0", "--no-cache",
                "--out", str(out), "--plain"]) == 0  # fmt: skip
    return json.loads(out.read_text())


def test_opencode_recommend_snippet(oc_result):
    r = _set(oc_result, "opencode", 500_000)
    (sn,) = [x for x in generate(r, harness="opencode", profile="balanced") if isinstance(x, Snippet)]
    cfg = json.loads(sn.content)
    lim = cfg["provider"]["bailian-cli"]["models"]["deepseek-v4-pro"]["limit"]
    assert lim["input"] == 520_000 and lim["output"] == 32_000 and lim["context"] == r["groups"][0]["max_window"]
    assert any("compaction.buffer" in n for n in sn.notes) and sn.warnings and not check([sn])
    assert not [x for x in generate(r, harness="opencode", scope="agent") if isinstance(x, Snippet)]
    r["groups"][0]["current_note"] = "compaction.auto is false in opencode.json: OpenCode does not compact by itself"
    (om,) = generate(r, harness="opencode", profile="balanced")  # a limit would never take effect
    assert not isinstance(om, Snippet) and "compaction.auto to true" in om.reason


# ---------- Gemini CLI ----------


def _jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows) + "\n")
    return path


def gm(mid, inp, cached=0, out=10, ts="2026-09-01T00:00:10Z", **kw):
    return {
        "id": mid,
        "timestamp": ts,
        "type": "gemini",
        "model": "gemini-3-pro-preview",
        "tokens": {"input": inp, "output": out, "cached": cached, "thoughts": 5, "tool": 0, "total": 0},
    } | kw


def test_gemini_rewrites_tools_compressions_and_unknowns(tmp_path):
    root = tmp_path / "tmp"
    call = {"id": "c1", "name": "read_file", "args": {"file_path": "/a.py"}, "result": [{"text": "x" * 40}]}
    path = _jsonl(root / "proj" / "chats" / "session-2026-09-01T00-00-abcd1234.jsonl", [
        {"sessionId": "abcd1234-x", "projectHash": "0" * 64, "startTime": "2026-09-01T00:00:00Z"},
        {"id": "u1", "timestamp": "2026-09-01T00:00:00Z", "type": "user", "content": [{"text": "add a test"}]},
        {"id": "g1", "timestamp": "2026-09-01T00:00:10Z", "type": "gemini", "content": "", "tokens": None},
        gm("g1", 12_000, 9000),
        gm("g1", 12_000, 9000, toolCalls=[call]),  # the same call written again with its tool
        {"id": "u2", "timestamp": "2026-09-01T00:00:12Z", "type": "user",
         "content": [{"functionResponse": {"id": "c1", "name": "read_file"}}]},
        gm("g2", 15_000, 11_000, ts="2026-09-01T00:00:20Z"),
        {"$set": {"lastUpdated": "2026-09-01T00:00:20Z"}},
        {"id": "s1", "timestamp": "2026-09-01T00:01:00Z", "type": "user",
         "content": [{"text": "<state_snapshot>done so far</state_snapshot>"}]},
        {"$patch": {"removeIds": ["g1", "g2"]}},
        {"$set": {"messages": [{"id": "s1", "type": "user",
                                "content": [{"text": "<state_snapshot>done so far</state_snapshot>"}]}]}},
        gm("g3", 4000, ts="2026-09-01T00:01:30Z"),
        {"$rewindTo": "g3"}, {"id": "x", "type": "mystery"}, {"what": 1}, "{torn",
    ])  # fmt: skip
    s = GeminiAdapter(root).parse_file(path)
    assert (s.id, s.role, s.context_window) == ("abcd1234-x", "main", 1_048_576)
    assert [t.ctx for t in s.turns] == [12_000, 15_000, 4000]
    t0 = s.turns[0]
    assert (t0.cache_read, t0.cache_write, t0.output, t0.user_text) == (9000, 0, 15, "add a test")
    assert t0.tool_keys == ["read_file:/a.py"] and s.turns[1].start_ts == t0.ts + 2
    (c,) = s.compactions  # the appended summary and the checkpoint are one compression
    assert (c.turn_index, c.pre_tokens) == (2, 15_000) and "state_snapshot" in c.summary_text
    assert s.unknown_events == {"message:mystery": 1, "record": 1} and s.parse_errors == 1


def test_gemini_window_follows_the_grouped_model(tmp_path):
    gemma = {"model": "gemma-4-31b-it"}
    path = _jsonl(tmp_path / "tmp" / "p" / "chats" / "session-x.jsonl", [
        gm("g1", 9000), gm("g2", 10_000) | gemma, gm("g3", 11_000) | gemma])  # fmt: skip
    s = GeminiAdapter(tmp_path / "tmp").parse_file(path)
    assert (s.model, s.context_window) == ("gemma-4-31b-it", 256_000)  # not the largest window seen


def test_gemini_files_subagent_legacy_json_and_cache(tmp_path):
    files = synth.write_gemini(tmp_path, n=2)
    legacy = tmp_path / "old" / "chats" / "session-2025-01-01T00-00-11111111.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"sessionId": "11111111-y", "messages": [gm("g1", 5000), gm("g2", 6000)]}))
    a = GeminiAdapter(tmp_path)
    found = list(a.find_files())
    assert legacy in found and set(files) <= set(found)
    ss = {s.id: s for s in a.sessions()}
    assert [t.ctx for t in ss["11111111-y"].turns] == [5000, 6000]
    assert (ss["sub-0001"].role, ss["sub-0001"].parent_id) == ("subagent", "00000000-aaaa-bbbb-cccc-dddddddddddd")
    mains = [s for s in ss.values() if s.role == "main" and s.id != "11111111-y"]
    assert all(s.compactions for s in mains) and not any(s.unknown_events or s.parse_errors for s in ss.values())
    cache = ScanCache(tmp_path / "cache")
    first = cache.parse(a, files[0])
    again = cache.parse(a, files[0])
    assert cache.stats["hit"] == 1 and [t.ctx for t in again.turns] == [t.ctx for t in first.turns]
    with open(files[0], "a") as f:
        f.write(json.dumps(gm("g-new", 700_000)) + "\n")
    assert cache.parse(a, files[0]).turns[-1].ctx == 700_000 and cache.stats["parsed"] == 2


def test_gemini_defaults_current_and_limits(tmp_path):
    assert token_limit("gemini-3-pro-preview") == 1_048_576 and token_limit("gemma-4-31b-it") == 256_000
    assert default_threshold("gemini-cli", "gemini-3-pro-preview", 1_048_576)[0] == 524_288
    assert effective_max("gemini-cli", 1_048_576) == 1_048_576
    st = tmp_path / "settings.json"
    st.write_text(json.dumps({"model": {"compressionThreshold": 0.3}}))
    assert read_current("gemini-cli", "gemini-3-pro-preview", st)[0] == 314_572
    st.write_text("// comment\n{}")
    assert read_current("gemini-cli", "gemini-3-pro-preview", st)[0] is None


@pytest.fixture(scope="module")
def gm_result(tmp_path_factory):
    d = tmp_path_factory.mktemp("gm")
    synth.write_gemini(d, n=3)
    from compactlab.cli.main import main as cli

    out = d / "res.json"
    assert cli(["simulate", "--harness", "gemini-cli", "--gemini-root", str(d), "--since-days", "0", "--no-cache",
                "--out", str(out), "--plain"]) == 0  # fmt: skip
    return json.loads(out.read_text())


def test_gemini_simulate_and_recommend(gm_result):
    g = next(g for g in gm_result["groups"] if g["role"] == "main")
    assert (g["max_window"], g["default"]["threshold"]) == (1_048_576, 524_288)
    assert g["window_source"].startswith("observed in logs")
    assert g["observed_compactions"] > 0
    r = _set(gm_result, "gemini-cli", 400_000)
    (sn,) = [x for x in generate(r, harness="gemini-cli", profile="balanced") if isinstance(x, Snippet)]
    assert json.loads(sn.content) == {"model": {"compressionThreshold": 0.381}}
    assert any("399,507" in n for n in sn.notes) and any("Experimental" in w for w in sn.warnings) and not check([sn])


# ---------- shared ----------


def test_units_fraction_and_free_share():
    f = fraction_for(400_000, 1_048_576)
    assert (f.value, f.tokens, f.step_tokens, f.note) == (0.381, 399_507, 1049, "")
    assert fraction_for(524_288, 1_048_576).value == 0.5  # exact values are not rounded down a step
    coarse = fraction_for(400_000, 1_048_576, places=2)
    assert coarse.value == 0.38 and "10,486 tokens" in coarse.note
    b = free_share(640_000, 1_000_000)
    assert (b.value, b.tokens) == (36.0, 640_000)
    assert free_share(641_234, 1_000_000).value == 35.9  # rounded up: never triggers above the pick
    f, lines = shared_fraction([("a", 400_000, 1_048_576), ("b", 100_000, 256_000)])
    assert f.value == 0.381 and lines == ["b: 0.381 of 256,000 triggers at 97,536 (its own pick: 100,000)"]


def test_manual_verify_keys():
    oc = manual_manifest(1.0, "opencode", "bailian/m1", 500_000)
    gm_ = manual_manifest(1.0, "gemini-cli", "gemma-4-31b-it", 128_000)
    (c1,) = changes_from_manifest(oc)
    (c2,) = changes_from_manifest(gm_)
    assert (c1.key, c1.model) == ("provider.bailian.models.m1.limit", "bailian/m1")
    assert (c2.key, c2.model, c2.new) == ("model.compressionThreshold", "gemma-4-31b-it", 128_000)  # stays per model
    with pytest.raises(ValueError, match="--model"):
        manual_manifest(1.0, "gemini-cli", "", 400_000)


def test_opencode_provider_is_the_platform():
    from compactlab.pricing import PricingTable

    t = PricingTable.load(offline=True)
    # OpenCode's `anthropic/...` is Anthropic's own API: the vendor's price, not an OpenRouter lookup
    p = t.lookup("anthropic/claude-opus-5-5", "anthropic")
    assert (p.key, p.kind, p.platform) == ("claude-opus-5-5", "exact", None)
    assert t.lookup("openrouter/claude-opus-5-5", "openrouter").platform == "openrouter"
    assert t.lookup("anthropic/claude-opus-5-5").platform == "openrouter"  # no recorded platform: the ID's shape
