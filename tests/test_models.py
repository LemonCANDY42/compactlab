"""Model IDs across platforms, how a model is priced (own row, platform, family, fallback), the opt-in refresh for new
models, Claude Code's window for model IDs it does not recognize, and the notices about all of it."""

import json

import pytest

from compactlab.adapters import get_adapter
from compactlab.model import Session, Turn
from compactlab.pricing import PricingTable
from compactlab.pricing.auto import load_state, maybe_refresh, set_enabled, unpriced
from compactlab.pricing.ids import claude_settings_key, family, nearest, parse
from compactlab.pricing.refresh import parse_litellm, parse_models_dev
from compactlab.sim.baseline import default_threshold, read_current, read_env_int
from compactlab.sim.engine import SimOptions, analyze
from compactlab.term.views import model_lines

PR = PricingTable.load(offline=True)


@pytest.mark.parametrize(
    ("raw", "mid", "platform", "region", "one_m"),
    [
        ("claude-opus-5-5", "claude-opus-5-5", None, None, False),
        ("us.anthropic.claude-fable-5-2-v1:0", "claude-fable-5-2", "amazon-bedrock", "us", False),
        ("global.anthropic.claude-fable-5-1", "claude-fable-5-1", "amazon-bedrock", "global", False),
        ("anthropic.claude-opus-5", "claude-opus-5", "amazon-bedrock", None, False),
        ("arn:aws:bedrock:us-east-1:1:inference-profile/eu.anthropic.claude-sonnet-5-5", "claude-sonnet-5-5",
         "amazon-bedrock", "eu", False),
        ("claude-opus-5-5@20260922", "claude-opus-5-5", "google-vertex-anthropic", None, False),
        ("anthropic/claude-fable-5.2", "claude-fable-5-2", "openrouter", None, False),
        ("openai/gpt-6.1-sol:batch", "gpt-6.1-sol", "openrouter", None, False),
        ("azure/gpt-6.1-sol", "gpt-6.1-sol", "azure", None, False),
        ("claude-opus-5-5-20261001", "claude-opus-5-5", None, None, False),
        ("claude-opus-4-6[1m]", "claude-opus-4-6", None, None, True),
        ("GPT-6.2-Astra", "gpt-6.2-astra", None, None, False),
    ],
)  # fmt: skip
def test_parse_model_ids(raw, mid, platform, region, one_m):
    m = parse(raw)
    assert (m.id, m.platform, m.region, m.one_m) == (mid, platform, region, one_m)


def test_family_and_nearest_version():
    assert family("claude-fable-5-2") == ("claude-fable", (5, 2))
    assert family("gpt-6.1-sol") == ("gpt-sol", (6, 1))
    assert nearest((5, 3), [(5,), (5, 1), (5, 2)]) == (5, 2)  # the newest not newer
    assert nearest((5,), [(4, 5), (5, 5)]) == (5, 5)  # same major version first
    assert nearest((4, 1), [(4, 5), (4, 7)]) == (4, 5)  # only newer ones: the oldest of them
    assert nearest((1,), []) is None
    assert claude_settings_key("us.anthropic.claude-opus-5-5-v1:0") == "claude-opus-5-5"
    assert claude_settings_key("deepseek-v4-pro") == "deepseek-v4-pro"


def test_lookup_kinds():
    own = PR.lookup("claude-fable-5-2")
    assert (own.key, own.kind, own.input, own.cache_read) == ("claude-fable-5-2", "exact", 8.0, 0.2)
    # a Bedrock ID with no Bedrock row: the vendor's list price, marked as such
    br = PR.lookup("us.anthropic.claude-fable-5-2-v1:0")
    assert (br.key, br.kind, br.platform) == ("claude-fable-5-2", "vendor", "amazon-bedrock")
    # a Codex model_provider name works as the platform; the vendor's own platform is "exact"
    assert PR.lookup("gpt-6.1-sol", "openai").kind == "exact"
    assert PR.lookup("gpt-6.1-sol", "azure").kind == "vendor"
    # a newer model of a known family borrows the closest row instead of an unrelated one
    nb = PR.lookup("claude-fable-5-3")
    assert (nb.key, nb.kind) == ("claude-fable-5-2", "family")
    assert PR.lookup("gpt-6.3-sol").key == "gpt-6.2-sol"
    assert PR.lookup("claude-haiku-5").key == "claude-haiku-5-5"
    assert PR.lookup("kimi-k9").kind == "fallback" and PR.lookup("").kind == "fallback"
    # [1m] is the 1M-window variant of a 200K model
    assert PR.lookup("claude-sonnet-4-5[1m]").max_window == 1_000_000
    assert PR.lookup("claude-sonnet-4-5").max_window == 200_000


def test_platform_rows_win_once_refreshed(tmp_path):
    out = tmp_path / "ref.json"
    rows = {"amazon-bedrock:us.claude-fable-5-2": {"input": 8.8, "output": 44.0, "cache_read": 0.22}}
    out.write_text(json.dumps({"version": 2, "models": rows}))
    t = PricingTable.load(refreshed=out)
    p = t.lookup("us.anthropic.claude-fable-5-2-v1:0")
    assert (p.key, p.kind, p.input) == ("amazon-bedrock:us.claude-fable-5-2", "exact", 8.8)
    assert t.lookup("eu.anthropic.claude-fable-5-2").kind == "vendor"  # another region: no row of its own
    assert t.lookup("claude-fable-5-3").key == "claude-fable-5-2"  # platform rows are never borrowed


def test_refresh_keeps_vendor_and_platform_rows():
    cost = {"input": 8, "output": 40, "cache_read": 0.2, "cache_write": 10}
    md = {
        "anthropic": {"models": {"claude-fable-5-2": {"cost": cost, "limit": {"context": 1000000}}}},
        "reseller": {"models": {
            "claude-fable-5-2": {"cost": {**cost, "input": 9}},  # the vendor lists it: its price wins
            "claude-fable-5.2": {"cost": {**cost, "input": 9}},  # a re-spelled copy: dropped too
            "gpt-6.1-sol": {"cost": {"input": 2, "output": 10}},  # the vendor is not in this file: kept
        }},
        "amazon-bedrock": {"models": {"us.anthropic.claude-fable-5-2": {"cost": {**cost, "input": 8.8}},
                                      "meta.llama-9": {"cost": cost}}},
        "openrouter": {"models": {"anthropic/claude-fable-5.2": {"cost": cost},
                                  "anthropic/claude-fable-5.2:batch": {"cost": {**cost, "input": 4}}}},
    }  # fmt: skip
    r = parse_models_dev(md)
    assert r["claude-fable-5-2"]["input"] == 8 and r["claude-fable-5-2"]["provider"] == "anthropic"
    assert r["gpt-6.1-sol"]["input"] == 2 and "claude-fable-5.2" not in r
    assert r["amazon-bedrock:us.claude-fable-5-2"]["input"] == 8.8
    assert "amazon-bedrock:llama-9" in r  # Bedrock rows keep any vendor
    assert r["openrouter:claude-fable-5-2"]["input"] == 8  # :batch is not what an agent pays
    ll = {
        "claude-fable-5-2": {"input_cost_per_token": 8e-6, "output_cost_per_token": 4e-5,
                             "litellm_provider": "anthropic"},
        "us.anthropic.claude-fable-5-2": {"input_cost_per_token": 8.8e-6, "output_cost_per_token": 4.4e-5,
                                          "litellm_provider": "bedrock_converse"},
        "azure/gpt-6.1-sol": {"input_cost_per_token": 2e-6, "output_cost_per_token": 1e-5, "litellm_provider": "azure"},
        "gpt-6-1-sol": {"input_cost_per_token": 3e-6, "output_cost_per_token": 1e-5, "litellm_provider": "someone"},
    }  # fmt: skip
    r2 = parse_litellm(ll)
    assert set(r2) == {"claude-fable-5-2", "amazon-bedrock:us.claude-fable-5-2", "azure:gpt-6.1-sol"}


def _dataset(url):
    return {"anthropic": {"models": {"claude-fable-5-3": {"cost": {"input": 7, "output": 35},
                                                          "limit": {"context": 1000000}}}}}  # fmt: skip


def test_auto_refresh_is_opt_in_and_rate_limited(tmp_path):
    state, out = tmp_path / "auto.json", tmp_path / "ref.json"
    calls = []

    def fetcher(url):
        calls.append(url)
        return _dataset(url)

    def reload():
        return PricingTable.load(refreshed=out)

    seen = [("claude-fable-5-3", None), ("claude-opus-5-5", None)]
    assert unpriced(PR, seen) == ["claude-fable-5-3"]
    t, info = maybe_refresh(PR, seen, 1000.0, reload, state, out, fetcher)
    assert info is None and not calls and t is PR  # off by default: nothing fetched
    set_enabled(True, state)
    t, info = maybe_refresh(PR, seen, 1000.0, reload, state, out, fetcher)
    assert info["refreshed"] and info["source"] == "models.dev" and info["still_missing"] == []
    assert t.lookup("claude-fable-5-3").input == 7 and len(calls) == 1
    # already priced now: nothing to do
    assert maybe_refresh(t, seen, 1100.0, reload, state, out, fetcher)[1] is None
    # still missing after a refresh: retried after a day, not on every run
    seen2 = [("claude-fable-9", None)]
    _, info = maybe_refresh(t, seen2, 5000.0, reload, state, out, fetcher)  # a new model, an hour later: retried
    assert info["refreshed"] and info["still_missing"] == ["claude-fable-9"]  # not in the public data yet
    n = len(calls)
    assert maybe_refresh(t, seen2, 5100.0, reload, state, out, fetcher)[1] is None and len(calls) == n
    assert maybe_refresh(t, seen2, 5000.0 + 86_400, reload, state, out, fetcher)[1] is not None


def test_auto_refresh_falls_back_and_keeps_the_last_file(tmp_path):
    state, out = tmp_path / "auto.json", tmp_path / "ref.json"
    set_enabled(True, state)

    def fetcher(url):
        if "models.dev" in url:
            raise OSError("offline")
        return {"claude-fable-5-3": {"input_cost_per_token": 7e-6, "output_cost_per_token": 3.5e-5,
                                     "litellm_provider": "anthropic"}}  # fmt: skip

    t, info = maybe_refresh(PR, [("claude-fable-5-3", None)], 1.0, lambda: PricingTable.load(refreshed=out), state,
                            out, fetcher)  # fmt: skip
    assert info["refreshed"] and info["source"] == "litellm" and "offline" in info["error"]
    assert t.lookup("claude-fable-5-3").kind == "exact"

    def broken(url):
        raise OSError("down")

    st = state.read_text()
    _, info = maybe_refresh(PR, [("claude-fable-7", None)], 1e9, lambda: PricingTable.load(refreshed=out), state,
                            out, broken)  # fmt: skip
    assert not info["refreshed"] and "down" in info["error"] and out.exists() and load_state(state)["enabled"]
    assert st != state.read_text()


def _turns(model, n=40, step=6000, start=20_000):
    return [Turn(ts=1_800_000_000 + i * 30.0, ctx=start + i * step, output=200, model=model) for i in range(n)]


def test_claude_code_window_for_unrecognized_ids():
    """Claude Code sizes the window from the model ID: a third-party model behind ANTHROPIC_BASE_URL gets 200K unless
    CLAUDE_CODE_MAX_CONTEXT_TOKENS says otherwise, whatever the model's real window."""
    ss = [Session(f"d{i}", "claude-code", "main", "p", turns=_turns("deepseek-v4-pro", n=25)) for i in range(3)]
    g = analyze(ss, PR, SimOptions(grid_start=100_000))["groups"][0]
    assert g["max_window"] == 200_000 and "does not recognize" in g["window_source"]
    assert g["default"]["threshold"] == 200_000 and g["window_note"] is None
    # contexts past 200K mean a larger window is set somewhere compactlab cannot see (e.g. the shell that started it)
    big = [Session("b", "claude-code", "main", "p", turns=_turns("deepseek-v4-pro", n=60))]
    gb = analyze(big, PR, SimOptions(grid_start=100_000))["groups"][0]
    assert gb["max_window"] == gb["window_note"]["peak"] == 374_000 and gb["window_note"]["assumed"] == 200_000
    g2 = analyze(ss + big, PR, SimOptions(grid_start=100_000, claude_max_context=1_000_000))["groups"][0]
    assert g2["max_window"] == 1_000_000 and g2["window_source"] == "CLAUDE_CODE_MAX_CONTEXT_TOKENS"
    # the same model under its own harness keeps its real window
    ds = [Session("x", "dsh", "main", "p", turns=_turns("deepseek-v4-pro"))]
    assert analyze(ds, PR, SimOptions(grid_start=100_000))["groups"][0]["max_window"] == 1_048_576


def test_default_rule_by_window_for_new_claude_models():
    thr, note = default_threshold("claude-code", "claude-opus-6", 1_000_000)
    assert thr == 967_000 and "1M" in note
    assert default_threshold("claude-code", "claude-opus-6", 200_000)[0] == 200_000
    assert default_threshold("claude-code", "kimi-k2.6", 1_000_000)[0] == 1_000_000  # not a model Claude Code sizes


def test_current_setting_matches_platform_ids(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"modelSettings": {"claude-opus-5-5": {"autoCompactWindow": 650000}},
                             "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1_000_000"}}))  # fmt: skip
    assert read_current("claude-code", "us.anthropic.claude-opus-5-5-v1:0", p)[0] == 650_000
    assert read_env_int(p, "CLAUDE_CODE_MAX_CONTEXT_TOKENS", {}) == 1_000_000
    assert read_env_int(None, "CLAUDE_CODE_MAX_CONTEXT_TOKENS", {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "x"}) is None


def test_codex_records_the_model_provider(tmp_path):
    from test_adapters import _write

    def at(sec):
        return f"2026-01-01T00:00:{sec:02d}Z"

    usage = {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 5}
    _write(tmp_path, "s.jsonl", [
        {"timestamp": at(0), "type": "session_meta", "payload": {"id": "s1", "model_provider": "azure"}},
        {"timestamp": at(0), "type": "turn_context", "payload": {"model": "gpt-6.1-sol"}},
        {"timestamp": at(1), "type": "event_msg", "payload": {"type": "user_message", "message": "hi"}},
        {"timestamp": at(3), "type": "event_msg",
         "payload": {"type": "token_count", "info": {"last_token_usage": usage}}},
    ])  # fmt: skip
    (s,) = get_adapter("codex")(tmp_path).sessions()
    assert s.platform == "azure"
    g = analyze([s], PR, SimOptions(grid_start=100_000))["groups"][0]
    assert g["platform"] == "azure" and g["price_kind"] == "vendor"


def test_model_notes_and_lines():
    ss = [Session(f"o{i}", "claude-code", "main", "p", turns=_turns("claude-opus-5-5")) for i in range(2)]
    ss += [Session("f", "claude-code", "main", "p", turns=_turns("claude-fable-5-3", n=120))]  # reaches 650k
    ss += [Session("s", "claude-code", "main", "p", turns=_turns("claude-sonnet-5"))]  # never gets near a threshold
    ss += [Session("k", "claude-code", "main", "p", turns=_turns("kimi-k9"))]
    current = {("claude-code", "claude-opus-5-5"): (650_000, "modelSettings.claude-opus-5-5 in settings.json")}
    res = analyze(ss, PR, SimOptions(grid_start=100_000), current=current)
    notes = res["model_notes"]
    kinds = {p["model"]: p["kind"] for p in notes["prices"]}
    assert kinds == {"claude-fable-5-3": "family", "kimi-k9": "fallback"}
    unset = {u["model"]: u for u in notes["unset"]}
    assert set(unset) == {"claude-fable-5-3"}  # Sonnet 5 has no setting either, but its sessions stay far below
    assert unset["claude-fable-5-3"]["peer"] == "claude-opus-5-5" and unset["claude-fable-5-3"]["peer_value"] == 650_000
    en, zh = model_lines(res, "en"), model_lines(res, "zh")
    assert en[0] == "models" and any("closest model of the same family" in x for x in en)
    assert any("pricing refresh" in x for x in en) and any("autoCompactWindow" in x for x in en)
    assert zh[0] == "模型" and any("借用同族" in x for x in zh)
    res["model_notes"]["auto_refresh"] = True
    assert any("automatic price refresh is on" in x for x in model_lines(res, "en"))
    clean = analyze(ss[:2], PR, SimOptions(grid_start=100_000), current=current)
    assert model_lines(clean, "en") == []


def test_pricing_auto_command(capsys):
    from compactlab.cli.main import main

    assert main(["pricing", "auto"]) == 0
    assert "automatic price refresh: off" in capsys.readouterr().out
    assert main(["pricing", "auto", "on"]) == 0
    assert "automatic price refresh: on" in capsys.readouterr().out
    assert load_state()["enabled"] is True
    assert main(["pricing", "auto", "off"]) == 0 and load_state()["enabled"] is False
    capsys.readouterr()
    assert main(["pricing", "show", "--model", "claude-fable-5-3", "--json", "--offline-pricing"]) == 0
    (row,) = json.loads(capsys.readouterr().out)
    assert (row["row"], row["match"]) == ("claude-fable-5-2", "family")


def test_background_runs_refresh_new_models_and_simulate_stays_offline(tmp_path, monkeypatch):
    from compactlab import synth
    from compactlab.cli.main import main
    from compactlab.pricing import auto

    root = tmp_path / "projects"
    synth.write_claude_code(root, models=("claude-fable-5-3",))
    calls = []

    def fetcher(url):
        calls.append(url)
        return _dataset(url)

    monkeypatch.setattr(auto, "fetch", fetcher)
    common = ["--claude-root", str(root), "--codex-root", str(tmp_path / "none"), "--since-days", "0",
              "--grid", "100k:600k:50k"]  # fmt: skip
    set_enabled(True)
    assert main(["simulate", *common, "--out", str(tmp_path / "r.json")]) == 0
    assert not calls  # only background runs fetch
    state = tmp_path / "state"
    assert main(["recalibrate", *common, "--state-dir", str(state), "--json"]) == 0
    assert len(calls) == 1 and load_state()["last_source"] == "models.dev"
    res = json.loads((state / "service" / "result.json").read_text())
    assert {g["price_kind"] for g in res["groups"]} == {"exact"}
    assert main(["recalibrate", *common, "--state-dir", str(state), "--json"]) == 0
    assert len(calls) == 1  # priced now: no second download


def test_review_fixes_for_lookup_and_refresh():
    # the old substring `match` still works as a last resort (rows written for it, e.g. codex variants)
    p = PR.lookup("gpt-5.1-codex-max")
    assert (p.key, p.kind) == ("gpt-5-codex", "family")
    # Bedrock versions are stripped only where they are versions
    assert parse("deepseek-v3").id == "deepseek-v3"
    assert parse("anthropic.claude-3-haiku-20240307-v1:0:200k").id == "claude-3-haiku"
    assert parse("openai.gpt-oss-120b-1:0").id == "gpt-oss-120b"
    assert parse("ollama/llama3").platform is None and parse("vertex_ai/gemini-3-pro").platform == "google-vertex"
    # a platform row without a 1-hour tier takes the vendor's, scaled to the platform's price
    t = PricingTable({**PR.data, "models": {**PR.data["models"], "amazon-bedrock:us.claude-fable-5-2": {
        "input": 8.8, "output": 44.0, "cache_write": 11.0, "cache_read": 0.22}}})  # fmt: skip
    br = t.lookup("us.anthropic.claude-fable-5-2")
    assert br.kind == "exact" and br.for_ttl(3600).cache_write == pytest.approx(17.6)
    # LiteLLM: a vendor's own rows under its prefix are kept; an Azure data-zone copy does not win
    ll = {
        "deepseek/deepseek-v4-pro": {"input_cost_per_token": 6.6e-7, "output_cost_per_token": 2e-6,
                                     "litellm_provider": "deepseek"},
        "azure/eu/gpt-6.1-sol": {"input_cost_per_token": 2.2e-6, "output_cost_per_token": 1.1e-5,
                                 "litellm_provider": "azure"},
        "azure/gpt-6.1-sol": {"input_cost_per_token": 2e-6, "output_cost_per_token": 1e-5, "litellm_provider": "azure"},
    }  # fmt: skip
    r = parse_litellm(ll)
    assert r["deepseek-v4-pro"]["input"] == 0.66 and r["azure:gpt-6.1-sol"]["input"] == 2.0
    # a model only resellers list yet is kept; a re-spelled copy of a vendor's model is not
    md = {
        "anthropic": {"models": {"claude-fable-5-2": {"cost": {"input": 8, "output": 40}}}},
        "reseller": {"models": {"claude-fable-5-3": {"cost": {"input": 7, "output": 35}},
                                "claude-fable-5.2": {"cost": {"input": 9, "output": 45}}}},
    }  # fmt: skip
    assert set(parse_models_dev(md)) == {"claude-fable-5-2", "claude-fable-5-3"}


def test_current_setting_does_not_read_the_1m_variant(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"modelSettings": {"claude-opus-4-6[1m]": {"autoCompactWindow": 800000}}}))
    assert read_current("claude-code", "claude-opus-4-6", p)[0] is None
    assert read_current("claude-code", "claude-opus-4-6[1m]", p)[0] == 800_000


def test_newer_bundled_prices_beat_an_older_refresh(tmp_path):
    out = tmp_path / "ref.json"
    rows = {"claude-fable-5-2": {"input": 99, "output": 99}, "claude-new-9": {"input": 1, "output": 2}}
    out.write_text(json.dumps({"version": 2, "retrieved": "2000-01-01", "models": rows}))
    t = PricingTable.load(refreshed=out)
    assert t.lookup("claude-fable-5-2").input == 8.0  # the bundle is newer
    assert t.lookup("claude-new-9").input == 1  # models the bundle lacks still come from the refresh
    out.write_text(json.dumps({"version": 2, "retrieved": "2999-01-01", "models": rows}))
    assert PricingTable.load(refreshed=out).lookup("claude-fable-5-2").input == 99


def test_refresh_logs_price_and_window_changes(tmp_path):
    from compactlab.pricing.refresh import recent_changes, refresh

    out = tmp_path / "ref.json"
    out.write_text(json.dumps({"version": 2, "models": {"claude-fable-5-2": {"input": 8, "output": 40,
                                                                              "max_window": 1000000}}}))  # fmt: skip
    moved: list = []
    ds = {"anthropic": {"models": {"claude-fable-5-2": {"cost": {"input": 6, "output": 40},
                                                        "limit": {"context": 2000000}}}}}  # fmt: skip
    refresh("models.dev", out, None, lambda url: ds, moved)
    assert {(c["field"], c["old"], c["new"]) for c in moved} == {("input", 8, 6.0), ("max_window", 1000000, 2000000)}
    assert recent_changes(out, {"claude-fable-5-2"})[-1]["field"] in ("input", "max_window")
    assert recent_changes(out, {"other"}) == []


def test_auto_refresh_runs_weekly_and_gives_up_on_unlisted_models(tmp_path):
    state, out = tmp_path / "auto.json", tmp_path / "ref.json"
    set_enabled(True, state)
    calls = []

    def fetcher(url):
        calls.append(url)
        return _dataset(url)

    def reload():
        return PricingTable.load(refreshed=out)

    known = [("claude-opus-5-5", None)]
    t, info = maybe_refresh(PR, known, 1e9, reload, state, out, fetcher)  # never refreshed: stale
    assert info["reason"] == "weekly" and len(calls) == 1
    import os

    os.utime(out, (1e9, 1e9))
    assert maybe_refresh(t, known, 1e9 + 86_400, reload, state, out, fetcher)[1] is None  # fresh
    assert maybe_refresh(t, known, 1e9 + 8 * 86_400, reload, state, out, fetcher)[1]["reason"] == "weekly"
    # a model no public file lists (a private inference profile): tried a few times, then left alone
    odd = [("arn:aws:bedrock:us-east-1:1:application-inference-profile/abc", None)]
    now = 2e9
    for _ in range(10):
        os.utime(out, (now, now))
        maybe_refresh(t, odd, now, reload, state, out, fetcher)
        now += 2 * 86_400
    assert load_state(state)["attempts"] == {odd[0][0]: 5}


def test_saved_result_notes_price_changes(tmp_path, capsys):
    from compactlab.cli.main import _load_result
    from compactlab.sim.engine import price_drift

    ss = [Session("o", "claude-code", "main", "p", turns=_turns("claude-opus-5-5"))]
    res = analyze(ss, PR, SimOptions(grid_start=100_000))
    assert price_drift(res, PR) == []
    res["groups"][0]["price"]["cache_read"] = 0.5  # as if made when the price was higher
    (d,) = price_drift(res, PR)
    assert (d["field"], d["old"], d["new"]) == ("cache_read", 0.5, 0.2)
    f = tmp_path / "r.json"
    f.write_text(json.dumps(res))
    _load_result(str(f))
    assert "changed since r.json was made: claude-opus-5-5 cache_read 0.5 -> 0.2" in capsys.readouterr().err


def test_window_changes_and_contradictions():
    from compactlab.sim.engine import group_window

    def codex(i, w):
        return Session(f"c{i}", "codex", "main", "p", turns=_turns("gpt-6.1-sol")[:3], context_window=w)

    for i, s in enumerate(ss := [codex(i, 258_000) for i in range(12)] + [codex(12 + i, 475_000) for i in range(10)]):
        for t in s.turns:
            t.ts += i * 3600
    w, src, note = group_window("codex", "gpt-6.1-sol", ss, 1_050_000, SimOptions())
    assert w == 475_000 and note["kind"] == "window_changed" and (note["from"], note["to"]) == (258_000, 475_000)
    mixed = [codex(i, 258_000 if i % 2 else 475_000) for i in range(22)]
    for i, s in enumerate(mixed):
        for t in s.turns:
            t.ts += i * 3600
    assert group_window("codex", "gpt-6.1-sol", mixed, 1_050_000, SimOptions())[2] is None  # no clean switch
    # a Claude model the price data lists at 200K whose contexts went past it: the 1M window
    big = [Session("b", "claude-code", "main", "p", turns=_turns("claude-sonnet-4-5", n=60))]
    w, src, note = group_window("claude-code", "claude-sonnet-4-5", big, 200_000, SimOptions())
    assert w == 1_000_000 and note["kind"] == "peak_over_window" and "beyond" in src


def test_default_drift_is_flagged():
    from compactlab.model import Compaction
    from compactlab.sim.engine import default_drift

    s = Session("d", "claude-code", "main", "p", turns=_turns("claude-opus-5-5"))
    s.compactions = [Compaction(turn_index=5 + i, pre_tokens=790_000 + i * 1000, ts=float(i), trigger="auto")
                     for i in range(6)]  # fmt: skip
    d = default_drift([s], 967_000)
    assert d["observed"] == 792_500 and d["n"] == 6 and d["assumed"] == 967_000
    assert default_drift([s], 800_000) is None  # within 10%
    s.compactions = s.compactions[:4]
    assert default_drift([s], 967_000) is None  # too few


def _codex(make_group, model, role, cost, span, cap=427_500, grid=None):
    grid = grid or [100_000 + 25_000 * i for i in range(14)]  # 100k..425k
    g = make_group(grid, cost, emax=cap, days=4.0, default_thr=cap)
    g.update(harness="codex", model=model, role=role, span=span, n_turns=1000, n_sessions=10)
    return g


def test_superseded_models(make_group):
    from compactlab.sim.engine import superseded

    flat = [10.0] * 14
    day = 86_400.0
    old = _codex(make_group, "gpt-6.1-sol", "main", flat, [0, 20 * day])
    new = _codex(make_group, "gpt-6.2-sol", "main", flat, [19 * day, 25 * day])
    other = _codex(make_group, "gpt-6.2-astra", "main", flat, [19 * day, 25 * day])
    assert superseded([old, new, other]) == {("codex", "gpt-6.1-sol"): "gpt-6.2-sol"}
    both = _codex(make_group, "gpt-6.1-sol", "main", flat, [0, 25 * day - 1])  # still used alongside the new one
    assert superseded([both, new]) == {}


def test_codex_one_limit_across_the_models_in_use(make_group):
    """Codex has one limit for every model. A replaced model that would want a low value no longer decides it; the
    models in use score it together, each at min(value, its own clamp)."""
    from compactlab.cli.main import _recs
    from compactlab.recommend import Omitted, generate
    from compactlab.sim.engine import global_keys

    day = 86_400.0
    cheap_low = [5.0 + i for i in range(14)]  # strongly prefers the lowest limit
    prefers_default = [20.0 - 0.2 * i for i in range(14)]  # gets cheaper as the limit rises
    old = _codex(make_group, "gpt-6.1-sol", "main", cheap_low, [0, 20 * day])
    new = _codex(make_group, "gpt-6.2-sol", "main", prefers_default, [19 * day, 25 * day])
    small = _codex(make_group, "gpt-6.2-astra", "main", prefers_default[:6], [19 * day, 25 * day], cap=232_500,
                   grid=[100_000 + 25_000 * i for i in range(6)])  # fmt: skip
    groups = [old, new, small]
    res = {"groups": groups, "options": {}, "global_keys": global_keys(groups, {})}
    gk = res["global_keys"]["codex"]
    assert gk["superseded"] == {"gpt-6.1-sol": "gpt-6.2-sol"}
    assert gk["models"] == ["gpt-6.2-sol · main", "gpt-6.2-astra · main"]
    assert gk["profiles"]["balanced"]["action"] == "keep default"
    assert {g["comparison"]["balanced"]["action"] for g in groups} == {"keep default"}  # the old model's 'set' too
    (om,) = generate(res, "codex", "global", "balanced")
    assert isinstance(om, Omitted) and "keep default" in om.reason and "replaced by gpt-6.2-sol" in om.reason
    for g in groups:
        g["profile"] = "balanced"
    assert _recs(res) == []
    # the models in use want a low limit: one value, clamped per model, written once
    new2 = _codex(make_group, "gpt-6.2-sol", "main", cheap_low, [19 * day, 25 * day])
    small2 = _codex(make_group, "gpt-6.2-astra", "main", cheap_low[:6], [19 * day, 25 * day], cap=232_500,
                    grid=[100_000 + 25_000 * i for i in range(6)])  # fmt: skip
    groups = [new2, small2]
    res = {"groups": groups, "options": {}, "global_keys": global_keys(groups, {})}
    gp = res["global_keys"]["codex"]["profiles"]["balanced"]
    assert gp["action"] == "set" and gp["threshold"] < 232_500
    assert {g["comparison"]["balanced"]["threshold"] for g in groups} == {gp["threshold"]}
    for g in groups:
        g["profile"] = "balanced"
    assert _recs(res) == [{"harness": "codex", "model": "gpt-6.2-sol", "value": gp["threshold"]}]
    (sn,) = [s for s in generate(res, "codex", "global", "balanced") if not isinstance(s, Omitted)]
    assert sn.content == f"model_auto_compact_token_limit = {gp['threshold']}"
    assert any("chosen across gpt-6.2-sol · main, gpt-6.2-astra · main" in n for n in sn.notes)


def test_codex_auto_apply_writes_its_one_limit_once(make_group, tmp_path):
    from compactlab.service.policy import Consent, Rules, run_auto_apply
    from compactlab.sim.engine import global_keys

    day = 86_400.0
    low = [5.0 + i for i in range(14)]
    groups = [_codex(make_group, m, "main", low, [0, 25 * day]) for m in ("gpt-6.2-sol", "gpt-6.2-astra")]
    for g in groups:
        g.update(days=30.0, n_turns=5000, time_model={"turn_source": "own", "compact_source": "own"})
    res = {"groups": groups, "options": {}, "global_keys": global_keys(groups, {})}
    cfg = tmp_path / "config.toml"
    cfg.write_text('model = "gpt-6.2-sol"\n')
    consent = Consent("codex", str(cfg), ["gpt-6.2-sol", "gpt-6.2-astra"])
    out = run_auto_apply(res, consent, tmp_path, Rules(min_days=1, min_turns=1), now=0.0, dry_run=True)
    assert sum(d["action"] == "apply" for d in out["decisions"]) == 2  # both models agree on the value
    assert out["diff"].count("+model_auto_compact_token_limit") == 1 and not out.get("rejected")


def test_codex_one_limit_keeps_the_guardrails(make_group):
    """The shared Codex value obeys --max-threshold, the profile's added-wait cap and measured (deep) recall, like a
    single group's pick."""
    from compactlab.sim.engine import global_keys

    day = 86_400.0
    low = [5.0 + i for i in range(14)]  # wants the lowest limit
    grid = [100_000 + 25_000 * i for i in range(14)]

    def one():
        g = _codex(make_group, "gpt-6.2-sol", "main", low, [0, 25 * day])
        return g, [g]

    g, gs = one()
    free = global_keys(gs, {})["codex"]["profiles"]["cost"]
    assert free["action"] == "set" and free["threshold"] == 100_000
    capped = global_keys(gs, {"max_threshold": 150_000})["codex"]["profiles"]["cost"]
    assert capped["threshold"] <= 150_000
    g, gs = one()
    g["time_compact_s"] = [4 * 3600.0 * (14 - i) for i in range(14)]  # every binding value adds 60+ min/day
    g["default"]["time_compact_s"] = 0.0
    bal = global_keys(gs, {})["codex"]["profiles"]["balanced"]
    assert bal["action"] == "keep default"  # every binding value breaks the 30 min/day wait cap
    g, gs = one()
    g["measured"] = [{"threshold": T, "measured_recall": 0.5 if T < 200_000 else 0.95} for T in grid]
    deep = global_keys(gs, {})["codex"]["profiles"]["cost"]
    assert deep["threshold"] >= 200_000  # measured recall rules out the lower values


def test_1m_variant_keeps_its_own_settings_key(tmp_path):
    assert claude_settings_key("claude-opus-4-6[1m]") == "claude-opus-4-6[1m]"
    assert claude_settings_key("claude-opus-4-6-20260101") == "claude-opus-4-6"
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"modelSettings": {"claude-opus-4-6": {"autoCompactWindow": 650000},
                                               "claude-opus-4-6[1m]": {"autoCompactWindow": 800000}}}))  # fmt: skip
    assert read_current("claude-code", "claude-opus-4-6[1m]", p)[0] == 800_000
    assert read_current("claude-code", "claude-opus-4-6", p)[0] == 650_000
