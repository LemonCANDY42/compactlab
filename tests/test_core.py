import math

import pytest

from compactlab.adapters import get_adapter
from compactlab.eval.factcheck import check_summary
from compactlab.model import Compaction, Session, Turn
from compactlab.pricing import PricingTable
from compactlab.sim.calibrate import PenaltyParams, repeat_stats
from compactlab.sim.engine import DEFAULT_ROT, Ctx, SimOptions, _actual_cost, analyze, make_trace, replay, rot_fn
from compactlab.sim.select import recommend
from compactlab.sim.timing import TimeModel

PR = PricingTable.load()


def test_pricing_lookup_and_long_context():
    p = PR.lookup("claude-haiku-5-5")
    assert p.key == "claude-haiku-5-5" and p.source.startswith("bundled")
    short, long_ = p.rates(50_000), p.rates(150_000)
    assert long_[0] > short[0] and long_[3] > short[3]
    assert PR.lookup("claude-opus-5-5").rates(900_000) == PR.lookup("claude-opus-5-5").rates(1000)
    assert PR.lookup("mystery").key == "fallback"


def test_pricing_override(tmp_path):
    f = tmp_path / "p.json"
    f.write_text('{"version":1,"models":{"claude-opus-5-5":{"input":1,"output":2,"cache_write":1,"cache_read":0.1}}}')
    p = PricingTable.load(f).lookup("claude-opus-5-5")
    assert p.output == 2 and p.source.startswith("user")
    assert PricingTable.load(f).lookup("claude-opus-5-5-20261001").key == "claude-opus-5-5"


def _trace(n=200, g=3000, gap=10):
    s = Session("x", "claude-code", "main", "p")
    ctx = 30_000
    for i in range(n):
        ctx += g
        s.turns.append(Turn(ts=i * gap, ctx=ctx, output=300, model="claude-opus-5-5"))
    return s


def _ctx(pen=None, tm=None):
    return Ctx(PR.lookup("claude-opus-5-5"), 30_000, 12_000, 300, pen or PenaltyParams(rho=0, fixed_tokens=0,
               correction_turns=0), tm or TimeModel(), 2000, rot_fn(DEFAULT_ROT), SimOptions())  # fmt: skip


def test_replay_no_compaction_matches_closed_form():
    s = _trace()
    tr = make_trace(s, 30_000)
    r = replay(tr, math.inf, _ctx())
    _, ro, rw, rr = PR.lookup("claude-opus-5-5").rates(1)
    exp = sum((30_000 + 3000 * i) * rr + 3000 * rw + 300 * ro for i in range(200))
    assert r.compactions == 0 and abs(r.cost - exp) < 1e-6 and r.penalty == 0


def test_compaction_count_and_idle_effect():
    s = _trace()
    tr = make_trace(s, 30_000)
    r = replay(tr, 150_000, _ctx())
    assert r.compactions >= 3
    idle = make_trace(s, 30_000)
    idle.gap = [400.0] * len(idle.gap)
    assert replay(idle, 150_000, _ctx()).cost > r.cost


def test_penalty_is_added_only_with_compactions():
    s = _trace()
    tr = make_trace(s, 30_000)
    pen = PenaltyParams(rho=0.1, fixed_tokens=5000, correction_turns=1)
    withp = replay(tr, 150_000, _ctx(pen))
    assert withp.penalty > 0 and withp.t_redisc > 0
    none = replay(tr, math.inf, _ctx(pen))
    assert none.penalty == 0 and none.t_redisc == 0


def test_time_tracks_context_and_compactions():
    s = _trace()
    tr = make_trace(s, 30_000)
    tm = TimeModel()  # prior: grows with context
    lo, hi = replay(tr, 150_000, _ctx(tm=tm)), replay(tr, math.inf, _ctx(tm=tm))
    assert lo.t_turns < hi.t_turns  # smaller contexts are faster per turn
    assert lo.t_compact > 0 and hi.t_compact == 0


def test_repeat_stats_detects_rediscovery():
    s = Session("x", "claude-code", "main", "p")
    for i in range(6):
        s.turns.append(Turn(ts=i, ctx=1000, tool_keys=[f"Read:/a{i}"], tool_result_tokens=[100]))
    s.turns.append(
        Turn(ts=7, ctx=1000, tool_keys=["Read:/a1"], tool_result_tokens=[500], user_text="you forgot the config")
    )
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


def test_groups_split_by_harness():
    a = _trace()
    b = _trace()
    b.id, b.harness = "y", "dsh"
    res = analyze([a, b], PR, SimOptions(grid_stop=500_000))
    keys = sorted((g["harness"], g["model"], g["role"]) for g in res["groups"])
    assert keys == [("claude-code", "claude-opus-5-5", "main"), ("dsh", "claude-opus-5-5", "main")]
    assert all(g["n_sessions"] == 1 for g in res["groups"])


def test_model_switch_is_priced_per_turn():
    s = Session("x", "claude-code", "main", "p")
    ctx = 30_000
    for i in range(200):
        ctx += 3000
        s.turns.append(Turn(ts=i * 10, ctx=ctx, output=300, model="claude-opus-5-5" if i < 120 else "claude-haiku-4-5"))
    assert s.model == "claude-opus-5-5"  # the session's label is its majority model...
    res = analyze([s], PR, SimOptions(base_tokens=30_000, grid_stop=500_000))
    g = res["groups"][0]
    exp = 0.0
    for i, t in enumerate(s.turns):
        _, ro, rw, rr = PR.lookup(t.model).rates(t.ctx)
        exp += (30_000 + 3000 * i) * rr + 3000 * rw + 300 * ro
    assert PR.lookup("claude-haiku-4-5").input != PR.lookup("claude-opus-5-5").input
    assert g["baseline_cost"] == pytest.approx(exp)  # ...but every turn is replayed at its own model's rates
    assert g["actual_cost"] == pytest.approx(
        sum(_actual_cost(Session("x", "claude-code", "main", "p", turns=[t]), PR.lookup(t.model)) for t in s.turns)
    )


def test_cap_below_the_grid_is_rejected_not_exceeded(make_group):
    from compactlab.sim.select import InfeasibleCap, select_profile

    g = make_group([100_000, 200_000, 300_000, 400_000], cost=[4.0, 3.0, 2.0, 1.0])
    for name in ("cost", "balanced", "time"):
        with pytest.raises(InfeasibleCap, match="50,000"):
            select_profile(g, name, cap=50_000)
        assert select_profile(g, name, cap=250_000)["threshold"] <= 250_000
    with pytest.raises(InfeasibleCap, match="--max-threshold 50,000"):
        analyze([_trace()], PR, SimOptions(max_threshold=50_000))


def _repeating(boundary: int, n: int = 70, sid: str = "r") -> Session:
    s = Session(sid, "claude-code", "main", "p")
    for i in range(n):
        s.turns.append(Turn(ts=i, ctx=50_000, tool_keys=["Read:/same.py"], tool_result_tokens=[1000]))
    s.compactions = [Compaction(boundary, pre_tokens=200_000)]
    return s


def test_calibration_needs_complete_comparable_windows():
    from compactlab.sim.calibrate import calibrate

    # work that repeats as much before a compaction as after it shows no re-discovery penalty
    flat = calibrate([_repeating(30, sid=str(i)) for i in range(5)], 35_000, 12_000)
    assert flat.source == "observed" and flat.n_observed == 5 and flat.rho == 0
    # a boundary this early has no full "before" window: it must not count (its negative slice made rho .377)
    early = calibrate([_repeating(10, sid=str(i)) for i in range(5)], 35_000, 12_000)
    assert early.source == "prior" and early.n_observed == 0
    # a window that holds another compaction is not comparable either
    s = _repeating(30)
    s.compactions.append(Compaction(40, pre_tokens=200_000))
    assert calibrate([s] * 5, 35_000, 12_000).n_observed == 0
