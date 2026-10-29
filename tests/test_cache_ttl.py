"""The replay prices the prompt cache at the tier the logs say it was billed at (5-minute or 1-hour)."""

import json

import pytest

from compactlab.adapters.claude_code import ClaudeCodeAdapter
from compactlab.model import Session, Turn
from compactlab.pricing import PricingTable
from compactlab.sim.engine import _actual_cost, group_ttl


def _session(turns):
    return Session(id="s", harness="claude-code", role="main", path="p", turns=turns)


def test_claude_parser_reads_the_write_tier(tmp_path):
    usage = {"input_tokens": 10, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 500,
             "cache_creation": {"ephemeral_1h_input_tokens": 400, "ephemeral_5m_input_tokens": 100},
             "output_tokens": 5}  # fmt: skip
    msg = {"id": "m1", "role": "assistant", "model": "claude-opus-5-5", "usage": usage, "content": []}
    rec = {"type": "assistant", "timestamp": "2026-09-01T12:00:00Z", "message": msg}
    p = tmp_path / "projects" / "x" / "s.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(rec) + "\n")
    (t,) = ClaudeCodeAdapter(tmp_path / "projects").parse_file(p).turns
    assert (t.ctx, t.cache_write, t.cache_write_1h) == (1510, 500, 400)


def test_price_for_ttl():
    p = PricingTable.load(offline=True).lookup("claude-opus-5-5")
    assert (p.cache_write, p.for_ttl(300).cache_write, p.for_ttl(3600).cache_write) == (5.0, 5.0, 8.0)
    gpt = PricingTable.load(offline=True).lookup("gpt-5-codex")
    assert gpt.for_ttl(3600) is gpt  # no 1-hour tier: unchanged
    haiku = PricingTable.load(offline=True).lookup("claude-haiku-5-5").for_ttl(3600)
    assert (haiku.cache_write, haiku.long_cache_write) == (0.2, 1.0)


def test_group_ttl_from_logs():
    hour = _session([Turn(ts=0, ctx=1000, cache_write=900, cache_write_1h=900)])
    five = _session([Turn(ts=0, ctx=1000, cache_write=900)])
    mixed = _session([Turn(ts=0, ctx=1000, cache_write=900, cache_write_1h=300)])
    assert group_ttl([hour], None, 300) == (3600, "logs: 100% of cache writes at the 1-hour tier")
    assert group_ttl([five], None, 300) == (300, "pricing default")
    assert group_ttl([mixed], None, 300)[0] == 300
    assert group_ttl([hour], 600, 300) == (600, "--cache-ttl")


def test_actual_cost_prices_each_tier():
    p = PricingTable.load(offline=True).lookup("claude-opus-5-5")
    t = Turn(ts=0, ctx=1_000_000, cache_write=1_000_000, cache_write_1h=600_000)
    assert _actual_cost(_session([t]), p) == pytest.approx(0.4 * 5.0 + 0.6 * 8.0)


def test_analyze_replays_hour_cache_with_hour_write_price():
    from compactlab.sim.engine import SimOptions, analyze

    turns = [Turn(ts=1_790_000_000 + i * 1800, ctx=50_000 + i * 20_000, cache_read=40_000 + i * 20_000,
                  cache_write=10_000, cache_write_1h=10_000, output=100, model="claude-opus-5-5")
             for i in range(60)]  # fmt: skip
    res = analyze([_session(turns)], PricingTable.load(offline=True), SimOptions(grid_step=100_000))
    (g,) = res["groups"]
    assert g["cache_ttl"] == 3600 and g["cache_ttl_source"].startswith("logs: 100%")
    five = analyze([_session(turns)], PricingTable.load(offline=True), SimOptions(grid_step=100_000, ttl_seconds=300))
    # 30-minute gaps: cold on every turn with a 5-minute cache, warm with a 1-hour one
    assert five["groups"][0]["baseline_cost"] > g["baseline_cost"]
