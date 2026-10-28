"""Synthetic transcript generator (fixtures and `compactlab demo`). Never reads real data."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

EPOCH = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
# Real compaction summaries run to thousands of tokens; pad the synthetic one to ~10k tokens.
SUMMARY_FILLER = "Context carried over: decisions, open tasks and file notes. " * 680
FILES = [f"/proj/src/mod{i}.py" for i in range(12)]


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _lat(rng: random.Random, ctx: int) -> timedelta:
    """Synthetic request latency: grows with context (prefill) plus noise."""
    return timedelta(seconds=2.5 + 0.8 * ctx / 100_000 + rng.uniform(0, 4))


def _clat(rng: random.Random, pre: int) -> timedelta:
    """Synthetic compaction latency: a summarizer call that reads the whole context and writes a long summary
    (real logs show minutes, not seconds, for large contexts)."""
    return timedelta(seconds=90 + 0.4 * pre / 1000 + rng.uniform(0, 30))


def _steps(rng: random.Random, n: int, start: int, hist_limit: int | None):
    """Yield (ctx, gap_s, file, user_text, compacted_before). hist_limit simulates the user's old threshold."""
    ctx = start
    for _ in range(n):
        compacted = False
        pre = ctx
        if hist_limit and ctx > hist_limit:
            ctx, compacted = 40_000 + 12_000, True
        ctx += int(rng.lognormvariate(8.6, 0.9))
        r = rng.random()
        gap = rng.choice([5, 8, 12, 20, 30, 60]) if r < 0.95 else (420 if r < 0.99 else 3600)
        yield (
            ctx,
            gap,
            rng.choice(FILES),
            ("you forgot what I said about the config" if rng.random() < 0.04 else ""),
            compacted,
            pre,
        )


def write_claude_code(
    root: Path, n_main: int = 6, n_sub: int = 4, seed: int = 7, models=("claude-opus-5-5", "claude-sonnet-5-5")
) -> list[Path]:
    rng = random.Random(seed)
    out = []
    proj = root / "-proj-demo"
    for i in range(n_main + n_sub):
        sub = i >= n_main
        sid = f"s{i:03d}"
        path = (proj / sid / "subagents" / f"agent-{i}.jsonl") if sub else (proj / f"{sid}.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        model = models[i % len(models)]
        n = rng.randint(25, 60) if sub else rng.randint(150, 300)
        t = EPOCH + timedelta(days=i)
        lines: list[dict[str, Any]] = []
        for k, (ctx, gap, key, utext, compacted, pre) in enumerate(
            _steps(rng, n, 30_000, None if sub else rng.choice([350_000, 450_000]))
        ):
            t += timedelta(seconds=gap)
            if compacted:
                t += _clat(rng, pre)
                lines.append(
                    {
                        "type": "system",
                        "subtype": "compact_boundary",
                        "timestamp": _iso(t),
                        "compactMetadata": {"trigger": "auto", "preTokens": pre},
                    }
                )
                lines.append(
                    {
                        "type": "user",
                        "isCompactSummary": True,
                        "timestamp": _iso(t),
                        "message": {
                            "role": "user",
                            "content": "Summary: edited /proj/src/mod3.py; all tests "
                            "pass; verified the build. Next: finish /proj/src/mod4.py.\n" + SUMMARY_FILLER,
                        },
                    }
                )
            if utext:
                lines.append({"type": "user", "timestamp": _iso(t), "message": {"role": "user", "content": utext}})
            read = int(ctx * 0.9)
            msg = {
                "id": f"msg_{sid}_{k}",
                "role": "assistant",
                "model": model,
                "usage": {
                    "input_tokens": 3,
                    "cache_read_input_tokens": read,
                    "cache_creation_input_tokens": ctx - read - 3,
                    "output_tokens": rng.randint(80, 900),
                },
                "content": [{"type": "tool_use", "id": f"tu_{sid}_{k}", "name": "Read", "input": {"file_path": key}}],
            }
            t += _lat(rng, ctx)
            rec = {"type": "assistant", "timestamp": _iso(t), "isSidechain": sub, "message": msg}
            lines.append(rec)
            if k % 7 == 0:
                lines.append(rec)  # streaming duplicate with the same message id
            lines.append(
                {
                    "type": "user",
                    "timestamp": _iso(t),
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": f"tu_{sid}_{k}",
                                "content": "x" * rng.randint(400, 8000),
                            }
                        ],
                    },
                }
            )
        path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        out.append(path)
    return out


def write_codex(root: Path, n: int = 4, seed: int = 11, model: str = "gpt-5-codex") -> list[Path]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        day = root / "2026" / "09" / f"{i + 1:02d}"
        day.mkdir(parents=True, exist_ok=True)
        path = day / f"rollout-2026-09-{i + 1:02d}T10-00-00-{i:04d}.jsonl"
        t = EPOCH + timedelta(days=i)
        lines = [
            {"timestamp": _iso(t), "type": "session_meta", "payload": {"id": f"codex-{i}", "source": "cli"}},
            {"timestamp": _iso(t), "type": "turn_context", "payload": {"model": model}},
        ]
        total = 0
        for k, (ctx, gap, key, utext, compacted, pre) in enumerate(_steps(rng, rng.randint(80, 160), 20_000, 300_000)):
            t += timedelta(seconds=gap)
            if compacted:
                t += _clat(rng, pre)
                lines.append(
                    {
                        "timestamp": _iso(t),
                        "type": "compacted",
                        "payload": {"message": "Summary of work so far.\n" + SUMMARY_FILLER},
                    }
                )
            if utext:
                lines.append(
                    {"timestamp": _iso(t), "type": "event_msg", "payload": {"type": "user_message", "message": utext}}
                )
            lines.append(
                {
                    "timestamp": _iso(t),
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "shell",
                        "call_id": f"c{k}",
                        "arguments": json.dumps({"command": ["cat", key]}),
                    },
                }
            )
            lines.append(
                {
                    "timestamp": _iso(t),
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": f"c{k}",
                        "output": "y" * rng.randint(300, 4000),
                    },
                }
            )
            out_tok = rng.randint(50, 600)
            total += ctx + out_tok
            tok = {
                "input_tokens": ctx,
                "cached_input_tokens": int(ctx * 0.85),
                "output_tokens": out_tok,
                "reasoning_output_tokens": 0,
                "total_tokens": ctx + out_tok,
            }
            info = {
                "total_token_usage": {**tok, "total_tokens": total},
                "last_token_usage": tok,
                "model_context_window": 400_000,
            }
            t += _lat(rng, ctx)
            ev = {"timestamp": _iso(t), "type": "event_msg", "payload": {"type": "token_count", "info": info}}
            lines += [ev, ev]  # Codex re-emits identical token_count events
        path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        out.append(path)
    return out
