import json

import pytest

from compactlab.eval.executors import ClaudeCodeExecutor, CodexExecutor
from compactlab.eval.hyper import EvalError, HyperPlan, MockClient, hyper_analyze
from compactlab.model import Session, Turn
from compactlab.pricing import PricingTable
from compactlab.pricing.refresh import parse_litellm, parse_models_dev, refresh
from compactlab.sim.select import tiers


def test_headroom_rules():
    grid = [100_000, 125_000, 150_000, 175_000, 200_000, 225_000, 250_000]
    comb = [10.0, 10.2, 10.5, 11.0, 11.4, 12.5, 14.0]
    rec = [0.95] * 6 + [0.85]
    comp = [40, 32, 26, 22, 19, 16, 14]
    t = tiers(grid, comb, rec, comp, 0.9, 100_000, tolerance=0.12, step=25_000)
    assert t["standard"]["threshold"] == 100_000
    assert t["headroom"]["threshold"] == 175_000            # 11.0 <= 11.2; 200k is 11.4 > 11.2
    assert t["headroom"]["fewer_compactions"] == 18 and abs(t["headroom"]["extra_cost_pct"] - 0.10) < 1e-9
    assert tiers(grid, comb, rec, comp, 0.9, 100_000, tolerance=0.5)["headroom"]["threshold"] == 225_000  # floor
    assert tiers(grid, comb, rec, comp, 0.9, 100_000, tolerance=0.5, cap=150_000)["headroom"]["threshold"] == 150_000
    assert tiers(grid, comb, rec, comp, 0.9, 100_000, tolerance=0.5, step=50_000)["headroom"]["threshold"] == 200_000
    assert tiers(grid, comb, rec, comp, 0.9, 150_000, tolerance=0.0)["headroom"]["threshold"] == 150_000  # never below


def test_models_dev_and_litellm_parsers():
    md = {"anthropic": {"models": {"claude-x-1": {"cost": {"input": 4, "output": 20, "cache_read": 0.2, "cache_write": 5,
                                                          "tiers": [{"input": 8, "output": 40, "tier": {"size": 200000, "type": "context"}}]},
                                                 "limit": {"context": 1000000}}, "llama": {"cost": {"input": 1, "output": 1}}}}}
    r = parse_models_dev(md)
    assert list(r) == ["claude-x-1"] and r["claude-x-1"]["long_context"]["above_tokens"] == 200000
    ll = {"gpt-9": {"input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6, "cache_read_input_token_cost": 1e-7,
                    "max_input_tokens": 400000}, "bedrock/claude": {"input_cost_per_token": 1}}
    r2 = parse_litellm(ll)
    assert r2 == {"gpt-9": {"input": 1.0, "output": 2.0, "cache_write": 1.0, "cache_read": 0.1, "max_window": 400000}}


def test_refresh_writes_layer_and_offline_ignores_it(tmp_path):
    out = tmp_path / "ref.json"
    n = refresh("models.dev", out, fetcher=lambda url: {"a": {"models": {"claude-opus-5-5": {"cost": {"input": 9, "output": 9}}}}})
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
        return json.dumps({"result": "hello", "usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 3}})
    c = ClaudeCodeExecutor(runner).complete("claude-sonnet-5-5", "hi", 50)
    assert (c.text, c.input_tokens, c.output_tokens) == ("hello", 15, 3)
    assert seen["cmd"][:2] == ["claude", "-p"] and "compactlab-hyper-" in seen["cwd"]
    c2 = CodexExecutor(lambda cmd, p, cwd: "  ok \n").complete("gpt-5-codex", "x" * 40, 50)
    assert c2.text == "ok" and c2.input_tokens == 11


def _session():
    s = Session("x", "claude-code", "main", "p")
    ctx = 30_000
    for i in range(60):
        ctx += 6000
        s.turns.append(Turn(ts=i, ctx=ctx, model="claude-opus-5-5", tool_keys=[f"Read:/proj/f{i % 9}.py"]))
    return s


def _result():
    grid = [100_000, 200_000, 300_000]
    return {"groups": [{"model": "claude-opus-5-5", "role": "main", "grid": grid, "recall": [0.95, 0.94, 0.93],
                        "recommendation": {"recall_floor": 0.9},
                        "tiers": {"standard": {"threshold": 100_000}, "headroom": {"threshold": 300_000}},
                        "gears": {"eco": 100_000, "recall": 100_000, "headroom": 300_000}, "base_tokens": 30_000}]}


def test_hyper_lowers_headroom_when_measured_recall_fails():
    s = _session()
    plan = HyperPlan([("claude-opus-5-5", "main", s, 100_000, 12), ("claude-opus-5-5", "main", s, 300_000, 45)], 0.1)

    res = hyper_analyze(plan, _result(), MockClient(), model="claude-sonnet-5-5", judge_model="claude-haiku-5-5",
                        pricing=PricingTable.load(offline=True), budget_usd=5, confirm=True)
    assert res["mode"] == "hyper" and res["hyper"]["groups"][0]["points"]
    # mock answers every probe correctly -> no adjustment
    assert res["groups"][0]["tiers"]["headroom"]["threshold"] == 300_000

    class Wrong(MockClient):
        def complete(self, model, prompt, max_tokens):
            c = super().complete(model, prompt, max_tokens)
            if prompt.startswith("Ground truth"):
                c.text = json.dumps({"correct": False})
            return c
    res2 = hyper_analyze(plan, _result(), Wrong(), model="claude-sonnet-5-5", judge_model="claude-haiku-5-5",
                         pricing=PricingTable.load(offline=True), budget_usd=5, confirm=True)
    assert res2["groups"][0]["tiers"]["headroom"]["threshold"] == 100_000
    assert res2["groups"][0]["tiers"]["headroom"]["lite_threshold"] == 300_000
    with pytest.raises(EvalError):
        hyper_analyze(plan, _result(), MockClient(), model="a", judge_model="a", pricing=PricingTable.load(offline=True),
                      budget_usd=5, confirm=True)
