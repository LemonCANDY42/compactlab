import json

from compactlab.adapters import get_adapter
from compactlab.adapters.cache import ScanCache
from compactlab.model import session_to_dict


def _norm(s):
    d = session_to_dict(s)
    return d


def test_cache_hit_resume_and_equivalence(tmp_path, claude_root):
    ad = get_adapter("claude-code")(claude_root)
    files = list(ad.find_files())
    cache = ScanCache(tmp_path / "c")
    first = [cache.parse(ad, f) for f in files]
    assert cache.stats["parsed"] == len(files)
    cache.save()
    assert oct((tmp_path / "c" / "scan-v1.json").stat().st_mode & 0o777) == "0o600"
    c2 = ScanCache(tmp_path / "c")
    again = [c2.parse(ad, f) for f in files]
    assert c2.stats["hit"] == len(files)
    assert [_norm(a) for a in again] == [_norm(b) for b in first]


def test_cache_resumes_appended_file_and_ignores_partial_line(tmp_path, claude_root):
    src = next(get_adapter("claude-code")(claude_root).find_files())
    f = tmp_path / "proj" / "s.jsonl"
    f.parent.mkdir()
    lines = src.read_text().splitlines(True)
    half = len(lines) // 2
    f.write_text("".join(lines[:half]) + lines[half][: len(lines[half]) // 2])  # torn last line
    ad = get_adapter("claude-code")(tmp_path)
    cache = ScanCache(tmp_path / "c")
    cache.parse(ad, f)
    f.write_text("".join(lines))
    resumed = cache.parse(ad, f)
    assert cache.stats["resumed"] == 1
    assert _norm(resumed) == _norm(ad.parse_file(f))


def test_cache_reparses_rewritten_file(tmp_path, claude_root):
    src = next(get_adapter("claude-code")(claude_root).find_files())
    f = tmp_path / "s.jsonl"
    f.write_text(src.read_text())
    ad = get_adapter("claude-code")(tmp_path)
    cache = ScanCache(tmp_path / "c")
    cache.parse(ad, f)
    f.write_text("\n" + src.read_text())  # head changed
    cache.parse(ad, f)
    assert cache.stats["parsed"] == 2


def _w(path, recs):
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")


def test_unknown_events_recorded_not_fatal(tmp_path):
    _w(tmp_path / "a.jsonl", [
        {"type": "brand-new-kind", "x": 1},
        {"type": "assistant", "message": {"id": "m1", "role": "assistant", "model": "claude-opus-5-5",
                                          "usage": {"input_tokens": 5, "cache_read_input_tokens": 1000}}},
    ])
    (s,) = get_adapter("claude-code")(tmp_path).sessions()
    assert s.unknown_events == {"brand-new-kind": 1} and len(s.turns) == 1


def test_claude_dedupes_by_message_and_request_id(tmp_path):
    a = {"type": "assistant", "requestId": "r1", "message": {"id": "m1", "role": "assistant", "model": "x",
                                                            "usage": {"input_tokens": 10}}}
    b = dict(a, requestId="r2")
    _w(tmp_path / "a.jsonl", [a, a, b])
    (s,) = get_adapter("claude-code")(tmp_path).sessions()
    assert len(s.turns) == 2


def tok(inp, out, total_in, total_out, ts="2026-09-01T00:00:00Z"):
    last = {"input_tokens": inp, "cached_input_tokens": 0, "output_tokens": out}
    tot = {"input_tokens": total_in, "cached_input_tokens": 0, "output_tokens": total_out, "total_tokens": total_in + total_out}
    return {"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": tot, "last_token_usage": last}}}


def test_codex_fork_prefix_and_inherited_opening(tmp_path):
    _w(tmp_path / "rollout-x.jsonl", [
        {"type": "session_meta", "payload": {"id": "child", "forked_from_id": "parent", "source": {"subagent": "x"}}},
        tok(0, 0, 50_000, 900),                                   # inherited opening, zero usage
        {"type": "session_meta", "payload": {"id": "parent"}},    # embedded ancestor: replay follows
        tok(40_000, 300, 40_000, 300),
        tok(45_000, 300, 85_000, 600),
        {"type": "turn_context", "payload": {"model": "gpt-5-codex"}},
        tok(52_000, 200, 137_000, 800),
        tok(52_000, 200, 137_000, 800),                           # identical re-emission
        {"type": "weird_new_line", "payload": {}},
        {"type": "event_msg", "payload": {"type": "brand_new_event"}},
    ])
    (s,) = get_adapter("codex")(tmp_path).sessions()
    assert s.id == "child" and s.parent_id == "parent" and s.role == "subagent"
    assert [t.ctx for t in s.turns] == [52_000]
    assert s.skipped_inherited == 3
    assert s.unknown_events == {"weird_new_line": 1, "event_msg:brand_new_event": 1}


def test_codex_falls_back_to_total_delta(tmp_path):
    e1, e2 = tok(1000, 10, 1000, 10), tok(0, 0, 3000, 30)
    del e2["payload"]["info"]["last_token_usage"]
    _w(tmp_path / "r.jsonl", [{"type": "turn_context", "payload": {"model": "gpt-5-codex"}}, e1, e2])
    (s,) = get_adapter("codex")(tmp_path).sessions()
    assert [t.ctx for t in s.turns] == [1000, 2000]


def test_env_roots(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", f"{tmp_path}/a,{tmp_path}/b")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
    assert get_adapter("claude-code").default_roots() == [tmp_path / "a" / "projects", tmp_path / "b" / "projects"]
    assert get_adapter("codex").default_roots()[0] == tmp_path / "cx" / "sessions"
