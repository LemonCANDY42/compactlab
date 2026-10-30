"""Deep mode: opt-in, agent-driven evaluation layered on top of lite.

Never runs by default. The CLI prints an estimated cost, requires `--yes`, and enforces `--budget`:
a call is refused when its estimate would cross the cap, and the run stops as soon as the measured spend
after a call exceeds it. A single call can still overshoot by what the executor actually used, since a
CLI that exposes no output limit (Codex) cannot be capped provider-side. Network or harness access happens
only inside an `LLMClient` (see executors.py).

Two pieces:
  run_eval()        probes + LLM judge on summaries that already exist in your history.
  deep_analyze()   re-summarizes sampled sessions at candidate thresholds, probes the summaries for
                    ground-truth facts, and reports measured recall next to lite's assumed recall.
"""

from __future__ import annotations

import importlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..model import Compaction, Session
from ..pricing import PricingTable
from .factcheck import Flag, check_summary

CONFIRM_FLAG = "--yes"


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
        if self.spent > self.cap:  # the estimate was wrong (or the executor ignored max_tokens): stop here
            raise BudgetExceeded(f"spent ${self.spent:.4f}, over cap ${self.cap:.2f}; stopped after {self.calls} calls")
        return out


FILE_QUESTION = (
    "List the exact file paths that were being worked on before the context was compacted, "
    "one per line, as far as this summary lets you tell. Say 'unknown' if it does not say."
)


def make_probes(s: Session, c: Compaction, limit: int = 5) -> list[Probe]:
    """Ground-truth probes from facts that are verifiably in the pre-compaction transcript."""
    seen: list[str] = []
    for t in reversed(s.turns[max(0, c.turn_index - 60) : c.turn_index]):
        for k in t.tool_keys:
            _name, _, arg = k.partition(":")
            if arg.startswith("/") and arg not in seen:
                seen.append(arg)
    # The question must not contain the answer: every probe asks the same open question and is scored
    # against its own file, so a summary that dropped the path scores 0.
    return [Probe("file", FILE_QUESTION, [p]) for p in seen[:limit]]


def estimate_cost(
    n_probes: int,
    summary_chars: int,
    answer_model: str,
    judge_model: str,
    repeats: int,
    pricing: PricingTable,
    max_tokens: int = 200,
) -> float:
    in_a = summary_chars // 4 + 150
    a = BudgetGuard(1.0, pricing).cost(answer_model, in_a, max_tokens)
    j = BudgetGuard(1.0, pricing).cost(judge_model, in_a // 2 + 300, max_tokens) * repeats
    return n_probes * (a + j)


def _answer(
    guard: BudgetGuard, client: LLMClient, model: str, summary: str, question: str, cache: dict[str, str]
) -> str:
    """Probes that share a question share one answer call."""
    if question not in cache:
        cache[question] = guard.call(client, model, f"Context summary:\n{summary}\n\nQuestion: {question}", 200).text
    return cache[question]


def _norm(x: str) -> str:
    return re.sub(r"\s+", " ", x.strip().lower())


def run_eval(
    s: Session,
    c: Compaction,
    client: LLMClient,
    *,
    answer_model: str,
    judge_model: str,
    pricing: PricingTable,
    budget_usd: float,
    repeats: int = 3,
    confirm: bool = False,
    probes: list[Probe] | None = None,
) -> EvalReport:
    if not confirm:
        raise EvalError(f"AI evaluation calls an LLM and costs money; pass {CONFIRM_FLAG} to run it")
    if answer_model == judge_model:
        raise EvalError("judge model must differ from the answering model")
    guard = BudgetGuard(budget_usd, pricing)
    rep = EvalReport()
    rep.flags = check_summary(s, c)
    answers: dict[str, str] = {}
    try:
        for p in probes if probes is not None else make_probes(s, c):
            ans = _answer(guard, client, answer_model, c.summary_text, p.question, answers)
            exact = any(_norm(t) in _norm(ans) for t in p.truth)
            votes = []
            for _ in range(max(repeats, 1)):
                jp = f'Ground truth: {p.truth}\nAnswer: {ans}\nReply with JSON {{"correct": true|false}} only.'
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
        self.known: set[str] = known or set()
        self.log: Counter[str] = Counter()

    def complete(self, model: str, prompt: str, max_tokens: int) -> Completion:
        self.log[model] += 1
        if prompt.startswith("Ground truth"):
            truth, _, ans = prompt.partition("Answer:")
            paths = re.findall(r"'(/[^']+)'", truth)
            ok = any(p in ans for p in paths) if not self.known else any(k in ans for k in self.known)
            text = json.dumps({"correct": ok})
        else:
            summary = prompt.partition("Question:")[0]  # answer only from the summary, never from the question
            paths = [p for p in re.findall(r"(?<![\w.])(/[\w./-]*\w)", summary) if not self.known or p in self.known]
            text = "\n".join(dict.fromkeys(paths)) or "unknown"
        return Completion(text, len(prompt) // 4 + 1, max(len(text) // 4, 1))


def load_client(spec: str) -> LLMClient:
    if spec == "mock":
        return MockClient()
    mod, _, cls = spec.partition(":")
    if not cls:
        raise EvalError("--client must be 'mock' or 'package.module:ClassName'")
    return getattr(importlib.import_module(mod), cls)()


# ---------------- deep_analyze: re-summarize at candidate thresholds ----------------

SUMMARY_PROMPT = (
    "You are compacting an agent session. Write a faithful summary that preserves file paths, "
    "decisions, open tasks, errors and test results. Do not claim anything not shown.\n\n"
)
MAX_TRANSCRIPT_CHARS = 60_000


def transcript_text(s: Session, end: int, max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
    """Reconstruct what compactlab kept of turns [0, end): user text, tool calls, output snippets.
    This is an excerpt, not the full transcript (compactlab never stores full tool outputs)."""
    parts = []
    for _i, t in enumerate(s.turns[:end]):
        if t.user_text:
            parts.append(f"[user] {t.user_text[:500]}")
        for k in t.tool_keys:
            parts.append(f"[tool] {k}")
        for sn in t.snippets[:2]:
            parts.append(f"[out] {sn[:300]}")
    text = "\n".join(parts)
    return text[-max_chars:]


def cut_index(s: Session, threshold: int, base: int) -> int | None:
    from ..sim.engine import make_trace

    ctx = base
    for i, g in enumerate(make_trace(s, base).g):
        ctx += g
        if ctx > threshold:
            return i + 1
    return None


@dataclass
class DeepPlan:
    pairs: list[tuple[str, str, Session, int, int]]  # (model, role, session, threshold, cut)
    est_usd: float


def plan_deep(
    sessions: list[Session],
    result: dict,
    pricing: PricingTable,
    model: str,
    judge_model: str,
    repeats: int = 3,
    sessions_per_group: int = 3,
    probes: int = 5,
) -> DeepPlan:
    pairs = []
    est = 0.0
    g_by = {(g["harness"], g["model"], g["role"]): g for g in result["groups"]}
    for (h, m, role), g in sorted(g_by.items()):
        cands = sorted({p["threshold"] for p in g["profiles"].values()})
        ss = sorted((s for s in sessions if (s.harness, s.model, s.role) == (h, m, role)), key=lambda s: -s.peak)
        for s in ss[:sessions_per_group]:
            for T in cands:
                cut = cut_index(s, T, g["base_tokens"])
                if cut is None or cut < 10:
                    continue
                pairs.append((m, role, s, T, cut))
                chars = len(transcript_text(s, cut))
                guard = BudgetGuard(1.0, pricing)
                est += guard.cost(model, chars // 4 + 100, 2000)
                est += estimate_cost(probes, 8000, model, judge_model, repeats, pricing)
    return DeepPlan(pairs, est)


def deep_analyze(
    plan: DeepPlan,
    result: dict,
    client: LLMClient,
    *,
    model: str,
    judge_model: str,
    pricing: PricingTable,
    budget_usd: float,
    repeats: int = 3,
    confirm: bool = False,
) -> dict:
    if not confirm:
        raise EvalError(f"deep mode calls an LLM and costs money; pass {CONFIRM_FLAG} after reviewing the estimate")
    if model == judge_model:
        raise EvalError("judge model must differ from the summarizing/answering model")
    from ..sim.engine import apply_measured

    guard = BudgetGuard(budget_usd, pricing)
    measured: dict[tuple[str, str, str, int], list[float]] = {}
    stopped = ""
    try:
        for m, role, s, T, cut in plan.pairs:
            summ = guard.call(client, model, SUMMARY_PROMPT + transcript_text(s, cut), 2000).text
            comp = Compaction(turn_index=cut, summary_text=summ)
            ps = make_probes(s, comp)
            if not ps:
                continue
            ok = 0
            answers: dict[str, str] = {}
            for p in ps:
                ans = _answer(guard, client, model, summ, p.question, answers)
                votes = []
                for _ in range(max(repeats, 1)):
                    raw = guard.call(
                        client,
                        judge_model,
                        f'Ground truth: {p.truth}\nAnswer: {ans}\nReply with JSON {{"correct": true|false}} only.',
                        50,
                    ).text
                    try:
                        votes.append(bool(json.loads(raw).get("correct")))
                    except (ValueError, AttributeError):
                        votes.append(False)
                ok += sum(votes) * 2 > len(votes)
            measured.setdefault((s.harness, m, role, T), []).append(ok / len(ps))
    except BudgetExceeded as e:
        stopped = str(e)
    out_groups = []
    for g in result["groups"]:
        pts: list[dict[str, Any]] = []
        for (h, m, role, T), vals in sorted(measured.items()):
            if (h, m, role) != (g["harness"], g["model"], g["role"]):
                continue
            assumed = g["recall"][g["grid"].index(T)] if T in g["grid"] else None
            pts.append(
                {
                    "threshold": T,
                    "measured_recall": sum(vals) / len(vals),
                    "samples": len(vals),
                    "assumed_recall": assumed,
                }
            )
        if not pts:
            continue
        dis = [
            p for p in pts if p["assumed_recall"] is not None and abs(p["measured_recall"] - p["assumed_recall"]) > 0.1
        ]
        g["measured"] = pts  # kept on the group: a later reselect applies the same guardrail
        adjusted = apply_measured(g)
        out_groups.append(
            {
                "harness": g["harness"],
                "model": g["model"],
                "role": g["role"],
                "points": pts,
                "disagreements": dis,
                "adjusted": adjusted,
            }
        )
    result["mode"] = "deep"
    result["deep"] = {
        "model": model,
        "judge_model": judge_model,
        "repeats": repeats,
        "spent_usd": guard.spent,
        "calls": guard.calls,
        "budget_usd": budget_usd,
        "stopped": stopped,
        "groups": out_groups,
    }
    return result
