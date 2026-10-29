"""Per-day impact arithmetic: calendar days, active days, months, and the wait / turn-speed split."""

import json

import pytest

from compactlab.cli import main
from compactlab.cli.main import impact_text
from compactlab.sim.engine import reselect
from compactlab.sim.select import MONTH_DAYS, compare, impact
from test_cli import sim


def _pt(cost, turns, compact, redisc, compactions, recall=0.95):
    time_s = turns + compact + redisc
    return {"combined_cost": cost, "time_s": time_s, "time_turns_s": turns, "time_compact_s": compact,
            "time_redisc_s": redisc, "compactions": compactions, "recall": recall}  # fmt: skip


def test_impact_divides_by_calendar_active_and_month():
    # -$6,144 over a 30-day window with 20 active days -> -$204.8/day, -$307.2/active day, -$6,144/month
    im = impact(-6144.0, 8640.0, 9000.0, 360.0, 300, 60, days=30, active_days=20)
    assert im["usd"]["per_day"] == pytest.approx(-204.8)
    assert im["usd"]["per_active_day"] == pytest.approx(-307.2)
    assert im["usd"]["per_month"] == pytest.approx(-6144.0)
    assert im["minutes"]["per_day"] == pytest.approx(4.8)  # 8640 s / 60 / 30
    assert im["minutes"]["per_active_day"] == pytest.approx(7.2)
    assert im["minutes"]["per_month"] == pytest.approx(144.0)
    assert im["wait_minutes"]["per_day"] == pytest.approx(5.0)
    assert im["turn_gain_minutes"]["per_day"] == pytest.approx(0.2)
    assert im["compactions"]["per_day"] == pytest.approx(10.0)
    assert im["compactions"]["per_month"] == pytest.approx(300.0)
    assert im["ref_compactions"]["per_active_day"] == pytest.approx(3.0)
    assert MONTH_DAYS == 30


def test_impact_floors_days_and_handles_missing_active_days():
    im = impact(-10.0, 0.0, 0.0, 0.0, 2, 1, days=0.2)  # a few hours of history count as one day
    assert im["days"] == 1.0 and im["usd"]["per_day"] == pytest.approx(-10.0)
    assert im["active_days"] is None and im["usd"]["per_active_day"] is None
    assert im["usd"]["per_month"] == pytest.approx(-300.0)


def test_compare_splits_time_into_wait_and_turn_gain():
    ref = _pt(cost=1000.0, turns=36_000, compact=600, redisc=0, compactions=10)
    p = _pt(cost=800.0, turns=30_000, compact=4_200, redisc=600, compactions=50)
    c = compare(p, ref, days=10, active_days=5)
    im = c["impact"]
    assert im["usd"]["per_day"] == pytest.approx(-20.0)  # saves $20/day
    assert im["wait_minutes"]["per_day"] == pytest.approx((4800 - 600) / 60 / 10)  # +7 min/day waiting
    assert im["turn_gain_minutes"]["per_day"] == pytest.approx(6000 / 60 / 10)  # 10 min/day faster turns
    # net minutes = wait - turn gain, on every basis
    for basis in ("per_day", "per_active_day", "per_month"):
        net = im["wait_minutes"][basis] - im["turn_gain_minutes"][basis]
        assert im["minutes"][basis] == pytest.approx(net)
    assert im["minutes"]["per_day"] == pytest.approx(-3.0)
    assert im["compactions"]["per_active_day"] == pytest.approx(10.0)
    assert c["d_time_min_per_day"] == pytest.approx(im["minutes"]["per_day"])  # legacy field agrees


def test_engine_impact_vs_default_and_current(tmp_path, claude_root, codex_root):
    res = json.loads(sim(tmp_path, claude_root, codex_root).read_text())
    for g in res["groups"]:
        assert 1 <= g["active_days"] <= max(g["days"], 1)
        for c in g["comparison"].values():
            im = c["vs_default"]["impact"]
            assert im["days"] == pytest.approx(max(g["days"], 1))
            assert im["usd"]["per_day"] == pytest.approx(c["vs_default"]["d_cost"] / im["days"])
            assert im["usd"]["per_active_day"] == pytest.approx(c["vs_default"]["d_cost"] / g["active_days"])
    # an older result without active_days still re-selects; per-active-day is then unknown, not wrong
    for g in res["groups"]:
        g.pop("active_days")
    reselect(res, "cost")
    c = res["groups"][0]["comparison"]["cost"]["vs_default"]["impact"]
    assert c["active_days"] is None and c["usd"]["per_active_day"] is None
    assert "?" in impact_text(res, "en")


def test_impact_text_signs_and_recommend_json(tmp_path, claude_root, codex_root, capsys):
    r = sim(tmp_path, claude_root, codex_root)
    capsys.readouterr()
    res = json.loads(r.read_text())
    g = next(g for g in res["groups"] if not g["no_action_needed"])
    g["comparison"]["balanced"]["vs_default"]["impact"]["usd"]["per_day"] = -204.8
    text = impact_text(res, "en")
    assert "-$204.8" in text and "-$ / -min = saves" in text and "list-price" in text.lower()
    assert "active days" in text and "每日影响" in impact_text(res, "both")
    assert main(["recommend", "--result", str(r), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    prof = out["impact"][0]["profiles"]
    assert set(prof) == {"cost", "balanced", "time"}
    assert "per_active_day" in prof["balanced"]["vs_default"]["usd"]
    assert main(["recommend", "--result", str(r), "--lang", "en"]) == 0
    assert "Per-day impact" in capsys.readouterr().out
