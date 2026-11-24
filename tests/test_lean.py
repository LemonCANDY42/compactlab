"""Lean sessions: the scan cache and the lite analysis keep no transcript text, and nothing they compute changes."""

import copy
import json

from compactlab import synth
from compactlab.adapters import get_adapter
from compactlab.adapters.cache import ScanCache
from compactlab.model import drop_inherited, lean, session_to_dict, tool_id
from compactlab.pricing import PricingTable
from compactlab.sim.calibrate import _is_rediscovery
from compactlab.sim.engine import SimOptions, analyze

MARKERS = (b"SECRET-PROMPT", b"SECRET-PATH", b"SECRET-OUTPUT", b"SECRET-SUMMARY")


def test_tool_id_keeps_the_tool_name_and_identity_but_not_the_argument():
    keys = ["Read:/SECRET-PATH/a.py", "Read:/SECRET-PATH/b.py", "Read", "Read:", "mcp__fs__read:/x", "exec_command"]
    ids = [tool_id(k) for k in keys]
    assert len(set(ids)) == len(keys)  # distinct keys stay distinct
    assert ids == [tool_id(k) for k in keys] == [tool_id(i) for i in ids]  # stable and idempotent
    for k, i in zip(keys, ids, strict=True):
        assert i.split(":", 1)[0] == k.split(":", 1)[0] and _is_rediscovery(i) == _is_rediscovery(k)
        assert "SECRET" not in i


def _logs(tmp_path):
    claude, codex = tmp_path / "claude", tmp_path / "codex"
    synth.write_claude_code(claude, n_main=8, n_sub=3)
    synth.write_codex(codex, n=5)
    return claude, codex


def _sessions(claude, codex):
    cc = get_adapter("claude-code")(claude)
    cx = get_adapter("codex")(codex)
    out = drop_inherited([cc.parse_file(p) for p in cc.find_files()])
    return out + [cx.parse_file(p) for p in cx.find_files()]


def test_the_lite_analysis_of_lean_sessions_is_identical(tmp_path):
    full = _sessions(*_logs(tmp_path))
    pricing = PricingTable.load(offline=True)
    lean_ones = [lean(s) for s in copy.deepcopy(full)]
    assert not any(t.user_text or t.snippets for s in lean_ones for t in s.turns)
    a = analyze(full, pricing, SimOptions())
    b = analyze(lean_ones, pricing, SimOptions())
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    # not vacuous: the penalty was measured from corrections, repeated reads and observed summaries
    observed = [g for g in a["groups"] if g["penalty_params"]["source"] == "observed"]
    assert observed and any(g["penalty_params"]["correction_turns"] > 0 for g in observed)
    assert any(g["penalty_params"]["rho"] > 0 for g in observed) and any(
        g["summary_tokens"] != 12_000 for g in observed
    )


def _secret_claude_log(tmp_path):
    src = sorted((tmp_path / "src").rglob("*.jsonl")) if (tmp_path / "src").exists() else []
    if not src:
        synth.write_claude_code(tmp_path / "src", n_main=1, n_sub=0)
        src = sorted((tmp_path / "src").rglob("*.jsonl"))
    out = []
    for line in src[0].read_text().splitlines():
        r = json.loads(line)
        m = r.get("message", {})
        if r.get("isCompactSummary"):
            m["content"] = "SECRET-SUMMARY " + m["content"]
        elif r.get("type") == "user" and isinstance(m.get("content"), str):
            m["content"] = "SECRET-PROMPT I already told you " + m["content"]
        for b in m.get("content", []) if isinstance(m.get("content"), list) else []:
            if b.get("type") == "tool_use":
                b["input"] = {"file_path": "/SECRET-PATH" + b["input"]["file_path"]}
            if b.get("type") == "tool_result":
                b["content"] = "SECRET-OUTPUT " + b["content"]
        out.append(json.dumps(r) + "\n")
    log = tmp_path / "logs" / "s.jsonl"
    log.parent.mkdir()
    log.write_text("".join(out))
    return log, out


def test_cache_rows_hold_no_transcript_text(tmp_path):
    log, lines = _secret_claude_log(tmp_path)
    assert all(m in "".join(lines).encode() for m in MARKERS)
    ad = get_adapter("claude-code")(tmp_path / "logs")
    full = ad.parse_file(log)
    assert any(t.corrects() for t in full.turns) and full.compactions[0].summary_len > 0
    log.write_text("".join(lines[: len(lines) // 2]))  # a resume too, from a lean row
    cache = ScanCache(tmp_path / "c")
    cache.parse(ad, log)
    log.write_text("".join(lines))
    got = cache.parse(ad, log)
    assert cache.stats["resumed"] == 1
    assert session_to_dict(got) == session_to_dict(lean(ad.parse_file(log)))
    assert any(t.correction for t in got.turns) and got.compactions[0].summary_len == full.compactions[0].summary_len
    cache.close()
    stored = b"".join(p.read_bytes() for p in (tmp_path / "c").iterdir())
    assert not any(m in stored for m in MARKERS)


def test_codex_keeps_the_first_summary_of_a_lean_compaction_on_resume(tmp_path):
    """Codex logs a compaction's message twice in some versions; the first one is kept. After a resume from a lean
    row the first one's text is gone, only its length remains: the second must still not replace it."""
    synth.write_codex(tmp_path / "src", n=1)
    lines = sorted((tmp_path / "src").rglob("*.jsonl"))[0].read_text().splitlines(True)
    at = next(i for i, x in enumerate(lines) if json.loads(x)["type"] == "compacted")
    again = json.loads(lines[at])
    again["payload"]["message"] = "a different, much longer message " * 50
    lines.insert(at + 1, json.dumps(again) + "\n")
    log = tmp_path / "logs" / "rollout.jsonl"
    log.parent.mkdir()
    log.write_text("".join(lines[: at + 1]))  # the first message is in the lean row, the second comes on resume
    ad = get_adapter("codex")(tmp_path / "logs")
    cache = ScanCache(tmp_path / "c")
    cache.parse(ad, log)
    log.write_text("".join(lines))
    got = cache.parse(ad, log)
    assert cache.stats["resumed"] == 1
    assert session_to_dict(got) == session_to_dict(lean(ad.parse_file(log)))


def test_deep_mode_reads_text_from_the_logs_not_the_cache(tmp_path):
    from compactlab.cli.main import main

    claude = tmp_path / "claude"
    synth.write_claude_code(claude, n_main=6, n_sub=2)
    args = ["simulate", "--claude-root", str(claude), "--harness", "claude-code", "--since-days", "0",
            "--offline-pricing", "--no-current", "--grid", "100k:600k:50k", "--cache-dir", str(tmp_path / "c"),
            "--lens-out", ""]  # fmt: skip
    assert main([*args, "--out", str(tmp_path / "lite.json")]) == 0  # fills the (lean) cache
    deep = [*args, "--mode", "deep", "--executor", "mock", "--budget", "5", "--yes", "--out", str(tmp_path / "d.json")]
    assert main(deep) == 0
    res = json.loads((tmp_path / "d.json").read_text())
    # probes come from the file paths in tool calls: with lean sessions there would be none, so no measured group
    assert res["mode"] == "deep" and res["deep"]["calls"] > 0 and res["deep"]["groups"]
    lite = json.loads((tmp_path / "lite.json").read_text())
    assert [g["penalty_params"] for g in res["groups"]] == [g["penalty_params"] for g in lite["groups"]]


def test_a_replaced_summary_forgets_a_lean_compactions_length():
    from compactlab.model import Compaction

    c = Compaction(turn_index=3, summary_text="x" * 100)
    lean_c = Compaction(turn_index=3, summary_chars=100)
    assert c.summary_len == lean_c.summary_len == 100
    for comp in (c, lean_c):
        comp.set_summary("")  # a parser that learns the summary was empty after all
        assert comp.summary_len == 0
        comp.set_summary("abc")
        assert comp.summary_len == 3


def test_the_fast_correction_check_agrees_with_the_regex():
    import random

    from compactlab.model import CORRECTION_RE, corrects

    fixed = ["I already told you", "AS I SAID", "you Forgot the test", "Not what I asked", "we discussed this",
             "We already did that", "already said so", "I ſaid it", "I toKld you", "ı told you",
             "İ told you", "toldx", "xi told", "我说过了", "你忘了吗", "nothing here", "", "i  told",
             "We already decided that", "we already discussed it", "we already discussedx", "we already decidedx",
             "x we already discussed", "I already mentionedx", "as i saidx"]  # fmt: skip
    rng = random.Random(5)
    parts = ["i ", "I ", "told", "said", "did", "we ", "already ", "you ", "forgot", "not what i", "as ", "x", " ",
             "discussed", "decided", "mentioned",
             "ſ", "K", "ı", "İ", "S", "AID", "我", "说过", "你忘了", "\n"]  # fmt: skip
    fuzz = ["".join(rng.choice(parts) for _ in range(rng.randint(1, 40))) for _ in range(40_000)]
    for text in fixed + fuzz:
        assert corrects(text) == (CORRECTION_RE.search(text) is not None), repr(text)
    assert sum(corrects(t) for t in fuzz) > 1000  # the fuzz reaches both answers
