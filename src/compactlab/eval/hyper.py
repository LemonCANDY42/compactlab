"""Opt-in AI-evaluation layer. Never runs by default; needs an explicit flag, prints a cost estimate,
and enforces a hard budget. Network access exists only inside a user-supplied LLMClient.
"""
from __future__ import annotations

import importlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol

from .factcheck import Flag, check_summary
from ..model import Compaction, Session
from ..pricing import PricingTable

CONFIRM_FLAG = "--i-understand-this-calls-an-llm"


class EvalError(Exception):
    pass


class BudgetExceeded(EvalError):
    pass


@dataclass
class Completion:
    text: str
    input_tokens: int
    output_tokens: int


class LLMClient(Protocol):
    def complete(self, model: str, prompt: str, max_tokens: int) -> Completion: ...


@dataclass
class Probe:
    kind: str
    question: str
    truth: list[str]


@dataclass
class ProbeResult:
    probe: Probe
    answer: str
    exact: bool
    judge_votes: list[bool]
    judged: bool

    @property
    def ok(self) -> bool:
        return self.judged if self.judge_votes else self.exact


@dataclass
class EvalReport:
    results: list[ProbeResult] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    spent_usd: float = 0.0
    calls: int = 0

    @property
    def exact_rate(self) -> float:
        return sum(r.exact for r in self.results) / max(len(self.results), 1)

    @property
    def judged_rate(self) -> float:
        return sum(r.ok for r in self.results) / max(len(self.results), 1)


class BudgetGuard:
    def __init__(self, cap_usd: float, pricing: PricingTable):
        if cap_usd <= 0:
            raise EvalError("budget cap must be > 0")
        self.cap, self.pricing, self.spent, self.calls = cap_usd, pricing, 0.0, 0

    def cost(self, model: str, in_tok: int, out_tok: int) -> float:
        ri, ro, _, _ = self.pricing.lookup(model).rates(in_tok)
        return in_tok * ri + out_tok * ro

    def call(self, client: LLMClient, model: str, prompt: str, max_tokens: int) -> Completion:
        est = self.cost(model, len(prompt) // 4 + 1, max_tokens)
        if self.spent + est > self.cap:
            raise BudgetExceeded(f"next call (~${est:.4f}) would exceed cap ${self.cap:.2f} (spent ${self.spent:.4f})")
        out = client.complete(model, prompt, max_tokens)
        self.spent += self.cost(model, out.input_tokens, out.output_tokens)
        self.calls += 1
        return out


def make_probes(s: Session, c: Compaction, limit: int = 5) -> list[Probe]:
    """Ground-truth probes from facts that are verifiably in the pre-compaction transcript."""
    seen: list[str] = []
    for t in reversed(s.turns[max(0, c.turn_index - 60):c.turn_index]):
        for k in t.tool_keys:
            name, _, arg = k.partition(":")
            if arg.startswith("/") and arg not in seen:
                seen.append(arg)
    probes = [Probe("file", f"The file {p!r} was accessed before the context was compacted. "
                    f"Answer with its exact path if you remember it being worked on, else say 'unknown'.", [p])
              for p in seen[:limit]]
    return probes


def estimate_cost(n_probes: int, summary_chars: int, answer_model: str, judge_model: str,
                  repeats: int, pricing: PricingTable, max_tokens: int = 200) -> float:
    in_a = summary_chars // 4 + 150
    a = BudgetGuard(1.0, pricing).cost(answer_model, in_a, max_tokens)
    j = BudgetGuard(1.0, pricing).cost(judge_model, in_a // 2 + 300, max_tokens) * repeats
    return n_probes * (a + j)


def _norm(x: str) -> str:
    return re.sub(r"\s+", " ", x.strip().lower())


def run_eval(s: Session, c: Compaction, client: LLMClient, *, answer_model: str, judge_model: str,
             pricing: PricingTable, budget_usd: float, repeats: int = 3, confirm: bool = False,
             probes: list[Probe] | None = None) -> EvalReport:
    if not confirm:
        raise EvalError(f"AI evaluation calls an LLM and costs money; pass {CONFIRM_FLAG} to run it")
    if answer_model == judge_model:
        raise EvalError("judge model must differ from the answering model")
    guard = BudgetGuard(budget_usd, pricing)
    rep = EvalReport()
    rep.flags = check_summary(s, c)
    try:
        for p in probes if probes is not None else make_probes(s, c):
            ans = guard.call(client, answer_model, f"Context summary:\n{c.summary_text}\n\nQuestion: {p.question}", 200).text
            exact = any(_norm(t) in _norm(ans) for t in p.truth)
            votes = []
            for _ in range(max(repeats, 1)):
                jp = (f"Ground truth: {p.truth}\nAnswer: {ans}\nReply with JSON {{\"correct\": true|false}} only.")
                raw = guard.call(client, judge_model, jp, 50).text
                try:
                    votes.append(bool(json.loads(raw).get("correct")))
                except (ValueError, AttributeError):
                    votes.append(False)
            rep.results.append(ProbeResult(p, ans, exact, votes, sum(votes) * 2 > len(votes)))
    finally:
        rep.spent_usd, rep.calls = guard.spent, guard.calls
    return rep


class MockClient:
    """Deterministic offline client for tests and dry demos."""
    def __init__(self, known: set[str] | None = None):
        self.known, self.log = known or set(), Counter()

    def complete(self, model: str, prompt: str, max_tokens: int) -> Completion:
        self.log[model] += 1
        if prompt.startswith("Ground truth"):
            truth, _, ans = prompt.partition("Answer:")
            paths = re.findall(r"'(/[^']+)'", truth)
            ok = any(p in ans for p in paths) if not self.known else any(k in ans for k in self.known)
            text = json.dumps({"correct": ok})
        else:
            m = re.search(r"'(/[^']+)'", prompt)
            text = m.group(1) if m and (not self.known or m.group(1) in self.known) else "unknown"
        return Completion(text, len(prompt) // 4 + 1, max(len(text) // 4, 1))


def load_client(spec: str) -> LLMClient:
    if spec == "mock":
        return MockClient()
    mod, _, cls = spec.partition(":")
    if not cls:
        raise EvalError("--client must be 'mock' or 'package.module:ClassName'")
    return getattr(importlib.import_module(mod), cls)()
