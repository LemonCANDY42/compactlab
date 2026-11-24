import json

from compactlab.adapters import cache as cache_mod
from compactlab.adapters import get_adapter
from compactlab.adapters.cache import CACHE_VERSION, ScanCache
from compactlab.model import session_to_dict


def _norm(s):
    d = session_to_dict(s)
    return d


def _mode(p):
    return oct(p.stat().st_mode & 0o777)


def test_cache_hit_resume_and_equivalence(tmp_path, claude_root):
    ad = get_adapter("claude-code")(claude_root)
    files = list(ad.find_files())
    cache = ScanCache(tmp_path / "c")
    first = [cache.parse(ad, f) for f in files]
    assert cache.stats["parsed"] == len(files)
    wal = cache.file.with_name(cache.file.name + "-wal")
    assert wal.exists() and _mode(wal) == "0o600"
    cache.close()
    assert cache.file.name == f"scan-v{CACHE_VERSION}.sqlite3"
    assert _mode(cache.file) == "0o600" and _mode(tmp_path / "c") == "0o700"
    c2 = ScanCache(tmp_path / "c")
    again = [c2.parse(ad, f) for f in files]
    assert c2.stats["hit"] == len(files)
    assert [_norm(a) for a in again] == [_norm(b) for b in first]
    assert c2.info()["files"] == len(files)


def test_cache_resumes_appended_file_and_ignores_partial_line(tmp_path, claude_root):
    src = next(get_adapter("claude-code")(claude_root).find_files())
    f = tmp_path / "proj" / "s.jsonl"
    f.parent.mkdir()
    lines = src.read_text().splitlines(True)
    half = len(lines) // 2
    f.write_text("".join(lines[:half]) + lines[half][: len(lines[half]) // 2])  # torn last line
    ad = get_adapter("claude-code")(tmp_path)
    cache = ScanCache(tmp_path / "c")
    assert cache.parse(ad, f).parse_errors == 0  # the torn line is left for later, not counted as bad
    cache.close()
    f.write_text("".join(lines))
    c2 = ScanCache(tmp_path / "c")  # the offset and parser state survive the process
    resumed = c2.parse(ad, f)
    assert c2.stats == {"hit": 0, "resumed": 1, "parsed": 0}
    assert resumed.parse_errors == 0  # the torn line was never fed half-written
    assert _norm(resumed) == _norm(ad.parse_file(f))
    assert _norm(c2.parse(ad, f)) == _norm(resumed) and c2.stats["hit"] == 1


def test_cache_never_resumes_from_a_row_replaced_mid_read(tmp_path, claude_root, monkeypatch):
    src = next(get_adapter("claude-code")(claude_root).find_files())
    f = tmp_path / "proj" / "s.jsonl"
    f.parent.mkdir()
    lines = src.read_text().splitlines(True)
    f.write_text("".join(lines[: len(lines) // 2]))  # longer than the 4 KiB head, so appending keeps the head
    ad = get_adapter("claude-code")(tmp_path)
    mine, other = ScanCache(tmp_path / "c"), ScanCache(tmp_path / "c")
    mine.parse(ad, f)
    f.write_text("".join(lines))
    read_state = ScanCache._state

    def racing(self, *a, **k):
        if self is mine:
            other.parse(ad, f)  # another run resumes the same file between our two reads
        return read_state(self, *a, **k)

    monkeypatch.setattr(ScanCache, "_state", racing)
    got = mine.parse(ad, f)
    assert other.stats["resumed"] == 1 and mine.stats["parsed"] == 2  # re-parsed, not resumed from a stale offset
    assert _norm(got) == _norm(ad.parse_file(f))


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


def test_cache_parser_version_bump_invalidates(tmp_path, claude_root, monkeypatch):
    ad = get_adapter("claude-code")(claude_root)
    f = next(ad.find_files())
    cache = ScanCache(tmp_path / "c")
    cache.parse(ad, f)
    cache.parse(ad, f)
    assert cache.stats == {"hit": 1, "resumed": 0, "parsed": 1}
    monkeypatch.setattr(ad.parser_cls, "version", ad.parser_cls.version + 1)
    cache.parse(ad, f)
    assert cache.stats["parsed"] == 2
    cache.parse(ad, f)
    assert cache.stats["hit"] == 2  # the re-parse replaced the row
    assert cache.info()["files"] == 1


def test_cache_prunes_rows_of_deleted_files(tmp_path, claude_root):
    ad = get_adapter("claude-code")(claude_root)
    keep, gone = (tmp_path / "logs" / n for n in ("keep.jsonl", "gone.jsonl"))
    keep.parent.mkdir()
    src = next(ad.find_files()).read_text()
    keep.write_text(src)
    gone.write_text(src)
    cache = ScanCache(tmp_path / "c")
    for p in (keep, gone):
        cache.parse(ad, p)
    assert cache.prune() == 0
    gone.unlink()
    assert cache.prune() == 1
    assert cache.info()["files"] == 1
    cache.parse(ad, keep)
    assert cache.stats["hit"] == 1


def test_pruned_rows_leave_no_trace_in_the_cache_files(tmp_path, claude_root):
    ad = get_adapter("claude-code")(claude_root)
    marker = "PRUNE-ME-" + "x" * 9000  # long enough to spill into overflow pages
    rec = json.loads(next(ad.find_files()).read_text().splitlines()[0])
    gone = tmp_path / "logs" / "gone.jsonl"
    gone.parent.mkdir()
    gone.write_text("".join(json.dumps({**rec, "message": {"role": "user", "content": marker + str(i)}}) + "\n"
                            for i in range(3)))  # fmt: skip
    cache = ScanCache(tmp_path / "c")
    cache.parse(ad, gone)
    cache.close()
    blob = b"".join(p.read_bytes() for p in (tmp_path / "c").iterdir())
    assert b"PRUNE-ME-" in blob  # the user text is cached
    gone.unlink()
    cache = ScanCache(tmp_path / "c")
    assert cache.prune() == 1
    cache.close()
    assert b"PRUNE-ME-" not in b"".join(p.read_bytes() for p in (tmp_path / "c").iterdir())


def test_cache_deletes_older_versions_and_clear_removes_all(tmp_path, claude_root):
    d = tmp_path / "c"
    d.mkdir()
    old = [d / "scan-v1.json", d / "scan-v3.json", d / ".scan.k2x_9abq", d / "scan-v3.sqlite3-wal"]
    for p in old:
        p.write_text("{}")
    unrelated = [d / "notes.txt", d / "scan-v1.json.bak", d / ".scan.toolongname"]
    for p in unrelated:
        p.write_text("mine")
    cache = ScanCache(d)
    assert cache.info()["old_bytes"] == 2 * len(old) and not cache.info()["exists"]
    assert all(p.exists() for p in old)  # inspecting deletes nothing
    ad = get_adapter("claude-code")(claude_root)
    cache.parse(ad, next(ad.find_files()))
    assert not any(p.exists() for p in old)
    assert all(p.exists() for p in unrelated)
    assert cache.info()["old_bytes"] == 0
    (d / "scan-v2.json").write_text("{}")
    removed = cache.clear()
    assert cache.file in removed and d / "scan-v2.json" in removed
    assert sorted(p.name for p in d.iterdir()) == sorted(p.name for p in unrelated)
    assert ScanCache(d).info() == {"path": str(cache.file), "exists": False, "files": 0, "bytes": 0, "old_bytes": 0}


def test_cache_recovers_from_a_corrupt_database(tmp_path, claude_root):
    ad = get_adapter("claude-code")(claude_root)
    f = next(ad.find_files())
    d = tmp_path / "c"
    d.mkdir()
    (d / f"scan-v{CACHE_VERSION}.sqlite3").write_bytes(b"not a database" * 512)
    cache = ScanCache(d)
    assert _norm(cache.parse(ad, f)) == _norm(ad.parse_file(f))
    cache.close()
    c2 = ScanCache(d)
    c2.parse(ad, f)
    assert c2.stats["hit"] == 1


def test_cache_rebuilds_after_corruption_found_mid_run(tmp_path, claude_root):
    ad = get_adapter("claude-code")(claude_root)
    files = list(ad.find_files())
    cache = ScanCache(tmp_path / "c")
    for f in files:
        cache.parse(ad, f)
    cache.close()
    with open(cache.file, "r+b") as fh:  # keep the header, wreck the table's root page
        fh.seek(4096)
        fh.write(b"\xff" * 4096)
    c2 = ScanCache(tmp_path / "c")
    assert [_norm(c2.parse(ad, f)) for f in files] == [_norm(ad.parse_file(f)) for f in files]
    c2.close()
    c3 = ScanCache(tmp_path / "c")
    for f in files:
        c3.parse(ad, f)
    assert c3.stats["hit"] >= len(files) - 1  # rebuilt as soon as the corruption was seen


def test_cache_reparses_an_unreadable_row(tmp_path, claude_root):
    ad = get_adapter("claude-code")(claude_root)
    f = next(ad.find_files())
    cache = ScanCache(tmp_path / "c")
    cache.parse(ad, f)
    cache._db.execute("UPDATE entries SET session = '{'")
    assert _norm(cache.parse(ad, f)) == _norm(ad.parse_file(f))
    assert cache.stats["parsed"] == 2
    cache.parse(ad, f)
    assert cache.stats["hit"] == 1


def test_cache_shared_by_two_runs_and_never_blocks_a_scan(tmp_path, claude_root, monkeypatch):
    ad = get_adapter("claude-code")(claude_root)
    f1, f2 = list(ad.find_files())[:2]
    a, b = ScanCache(tmp_path / "c"), ScanCache(tmp_path / "c")
    a.parse(ad, f1)
    b.parse(ad, f2)
    b.parse(ad, f1)
    assert b.stats == {"hit": 1, "resumed": 0, "parsed": 1}
    monkeypatch.setattr(cache_mod, "BUSY_TIMEOUT_S", 0.05)
    c = ScanCache(tmp_path / "c")
    c.parse(ad, f1)  # connect before the lock is taken
    a._db.execute("BEGIN IMMEDIATE")  # another run holds the write lock
    f3 = tmp_path / "new.jsonl"
    f3.write_text(f1.read_text())
    assert _norm(c.parse(ad, f3)) == _norm(ad.parse_file(f3))  # parsed, just not cached
    a._db.execute("ROLLBACK")
    assert c.info()["files"] == 2


def test_no_cache_writes_nothing(tmp_path, claude_root):
    from compactlab.cli.main import main

    d = tmp_path / "c"
    args = ["simulate", "--claude-root", str(claude_root), "--harness", "claude-code", "--since-days", "0",
            "--offline-pricing", "--no-current", "--lens-out", "", "--out", str(tmp_path / "r.json")]  # fmt: skip
    assert main([*args, "--no-cache", "--cache-dir", str(d)]) == 0
    assert not d.exists()
    assert main([*args, "--cache-dir", str(d)]) == 0
    assert ScanCache(d).info()["files"] == len(list(claude_root.rglob("*.jsonl")))


def test_cache_cli_info_and_clear(tmp_path, claude_root, capsys):
    from compactlab.cli.main import main

    d = tmp_path / "c"
    ad = get_adapter("claude-code")(claude_root)
    cache = ScanCache(d)
    cache.parse(ad, next(ad.find_files()))
    cache.close()
    (d / "scan-v1.json").write_text("{}")
    capsys.readouterr()
    assert main(["cache", "info", "--cache-dir", str(d), "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["files"] == 1 and info["exists"] and info["bytes"] > 0 and info["old_bytes"] == 2
    assert main(["cache", "info", "--cache-dir", str(d)]) == 0
    assert "1 files" in capsys.readouterr().out
    assert main(["cache", "clear", "--cache-dir", str(d)]) == 0
    assert "removed 2 cache files" in capsys.readouterr().out
    assert list(d.iterdir()) == []


def _w(path, recs):
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")


def test_unknown_events_recorded_not_fatal(tmp_path):
    _w(
        tmp_path / "a.jsonl",
        [
            {"type": "brand-new-kind", "x": 1},
            {
                "type": "assistant",
                "message": {
                    "id": "m1",
                    "role": "assistant",
                    "model": "claude-opus-5-5",
                    "usage": {"input_tokens": 5, "cache_read_input_tokens": 1000},
                },
            },
        ],
    )
    (s,) = get_adapter("claude-code")(tmp_path).sessions()
    assert s.unknown_events == {"brand-new-kind": 1} and len(s.turns) == 1


def test_claude_dedupes_by_message_and_request_id(tmp_path):
    a = {
        "type": "assistant",
        "requestId": "r1",
        "message": {"id": "m1", "role": "assistant", "model": "x", "usage": {"input_tokens": 10}},
    }
    b = dict(a, requestId="r2")
    _w(tmp_path / "a.jsonl", [a, a, b])
    (s,) = get_adapter("claude-code")(tmp_path).sessions()
    assert len(s.turns) == 2


def tok(inp, out, total_in, total_out, ts="2026-09-01T00:00:00Z"):
    last = {"input_tokens": inp, "cached_input_tokens": 0, "output_tokens": out}
    tot = {
        "input_tokens": total_in,
        "cached_input_tokens": 0,
        "output_tokens": total_out,
        "total_tokens": total_in + total_out,
    }
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {"total_token_usage": tot, "last_token_usage": last}},
    }


def test_codex_fork_prefix_and_inherited_opening(tmp_path):
    _w(
        tmp_path / "rollout-x.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {"id": "child", "forked_from_id": "parent", "source": {"subagent": "x"}},
            },
            tok(0, 0, 50_000, 900),  # inherited opening, zero usage
            {"type": "session_meta", "payload": {"id": "parent"}},  # embedded ancestor: replay follows
            tok(40_000, 300, 40_000, 300),
            tok(45_000, 300, 85_000, 600),
            {"type": "turn_context", "payload": {"model": "gpt-5-codex"}},
            tok(52_000, 200, 137_000, 800),
            tok(52_000, 200, 137_000, 800),  # identical re-emission
            {"type": "weird_new_line", "payload": {}},
            {"type": "event_msg", "payload": {"type": "brand_new_event"}},
        ],
    )
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
