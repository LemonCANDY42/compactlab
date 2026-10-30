import pytest

from compactlab.eval.deep import (
    BudgetExceeded,
    Completion,
    EvalError,
    MockClient,
    estimate_cost,
    make_probes,
    run_eval,
)
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
    rep = run_eval(
        s,
        c,
        MockClient(),
        answer_model="claude-sonnet-5-5",
        judge_model="claude-haiku-5-5",
        pricing=PR,
        budget_usd=1,
        repeats=3,
        confirm=True,
    )
    # the summary names 2 of the 5 probed files; the questions no longer hand the rest over
    assert len(rep.results) == 5 and rep.exact_rate == 0.4 and rep.judged_rate == 0.4
    assert rep.calls == 1 + 5 * 3 and rep.spent_usd > 0  # one shared answer call, three judge votes each
    assert any(f.kind == "unsupported_claim" for f in rep.flags)


def test_budget_cap_is_hard():
    s, c = session()
    with pytest.raises(BudgetExceeded):
        run_eval(
            s,
            c,
            MockClient(),
            answer_model="claude-fable-5-1",
            judge_model="claude-haiku-5-5",
            pricing=PR,
            budget_usd=1e-7,
            confirm=True,
        )


def test_estimate_scales_with_repeats():
    a = estimate_cost(5, 4000, "claude-sonnet-5-5", "claude-haiku-5-5", 1, PR)
    b = estimate_cost(5, 4000, "claude-sonnet-5-5", "claude-haiku-5-5", 3, PR)
    assert 0 < a < b


def test_probes_from_ground_truth():
    s, c = session()
    assert make_probes(s, c)[0].truth == ["/proj/f5.py"]


def test_probe_questions_do_not_contain_the_answer():
    s, c = session()
    probes = make_probes(s, c)
    assert probes and all(t not in p.question for p in probes for t in p.truth)
    assert not any("/proj" in p.question for p in probes)


def test_empty_summary_scores_zero_and_summary_with_paths_scores_full():
    s, c = session()
    kw = dict(answer_model="claude-sonnet-5-5", judge_model="claude-haiku-5-5", pricing=PR, budget_usd=1, confirm=True)
    empty = run_eval(s, Compaction(6, summary_text=""), MockClient(), **kw)
    assert empty.results and empty.exact_rate == 0.0 and empty.judged_rate == 0.0
    assert run_eval(s, c, MockClient(), **kw).exact_rate > 0


class Overspender(MockClient):
    """Ignores max_tokens and reports 1M output tokens per call."""

    def __init__(self):
        super().__init__()
        self.n = 0

    def complete(self, model, prompt, max_tokens):
        self.n += 1
        return Completion("x", 10, 1_000_000)


def test_budget_stops_after_a_call_that_overspends():
    s, c = session()
    client = Overspender()
    with pytest.raises(BudgetExceeded, match="over cap"):
        run_eval(
            s,
            c,
            client,
            answer_model="claude-haiku-5-5",
            judge_model="claude-sonnet-5-5",
            pricing=PR,
            budget_usd=0.001,
            confirm=True,
        )
    assert client.n == 1  # the first call passed the estimate, overspent, and nothing ran after it
