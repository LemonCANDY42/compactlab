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
