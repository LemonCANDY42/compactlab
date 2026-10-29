import json

from compactlab.adapters import get_adapter
from compactlab.cli.main import main
from compactlab.model import Compaction, Session, Turn
from compactlab.pricing import PricingTable
from compactlab.sim.baseline import default_threshold, effective_max, read_current
from compactlab.sim.engine import SimOptions, analyze
from compactlab.sim.timing import fit


def test_turn_and_compaction_latency_fit(claude_root):
    ss = [s for s in get_adapter("claude-code")(claude_root).sessions() if s.role == "main"]
    tm = fit(ss)
    assert tm.n_turn_samples > 500 and tm.turn_confidence == "ok"
    assert tm.slope_s_per_100k > 0 and tm.turn(400_000) >= tm.turn(100_000)
    assert tm.n_compact_samples >= 5 and tm.compact(500_000) > tm.compact(100_000)


def test_thin_time_data_falls_back_to_prior():
    s = Session("x", "claude-code", "main", "p", turns=[Turn(ts=None, ctx=1000)])
    tm = fit([s])
    assert tm.turn_confidence == "prior" and tm.compact_confidence == "prior"
    assert tm.turn(200_000) > tm.turn(0)
    s2 = Session(
        "y", "claude-code", "main", "p", turns=[Turn(ts=10.0 + i, start_ts=5.0 + i, ctx=1000) for i in range(8)]
    )
    s2.compactions = [Compaction(1, pre_tokens=100_000, duration_s=30)]
    tm2 = fit([s2])
    assert tm2.turn_confidence == "low" and tm2.compact_confidence == "prior"
    assert tm2.turn_source == "prior" and not tm2.xs  # a thin curve is not used


def test_low_confidence_compaction_fit_falls_back(claude_root):
    ss = [s for s in get_adapter("claude-code")(claude_root).sessions() if s.role == "main"]
    pool = fit(ss)
    assert pool.compact_confidence == "ok" and pool.compact_source == "own"
    few = Session("z", "claude-code", "main", "p", turns=[Turn(ts=10.0, start_ts=5.0, ctx=1000)])
    few.compactions = [Compaction(0, pre_tokens=100_000 * (i + 1), duration_s=5) for i in range(6)]  # 6 samples
    tm = fit([few], pool)
    assert tm.compact_confidence == "low" and tm.compact_source == "pooled" and tm.compact_ab == pool.compact_ab
    alone = fit([few])
    assert alone.compact_source == "prior" and alone.compact(300_000) >= 100  # pessimistic prior


def test_harness_defaults():
    assert default_threshold("claude-code", "claude-opus-5-5", 1_000_000)[0] == 967_000
    assert default_threshold("claude-code", "claude-sonnet-4-6", 1_000_000)[0] == 200_000
    assert default_threshold("claude-code", "claude-haiku-4-5", 200_000)[0] == 200_000
    assert default_threshold("codex", "gpt-5-codex", 400_000)[0] == 360_000
    assert effective_max("codex", 400_000) == 360_000 and effective_max("claude-code", 1_000_000) == 1_000_000


def test_read_current(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(
        json.dumps({"autoCompactWindow": 400000, "modelSettings": {"claude-opus-5-5": {"autoCompactWindow": 500000}}})
    )
    assert read_current("claude-code", "claude-opus-5-5", f)[0] == 500_000
    assert read_current("claude-code", "claude-sonnet-5-5", f)[0] == 400_000
    f.write_text(json.dumps({"modelSettings": {"claude-opus-5-5": {"autoCompactWindow": "auto"}}}))
    assert read_current("claude-code", "claude-opus-5-5", f)[0] is None
    t = tmp_path / "config.toml"
    t.write_text(
        'model = "x"\nmodel_auto_compact_token_limit = 250_000\n[profiles.a]\nmodel_auto_compact_token_limit = 1\n'
    )
    assert read_current("codex", "gpt-5-codex", t)[0] == 250_000
    assert read_current("codex", "gpt-5-codex", tmp_path / "missing.toml")[0] is None


def test_analyze_baselines_and_no_action(claude_root, codex_root):
    ss = list(get_adapter("claude-code")(claude_root).sessions()) + list(get_adapter("codex")(codex_root).sessions())
    cur = {("claude-code", "claude-opus-5-5"): (600_000, "test")}
    res = analyze(ss, PricingTable.load(offline=True), SimOptions(), current=cur)
    by = {(g["model"], g["role"]): g for g in res["groups"]}
    main_g = by[("claude-opus-5-5", "main")]
    assert main_g["default"]["threshold"] == 967_000 and main_g["current"]["configured"] == 600_000
    hd = main_g["comparison"]["balanced"]
    assert hd["action"] == "set" and hd["vs_default"]["ref_compactions"] == main_g["default"]["compactions"]
    assert {"d_cost", "d_time_s", "time_saved_context_s", "time_added_compaction_s", "d_recall"} <= set(
        hd["vs_default"]
    )
    assert hd["vs_current"] and set(main_g["comparison"]) == {"cost", "balanced", "time"}
    sub = by[("claude-opus-5-5", "subagent")]
    assert sub["no_action_needed"] and sub["comparison"]["balanced"]["action"] == "no action needed"
    cx = by[("gpt-5-codex", "main")]
    assert cx["max_window"] == 400_000 and max(cx["grid"]) <= 360_000 and cx["window_source"] == "observed in logs"
    zero = analyze(ss, PricingTable.load(offline=True), SimOptions(value_of_time=0))
    g0 = next(g for g in zero["groups"] if g["model"] == "claude-opus-5-5" and g["role"] == "main")
    assert g0["combined"] == g0["combined_cost"] and g0["profiles"]["balanced"]["value_of_time"] == 0
    assert g0["profiles"]["time"]["value_of_time"] == 600


def test_cli_summary_languages_and_apply_skips_no_action(tmp_path, claude_root, capsys):
    out = tmp_path / "r.json"
    base = ["simulate", "--claude-root", str(claude_root), "--harness", "claude-code", "--since-days", "0"]
    base += ["--no-cache", "--out", str(out), "--no-current"]
    for lang, marker in (("en", "no action needed"), ("zh", "无需调整"), ("both", "无需调整")):
        assert main([*base, "--lang", lang]) == 0
        text = capsys.readouterr().out
        assert marker in text
        assert ("default" in text) == (lang != "zh")
    s = tmp_path / "settings.json"
    s.write_text("{}")
    assert main(["apply", "--result", str(out), "--claude-settings", str(s), "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert all(c["key"].endswith("autoCompactWindow") for c in plan["changes"])
    assert {c["key"].split(".")[1] for c in plan["changes"]} <= {"claude-opus-5-5", "claude-sonnet-5-5"}
