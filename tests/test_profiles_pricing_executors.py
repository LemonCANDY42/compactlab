import json

import pytest

from compactlab.eval.deep import DeepPlan, EvalError, MockClient, deep_analyze
from compactlab.eval.executors import ClaudeCodeExecutor, CodexExecutor
from compactlab.model import Session, Turn
from compactlab.pricing import PricingTable
from compactlab.pricing.refresh import parse_litellm, parse_models_dev, refresh
from compactlab.sim.select import PROFILES, select_profile

GRID = [100_000 + 50_000 * i for i in range(19)]  # 100k .. 1M


def _times(fast_at):
    """Wall-clock that is lowest at `fast_at` (seconds), compaction share high for small windows."""
    return [3600 * (1 + abs(T - fast_at) / 200_000) for T in GRID]


def test_profiles_order_and_guardrails(make_group):
    cost = [100 + (T - 200_000) ** 2 / 1e9 for T in GRID]  # cheapest at 200k
    g = make_group(GRID, cost, time_s=_times(600_000), compact_s=[0.0] * len(GRID), emax=1_000_000, days=30)
    p = g["profiles"]
    assert g["profile"] == "balanced" and set(p) == {"cost", "balanced", "time"}
    assert p["cost"]["threshold"] <= p["balanced"]["threshold"] <= p["time"]["threshold"]
    assert p["balanced"]["threshold"] >= 400_000 and p["time"]["threshold"] >= 500_000
    assert [p[n]["value_of_time"] for n in ("cost", "balanced", "time")] == [10.0, 250.0, 600.0]
    # every profile is compared with the default, per day
    v = g["comparison"]["balanced"]["vs_default"]
    assert {"d_time_min_per_day", "compactions_per_day", "added_wait_min_per_day"} <= set(v)


def test_min_window_guardrail_and_cost_warning(make_group):
    cost = [100 + 10 * i for i in range(len(GRID))]  # money says: smallest window
    g = make_group(GRID, cost, emax=1_000_000)
    p = g["profiles"]
    assert p["cost"]["threshold"] == 100_000 and "below_quarter_window" in p["cost"]["flags"]
    assert p["balanced"]["threshold"] == 400_000 and "guardrail_applied" in p["balanced"]["flags"]
    assert p["time"]["threshold"] == 500_000 and p["time"]["unconstrained_threshold"] == 100_000


def test_wait_cap_guardrail(make_group):
    n = len(GRID)
    cost = [100 + 10 * i for i in range(n)]
    compact = [max(0.0, (600_000 - T)) / 100_000 * 60 * 20 for T in GRID]  # 20 min/day per 100k below 600k
    g = make_group(GRID, cost, time_s=[3600 + c for c in compact], compact_s=compact, emax=1_000_000)
    b = g["profiles"]["balanced"]
    assert g["comparison"]["balanced"]["vs_default"]["added_wait_min_per_day"] <= 30
    assert b["threshold"] >= 450_000
    c = g["profiles"]["cost"]  # cost does not enforce the cap, it flags it
    assert c["threshold"] == 100_000 and "wait_over_cap" in c["flags"]


def test_value_of_time_override_only_moves_the_selected_profile(make_group):
    from compactlab.sim.engine import reselect

    cost = [100 + (T - 200_000) ** 2 / 1e9 for T in GRID]
    g = make_group(GRID, cost, time_s=_times(800_000), emax=1_000_000, days=30)
    res = {"groups": [g], "options": {}}
    before = {n: g["profiles"][n]["threshold"] for n in PROFILES}
    reselect(res, "cost", 1000.0)
    after = {n: g["profiles"][n]["threshold"] for n in PROFILES}
    assert g["profile"] == "cost" and after["cost"] > before["cost"]
    assert after["balanced"] == before["balanced"] and after["time"] == before["time"]
    assert select_profile(g, "cost", vot=0.0)["threshold"] == 200_000


def test_keep_default_when_profile_is_not_better(make_group):
    g = make_group(GRID, [100.0] * len(GRID), emax=1_000_000)
    assert all(c["action"] == "keep default" for c in g["comparison"].values())


def test_models_dev_and_litellm_parsers():
    md = {
        "anthropic": {
            "models": {
                "claude-x-1": {
                    "cost": {
                        "input": 4,
                        "output": 20,
                        "cache_read": 0.2,
                        "cache_write": 5,
                        "tiers": [{"input": 8, "output": 40, "tier": {"size": 200000, "type": "context"}}],
                    },
                    "limit": {"context": 1000000},
                },
                "llama": {"cost": {"input": 1, "output": 1}},
            }
        }
    }
    r = parse_models_dev(md)
    assert list(r) == ["claude-x-1"] and r["claude-x-1"]["long_context"]["above_tokens"] == 200000
    ll = {
        "gpt-9": {
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 2e-6,
            "cache_read_input_token_cost": 1e-7,
            "max_input_tokens": 400000,
        },
        "bedrock/claude": {"input_cost_per_token": 1},
    }
    r2 = parse_litellm(ll)
    assert r2 == {"gpt-9": {"input": 1.0, "output": 2.0, "cache_write": 1.0, "cache_read": 0.1, "max_window": 400000}}


def test_refresh_writes_layer_and_offline_ignores_it(tmp_path):
    out = tmp_path / "ref.json"
    n = refresh(
        "models.dev",
        out,
        fetcher=lambda url: {"a": {"models": {"claude-opus-5-5": {"cost": {"input": 9, "output": 9}}}}},
    )
    assert n == 1
    t = PricingTable.load(refreshed=out)
    assert t.lookup("claude-opus-5-5").input == 9 and t.lookup("claude-opus-5-5").source.startswith("models.dev")
    assert PricingTable.load(refreshed=out, offline=True).lookup("claude-opus-5-5").input == 4.0
    with pytest.raises(ValueError):
        refresh("models.dev", out, fetcher=lambda url: {})


def test_executors_parse_output():
    seen = {}

    def runner(cmd, prompt, cwd):
        seen["cmd"], seen["cwd"] = cmd, cwd
        return json.dumps(
            {"result": "hello", "usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 3}}
        )

    c = ClaudeCodeExecutor(runner).complete("claude-sonnet-5-5", "hi", 50)
    assert (c.text, c.input_tokens, c.output_tokens) == ("hello", 15, 3)
    assert seen["cmd"][:2] == ["claude", "-p"] and "compactlab-deep-" in seen["cwd"]
    c2 = CodexExecutor(lambda cmd, p, cwd: "  ok \n").complete("gpt-5-codex", "x" * 40, 50)
    assert c2.text == "ok" and c2.input_tokens == 11


def _session():
    s = Session("x", "claude-code", "main", "p")
    ctx = 30_000
    for i in range(60):
        ctx += 6000
        s.turns.append(Turn(ts=i, ctx=ctx, model="claude-opus-5-5", tool_keys=[f"Read:/proj/f{i % 9}.py"]))
    return s


def _result(make_group):
    g = make_group([100_000, 200_000, 300_000], cost=[3.0, 2.0, 1.0], recall=[0.95, 0.94, 0.93])
    g.update({"model": "claude-opus-5-5", "role": "main", "base_tokens": 30_000})
    return {"groups": [g], "options": {}}


def test_deep_lowers_profiles_when_measured_recall_fails(make_group):
    s = _session()
    plan = DeepPlan([("claude-opus-5-5", "main", s, 100_000, 12), ("claude-opus-5-5", "main", s, 300_000, 45)], 0.1)

    res = deep_analyze(
        plan,
        _result(make_group),
        MockClient(),
        model="claude-sonnet-5-5",
        judge_model="claude-haiku-5-5",
        pricing=PricingTable.load(offline=True),
        budget_usd=5,
        confirm=True,
    )
    assert res["mode"] == "deep" and res["deep"]["groups"][0]["points"]
    # mock answers every probe correctly -> no adjustment
    assert {p["threshold"] for p in res["groups"][0]["profiles"].values()} == {300_000}

    class Wrong(MockClient):
        def complete(self, model, prompt, max_tokens):
            c = super().complete(model, prompt, max_tokens)
            if prompt.startswith("Ground truth"):
                c.text = json.dumps({"correct": False})
            return c

    res2 = deep_analyze(
        plan,
        _result(make_group),
        Wrong(),
        model="claude-sonnet-5-5",
        judge_model="claude-haiku-5-5",
        pricing=PricingTable.load(offline=True),
        budget_usd=5,
        confirm=True,
    )
    g2 = res2["groups"][0]
    assert g2["profiles"]["balanced"]["threshold"] == 100_000 and g2["recommendation"]["threshold"] == 100_000
    assert g2["profiles"]["balanced"]["lite_threshold"] == 300_000
    assert "deep_recall_below_floor" in g2["profiles"]["time"]["flags"]
    assert g2["comparison"]["cost"]["threshold"] == 100_000
    with pytest.raises(EvalError):
        deep_analyze(
            plan,
            _result(make_group),
            MockClient(),
            model="a",
            judge_model="a",
            pricing=PricingTable.load(offline=True),
            budget_usd=5,
            confirm=True,
        )
