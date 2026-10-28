import math

from compactlab import synth
from compactlab.adapters import get_adapter
from compactlab.sim.calibrate import PenaltyParams, repeat_stats
from compactlab.eval.factcheck import check_summary
from compactlab.model import Compaction, Session, Turn
from compactlab.pricing import PricingTable
from compactlab.sim.select import recommend
from compactlab.sim.engine import SimOptions, analyze, make_trace, replay, rot_fn, DEFAULT_ROT

PR = PricingTable.load()


def test_pricing_lookup_and_long_context():
    p = PR.lookup("claude-haiku-5-5")
    assert p.key == "claude-haiku-5"
    short, long_ = p.rates(50_000), p.rates(150_000)
    assert long_[0] > short[0] and long_[3] > short[3]
    assert PR.lookup("claude-opus-5-5").rates(900_000) == PR.lookup("claude-opus-5-5").rates(1000)
    assert PR.lookup("mystery").key == "fallback"


def test_pricing_override(tmp_path):
    f = tmp_path / "p.json"
    f.write_text('{"version":1,"models":{"claude-opus-5":{"match":["opus-5"],"input":1,"output":2,"cache_write":1,"cache_read":0.1}}}')
    p = PricingTable.load(f).lookup("claude-opus-5-5")
    assert p.output == 2


def _trace(n=200, g=3000, gap=10):
    s = Session("x", "claude-code", "main", "p")
    ctx = 30_000
    for i in range(n):
        ctx += g
        s.turns.append(Turn(ts=i * gap, ctx=ctx, output=300, model="claude-opus-5-5"))
    return s


def test_replay_no_compaction_matches_closed_form():
    s = _trace()
    price = PR.lookup("claude-opus-5-5")
    tr = make_trace(s, 30_000)
    cost, k, _ = replay(tr, math.inf, price, 30_000, 12_000, 300, None, rot_fn(DEFAULT_ROT), SimOptions())
    ri, ro, rw, rr = price.rates(1)
    exp = sum((30_000 + 3000 * i) * rr + 3000 * rw + 300 * ro for i in range(200))
    assert k == 0 and abs(cost - exp) < 1e-6


def test_compaction_count_and_idle_effect():
    s = _trace()
    price = PR.lookup("claude-opus-5-5")
    tr = make_trace(s, 30_000)
    args = (price, 30_000, 12_000, 300, None, rot_fn(DEFAULT_ROT), SimOptions())
    _, k, _ = replay(tr, 150_000, *args)
    assert k >= 3
    # long idle gaps make every prefix a cache write => strictly higher cost
    idle = make_trace(s, 30_000)
    idle.gap = [400.0] * len(idle.gap)
    assert replay(idle, 150_000, *args)[0] > replay(tr, 150_000, *args)[0]


def test_penalty_is_added_only_with_compactions():
    s = _trace()
    price = PR.lookup("claude-opus-5-5")
    tr = make_trace(s, 30_000)
    pen = PenaltyParams(rho=0.1, fixed_tokens=5000, correction_turns=1)
    base = replay(tr, 150_000, price, 30_000, 12_000, 300, None, rot_fn(DEFAULT_ROT), SimOptions())[0]
    withp = replay(tr, 150_000, price, 30_000, 12_000, 300, pen, rot_fn(DEFAULT_ROT), SimOptions())[0]
    assert withp > base
    assert replay(tr, math.inf, price, 30_000, 12_000, 300, pen, rot_fn(DEFAULT_ROT), SimOptions())[0] == \
        replay(tr, math.inf, price, 30_000, 12_000, 300, None, rot_fn(DEFAULT_ROT), SimOptions())[0]


def test_repeat_stats_detects_rediscovery():
    s = Session("x", "claude-code", "main", "p")
    for i in range(6):
        s.turns.append(Turn(ts=i, ctx=1000, tool_keys=[f"Read:/a{i}"], tool_result_tokens=[100]))
    s.turns.append(Turn(ts=7, ctx=1000, tool_keys=["Read:/a1"], tool_result_tokens=[500], user_text="you forgot the config"))
    reps, toks, corr = repeat_stats(s, 6, 7)
    assert (reps, toks, corr) == (1, 500, 1)


def test_recommend_floor_and_largest_within_tolerance():
    grid = [100, 150, 200, 250]
    r = recommend(grid, [10, 10.1, 11, 14], [0.95, 0.94, 0.93, 0.80], 0.90, tol=0.02)
    assert r["threshold"] == 150 and r["floor_met"]
    r = recommend(grid, [10, 10.1, 11, 14], [0.95, 0.94, 0.93, 0.80], 0.99)
    assert not r["floor_met"] and r["threshold"] == 100
    r = recommend(grid, [10, 10, 10, 10], [0.9, 0.9, 0.9, 0.9], 0.9)
    assert r["threshold"] == 250 and r["range"] == [100, 250]


def test_analyze_synthetic(claude_root, codex_root):
    ss = list(get_adapter("claude-code")(claude_root).sessions()) + list(get_adapter("codex")(codex_root).sessions())
    res = analyze(ss, PR, SimOptions(grid_stop=500_000))
    keys = {(g["model"], g["role"]) for g in res["groups"]}
    assert ("claude-opus-5-5", "main") in keys and ("claude-sonnet-5-5", "subagent") in keys
    g = next(g for g in res["groups"] if g["model"] == "gpt-5-codex")
    assert g["harness"] == "codex" and len(g["grid"]) == len(g["cost"]) == len(g["recall"])
    assert g["cost"][0] > 0 and g["recommendation"]["threshold"] in g["grid"]
    assert len(res["sessions"]) == 14


def test_factcheck_flags_unsupported_claims():
    s = Session("x", "claude-code", "main", "p")
    s.turns = [Turn(ts=1, ctx=10, tool_keys=["Read:/proj/a.py"], snippets=["def a(): pass"])]
    c = Compaction(1, summary_text="All tests pass. Edited /proj/a.py and /proj/ghost.py using `fooBar`.")
    kinds = {f.kind: f for f in check_summary(s, c)}
    assert "unsupported_claim" in kinds
    assert kinds["unknown_path"].text == "/proj/ghost.py"
    assert "unknown_identifier" in kinds
    s.turns.append(Turn(ts=2, ctx=10, tool_keys=["Bash:pytest -q"], snippets=["$ pytest -q", "2 failed, 3 passed"]))
    c2 = Compaction(2, summary_text="All tests pass.")
    assert [f.kind for f in check_summary(s, c2)] == ["contradicted_claim"]
