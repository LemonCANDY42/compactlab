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


def test_claude_compaction_trigger_is_recorded(tmp_path):
    def boundary(trigger, uuid):
        meta = {"preTokens": 500_000}
        if trigger:
            meta["trigger"] = trigger
        return {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": uuid,
            "timestamp": "2026-01-01T00:00:00Z",
            "compactMetadata": meta,
        }

    _write(
        tmp_path,
        "s.jsonl",
        [
            x
            for i, t in enumerate(("auto", "manual", None, "weird"))
            for x in (_claude_turn(str(i), {"input_tokens": 100, "output_tokens": 5}), boundary(t, f"u{i}"))
        ]
        + [_claude_turn("z", {"input_tokens": 100, "output_tokens": 5})],
    )
    (s,) = list(get_adapter("claude-code")(tmp_path).sessions())
    assert [c.trigger for c in s.compactions] == ["auto", "manual", "", ""]
    from compactlab.model import is_auto

    assert [is_auto(c) for c in s.compactions] == [True, False, True, True]


def test_claude_rewritten_compact_boundaries_count_once(tmp_path):
    def boundary(uuid, pre):
        return {
            "type": "system", "subtype": "compact_boundary", "uuid": uuid,
            "timestamp": "2026-01-01T00:00:00Z", "compactMetadata": {"preTokens": pre, "trigger": "auto"},
        }  # fmt: skip

    def summary(text):
        return {"type": "user", "isCompactSummary": True, "message": {"role": "user", "content": text}}

    turn = lambda i: _claude_turn(str(i), {"input_tokens": 100, "output_tokens": 5})  # noqa: E731
    _write(
        tmp_path,
        "s.jsonl",
        [turn(0), boundary("u1", 500_000), summary("first"), turn(1), boundary("u2", 600_000), summary("second"),
         boundary("u1", 500_000), summary("first"), turn(2)],
    )  # fmt: skip
    (s,) = list(get_adapter("claude-code")(tmp_path).sessions())
    assert [(c.pre_tokens, c.summary_text) for c in s.compactions] == [(500_000, "first"), (600_000, "second")]


def test_codex_custom_tool_calls_are_tool_results(tmp_path):
    """Current Codex runs most commands as `custom_tool_call` (name `exec`) with list-of-blocks output, and writes a
    response's token_count after that response's tool output: call -> output -> token_count."""
    from compactlab.adapters.base import parse_ts

    def at(sec):
        return f"2026-01-01T00:00:{sec:02d}Z"

    def item(sec, **p):
        return {"timestamp": at(sec), "type": "response_item", "payload": p}

    def tc(sec, inp):
        usage = {"input_tokens": inp, "cached_input_tokens": 0, "output_tokens": 5}
        return {"timestamp": at(sec), "type": "event_msg",
                "payload": {"type": "token_count", "info": {"last_token_usage": usage}}}  # fmt: skip

    text = [{"type": "input_text", "text": "y" * 400}]
    image = {"type": "input_image", "image_url": "data:image/png;base64," + "A" * 4000}
    _write(
        tmp_path,
        "s.jsonl",
        [
            {"timestamp": at(0), "type": "turn_context", "payload": {"model": "gpt-x"}},
            {"timestamp": at(1), "type": "event_msg", "payload": {"type": "user_message", "message": "hi"}},
            item(4, type="reasoning"),
            item(6, type="custom_tool_call", call_id="c1", name="exec", input="cat README.md"),
            item(9, type="custom_tool_call_output", call_id="c1", output=[{"type": "input_text", "text": "x" * 800}]),
            tc(10, 1000),
            item(14, type="function_call", call_id="c2", name="exec_command", arguments='{"cmd": "ls"}'),
            item(15, type="function_call_output", call_id="c2", output=[*text, image]),
            tc(16, 1300),
            item(20, type="custom_tool_call", call_id="c3", name="exec", input="rg foo"),
            item(23, type="custom_tool_call_output", call_id="c3", output="z" * 120),
            tc(24, 1400),
            item(30, type="message", role="assistant", content=[]),
            tc(31, 1500),
        ],
    )
    (s,) = get_adapter("codex")(tmp_path).sessions()
    assert [t.tool_keys for t in s.turns[:3]] == [["exec:cat README.md"], ["exec_command:ls"], ["exec:rg foo"]]
    assert s.turns[0].tool_result_tokens == [200]  # 800 chars of text
    assert s.turns[1].tool_result_tokens == [100]  # text blocks only: an image's base64 is not text
    assert s.turns[2].tool_result_tokens == [30]  # a plain-string output
    # a response runs from the input before it to its last output item, not to the token_count written after the
    # tool already ran
    assert [(t.start_ts, t.ts) for t in s.turns] == [
        (parse_ts(at(a)), parse_ts(at(b))) for a, b in ((1, 6), (9, 14), (15, 20), (23, 30))
    ]
    assert s.unknown_events == {}


def test_claude_idle_compaction_is_told_apart(tmp_path):
    """Idle compaction (Claude Code's `idleCompaction`): a summary precomputed while the cache is still warm, applied
    when the user comes back after an hour or more. It is not the threshold and costs the user no wait."""
    from compactlab.model import is_auto, is_threshold

    def turn(mid, at):
        return {**_claude_turn(mid, {"input_tokens": 300_000, "output_tokens": 5}), "timestamp": at}

    def boundary(uuid, at, **meta):
        return {"type": "system", "subtype": "compact_boundary", "uuid": uuid, "timestamp": at,
                "compactMetadata": {"preTokens": 300_000, "trigger": "auto", **meta}}  # fmt: skip

    _write(
        tmp_path,
        "s.jsonl",
        [
            turn("a", "2026-01-01T00:00:00Z"),
            boundary("u1", "2026-01-01T03:12:00Z", durationMs=48, precomputed=True),  # away 3 h: idle
            turn("b", "2026-01-01T03:12:30Z"),
            boundary("u2", "2026-01-01T04:30:00Z", durationMs=2100),  # older logs: no `precomputed`, near-instant
            turn("c", "2026-01-01T04:30:30Z"),
            boundary("u3", "2026-01-01T05:40:00Z", durationMs=81_586),  # back after 70 min, over the threshold
            turn("d", "2026-01-01T05:42:00Z"),
            boundary("u4", "2026-01-01T05:43:00Z", durationMs=60_000),  # working: the threshold
            turn("e", "2026-01-01T05:45:00Z"),
        ],
    )
    (s,) = list(get_adapter("claude-code")(tmp_path).sessions())
    assert [c.idle for c in s.compactions] == [True, True, False, False]
    assert [is_threshold(c) for c in s.compactions] == [False, False, True, True]
    assert all(is_auto(c) for c in s.compactions)
    # the logged duration is the wait; an idle summary was ready before the user came back
    assert [c.duration_s for c in s.compactions] == [None, None, 81.586, 60.0]


def test_codex_aborted_response_does_not_stretch_the_next_turn(tmp_path):
    from compactlab.adapters.base import parse_ts

    def at(sec):
        return f"2026-01-01T00:{sec // 60:02d}:{sec % 60:02d}Z"

    usage = {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 5}
    _write(
        tmp_path,
        "s.jsonl",
        [
            {"timestamp": at(0), "type": "turn_context", "payload": {"model": "gpt-x"}},
            {"timestamp": at(1), "type": "event_msg", "payload": {"type": "user_message", "message": "a"}},
            {"timestamp": at(3), "type": "response_item", "payload": {"type": "reasoning"}},
            {"timestamp": at(4), "type": "event_msg", "payload": {"type": "turn_aborted"}},
            {"timestamp": at(300), "type": "event_msg", "payload": {"type": "user_message", "message": "b"}},
            {"timestamp": at(302), "type": "response_item", "payload": {"type": "reasoning"}},
            {"timestamp": at(306), "type": "response_item", "payload": {"type": "message", "role": "assistant"}},
            {
                "timestamp": at(307),
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {"last_token_usage": usage}},
            },
        ],
    )
    (s,) = get_adapter("codex")(tmp_path).sessions()
    assert (s.turns[0].start_ts, s.turns[0].ts) == (parse_ts(at(300)), parse_ts(at(306)))
