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


def test_replay_keeps_a_hand_typed_compact_only_while_it_is_still_needed():
    s = _trace()
    plain = make_trace(s, 30_000)
    s.compactions = [Compaction(100, pre_tokens=30_000 + 3000 * 100, trigger="manual")]
    tr = make_trace(s, 30_000)
    assert tr.manual == {100: 330_000}
    assert replay(tr, math.inf, _ctx()).compactions == 1  # no threshold: the user's /compact still happens
    assert replay(plain, math.inf, _ctx()).compactions == 0
    # a threshold that already keeps the context small makes the typed /compact unnecessary
    assert replay(tr, 150_000, _ctx()).compactions == replay(plain, 150_000, _ctx()).compactions
    s.compactions = [Compaction(100, pre_tokens=330_000, trigger="auto")]
    assert make_trace(s, 30_000).manual == {}


def test_a_forked_log_does_not_bill_the_parents_history_twice():
    from compactlab.model import drop_inherited

    def sess(sid, start, n):
        s = Session(sid, "claude-code", "main", sid)
        s.turns = [Turn(ts=start + i, ctx=1000 + i, output=5, model="m") for i in range(n)]
        return s

    parent = sess("parent", 100.0, 6)
    parent.compactions = [Compaction(3, pre_tokens=900, ts=103.0, trigger="auto")]
    fork = sess("fork", 100.0, 6)  # the fork copies the parent's six turns, then adds three of its own
    fork.turns += [Turn(ts=200.0 + i, ctx=5000 + i, output=5, model="m") for i in range(3)]
    fork.compactions = [
        Compaction(3, pre_tokens=900, ts=103.0, trigger="auto"),
        Compaction(7, pre_tokens=6000, ts=201.0),
    ]
    a, b = drop_inherited([parent, fork])
    assert (a.id, len(a.turns), len(a.compactions)) == ("parent", 6, 1)
    assert (b.id, len(b.turns), b.skipped_inherited) == ("fork", 3, 6)
    assert [(c.turn_index, c.pre_tokens) for c in b.compactions] == [(1, 6000)]
    lone = sess("lone", 500.0, 2)
    assert drop_inherited([lone]) == [lone] and lone.skipped_inherited == 0


def _tool_session(results, key="Read:/a"):
    s = Session("x", "claude-code", "main", "p")
    s.turns = [Turn(ts=i, ctx=1000 + i, tool_keys=[key], tool_result_tokens=[r]) for i, r in enumerate(results)]
    return s


def test_rediscovery_step_matches_the_penalty_units():
    from compactlab.sim.calibrate import PRIOR_STEP_TOKENS, rediscovery_step

    observed = PenaltyParams(rho=0.01, fixed_tokens=0, correction_turns=0, source="observed", n_observed=9)
    big = [_tool_session([100, 300] * 15)]
    # observed re-read tokens are made of these calls: tokens / mean size = calls
    assert rediscovery_step(big, observed) == 200
    # prior re-read tokens pair with the prior step, whatever the logged tool results look like
    assert rediscovery_step([_tool_session([4] * 40)], PenaltyParams()) == PRIOR_STEP_TOKENS
    # too few re-discovery results, or none (edits are not re-discovery): the prior step
    assert rediscovery_step([_tool_session([100] * 5)], observed) == PRIOR_STEP_TOKENS
    assert rediscovery_step([_tool_session([5000] * 40, key="Edit:/a")], observed) == PRIOR_STEP_TOKENS


def test_tiny_tool_results_do_not_inflate_rediscovery_time():
    """Regression: a Codex group whose logged tool results were ~4 tokens replayed ~65 h of re-discovery per
    compaction, because the prior's re-read tokens were divided by that median."""
    ss = []
    for k in range(3):
        s = Session(f"s{k}", "codex", "main", "p")
        ctx = 30_000
        for i in range(120):
            ctx += 3000
            s.turns.append(Turn(ts=k * 10_000 + i * 20, ctx=ctx, output=200, model="gpt-6-astra",
                                tool_keys=["exec:ls"], tool_result_tokens=[4]))  # fmt: skip
        ss.append(s)
    res = analyze(ss, PR, SimOptions(grid_start=100_000, grid_stop=300_000, grid_step=50_000))
    (g,) = res["groups"]
    assert g["penalty_params"]["source"] == "prior"
    med = g["time_model"]["median_turn_s"]
    for T, redisc, comps in zip(g["grid"], g["time_redisc_s"], g["compactions"], strict=True):
        # prior: (6000 + 4% of what was dropped) / 2000 + 0.3 turns per compaction, never thousands
        assert redisc <= comps * ((6000 + 0.04 * (T + 3000)) / 2000 + 0.3) * med + 1e-6, T


def _idle_session(n=200, away_at=100, away_s=7200.0, g=3000):
    s = Session("idle", "claude-code", "main", "p")
    ctx, ts = 30_000, 0.0
    for i in range(n):
        ts += away_s if i == away_at else 10.0
        ctx += g
        s.turns.append(Turn(ts=ts, ctx=ctx, output=300, model="claude-opus-5-5"))
    return s


def _idle_ctx(on=True, rate=1.0):
    pen = PenaltyParams(rho=0, fixed_tokens=0, correction_turns=0)
    return Ctx(PR.lookup("claude-opus-5-5").for_ttl(3600), 30_000, 12_000, 3600, pen, TimeModel(), 2000,
               rot_fn(DEFAULT_ROT), SimOptions(), idle_on=on, idle_rate=rate)  # fmt: skip


def test_idle_compaction_replay():
    """Away for 2 h at 330k: the summary is made while the cache is warm (read + summary output), the user returns to
    a small context whose cache write is cheap, and nobody waits for it."""
    assert make_trace(_idle_session(), 30_000).idle_obs == {}  # no idle compaction anywhere: nothing observed
    assert make_trace(_idle_session(), 30_000, idle_since=1e12).idle_obs == {}  # all before the feature appeared
    tr = make_trace(_idle_session(), 30_000, idle_since=0.0)
    assert tr.idle_obs == {100: False}  # eligible in the log, and no idle compaction there
    tr.idle_obs = {}  # unknown instead: it fires at the rate (1.0 here)
    on, off = replay(tr, math.inf, _idle_ctx(True)), replay(tr, math.inf, _idle_ctx(False))
    assert (on.compactions, on.idle_compactions, off.compactions, off.idle_compactions) == (1, 1, 0, 0)
    assert on.t_compact == 0 and on.t_redisc == 0
    _, ro, _, rr = PR.lookup("claude-opus-5-5").for_ttl(3600).rates(1)
    ctx = 30_000 + 100 * 3000
    assert on.idle_cost == pytest.approx(ctx * rr + 12_000 * ro)
    assert on.cost < off.cost  # rewriting 330k cold costs more than the summary and the smaller turns after it
    # a gap the log shows did not fire stays without one; a short gap or a small context never fires
    tr.idle_obs[100] = False
    assert replay(tr, math.inf, _idle_ctx(True)).idle_compactions == 0
    assert replay(make_trace(_idle_session(away_s=1800.0), 30_000), math.inf, _idle_ctx()).idle_compactions == 0
    assert replay(make_trace(_idle_session(away_at=40), 30_000), math.inf, _idle_ctx()).idle_compactions == 0


def test_idle_compaction_observed_outcomes_and_tail():
    s = _idle_session()
    s.turns.append(Turn(ts=s.turns[-1].ts + 4000, ctx=s.turns[-1].ctx + 3000, output=10, model="claude-opus-5-5"))
    s.compactions.append(Compaction(turn_index=100, pre_tokens=330_000, ts=s.turns[100].ts, trigger="auto", idle=True))
    tr = make_trace(s, 30_000, data_end=s.turns[-1].ts + 10_000, idle_since=s.turns[100].ts)
    assert tr.idle_obs == {100: True, 200: False}  # fired at 100; eligible at 200 (630k, away 67 min) but did not
    assert tr.tail_gap == 10_000
    r = replay(tr, math.inf, _idle_ctx(True, rate=0.5))
    assert r.idle_compactions == 1
    # never resumed: the summary was still made while the app was open, at the observed rate
    tail_ctx = 30_000 + 12_000 + 100 * 3000
    _, ro, _, rr = PR.lookup("claude-opus-5-5").for_ttl(3600).rates(1)
    assert r.idle_tail_cost == pytest.approx(0.5 * (tail_ctx * rr + 12_000 * ro))


def _idle_sessions(n=6, shift=0):
    """`shift` raises every context: from 400k the default threshold (967k) is reached too."""
    ss = []
    for k in range(n):
        s = _idle_session()
        s.id = f"idle-{k}"
        for t in s.turns:
            t.ts += k * 86_400
            t.ctx += shift
            t.cache_write, t.cache_write_1h = 3000, 3000  # the logs say the one-hour cache
        if k % 2 == 0:  # half of the eligible gaps fired
            s.compactions.append(
                Compaction(turn_index=100, pre_tokens=330_000, ts=s.turns[100].ts, trigger="auto", idle=True)
            )
        ss.append(s)
    return ss


def test_idle_advice_adds_up_the_groups_it_applies_to():
    res = analyze(_idle_sessions(), PR, SimOptions(grid_start=100_000, grid_stop=700_000))
    (g,) = res["groups"]
    assert g["cache_ttl"] == 3600 and g["observed_idle_compactions"] == 3 and g["observed_compactions"] == 0
    assert g["idle"]["applies"] and g["idle"]["enabled"] and g["idle"]["rate"] == 0.5
    assert g["idle"]["alt"]["enabled"] is False
    adv = res["idle_compaction"]
    assert adv["enabled"] and adv["groups"] == ["claude-opus-5-5 · main"]
    bal = adv["profiles"]["balanced"]
    # flipping it off removes exactly the idle compactions at the same thresholds
    assert bal["flipped"]["idle_compactions"] == 0 and bal["configured"]["idle_compactions"] > 0
    assert isinstance(bal["switch"], bool) and set(adv["current"]) >= {"d_usd_per_day", "d_min_per_day", "switch"}
    # with the setting off, the logs say nothing about how often it would fire: assume every eligible gap
    off = analyze(_idle_sessions(), PR, SimOptions(grid_start=100_000, grid_stop=700_000, idle_compaction=False))
    assert off["groups"][0]["idle"]["rate"] == 1.0 and not off["idle_compaction"]["enabled"]


def test_idle_compaction_needs_the_one_hour_cache():
    ss = _idle_sessions()
    for s in ss:
        for t in s.turns:
            t.cache_write_1h = 0
    res = analyze(ss, PR, SimOptions(grid_start=100_000, grid_stop=700_000))
    assert res["groups"][0]["idle"]["applies"] is False and res["idle_compaction"] is None
    assert all(x == 0 for x in res["groups"][0]["idle_compactions"])


def test_idle_review_regressions():
    from compactlab.sim.engine import reselect

    # the default never reaches its threshold here (peak 630k < 967k): no action, whichever way idle compaction is set
    for idle in (None, False):
        res = analyze(_idle_sessions(), PR, SimOptions(grid_start=100_000, grid_stop=700_000, idle_compaction=idle))
        assert res["groups"][0]["no_action_needed"], idle
    # the tail of a set pick is kept, and reselect brings the idle verdict along
    ss = _idle_sessions(shift=400_000)  # now the default does compact
    res = analyze(ss, PR, SimOptions(grid_start=100_000, grid_stop=1_000_000))
    g = res["groups"][0]
    assert not g["no_action_needed"]
    assert "idle_tail_cost" in g and "manual_compactions" in g
    picked = [n for n in g["comparison"] if g["comparison"][n]["action"] == "set"]
    assert picked and all(g["idle"]["profiles"][n]["idle_tail_cost"] == g["idle_tail_cost"][
        g["grid"].index(g["comparison"][n]["threshold"])] for n in picked)  # fmt: skip
    # re-selecting a saved result gives the same idle verdict as a fresh run with those options
    reselect(res, "cost", 0.0)
    fresh = analyze(ss, PR, SimOptions(grid_start=100_000, grid_stop=1_000_000, profile="cost", value_of_time=0.0))
    assert res["idle_compaction"] == fresh["idle_compaction"] and res["idle_compaction"]["profile"] == "cost"


def test_a_logged_idle_compaction_fires_even_on_a_shorter_return():
    s = _idle_session(away_s=52 * 60.0)  # the adapter counts 50 min+; the replay's own rule is 55 min
    s.compactions.append(Compaction(turn_index=100, pre_tokens=330_000, ts=s.turns[100].ts, trigger="auto", idle=True))
    tr = make_trace(s, 30_000, idle_since=0.0)
    assert tr.idle_obs == {100: True}
    assert replay(tr, math.inf, _idle_ctx(True, rate=0.0)).idle_compactions == 1
