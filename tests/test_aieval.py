import pytest

from compactlab.eval.hyper import BudgetExceeded, EvalError, MockClient, estimate_cost, make_probes, run_eval
from compactlab.model import Compaction, Session, Turn
from compactlab.pricing import PricingTable

PR = PricingTable.load()


def session():
    s = Session("x", "claude-code", "main", "p")
    s.turns = [Turn(ts=i, ctx=1000, tool_keys=[f"Read:/proj/f{i}.py"]) for i in range(6)]
    c = Compaction(6, summary_text="Worked on /proj/f5.py and /proj/f4.py. All tests pass.")
    s.compactions.append(c)
    return s, c


def test_requires_confirm_and_different_judge():
    s, c = session()
    with pytest.raises(EvalError, match="--yes"):
        run_eval(s, c, MockClient(), answer_model="a", judge_model="b", pricing=PR, budget_usd=1)
    with pytest.raises(EvalError, match="differ"):
        run_eval(s, c, MockClient(), answer_model="a", judge_model="a", pricing=PR, budget_usd=1, confirm=True)


def test_runs_with_mock_and_flags():
    s, c = session()
    rep = run_eval(s, c, MockClient(), answer_model="claude-sonnet-5-5", judge_model="claude-haiku-5-5",
                   pricing=PR, budget_usd=1, repeats=3, confirm=True)
    assert len(rep.results) == 5 and rep.exact_rate == 1.0 and rep.judged_rate == 1.0
    assert rep.calls == 5 * 4 and rep.spent_usd > 0
    assert any(f.kind == "unsupported_claim" for f in rep.flags)


def test_budget_cap_is_hard():
    s, c = session()
    with pytest.raises(BudgetExceeded):
        run_eval(s, c, MockClient(), answer_model="claude-fable-5-1", judge_model="claude-haiku-5-5",
                 pricing=PR, budget_usd=1e-7, confirm=True)


def test_estimate_scales_with_repeats():
    a = estimate_cost(5, 4000, "claude-sonnet-5-5", "claude-haiku-5-5", 1, PR)
    b = estimate_cost(5, 4000, "claude-sonnet-5-5", "claude-haiku-5-5", 3, PR)
    assert 0 < a < b


def test_probes_from_ground_truth():
    s, c = session()
    assert make_probes(s, c)[0].truth == ["/proj/f5.py"]
