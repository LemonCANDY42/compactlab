from compactlab.adapters import adapter_names, get_adapter


def test_registry():
    assert {"claude-code", "codex"} <= set(adapter_names())


def test_claude_code_parse(claude_root):
    ss = list(get_adapter("claude-code")(claude_root).sessions())
    assert len(ss) == 10
    assert {s.role for s in ss} == {"main", "subagent"}
    main = [s for s in ss if s.role == "main"]
    assert any(s.compactions for s in main)
    s = main[0]
    ids = [t for t in s.turns]
    assert all(t.ctx > 0 for t in ids)
    # streaming duplicates (same message id) are removed: 1 turn per generated step
    assert len(s.turns) >= 150
    c = next(c for m in main for c in m.compactions)
    assert "Summary" in c.summary_text and c.pre_tokens


def test_codex_parse_dedupes(codex_root):
    ss = list(get_adapter("codex")(codex_root).sessions())
    assert len(ss) == 4
    s = ss[0]
    assert s.model == "gpt-5-codex" and s.context_window == 400_000
    assert len(s.turns) == len({round(t.ctx) for t in s.turns}) or len(s.turns) > 70
    assert s.compactions
    assert s.turns[0].tool_keys and s.turns[0].tool_keys[0].startswith("shell:")


def test_tolerates_garbage(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('not json\n{"type":"weird"}\n[1,2]\n{"message":5}\n')
    assert list(get_adapter("claude-code")(tmp_path).sessions()) == []
    assert list(get_adapter("codex")(tmp_path).sessions()) == []


def _write(tmp_path, name, lines):
    import json

    (tmp_path / name).write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def _claude_turn(mid, usage):
    return {
        "type": "assistant",
        "timestamp": "2026-01-01T00:00:00Z",
        "requestId": "r" + mid,
        "message": {"role": "assistant", "id": mid, "model": "claude-opus-4", "usage": usage, "content": []},
    }


def test_claude_malformed_usage_skips_record_not_scan(tmp_path):
    _write(
        tmp_path,
        "s.jsonl",
        [
            _claude_turn("a", {"input_tokens": 100, "output_tokens": 5}),
            _claude_turn("b", {"input_tokens": "oops"}),
            _claude_turn("c", {"input_tokens": 10, "cache_creation": {"ephemeral_1h_input_tokens": -3}}),
            _claude_turn("d", {"input_tokens": float("nan")}),
            {"type": "system", "subtype": "compact_boundary", "compactMetadata": {"preTokens": "many"}},
            _claude_turn("e", {"input_tokens": 200}),
        ],
    )
    (s,) = get_adapter("claude-code")(tmp_path).sessions()
    assert [t.ctx for t in s.turns] == [100, 200]
    assert s.parse_errors == 4 and not s.compactions


def test_codex_malformed_usage_skips_record_not_scan(tmp_path):
    def tc(last, total=None, **info):
        return {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": last, "total_token_usage": total, **info},
            },
        }

    ok = {"input_tokens": 50, "cached_input_tokens": 0, "output_tokens": 2}
    _write(
        tmp_path,
        "s.jsonl",
        [
            tc(ok, {"input_tokens": 50, "cached_input_tokens": 0, "output_tokens": 2, "total_tokens": 52}),
            tc({"input_tokens": "lots", "output_tokens": 1}),
            tc(ok, {"input_tokens": "x"}),
            tc(ok, model_context_window="big"),
            tc("nope"),
            tc(
                {**ok, "input_tokens": 80},
                {"input_tokens": 130, "cached_input_tokens": 0, "output_tokens": 4, "total_tokens": 134},
            ),
        ],
    )
    (s,) = get_adapter("codex")(tmp_path).sessions()
    assert [t.ctx for t in s.turns] == [50, 80]
    assert s.parse_errors == 4
