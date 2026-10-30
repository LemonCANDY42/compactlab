import json
import time

import pytest

from compactlab import verify
from compactlab.cli import main
from compactlab.model import Compaction, Session, Turn
from compactlab.pricing import PricingTable

T0 = 1_760_000_000.0  # the change is applied here
DAY = 86400.0
MODEL = "claude-opus-5-5"


def _session(sid, start, compact_at=None, turns=60):
    """Context grows 12k/turn from 40k, one turn a minute; with `compact_at` it drops to 40k when it reaches it."""
    ts, ctx, out, comps = [], 40_000, [], []
    for i in range(turns):
        if compact_at and ctx >= compact_at:
            comps.append(Compaction(turn_index=i, pre_tokens=ctx, ts=start + i * 60))
            ctx = 40_000
        ctx += 12_000
        out.append(
            Turn(ts=start + i * 60, ctx=ctx, cache_read=ctx - 12_000, cache_write=12_000, output=400, model=MODEL)
        )
        ts.append(ctx)
    return Session(
        id=sid, harness="claude-code", role="main", path="", turns=out, compactions=comps, context_window=1_000_000
    )


def _sessions(after_compact_at=300_000, n=4):
    before = [_session(f"b{i}", T0 - (i + 1) * DAY * 0.9 - 3600) for i in range(n)]
    after = [_session(f"a{i}", T0 + (i + 1) * DAY * 0.9 + 3600, compact_at=after_compact_at) for i in range(n)]
    return before + after


def _manifest(old=None, new=300_000):
    change = {"harness": "claude-code", "key": f"modelSettings.{MODEL}.autoCompactWindow", "model": MODEL,
              "old": old, "new": new}  # fmt: skip
    return {"id": "m1", "created": T0, "entries": [{"file": "settings.json", "changes": [change]}]}


def test_split_drops_straddling_and_untimed_sessions():
    before = _session("b", T0 - 7200, turns=10)
    after = _session("a", T0 + 3600, turns=10)
    straddle = _session("s", T0 - 600, turns=30)
    untimed = Session("u", "claude-code", "main", "", turns=[Turn(ts=None, ctx=10)])
    b, a, dropped = verify.split([before, after, straddle, untimed], T0)
    assert [s.id for s in b] == ["b"] and [s.id for s in a] == ["a"] and dropped == 2


def test_change_is_seen_when_the_threshold_took_effect():
    res = verify.verify(_sessions(), _manifest(), PricingTable.load(offline=True))
    (g,) = res["groups"]
    assert (
        g["old"] is None and g["new"] == 300_000 and g["before"]["compactions"] == 0 and g["after"]["compactions"] > 0
    )
    assert g["in_effect"] == "yes"
    # the replay of the before sessions at 300k predicts roughly the extra compactions the after sessions had
    assert g["predicted"]["compactions_per_kturn"] == pytest.approx(g["observed"]["compactions_per_kturn"], rel=0.6)
    assert g["observed"]["cost_pct"] < 0 and g["predicted"]["cost_pct"] < 0  # both say cheaper per turn
    assert g["verdict"] in ("consistent", "less", "more")
    text = verify.render(res)
    assert "cost per 1k turns" in text and "Wall-clock is not verified" in text


def test_setting_not_in_effect_is_flagged():
    # the change was written but the harness kept running at the default: no compaction after
    res = verify.verify(_sessions(after_compact_at=None), _manifest(), PricingTable.load(offline=True))
    g = res["groups"][0]
    assert g["in_effect"] == "none seen" and g["observed"]["compactions_per_kturn"] == 0


def test_too_early_is_not_a_verdict():
    ss = _sessions(n=4)
    ss = [s for s in ss if s.id.startswith("b") or s.id == "a0"]
    res = verify.verify(ss, _manifest(), PricingTable.load(offline=True))
    assert res["groups"][0]["verdict"] == "too_early"
    assert "too early" in res["groups"][0]["summary"]


def test_no_sessions_on_one_side():
    ss = [s for s in _sessions() if s.id.startswith("b")]
    res = verify.verify(ss, _manifest(), PricingTable.load(offline=True))
    assert res["groups"][0]["verdict"] == "no_data"


def test_changes_from_manifest_reads_old_values():
    (c,) = verify.changes_from_manifest(_manifest(old=500_000))
    assert (c.old, c.new, c.harness) == (500_000, 300_000, "claude-code")
    (c,) = verify.changes_from_manifest(_manifest(old="auto"))
    assert c.old is None


def test_cli_needs_a_manifest(tmp_path, claude_root, codex_root, capsys):
    rc = main(["verify", "--state-dir", str(tmp_path / "none"), "--claude-root", str(claude_root), "--no-cache"])
    assert rc == 2 and "apply" in capsys.readouterr().err


def test_cli_runs_on_synthetic_logs(tmp_path, claude_root, codex_root, capsys):
    sd = tmp_path / "state"
    (sd / "manifests").mkdir(parents=True)
    (sd / "manifests" / "m1.json").write_text(json.dumps(_manifest() | {"created": time.time() - 3600}))
    rc = main(["verify", "--state-dir", str(sd), "--claude-root", str(claude_root), "--codex-root", str(codex_root),
               "--since-days", "0", "--no-cache", "--json"])  # fmt: skip
    assert rc == 0
    assert "groups" in json.loads(capsys.readouterr().out)


# ---- regressions from the Codex review ----


def test_inherited_setting_is_the_baseline_when_the_manifest_records_it():
    m = _manifest(old=None)
    m["entries"][0]["changes"][0]["old_effective"] = 500_000  # a top-level autoCompactWindow governed this model
    (c,) = verify.changes_from_manifest(m)
    assert c.old == 500_000
    g = verify.verify(_sessions(), m, PricingTable.load(offline=True))["groups"][0]
    assert g["old_effective"] == 500_000 or g["old"] == 500_000


def test_a_global_key_is_one_setting_for_every_model():
    key = "model_auto_compact_token_limit"
    ch = lambda model, old, new: {"harness": "codex", "key": key, "model": model, "old": old, "new": new}  # noqa: E731
    m = {"created": T0, "entries": [{"changes": [ch("gpt-a", 100_000, 200_000), ch("gpt-b", 100_000, 300_000)]}]}
    (c,) = verify.changes_from_manifest(m)
    assert (c.model, c.old, c.new) == ("", 100_000, 300_000)


def test_mixed_model_sessions_are_priced_per_turn():
    pricing = PricingTable.load(offline=True)
    s = _session("m", T0, turns=3)
    s.turns[2].model = "claude-sonnet-5-5"
    cheaper = verify.observe([s], pricing)["cost"]
    s.turns[2].model = MODEL
    assert cheaper != verify.observe([s], pricing)["cost"]


def test_in_effect_uses_the_threshold_the_harness_honours():
    base = {"after": {"median_compaction_tokens": 304_000}, "new": 1_000_000, "new_effective": 300_000,
            "expected_compactions_after": 3}  # fmt: skip
    assert verify._in_effect(base) == "yes"  # configured 1M, clamped to 300k
    none = {**base, "after": {"median_compaction_tokens": None}}
    assert verify._in_effect(none) == "none seen"
    assert verify._in_effect({**none, "expected_compactions_after": 0}) == "n/a"


def test_apply_records_the_inherited_setting(tmp_path):
    from compactlab.apply import Change, _effective_old

    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"autoCompactWindow": 500000}))
    ch = Change("claude-code", f, f"modelSettings.{MODEL}.autoCompactWindow", MODEL, None, 300000)
    assert _effective_old(ch) == 500_000  # the model key is absent, but a top-level setting governed it
